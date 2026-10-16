# Xiao: Admin-side QCM editing and statistics. Keep validation strict here.
from flask import Blueprint, request, jsonify, abort, current_app
from pathlib import Path
import json, hashlib, re, os, tempfile, fcntl
from contextlib import contextmanager, ExitStack
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

def _text(value, required=False):
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise ValueError('un texte est attendu')
    value = value.strip()
    if required and not value:
        raise ValueError('un texte non vide est attendu')
    return value


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
    text = _text(value.get('texte', value.get('text', '')))
    image = _text(value.get('image', ''))
    latex = _text(value.get('latex', ''))

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

        try:
            question = _text(item.get('question'), required=True)
        except ValueError:
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
            reponse = qcm_engine.qcm_integer(item.get('reponse'))
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
            temps = qcm_engine.qcm_integer(item.get('temps', QCM_DEFAULT_TIME_S))
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

        image = _text(item.get('image', item.get('image_complete', '')))
        latex = _text(item.get('latex', ''))

        if image:
            normalized['image'] = image
        if latex:
            normalized['latex'] = latex
        chapitre = _text(item.get('chapitre', ''))
        if chapitre:
            normalized['chapitre'] = chapitre
        explication = _text(item.get('explication', ''))
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


def bank_revision(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bank(theme):
    path = get_qcm_path(theme)
    raw = path.read_bytes() if path.exists() else b''
    parsed = json.loads(raw.decode('utf-8-sig')) if raw else []
    return validate_qcm_questions(parsed, theme), bank_revision(raw)


@contextmanager
def bank_lock(theme):
    path = get_qcm_path(theme)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic_bank_write(path, raw):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class BankConflict(ValueError):
    pass


def validate_bank_questions(questions, theme):
    clean = validate_qcm_questions(questions, theme)
    ids = [q['qid'] for q in clean]
    if len(ids) != len(set(ids)):
        raise ValueError('Une question est présente plusieurs fois. Retire le doublon avant d’enregistrer.')
    return clean


def notify_catalog_change():
    # Emit only after releasing the file lock: a socket write may yield.
    if qcm_engine.io is not None:
        try:
            qcm_engine.push_lobbies()
        except Exception as exc:
            print(f'[qcm-admin] actualisation des lobbies impossible: {exc!r}')


def store_bank(theme, questions, revision=None):
    """Caller holds bank_lock across reading, building and replacing a bank."""
    path = get_qcm_path(theme)
    previous, current_revision = read_bank(theme)
    if revision is not None and revision != current_revision:
        raise BankConflict('La banque a changé depuis son chargement. Recharge-la avant de réessayer.')
    clean = validate_bank_questions(questions, theme)
    from content_catalog import chapter_name
    old_chapters = {chapter_name(q.get('chapitre')) for q in previous}
    new_chapters = {chapter_name(q.get('chapitre')) for q in clean} - old_chapters
    ensure_db()
    conn = db()
    created_chapters = []
    raw = (json.dumps(clean, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    try:
        with conn:
            for chapter in new_chapters:
                # Previously imported chapters stay active; newly introduced ones
                # enter the bank inactive until a teacher explicitly enables them.
                inserted = conn.execute('INSERT OR IGNORE INTO content_chapters '
                                        '(theme, chapitre, qcm_active, ankimie_active) VALUES(?,?,0,0)',
                                        (theme, chapter))
                if inserted.rowcount:
                    created_chapters.append(chapter)
        # Commit inactive flags BEFORE exposing a new chapter in the file.
        # A worker crash after the atomic rename cannot accidentally activate it.
        _atomic_bank_write(path, raw)
    except Exception:
        try:
            with conn:
                for chapter in created_chapters:
                    conn.execute('DELETE FROM content_chapters WHERE theme=? AND chapitre=?',
                                 (theme, chapter))
        except Exception:
            # Keeping an empty inactive draft is safe if metadata cleanup fails.
            pass
        raise
    finally:
        conn.close()
    # Retiring a question never implicitly deletes student history. The existing
    # explicit purge API remains available for deliberate historical cleanup.
    qcm_engine._QUESTIONS_CACHE.clear()
    return clean, bank_revision(raw)

@bp.route('/api/admin/qcm/<theme>', methods=['GET'])
@require_admin
def admin_get_qcm(theme):
    path = get_qcm_path(theme)
    try:
        questions, revision = read_bank(theme)
        mtime = path.stat().st_mtime if path.exists() else None
    except (ValueError, UnicodeError) as e:
        return jsonify({'ok': False, 'error': f'Banque invalide : {e}'}), 400
    except OSError as e:
        return jsonify({'ok': False, 'error': f'lecture impossible : {e}'}), 500
    from content_catalog import chapter_overview
    return jsonify({'ok': True, 'theme': theme,
                    'raw': json.dumps(questions, ensure_ascii=False, indent=2),
                    'mtime': mtime, 'revision': revision,
                    'chapters': chapter_overview(theme, questions)})

def _purge_retired_qids(theme=None, chapitre=''):
    """Remove qcm_answers rows for questions no longer present in the files."""
    ensure_db()
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    with ExitStack() as locks:
        for t in sorted(themes):
            locks.enter_context(bank_lock(t))
        active_qids = set()
        for t in themes:
            path = get_qcm_path(t)
            if not path.is_file() or not path.read_bytes().strip():
                raise ValueError('Banque absente ou vide : ' + t)
            questions, _ = read_bank(t)
            active_qids.update(q['qid'] for q in questions if q.get('qid'))
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
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'objet JSON attendu'}), 400
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
        with bank_lock(theme):
            questions, revision = store_bank(theme, parsed, payload.get('revision'))
    except BankConflict as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    except (ValueError, UnicodeError) as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except OSError as e:
        return jsonify({'ok': False, 'error': f'impossible d’enregistrer : {e}'}), 500
    notify_catalog_change()
    return jsonify({'ok': True, 'theme': theme, 'count': len(questions), 'revision': revision})

@bp.route('/api/admin/qcm/purge-retired', methods=['POST'])
@require_admin
def admin_qcm_purge_retired():
    try:
        deleted, count = _purge_retired_qids(request.args.get('theme', ''), request.args.get('chapitre', ''))
    except (OSError, ValueError, UnicodeError) as exc:
        current_app.logger.warning('Purge QCM annulée : %s', exc)
        return jsonify({'ok': False, 'error': 'Purge annulée : une banque est absente ou illisible.'}), 409
    return jsonify({'ok': True, 'deleted': deleted, 'questions': count})

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
            if not isinstance(podium, list):
                raise ValueError('le podium doit être une liste')
            if any(not isinstance(entry, dict) or not isinstance(entry.get('prenom'), str)
                   or type(entry.get('score')) is not int for entry in podium):
                raise ValueError('entrée de podium invalide')
        except (TypeError, ValueError) as exc:
            current_app.logger.warning('Podium QCM invalide (%s) : %s', r['gid'], exc)
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
    # Vue ADMIN uniquement : on ne compte que les reponses posterieures au
    # dernier reset_at (qcm_stats_reset). Les vues eleves ignorent ce marqueur
    # et lisent tout qcm_answers — leur historique est intact.
    sql = ("SELECT a.qid, a.theme, a.chapitre, a.question, COUNT(*) AS n, "
           "SUM(a.ok) AS bonnes, MAX(a.created_at) AS derniere "
           "FROM qcm_answers a "
           "LEFT JOIN qcm_stats_reset r ON r.qid = a.qid AND r.theme = a.theme "
           "WHERE a.created_at > COALESCE(r.reset_at, '')")
    clauses, params_sql = [], []
    if theme in QCM_FILES:
        clauses.append('a.theme = ?')
        params_sql.append(theme)
    if chapitre:
        clauses.append('a.chapitre = ?')
        params_sql.append(chapitre)
    if clauses:
        sql += ' AND ' + ' AND '.join(clauses)
    sql += ' GROUP BY a.qid'
    conn = db()
    stats = {r['qid']: r for r in conn.execute(sql, params_sql).fetchall()}
    # Distribution des reponses fausses par question (choice NULL = sans reponse).
    wrong_sql = ("SELECT a.qid, a.choice, COUNT(*) AS nb FROM qcm_answers a "
                 "LEFT JOIN qcm_stats_reset r ON r.qid = a.qid AND r.theme = a.theme "
                 "WHERE a.ok = 0 AND a.created_at > COALESCE(r.reset_at, '')")
    if clauses:
        wrong_sql += ' AND ' + ' AND '.join(clauses)
    wrong_sql += ' GROUP BY a.qid, a.choice'
    wrong_by_qid = {}
    for r in conn.execute(wrong_sql, params_sql).fetchall():
        key = 'absent' if r['choice'] is None else str(r['choice'])
        wrong_by_qid.setdefault(r['qid'], {})[key] = r['nb']
    conn.close()
    out, seen = [], set()
    for qid, q in catalog.items():
        if chapitre and q.get('chapitre') != chapitre:
            continue
        seen.add(qid)
        s = stats.get(qid)
        n = s['n'] if s else 0
        # Feature « non abordees » : aucune reponse prise en compte cote admin
        # (jamais repondue, ou rien depuis le dernier reset) => hors de la liste.
        if n == 0:
            continue
        bonnes = (s['bonnes'] or 0) if s else 0
        out.append({
            'qid': qid, 'theme': q.get('theme', ''),
            'chapitre': q.get('chapitre', 'Autre'),
            'question': q.get('question', ''),
            'options': [c.get('text', '') for c in q.get('choices', [])],
            'sorties': n,
            'taux_echec': round(100 * (n - bonnes) / n, 1) if n else 0,
            'derniere': s['derniere'] if s else None,
            'wrong': wrong_by_qid.get(qid, {}),
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


@bp.route('/api/admin/qcm/questions/reset-stats', methods=['POST'])
@require_admin
def admin_qcm_reset_question_stats():
    """Reinitialise les stats ADMIN de questions (les eleves gardent tout).

    On ne supprime JAMAIS qcm_answers : on pose un marqueur reset_at et les
    requetes admin ne comptent que les reponses posterieures. Chaque question
    est ciblee par sa cle unique (qid, theme) — pas par son texte.
    """
    ensure_db()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'objet JSON attendu'}), 400
    questions = data.get('questions')
    if not isinstance(questions, list) or not questions:
        return jsonify({'ok': False, 'error': 'liste questions requise'}), 400
    if len(questions) > 500:
        return jsonify({'ok': False, 'error': 'trop de questions d\'un coup'}), 400
    now = now_paris().isoformat()
    rows = []
    for item in questions:
        if not isinstance(item, dict):
            return jsonify({'ok': False, 'error': 'format attendu : {qid, theme}'}), 400
        qid = str(item.get('qid') or '').strip()
        theme = str(item.get('theme') or '').strip()
        if not qid or theme not in QCM_FILES:
            return jsonify({'ok': False, 'error': 'qid ou theme invalide'}), 400
        rows.append((qid, theme, now))
    conn = db()
    conn.executemany(
        'INSERT OR REPLACE INTO qcm_stats_reset(qid, theme, reset_at) VALUES(?,?,?)',
        rows)
    for qid, theme, _ in rows:
        log_event(conn, 'admin', 'qcm_stats_reset', f'{theme}:{qid}')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'reset': len(rows)})

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

def record_qcm_answer(qid, theme, chapitre, question, prenom, ok, elapsed, choice=None):
    # Xiao: Ignore answers without a stable question ID.
    if not qid:
        return
    try:
        ensure_db()
        conn = db()
        conn.execute(
            'INSERT INTO qcm_answers(qid, theme, chapitre, question, prenom, ok, choice, elapsed, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (
                qid,
                theme,
                chapitre,
                (question or '')[:300],
                prenom,
                1 if ok else 0,
                choice,
                elapsed,
                now_paris().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        # Small: Same rule here: stats failure shouldn't take down the QCM.
        print(f'[qcm-stats] enregistrement impossible: {exc!r}')
