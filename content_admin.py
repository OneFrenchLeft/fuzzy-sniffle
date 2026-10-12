"""Visual content administration; QCM files remain the canonical question bank."""
import csv
import io
import unicodedata
import json
import hashlib
from flask import Blueprint, request, jsonify, Response, current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature
from auth import require_admin
from config import QCM_FILES
from db import db, ensure_db
from content_catalog import chapter_name, chapter_overview
from qcm_admin import (read_bank, store_bank, bank_lock, validate_qcm_questions, validate_bank_questions,
                       get_qcm_path, BankConflict, notify_catalog_change)

bp = Blueprint('content_admin', __name__)


@bp.errorhandler(OSError)
def unavailable_bank(exc):
    return jsonify({'ok': False, 'error': 'Impossible de lire ou d’enregistrer la banque. Réessaie sans fermer tes modifications.'}), 500


@bp.errorhandler(ValueError)
def invalid_bank(exc):
    return error_response(exc)


def error_response(exc):
    status = 409 if isinstance(exc, BankConflict) else 400
    message = str(exc)
    fields = {}
    for word, field in [('question requis', 'question'), ('choix', 'choix'),
                        ('reponse', 'reponse'), ('temps', 'temps')]:
        if word in message:
            fields[field] = message
    return jsonify({'ok': False, 'error': message, 'fields': fields}), status


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError('Les informations envoyées sont invalides.')
    return value


def selected_chapter(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError('Choisis un chapitre (200 caractères maximum).')
    return value.strip()


def import_digest(questions):
    return hashlib.sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def import_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='qcm-import-preview')


def bank_result(theme, questions, revision):
    return jsonify({'ok': True, 'questions': questions, 'revision': revision,
                    'chapters': chapter_overview(theme, questions)})


@bp.route('/api/admin/content/subjects')
@require_admin
def subjects():
    out = []
    for theme in QCM_FILES:
        try:
            questions, _ = read_bank(theme)
            out.append({'theme': theme, 'count': len(questions),
                        'chapters': chapter_overview(theme, questions)})
        except (ValueError, OSError, UnicodeError) as exc:
            out.append({'theme': theme, 'error': str(exc)})
    return jsonify({'ok': True, 'subjects': out})


@bp.route('/api/admin/content/<theme>/chapters', methods=['POST', 'PUT'])
@require_admin
def chapters(theme):
    get_qcm_path(theme)
    try:
        data = body()
        ch = selected_chapter(data.get('chapitre'))
        with bank_lock(theme):
            questions, _ = read_bank(theme)
            known = {r['chapitre'] for r in chapter_overview(theme, questions)}
            if request.method == 'POST' and any(c.casefold() == ch.casefold() for c in known):
                raise ValueError('Ce chapitre existe déjà. Sélectionne-le dans la liste.')
            if request.method == 'PUT' and ch not in known:
                raise ValueError('Ce chapitre n’existe pas dans cette matière.')
            flags = {k: data[k] for k in ('qcm_active', 'ankimie_active') if k in data}
            if request.method == 'PUT' and (not flags or any(type(v) is not bool for v in flags.values())):
                raise ValueError('Choisis si le chapitre est actif ou inactif.')
            ensure_db()
            conn = db()
            try:
                with conn:
                    initial = 0 if request.method == 'POST' else 1
                    anki_initial = initial if theme == 'chimie' else 0
                    conn.execute('INSERT OR IGNORE INTO content_chapters VALUES(?,?,?,?)',
                                 (theme, ch, initial, anki_initial))
                    for key, value in flags.items():
                        # The column name is from the explicit allow-list above.
                        conn.execute(f'UPDATE content_chapters SET {key}=? WHERE theme=? AND chapitre=?',
                                     (int(value), theme, ch))
            finally:
                conn.close()
        if 'qcm_active' in flags:
            notify_catalog_change()
        return jsonify({'ok': True, 'chapters': chapter_overview(theme, questions)})
    except (ValueError, UnicodeError) as exc:
        return error_response(exc)


