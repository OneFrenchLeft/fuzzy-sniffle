# Xiao: Admin-side QCM editing and statistics. Keep validation strict here.
from flask import Blueprint, request, jsonify, abort
from pathlib import Path
import json, hashlib, re
from config import (QCM_FILES, QCM_MIN_TIME_S, QCM_MAX_TIME_S, QCM_DEFAULT_TIME_S,
                    QCM_THEME_PREFIX, DATA, QID_RE, now_paris)
from db import db, ensure_db, log_event
import qcm_engine
from auth import require_admin

bp = Blueprint('qcm_admin', __name__)

def qid_for(theme, chapitre, question):
    # Small: Stable IDs depend on theme, chapter and question content.
    prefix = QCM_THEME_PREFIX.get(theme, 'x')
    ch = hashlib.sha1(chapitre.strip().encode('utf-8')).hexdigest()[:12]
    qh = hashlib.sha1(question.strip().encode('utf-8')).hexdigest()[:8]
    return f'{prefix}-{ch}-{qh}'

def normalize_qcm_choice(value):
    # Ethan: Normalize both simple and rich choices into one format.
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('un choix texte ne peut pas être vide')
        return text

    if not isinstance(value, dict):
        raise ValueError('un choix doit être une chaîne ou un objet')

    out = {}
    text = str(value.get('texte', value.get('text', ''))).strip()
    image = str(value.get('image', '')).strip()
    latex = str(value.get('latex', '')).strip()

    if text:
        out['texte'] = text
    if image:
        out['image'] = image
    if latex:
        out['latex'] = latex

    if not out:
        # Steph: Empty rich objects are still empty choices.
        raise ValueError('un choix riche doit contenir texte, image ou latex')

    return out

def validate_qcm_questions(raw, theme=None):
    # Flying: Validate the complete payload before writing anything.
    if not isinstance(raw, list):
        raise ValueError('le fichier QCM doit contenir une liste')

    clean = []

    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'question {position} : objet JSON attendu')

        question = str(item.get('question', '')).strip()
        if not question:
            raise ValueError(f'question {position} : champ question requis')

        choix_raw = item.get('choix')
        if not isinstance(choix_raw, list):
            raise ValueError(f'question {position} : tableau choix requis')

        choix = [
            normalize_qcm_choice(choice)
            for choice in choix_raw
        ]

        if not 2 <= len(choix) <= 4:
            raise ValueError(
                f'question {position} : il faut entre 2 et 4 choix'
            )

        try:
            reponse = int(item.get('reponse'))
        except (TypeError, ValueError):
            # Eliot: Validate the answer index before trusting it.
            raise ValueError(
                f'question {position} : reponse doit être un entier'
            )

        if not 1 <= reponse <= len(choix):
            raise ValueError(
                f'question {position} : reponse doit être comprise entre '
                f'1 et {len(choix)}'
            )

        try:
            temps = int(item.get('temps', QCM_DEFAULT_TIME_S))
        except (TypeError, ValueError):
            raise ValueError(
                f'question {position} : temps doit être un entier'
            )

        if not QCM_MIN_TIME_S <= temps <= QCM_MAX_TIME_S:
            raise ValueError(
                f'question {position} : temps entre '
                f'{QCM_MIN_TIME_S} et {QCM_MAX_TIME_S} secondes'
            )

        normalized = {
            'question': question,
            'choix': choix,
            'reponse': reponse,
            'temps': temps,
        }

        image_complete = str(
            item.get('image_complete', item.get('image', ''))
        ).strip()
        latex = str(item.get('latex', '')).strip()

        if image_complete:
            normalized['image_complete'] = image_complete
        if latex:
            normalized['latex'] = latex
        chapitre = str(item.get('chapitre', '')).strip()
        if chapitre:
            normalized['chapitre'] = chapitre
        explication = str(item.get('explication', '')).strip()
        if explication:
            normalized['explication'] = explication

        qid = str(item.get('qid', '')).strip()
        if not QID_RE.match(qid):
            # Xiao: Generate the ID only when the supplied one isn't valid.
            qid = qid_for(theme, chapitre or 'Autre', question) if theme else ''
        if qid:
            normalized['qid'] = qid

        clean.append(normalized)

    return clean

