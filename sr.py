# Xiao: Routes for the student review space. Keep the business logic out of here.
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, session
from fsrs import apply_review, preview_all_grades, humanize_interval
from config import (GRADE_MAP_FR, MAX_NOTE_LENGTH, QCM_FILES, QCM_WEAK_LIMIT,
                    now_paris, read_params)
from db import db, ensure_db, log_event, csv_safe, csv_response
from helpers import card_label, card_exists, interleave_by_chapitre, parse_duration_seconds
from replay import replay_reviews, get_user_weights
from streak import (compute_streak, activity_days, compute_record,
                     reconcile_streak, streak_verdict, close_day)
from auth import require_admin, require_sr_user
import qcm_engine
import io, csv

bp = Blueprint('sr', __name__)

def ensure_sr_row(conn, prenom, numero):
    # Small: Create the state lazily; no reason to pre-fill the whole table.
    row = conn.execute('SELECT * FROM sr_state_user WHERE prenom=? AND numero=?', (prenom, numero)).fetchone()
    if row is None:
        today = now_paris().date().isoformat()
        conn.execute(
            'INSERT INTO sr_state_user (prenom, numero, stability, difficulty, state, last_review, next_review, repetitions, lapses) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (prenom, numero, None, None, 'new', None, today, 0, 0)
        )
        row = conn.execute('SELECT * FROM sr_state_user WHERE prenom=? AND numero=?', (prenom, numero)).fetchone()
    return row

@bp.route('/api/sr/today', methods=['GET'])
@require_sr_user
def sr_today():
    ensure_db()
    prenom = session['sr_user']
    try:
        # Ethan: Logging the open is useful for streak validation. Please don't remove it.
        conn0 = db()
        log_event(conn0, prenom, 'sr_open')
        conn0.commit()
        conn0.close()
    except Exception:
        pass
    today = now_paris().date().isoformat()
    params = read_params()
    max_active = int(params.get('max_active_num', 36))
    new_limit = int(params.get('daily_new_limit', 3))
    review_limit = int(params.get('daily_review_limit', 3))

    conn = db()
    all_cards = conn.execute('SELECT numero FROM forgecards WHERE numero <= ? AND hors_serie = 0', (max_active,)).fetchall()
    for c in all_cards:
        ensure_sr_row(conn, prenom, c['numero'])
    conn.commit()

    try:
        close_day(conn, prenom, (now_paris().date() - timedelta(days=1)).isoformat(),
                  params, reason='sr_open_spend')
    except Exception:
        pass

    done_today = conn.execute(
        "SELECT numero, was_new FROM reviews WHERE prenom=? AND substr(created_at,1,10)=?",
        (prenom, today)
    ).fetchall()
    new_done_today = sum(1 for r in done_today if r['was_new'])
    review_done_today = sum(1 for r in done_today if not r['was_new'])
    new_slots_left = max(0, new_limit - new_done_today)
    review_slots_left = max(0, review_limit - review_done_today)

    rows = conn.execute(
        'SELECT f.numero, f.fiche_file, f.correction_file, f.bareme_file, f.titre, f.indices, f.chapitre, '
        's.difficulty, s.stability, s.next_review, s.last_review, s.repetitions '
        'FROM forgecards f '
        'JOIN sr_state_user s ON s.numero = f.numero AND s.prenom = ? '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY f.numero ASC',
        (prenom, max_active)
    ).fetchall()
    streak = compute_streak(conn, prenom)
    conn.close()

    due = [dict(r) for r in rows if (r['next_review'] is None or r['next_review'] <= today)]
    due.sort(key=lambda c: (c.get('next_review') or '', -(c.get('difficulty') or 5)))

    new_due = [c for c in due if c.get('repetitions', 0) == 0]
    review_due = [c for c in due if c.get('repetitions', 0) > 0]

    selected_new = new_due[:new_slots_left]
    selected_review = review_due[:review_slots_left]
    selected = selected_review + selected_new

    # Flying: Interleave after selecting, otherwise the chapter balancing is pointless.
    interleaved = interleave_by_chapitre(selected)
    for chosen in interleaved:
        chosen['was_new'] = chosen.get('repetitions', 0) == 0

    tomorrow = (now_paris().date() + timedelta(days=1)).isoformat()
    tomorrow_cards = [c for c in [dict(r) for r in rows] if c.get('next_review') == tomorrow]
    nb_new_tomorrow = sum(1 for c in tomorrow_cards if c.get('repetitions', 0) == 0)
    nb_review_tomorrow = sum(1 for c in tomorrow_cards if c.get('repetitions', 0) > 0)
    tomorrow_forecast = {
        'total': nb_new_tomorrow + nb_review_tomorrow,
        'nb_new': nb_new_tomorrow,
        'nb_review': nb_review_tomorrow,
    }
    conn = db()
    # Steph: Same verdict as the guard, otherwise the UI and backend will disagree again.
    auto_validated_streak = len(all_cards) > 0 and streak_verdict(conn, prenom, params, prediction=True) == 'validated'
    jrow = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    jokers = jrow['count'] if jrow else 0

    return jsonify({
        'cards': interleaved,
        'streak': streak,
        'new_done_today': new_done_today,
        'new_limit': new_limit,
        'review_done_today': review_done_today,
        'review_limit': review_limit,
        'new_remaining_pool': max(0, len(new_due) - len(selected_new)),
        'review_remaining_pool': max(0, len(review_due) - len(selected_review)),
        'tomorrow_forecast': tomorrow_forecast,
        'auto_validated_streak': auto_validated_streak,
        'jokers': jokers,
    })