@bp.route('/api/admin/qcm/<theme>/questions', methods=['POST'])
@bp.route('/api/admin/qcm/<theme>/questions/<qid>', methods=['PUT', 'DELETE'])
@require_admin
def question_mutation(theme, qid=None):
    get_qcm_path(theme)
    try:
        data = body()
        with bank_lock(theme):
            questions, _ = read_bank(theme)
            if qid:
                position = next((i for i, q in enumerate(questions) if q['qid'] == qid), None)
                if position is None:
                    return jsonify({'ok': False, 'error': 'La question n’existe plus. Recharge la banque.'}), 404
                if request.method == 'DELETE':
                    questions.pop(position)
                else:
                    item = data.get('question')
                    if not isinstance(item, dict):
                        raise ValueError('Complète les informations de la question.')
                    questions[position] = {**item, 'qid': qid}
            else:
                item = data.get('question')
                if not isinstance(item, dict):
                    raise ValueError('Complète les informations de la question.')
                # A duplicate action must never carry the original question ID.
                questions.append({k: v for k, v in item.items() if k != 'qid'})
            clean, revision = store_bank(theme, questions, data.get('revision'))
        notify_catalog_change()
        return bank_result(theme, clean, revision)
    except (ValueError, UnicodeError) as exc:
        return error_response(exc)


@bp.route('/api/admin/content/<theme>/chapters', methods=['DELETE'])
@require_admin
def delete_chapter(theme):
    get_qcm_path(theme)
    data = body()
    ch = selected_chapter(data.get('chapitre'))
    with bank_lock(theme):
        questions, revision = read_bank(theme)
        if data.get('revision') != revision:
            raise BankConflict('La banque a changé. Recharge les chapitres avant de réessayer.')
        if ch not in {r['chapitre'] for r in chapter_overview(theme, questions)}:
            raise ValueError('Ce chapitre n’existe plus dans cette matière.')
        moving = [q for q in questions if chapter_name(q.get('chapitre')) == ch]
        destination = selected_chapter(data.get('destination')) if moving else None
        if destination == ch:
            raise ValueError('Choisis un autre chapitre pour conserver les questions.')
        candidate = [{**q, 'chapitre': destination} if chapter_name(q.get('chapitre')) == ch else q
                     for q in questions]
        # Preserve stable IDs and histories. New destinations enter inactive.
        # Keep the original chapter metadata until the bank write succeeds.
        clean, revision = store_bank(theme, candidate, revision)
        conn = db()
        try:
            with conn:
                conn.execute('DELETE FROM content_chapters WHERE theme=? AND chapitre=?', (theme, ch))
        finally:
            conn.close()
    notify_catalog_change()
    return bank_result(theme, clean, revision)


@bp.route('/api/admin/qcm/<theme>/validate', methods=['POST'])
@require_admin
def validate_questions(theme):
    get_qcm_path(theme)
    try:
        clean = validate_bank_questions(body().get('questions'), theme)
        return jsonify({'ok': True, 'questions': clean})
    except (ValueError, UnicodeError) as exc:
        return error_response(exc)


CSV_HEADERS = ['Question', 'Réponse A', 'Réponse B', 'Réponse C', 'Réponse D',
               'Bonne réponse', 'Temps', 'Explication', 'Image', 'Formule']


@bp.route('/api/admin/qcm/import-template.csv')
@require_admin
def import_template():
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(CSV_HEADERS)
    writer.writerow(['Quelle est la vitesse de la lumière dans le vide ?',
                     '300 000 km/s', '150 000 km/s', '', '', 'A', '30',
                     'La vitesse de la lumière vaut environ 300 000 km/s.', '', ''])
    return Response('\ufeff' + stream.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=modele-questions.csv'})


def _header(value):
    plain = unicodedata.normalize('NFD', value).encode('ascii', 'ignore').decode()
    return ''.join(c for c in plain.casefold() if c.isalnum())


def csv_questions(text):
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=';,\t').delimiter
    except csv.Error:
        delimiter = ';'
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), delimiter=delimiter, strict=True)
    if not reader.fieldnames or 'question' not in [_header(h) for h in reader.fieldnames]:
        raise ValueError('La colonne « Question » est manquante. Utilise le modèle CSV proposé.')
    out = []
    for row in reader:
        if None in row:
            raise ValueError('Une ligne contient trop de colonnes. Vérifie les séparateurs du fichier.')
        row = {_header(k): (v or '').strip() for k, v in row.items()}
        if not any(row.values()):
            continue
        choices = [row.get('reponse' + letter, row.get('choix' + letter, '')) for letter in 'abcd']
        while choices and not choices[-1]:
            choices.pop()
        answer = row.get('bonnereponse', '').upper()
        out.append({'question': row.get('question', ''), 'choix': choices,
                    'reponse': 'ABCD'.index(answer) + 1 if answer in ('A', 'B', 'C', 'D') else answer,
                    'temps': row.get('temps') or 30, 'explication': row.get('explication', ''),
                    'image': row.get('image', ''), 'latex': row.get('formule', '')})
    return out