def get_qcm_path(theme):
    # Small: Unknown themes should fail before touching the filesystem.
    path = QCM_FILES.get(theme)
    if path is None:
        abort(404)
    return path

@bp.route('/api/admin/qcm/<theme>', methods=['GET'])
@require_admin
def admin_get_qcm(theme):
    path = get_qcm_path(theme)
    if not path.exists():
        return jsonify({'ok': True, 'theme': theme, 'raw': '[]', 'mtime': None})
    try:
        raw = path.read_text(encoding='utf-8')
        mtime = path.stat().st_mtime
    except OSError as e:
        return jsonify({'ok': False, 'error': f'lecture impossible : {e}'}), 500
    return jsonify({'ok': True, 'theme': theme, 'raw': raw, 'mtime': mtime})

def _purge_retired_qids(theme=None, chapitre=''):
    """Remove qcm_answers rows for questions no longer present in the files."""
    # Ethan: Build the active ID set from the current QCM files.
    ensure_db()
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    active_qids = set()
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            if chapitre and (q.get('chapitre') or 'Autre') != chapitre:
                continue
            if q.get('qid'):
                active_qids.add(q['qid'])
    clauses, args = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        args.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        args.append(chapitre)
    where = (' AND '.join(clauses)) if clauses else '1=1'
    conn = db()
    rows = conn.execute(f'SELECT DISTINCT qid FROM qcm_answers WHERE {where}', args).fetchall()
    qids = [r['qid'] for r in rows if r['qid'] not in active_qids]
    deleted = 0
    if qids:
        # Steph: Parameterize every ID; don't be that person.
        marks = ','.join('?' * len(qids))
        cur = conn.execute(f'DELETE FROM qcm_answers WHERE qid IN ({marks}) AND {where}', qids + args)
        deleted = cur.rowcount
        conn.commit()
    conn.close()
    return deleted, len(qids)

@bp.route('/api/admin/qcm/<theme>', methods=['PUT'])
@require_admin
def admin_save_qcm(theme):
    path = get_qcm_path(theme)
    payload = request.get_json(silent=True) or {}
    raw = payload.get('raw')
    # Flying: Accept both parsed JSON and raw JSON for frontend compatibility.
    if isinstance(raw, list):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return jsonify({'ok': False, 'error': f'JSON invalide : {e}'}), 400
    else:
        alt = payload.get('questions')
        if isinstance(alt, list):
            parsed = alt
        else:
            return jsonify({'ok': False, 'error': 'champ raw requis (string JSON ou liste)'}), 400
    try:
        questions = validate_qcm_questions(parsed, theme=theme)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    normalized_raw = json.dumps(questions, ensure_ascii=False, indent=2) + '\n'
    try:
        # Eliot: Write to a temp file first so a failed write doesn't destroy the original.
        temporary.write_text(normalized_raw, encoding='utf-8')
        temporary.replace(path)
    except OSError as e:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({'ok': False, 'error': f'impossible d’enregistrer : {e}'}), 500
    try:
        purged, _ = _purge_retired_qids(theme=theme)
        if purged:
            print(f'[qcm-admin] purge auto : {purged} reponses supprimees ({theme})')
    except Exception as exc:
        # Xiao: Purging is cleanup; it shouldn't make a successful save look failed.
        print(f'[qcm-admin] purge auto impossible: {exc!r}')
    return jsonify({'ok': True, 'theme': theme, 'count': len(questions)})

@bp.route('/api/admin/qcm/purge-retired', methods=['POST'])
@require_admin
def admin_qcm_purge_retired():
    # Small: Same purge rules as the automatic cleanup above. Yes, duplicated on purpose.
    ensure_db()
    theme = request.args.get('theme', '')
    chapitre = request.args.get('chapitre', '')
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    active_qids = set()
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            if chapitre and (q.get('chapitre') or 'Autre') != chapitre:
                continue
            if q.get('qid'):
                active_qids.add(q['qid'])
    clauses, args = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        args.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        args.append(chapitre)
    where = (' AND '.join(clauses)) if clauses else '1=1'
    conn = db()
    rows = conn.execute(f'SELECT DISTINCT qid FROM qcm_answers WHERE {where}', args).fetchall()
    qids = [r['qid'] for r in rows if r['qid'] not in active_qids]
    deleted = 0
    if qids:
        marks = ','.join('?' * len(qids))
        cur = conn.execute(
            f'DELETE FROM qcm_answers WHERE qid IN ({marks}) AND {where}',
            qids + args)
        deleted = cur.rowcount
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted, 'questions': len(qids)})