@bp.route('/api/sr/dashboard', methods=['GET'])
@require_sr_user
def sr_dashboard():
    ensure_db()
    prenom = session['sr_user']
    today = now_paris().date()
    week_ago = (today - timedelta(days=7)).isoformat()
    conn = db()
    streak = compute_streak(conn, prenom)
    jrow = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    jokers = jrow['count'] if jrow else 0
    total = conn.execute('SELECT COUNT(*) AS c FROM reviews WHERE prenom=?', (prenom,)).fetchone()['c']
    semaine = conn.execute(
        'SELECT COUNT(*) AS c FROM reviews WHERE prenom=? AND substr(created_at,1,10)>=?',
        (prenom, week_ago)).fetchone()['c']
    days = activity_days(conn, prenom)
    record = compute_record(days)
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'streak': streak, 'record': record,
                    'jokers': jokers, 'semaine': semaine, 'total': total})

@bp.route('/api/sr/qcm/errors', methods=['GET'])
@require_sr_user
def sr_qcm_errors():
    # Eliot: Only keep the latest failed attempt for each question.
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    rows = conn.execute(
        'SELECT a.qid, a.theme, a.chapitre, a.question, a.ok, a.elapsed, a.created_at '
        'FROM qcm_answers a '
        'JOIN (SELECT qid, MAX(id) AS mid FROM qcm_answers '
        'WHERE prenom = ? AND ok = 0 GROUP BY qid) m ON a.id = m.mid '
        'ORDER BY a.created_at DESC LIMIT 10',
        (prenom,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = {
            'qid': r['qid'], 'theme': r['theme'], 'chapitre': r['chapitre'],
            'question': r['question'], 'ok': bool(r['ok']),
            'elapsed': r['elapsed'], 'created_at': r['created_at'],
        }
        path = QCM_FILES.get(r['theme'])
        if path and path.exists():
            for q in qcm_engine.read_qcm_questions(path, theme=r['theme']):
                if q.get('qid') == r['qid']:
                    if q.get('explication'):
                        item['explication'] = q['explication']
                    try:
                        idx = q.get('answer')
                        choix = q.get('choices') or []
                        if idx is not None and 0 <= idx < len(choix):
                            bonne = choix[idx]
                            if isinstance(bonne, dict):
                                item['bonne_reponse'] = bonne.get('texte') or bonne.get('text') or ''
                            else:
                                item['bonne_reponse'] = str(bonne)
                    except Exception:
                        pass
                    break
        out.append(item)
    return jsonify({'ok': True, 'rows': out})

@bp.route('/api/sr/qcm/weak', methods=['GET'])
@require_sr_user
def sr_qcm_weak():
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    # Xiao: Group by qid, not question text. Text can change; IDs should not.
    rows = conn.execute(
        "SELECT question, theme, chapitre, COUNT(*) AS n, SUM(ok) AS bonnes "
        "FROM qcm_answers WHERE prenom=? GROUP BY qid "
        "HAVING n > bonnes ORDER BY (n - bonnes) * 1.0 / n DESC, n DESC LIMIT %d" % QCM_WEAK_LIMIT,
        (prenom,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        n = r['n']
        out.append({'question': r['question'], 'theme': r['theme'], 'chapitre': r['chapitre'],
                    'sorties': n, 'echecs': n - (r['bonnes'] or 0),
                    'taux_echec': round(100 * (n - (r['bonnes'] or 0)) / n, 1)})
    return jsonify({'ok': True, 'rows': out})

@bp.route('/api/sr/<int:numero>/preview', methods=['GET'])
@require_sr_user
def sr_preview(numero):
    ensure_db()
    prenom = session['sr_user']
    now = now_paris()
    params = read_params()
    retention = float(params.get('fsrs_retention', 0.90))
    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    row = ensure_sr_row(conn, prenom, numero)
    prof_row = conn.execute('SELECT teacher_difficulty FROM forgecards WHERE numero=?', (numero,)).fetchone()
    prof_difficulty = prof_row['teacher_difficulty'] if prof_row else None
    weights = get_user_weights(conn, prenom)
    conn.close()

    # Small: Preview uses the same weights as an actual review.
    preview = preview_all_grades(row['stability'], row['difficulty'], row['last_review'], now, retention,
                                 w=weights, prof_difficulty=prof_difficulty)
    for label, info in preview.items():
        info['label'] = humanize_interval(info['interval_days'])

    return jsonify({'numero': numero, 'preview': preview})

@bp.route('/api/sr/<int:numero>/review', methods=['POST'])
@require_sr_user
def sr_review(numero):
    ensure_db()
    prenom = session['sr_user']
    data = request.get_json(silent=True) or {}
    result = data.get('result')
    note = (data.get('note') or '').strip()[:MAX_NOTE_LENGTH]
    duration = parse_duration_seconds(data.get('duration_seconds'))

    grade = GRADE_MAP_FR.get(result)
    if grade is None:
        # Ethan: Validate the grade before touching the database.
        return jsonify({'ok': False, 'error': 'result invalide: again/hard/good/easy'}), 400
    if grade == 1 and len(note) < 10:
        # Steph: A failed card needs an actual explanation, not "idk".
        return jsonify({'ok': False, 'error': "Decris ton blocage (10 caracteres min) : items du bareme rates, endroit ou tu as bloque..."}), 400

    now = now_paris()
    params = read_params()
    retention = float(params.get('fsrs_retention', 0.90))

    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    prof_row = conn.execute('SELECT teacher_difficulty FROM forgecards WHERE numero=?', (numero,)).fetchone()
    prof_difficulty = prof_row['teacher_difficulty'] if prof_row else None
    row = ensure_sr_row(conn, prenom, numero)
    was_new = 1 if (row['repetitions'] or 0) == 0 else 0
    reps = (row['repetitions'] or 0) + 1
    lapses = (row['lapses'] or 0) + (1 if grade == 1 else 0)
    weights = get_user_weights(conn, prenom)
    r = apply_review(row['stability'], row['difficulty'], row['last_review'], grade, now, retention,
                     w=weights, prof_difficulty=prof_difficulty)

    conn.execute(
        'UPDATE sr_state_user SET stability=?, difficulty=?, state=?, last_review=?, next_review=?, '
        'repetitions=?, lapses=?, last_retrievability=? WHERE prenom=? AND numero=?',
        (r['stability'], r['difficulty'], r['state'], now.isoformat(), r['next_review'],
         reps, lapses, r['retrievability_before'], prenom, numero)
    )
    conn.execute(
        'INSERT INTO reviews(numero, prenom, result, note, duration_seconds, was_new, created_at) VALUES(?,?,?,?,?,?,?)',
        (numero, prenom, result, note, duration, was_new, now.isoformat())
    )
    log_event(conn, prenom, 'review', f'{numero}:{result}')
    conn.commit()
    # Flying: Reconcile only after the review is safely committed.
    streak = reconcile_streak(conn, prenom)
    conn.close()
    return jsonify({'ok': True, 'numero': numero, **r, 'repetitions': reps, 'lapses': lapses, 'streak': streak})

@bp.route('/api/sr/<int:numero>/advance', methods=['POST'])
@require_sr_user
def sr_advance(numero):
    ensure_db()
    prenom = session['sr_user']
    data = request.get_json(silent=True) or {}
    try:
        days = max(1, min(30, int(data.get('days', 1))))
    except (TypeError, ValueError):
        # Eliot: Invalid input gets a safe default. Better than making the route complain.
        days = 1
    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    row = ensure_sr_row(conn, prenom, numero)
    base_date = now_paris().date()
    if row['next_review']:
        try:
            base_date = max(base_date, datetime.strptime(row['next_review'], '%Y-%m-%d').date())
        except Exception:
            # Xiao: Keep malformed legacy dates from killing the endpoint.
            pass
    new_next = (base_date + timedelta(days=days)).isoformat()
    conn.execute('UPDATE sr_state_user SET next_review=? WHERE prenom=? AND numero=?', (new_next, prenom, numero))
    log_event(conn, prenom, 'advance', f'{numero}:+{days}j')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'numero': numero, 'next_review': new_next})

@bp.route('/api/sr/export', methods=['GET'])
@require_admin
def sr_export():
    ensure_db()
    max_active = int(read_params().get('max_active_num', 36))
    conn = db()
    rows = conn.execute(
        'SELECT s.prenom, f.code AS numero, s.difficulty, s.stability, s.last_review, s.next_review, s.repetitions, s.lapses, s.last_retrievability '
        'FROM sr_state_user s JOIN forgecards f ON f.numero = s.numero '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY s.prenom ASC, s.numero ASC',
        (max_active,)
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Prénom', 'Numéro', 'Difficulté', 'Stabilité', 'Dernière révision', 'Prochaine révision', 'Répétitions', "Nombres d'oublie (implique recommencer FSRS)", 'Dernière retention testée'])
    for r in rows:
        writer.writerow([csv_safe(r['prenom']), r['numero'], r['difficulty'], r['stability'], r['last_review'], r['next_review'], r['repetitions'], r['lapses'], r['last_retrievability']])
    return csv_response(output, 'Statistiques_FSRS.csv')

@bp.route('/api/qcm/last-states', methods=['GET'])
@require_sr_user
def qcm_last_states():
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    rows = conn.execute(
        "SELECT a.qid, a.theme FROM qcm_answers a "
        "JOIN (SELECT qid, MAX(id) AS mid FROM qcm_answers WHERE prenom=? GROUP BY qid) m "
        "ON a.id = m.mid WHERE a.ok = 0",
        (prenom,)
    ).fetchall()
    conn.close()
    valid = set()
    for theme, path in QCM_FILES.items():
        if path.exists():
            for q in qcm_engine.read_qcm_questions(path, theme=theme):
                if q.get('qid'):
                    valid.add(q['qid'])
    counts = {}
    for r in rows:
        if r['qid'] in valid:
            t = r['theme'] or 'autre'
            counts[t] = counts.get(t, 0) + 1
    return jsonify({'ok': True, 'wrongs': counts})