@bp.route('/api/admin/qcm/<theme>/import/preview', methods=['POST'])
@require_admin
def import_preview(theme):
    get_qcm_path(theme)
    try:
        if request.files:
            upload = request.files.get('file')
            if upload is None:
                raise ValueError('Choisis un fichier à importer.')
            ch = selected_chapter(request.form.get('chapitre'))
            format_name = request.form.get('format', 'csv')
            raw = upload.read()
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                if format_name != 'csv':
                    raise ValueError('Exporte le fichier au format UTF-8 avant de l’importer.')
                try:
                    text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('cp1252')
                except UnicodeError:
                    raise ValueError('Ce fichier n’est pas lisible. Exporte-le au format CSV UTF-8 depuis ton tableur.')
        else:
            data = body()
            text = data.get('text', '')
            ch = selected_chapter(data.get('chapitre'))
            format_name = data.get('format', 'csv')
        if not isinstance(text, str):
            raise ValueError('Le contenu du fichier est invalide.')
        if format_name not in ('csv', 'json'):
            raise ValueError('Le format du fichier n’est pas reconnu.')
        parsed = csv_questions(text) if format_name == 'csv' else json.loads(text)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError('Le fichier doit contenir au moins une question.')
        previous, revision = read_bank(theme)
        ids = {q['qid'] for q in previous}
        clean, errors = [], []
        for position, item in enumerate(parsed, start=1):
            try:
                if not isinstance(item, dict):
                    raise ValueError('Les informations de cette question sont invalides.')
                q = validate_qcm_questions([{**item, 'chapitre': ch}], theme)[0]
                if q['qid'] in ids:
                    raise ValueError('Cette question est déjà présente dans la banque ou dans ce fichier.')
                ids.add(q['qid'])
                clean.append(q)
            except ValueError as exc:
                errors.append({'position': position, 'question': str(item.get('question', '')) if isinstance(item, dict) else '',
                               'error': str(exc)})
        token = import_signer().dumps({'theme': theme, 'chapitre': ch, 'revision': revision,
                                      'digest': import_digest(clean)}) if not errors else None
        return jsonify({'ok': True, 'chapitre': ch, 'count': len(parsed),
                        'valid_count': len(clean), 'errors': errors, 'questions': clean,
                        'can_import': not errors, 'revision': revision, 'preview_token': token})
    except (ValueError, UnicodeError, csv.Error) as exc:
        return error_response(exc)


@bp.route('/api/admin/qcm/<theme>/import', methods=['POST'])
@require_admin
def import_commit(theme):
    get_qcm_path(theme)
    try:
        data = body()
        ch = selected_chapter(data.get('chapitre'))
        incoming = data.get('questions')
        if not isinstance(incoming, list) or not incoming:
            raise ValueError('Il n’y a aucune question validée à importer.')
        incoming = validate_qcm_questions([{**q, 'chapitre': ch} if isinstance(q, dict) else q
                                           for q in incoming], theme)
        try:
            token = data.get('preview_token')
            if not isinstance(token, str):
                raise ValueError('Vérifie et prévisualise le fichier avant de confirmer l’import.')
            preview = import_signer().loads(token, max_age=1800)
        except BadSignature:
            raise ValueError('Vérifie et prévisualise à nouveau le fichier avant de confirmer l’import.')
        if preview != {'theme': theme, 'chapitre': ch, 'revision': data.get('revision'),
                       'digest': import_digest(incoming)}:
            raise ValueError('Le contenu ou le chapitre a changé. Prévisualise à nouveau l’import.')
        with bank_lock(theme):
            previous, _ = read_bank(theme)
            clean, revision = store_bank(theme, previous + incoming, data.get('revision'))
        notify_catalog_change()
        return bank_result(theme, clean, revision)
    except (ValueError, UnicodeError) as exc:
        return error_response(exc)

# Ankimie n'a plus de routes de gestion : elle pioche automatiquement dans
# les questions des chapitres actifs en QCM (voir content_catalog.py).