@bp.route('/api/admin/qcm/games', methods=['GET'])
@require_admin
def admin_qcm_games():
    ensure_db()
    conn = db()
    rows = conn.execute(
        "SELECT gid, themes, nb_questions, nb_players, podium, created_at "
        "FROM qcm_games ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            podium = json.loads(r['podium'] or '[]')
        except Exception:
            # Ethan: Bad historical podium data shouldn't break the admin page.
            podium = []
        out.append({'gid': r['gid'], 'themes': r['themes'], 'nb_questions': r['nb_questions'],
                    'nb_players': r['nb_players'], 'podium': podium[:3], 'created_at': r['created_at']})
    return jsonify({'ok': True, 'rows': out})

@bp.route('/api/admin/qcm/weak', methods=['GET'])
@require_admin
def admin_qcm_weak():
    # Steph: Build the catalog from files, then merge database statistics.
    ensure_db()
    theme = request.args.get('theme', '')
    chapitre = request.args.get('chapitre', '')
    catalog = {}
    chapitres_disponibles = set()
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            chapitres_disponibles.add(q.get('chapitre', 'Autre'))
            if q.get('qid'):
                catalog[q['qid']] = q
    sql = ("SELECT qid, theme, chapitre, question, COUNT(*) AS n, "
           "SUM(ok) AS bonnes, MAX(created_at) AS derniere "
           "FROM qcm_answers")
    clauses, params_sql = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        params_sql.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        params_sql.append(chapitre)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' GROUP BY qid'
    conn = db()
    stats = {r['qid']: r for r in conn.execute(sql, params_sql).fetchall()}
    conn.close()
    out, seen = [], set()
    for qid, q in catalog.items():
        if chapitre and q.get('chapitre') != chapitre:
            continue
        seen.add(qid)
        s = stats.get(qid)
        n = s['n'] if s else 0
        bonnes = (s['bonnes'] or 0) if s else 0
        out.append({
            'qid': qid, 'theme': q.get('theme', ''),
            'chapitre': q.get('chapitre', 'Autre'),
            'question': q.get('question', ''),
            'sorties': n,
            'taux_echec': round(100 * (n - bonnes) / n, 1) if n else 0,
            'derniere': s['derniere'] if s else None,
            'absente': False,
        })
    retired_count = sum(1 for qid in stats if qid not in seen)
    out.sort(key=lambda r: (-r['taux_echec'], -r['sorties']))
    return jsonify({
        'ok': True,
        'theme': theme,
        'chapitres': sorted(chapitres_disponibles, key=str.lower),
        'rows': out,
        'retired_count': retired_count,
    })

def record_qcm_game(gid, theme, nb_questions, nb_players, podium):
    # Flying: Stats logging must never break the actual QCM flow.
    try:
        ensure_db()
        conn = db()
        conn.execute(
            'INSERT INTO qcm_games(gid, themes, nb_questions, nb_players, podium, created_at) '
            'VALUES(?,?,?,?,?,?)',
            (gid, theme, nb_questions, nb_players, json.dumps(podium, ensure_ascii=False),
             now_paris().isoformat())
        )
        for entry in podium:
            if entry.get('prenom'):
                log_event(conn, entry['prenom'], 'qcm_played',
                          f"{gid}:{entry.get('score', 0)}")
        conn.commit()
        conn.close()
    except Exception as exc:
        # Eliot: Analytics can fail quietly; the game shouldn't.
        print(f'[qcm-games] enregistrement impossible: {exc!r}')

def record_qcm_answer(qid, theme, chapitre, question, prenom, ok, elapsed):
    # Xiao: Ignore answers without a stable question ID.
    if not qid:
        return
    try:
        ensure_db()
        conn = db()
        conn.execute(
            'INSERT INTO qcm_answers(qid, theme, chapitre, question, prenom, ok, elapsed, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (
                qid,
                theme,
                chapitre,
                (question or '')[:300],
                prenom,
                1 if ok else 0,
                elapsed,
                now_paris().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        # Small: Same rule here: stats failure shouldn't take down the QCM.
        print(f'[qcm-stats] enregistrement impossible: {exc!r}')
