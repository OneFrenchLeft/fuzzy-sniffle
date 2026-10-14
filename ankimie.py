# -*- coding: utf-8 -*-
"""Ankimie : repetition espacee (FSRS) sur les questions QCM.

Entierement independante de la SR Forgecards : etats (qcm_sr_state), reviews
(qcm_sr_reviews) et quota (qcm_daily_new_limit) sont propres. Seule la
retention cible FSRS (fsrs_retention) est partagee. Le deck est automatique :
toutes les questions des chapitres actifs en QCM, sans gestion de collections.
Tout est clefe sur le qid stable des questions.
"""
from datetime import timedelta

from flask import Blueprint, jsonify, request, session

from config import QCM_FILES, GRADE_MAP_FR, now_paris, read_params
from db import db, ensure_db, log_event
from fsrs import apply_review, preview_all_grades, humanize_interval
from auth import require_sr_user, require_admin, require_module
import qcm_engine

bp = Blueprint('ankimie', __name__)


def _deck_label():
    """Label informatif du deck (matieres piochees), pour les reponses API."""
    from content_catalog import ankimie_subjects
    subs = ankimie_subjects()
    return '/'.join(subs) if subs else 'aucune'

# preview_all_grades est clefe par ses labels internes ; l'API expose les cles
# FR attendues par le client (boutons d'auto-evaluation).
_PREVIEW_KEY = {'PAS_REUSSI': 'again', 'DIFFICILE': 'hard',
                'J_AI_DU_REFLECHIR': 'good', 'ULTRA_FACILE': 'easy'}


def _retention(params):
    """Retention cible du deck Ankimie (param admin dedie, repli sur la FSRS)."""
    try:
        return float(params.get('ankimie_retention', params.get('fsrs_retention', 0.90)))
    except (TypeError, ValueError):
        return 0.90


def _deck_questions():
    from content_catalog import ankimie_questions
    banks = {theme: qcm_engine.read_qcm_questions(path, theme=theme)
             for theme, path in QCM_FILES.items()}
    return ankimie_questions(banks)


def _deck_index():
    return {q['qid']: q for q in _deck_questions()}


def _ensure_states(conn, prenom, today):
    """Une ligne d'etat par question du deck, lazy (comme sr_today)."""
    index = _deck_index()
    if not index:
        return
    existing = {r[0] for r in conn.execute(
        'SELECT qid FROM qcm_sr_state WHERE prenom=?', (prenom,)).fetchall()}
    missing = [(prenom, qid, today) for qid in index if qid not in existing]
    if missing:
        conn.executemany(
            "INSERT INTO qcm_sr_state (prenom, qid, state, next_review, repetitions, lapses) "
            "VALUES (?, ?, 'new', ?, 0, 0)", missing)
        conn.commit()


@bp.route('/api/ankimie/today', methods=['GET'])
@require_module('ankimie')
@require_sr_user
def ankimie_today():
    """File du jour : toutes les revisions dues + nouvelles dans la limite du quota.

    Jamais d'answer/explication dans cette reponse : le client les demande
    a la revelation, question par question (pas de spoiler dans le payload).
    """
    ensure_db()
    prenom = session['sr_user']
    params = read_params()
    new_limit = max(0, int(params.get('qcm_daily_new_limit', 10)))
    today = now_paris().date().isoformat()
    index = _deck_index()
    conn = db()
    _ensure_states(conn, prenom, today)
    done = conn.execute(
        'SELECT was_new, COUNT(*) FROM qcm_sr_reviews '
        'WHERE prenom=? AND substr(created_at,1,10)=? GROUP BY was_new',
        (prenom, today)).fetchall()
    new_done = sum(c for w, c in done if w)
    rows = conn.execute(
        'SELECT qid, repetitions, next_review FROM qcm_sr_state WHERE prenom=?',
        (prenom,)).fetchall()
    conn.close()
    due = [dict(r) for r in rows if r['qid'] in index
           and (r['next_review'] is None or r['next_review'] <= today)]
    new_due = sorted((c for c in due if not c['repetitions']), key=lambda c: c['qid'])
    review_due = sorted((c for c in due if c['repetitions']),
                        key=lambda c: (c['next_review'] or '', c['qid']))
    new_slots_left = max(0, new_limit - new_done)
    selected = review_due + new_due[:new_slots_left]
    cards = []
    for c in selected:
        q = index[c['qid']]
        cards.append({
            'qid': c['qid'],
            'question': q['question'],
            'choices': q['choices'],
            'chapitre': q['chapitre'],
            'image': q['image'],
            'was_new': not c['repetitions'],
        })
    return jsonify({
        'ok': True,
        'deck': _deck_label(),
        'cards': cards,
        'new_done_today': new_done,
        'new_limit': new_limit,
        'new_remaining_pool': max(0, len(new_due) - min(len(new_due), new_slots_left)),
    })


@bp.route('/api/ankimie/<qid>/answer', methods=['GET'])
@require_module('ankimie')
@require_sr_user
def ankimie_answer(qid):
    """Revelation : bonne reponse + explication + intervalles prevus.

    Les intervalles sous les boutons sont CALCULES par FSRS (etat actuel de
    la carte, retention cible du deck), jamais ecrits en dur : l'eleve voit
    la consequence reelle de chaque note avant de cliquer.
    """
    q = _deck_index().get(qid)
    if not q:
        return jsonify({'ok': False, 'error': 'question introuvable'}), 404
    ensure_db()
    prenom = session['sr_user']
    params = read_params()
    now = now_paris()
    conn = db()
    row = conn.execute(
        'SELECT stability, difficulty, last_review FROM qcm_sr_state WHERE prenom=? AND qid=?',
        (prenom, qid)).fetchone()
    conn.close()
    intervals = {}
    try:
        preview = preview_all_grades(
            row['stability'] if row else None,
            row['difficulty'] if row else None,
            row['last_review'] if row else None,
            now, requested_retention=_retention(params))
        for label, res in preview.items():
            key = _PREVIEW_KEY.get(label)
            if key:
                intervals[key] = humanize_interval(res['interval_days'])
    except Exception:
        intervals = {}
    return jsonify({'ok': True, 'qid': qid, 'answer': q['answer'],
                    'explication': q.get('explication', ''), 'intervals': intervals})


@bp.route('/api/ankimie/<qid>/review', methods=['POST'])
@require_module('ankimie')
@require_sr_user
def ankimie_review(qid):
    """Auto-evaluation 4 boutons -> FSRS. `chosen` = index du choix de l'eleve."""
    ensure_db()
    prenom = session['sr_user']
    data = request.get_json(silent=True) or {}
    result = data.get('result')
    grade = GRADE_MAP_FR.get(result)
    if grade is None:
        return jsonify({'ok': False, 'error': 'result invalide: again/hard/good/easy'}), 400
    index = _deck_index()
    q = index.get(qid)
    if not q:
        return jsonify({'ok': False, 'error': 'question introuvable'}), 404
    try:
        chosen = data.get('chosen')
        chosen = int(chosen) if chosen is not None else None
    except (TypeError, ValueError):
        chosen = None
    if chosen is not None and not (0 <= chosen < len(q['choices'])):
        return jsonify({'ok': False, 'error': 'choix invalide'}), 400
    try:
        duration = int(data.get('duration_seconds'))
        duration = max(0, min(duration, 3600))
    except (TypeError, ValueError):
        duration = None
    # Mode flashcard (pas de choix clique) : la reussite se deduit de
    # l'auto-evaluation — 'again' = rate, hard/good/easy = su.
    if chosen is not None:
        correct = 1 if chosen == q['answer'] else 0
    else:
        correct = 1 if grade >= 2 else 0
    params = read_params()
    retention = _retention(params)
    now = now_paris()
    conn = db()
    row = conn.execute(
        'SELECT * FROM qcm_sr_state WHERE prenom=? AND qid=?', (prenom, qid)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO qcm_sr_state (prenom, qid, state, next_review, repetitions, lapses) "
            "VALUES (?, ?, 'new', ?, 0, 0)", (prenom, qid, now.date().isoformat()))
        row = conn.execute(
            'SELECT * FROM qcm_sr_state WHERE prenom=? AND qid=?', (prenom, qid)).fetchone()
    was_new = 1 if (row['repetitions'] or 0) == 0 else 0
    reps = (row['repetitions'] or 0) + 1
    lapses = (row['lapses'] or 0) + (1 if grade == 1 else 0)
    r = apply_review(row['stability'], row['difficulty'], row['last_review'],
                     grade, now, retention)
    conn.execute(
        'UPDATE qcm_sr_state SET stability=?, difficulty=?, state=?, last_review=?, '
        'next_review=?, repetitions=?, lapses=?, last_retrievability=? '
        'WHERE prenom=? AND qid=?',
        (r['stability'], r['difficulty'], r['state'], now.isoformat(), r['next_review'],
         reps, lapses, r['retrievability_before'], prenom, qid))
    cur = conn.execute(
        'INSERT INTO qcm_sr_reviews(qid, prenom, chapitre, result, grade, correct, was_new, duration_seconds, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (qid, prenom, q.get('chapitre') or '', result, grade, correct, was_new, duration,
         now.isoformat()))
    # Snapshot de l'etat precedent pour "annuler la derniere note".
    conn.execute(
        'INSERT OR REPLACE INTO ankimie_undo(prenom, review_id, qid, prev_stability, prev_difficulty, '
        'prev_state, prev_last_review, prev_next_review, prev_repetitions, prev_lapses, '
        'prev_last_retrievability, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (prenom, cur.lastrowid, qid, row['stability'], row['difficulty'], row['state'],
         row['last_review'], row['next_review'], row['repetitions'] or 0, row['lapses'] or 0,
         row['last_retrievability'], now.isoformat()))
    log_event(conn, prenom, 'ankimie_review', f'{qid}:{result}')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'qid': qid, 'correct': correct, **r,
                    'repetitions': reps})


@bp.route('/api/ankimie/undo', methods=['POST'])
@require_module('ankimie')
@require_sr_user
def ankimie_undo():
    """Annule la derniere review de l'eleve : restaure l'etat FSRS d'avant
    et retire la review du journal. Une seule profondeur (derniere carte)."""
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    undo = conn.execute('SELECT * FROM ankimie_undo WHERE prenom=?', (prenom,)).fetchone()
    if not undo:
        conn.close()
        return jsonify({'ok': False, 'error': 'Rien à annuler.'}), 404
    review = conn.execute(
        'SELECT id FROM qcm_sr_reviews WHERE id=? AND prenom=?',
        (undo['review_id'], prenom)).fetchone()
    if not review:
        conn.execute('DELETE FROM ankimie_undo WHERE prenom=?', (prenom,))
        conn.commit()
        conn.close()
        return jsonify({'ok': False, 'error': 'Déjà annulée.'}), 404
    # La review annulee doit etre la plus recente de l'eleve, sinon
    # l'annulation casserait la chronologie FSRS des cartes suivantes.
    last = conn.execute(
        'SELECT id FROM qcm_sr_reviews WHERE prenom=? ORDER BY id DESC LIMIT 1',
        (prenom,)).fetchone()
    if not last or last['id'] != review['id']:
        conn.close()
        return jsonify({'ok': False, 'error': 'Seule la toute dernière carte peut être annulée.'}), 409
    conn.execute('DELETE FROM qcm_sr_reviews WHERE id=?', (review['id'],))
    conn.execute(
        'UPDATE qcm_sr_state SET stability=?, difficulty=?, state=?, last_review=?, '
        'next_review=?, repetitions=?, lapses=?, last_retrievability=? '
        'WHERE prenom=? AND qid=?',
        (undo['prev_stability'], undo['prev_difficulty'], undo['prev_state'] or 'new',
         undo['prev_last_review'], undo['prev_next_review'], undo['prev_repetitions'] or 0,
         undo['prev_lapses'] or 0, undo['prev_last_retrievability'], prenom, undo['qid']))
    conn.execute('DELETE FROM ankimie_undo WHERE prenom=?', (prenom,))
    log_event(conn, prenom, 'ankimie_undo', undo['qid'])
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'qid': undo['qid']})


@bp.route('/api/admin/ankimie/reset/<prenom>', methods=['POST'])
@require_admin
def ankimie_reset_student(prenom):
    """Reinitialise l'etat FSRS Ankimie d'un eleve (toutes questions repassent
    en nouvelles). Le journal des reviews est conserve."""
    ensure_db()
    conn = db()
    if not conn.execute('SELECT 1 FROM users WHERE prenom=?', (prenom,)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Élève introuvable.'}), 404
    n = _reset_states(conn, 'prenom=?', (prenom,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'reset': n})


@bp.route('/api/admin/ankimie/reset-question/<qid>', methods=['POST'])
@require_admin
def ankimie_reset_question(qid):
    """Reinitialise l'etat FSRS d'une question pour TOUS les eleves (utile
    apres correction d'un enonce). Le journal des reviews est conserve."""
    ensure_db()
    conn = db()
    n = _reset_states(conn, 'qid=?', (qid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'reset': n})


def _reset_states(conn, where, args):
    today = now_paris().date().isoformat()
    return conn.execute(
        "UPDATE qcm_sr_state SET stability=NULL, difficulty=NULL, state='new', "
        "last_review=NULL, next_review=?, repetitions=0, lapses=0, "
        "last_retrievability=NULL WHERE " + where, (today,) + tuple(args)).rowcount


@bp.route('/api/ankimie/stats', methods=['GET'])
@require_admin
def ankimie_stats():
    """Suivi admin du deck : par eleve, par chapitre, par question,
    retention reelle et prevision de charge sur 7 jours."""
    ensure_db()
    today = now_paris().date()
    today_s = today.isoformat()
    conn = db()
    active_qids = set(_deck_index())

    students = []
    rows = conn.execute(
        'SELECT prenom, COUNT(*) AS vues, SUM(correct) AS bonnes, '
        'SUM(CASE WHEN substr(created_at,1,10)=? THEN 1 ELSE 0 END) AS aujourd_hui, '
        'AVG(duration_seconds) AS avg_dur '
        'FROM qcm_sr_reviews GROUP BY prenom ORDER BY prenom', (today_s,)).fetchall()
    for r in rows:
        due_rows = conn.execute(
            'SELECT qid FROM qcm_sr_state '
            'WHERE prenom=? AND (next_review IS NULL OR next_review<=?)',
            (r['prenom'], today_s)).fetchall()
        due = sum(row['qid'] in active_qids for row in due_rows)
        students.append({
            'prenom': r['prenom'],
            'vues': r['vues'],
            'aujourd_hui': r['aujourd_hui'],
            'taux': round(100.0 * (r['bonnes'] or 0) / r['vues'], 1) if r['vues'] else None,
            'avg_dur': round(r['avg_dur']) if r['avg_dur'] is not None else None,
            'dues': due,
        })

    chapters = []
    for r in conn.execute(
            'SELECT chapitre, COUNT(*) AS n, SUM(correct) AS bonnes '
            'FROM qcm_sr_reviews GROUP BY chapitre ORDER BY n DESC').fetchall():
        chapters.append({
            'chapitre': r['chapitre'] or 'Autre',
            'questions': r['n'],
            'taux': round(100.0 * (r['bonnes'] or 0) / r['n'], 1) if r['n'] else None,
        })

    # Par question : les plus ratees (taux d'echec = reponses fausses),
    # 3 vues minimum pour eviter le bruit statistique.
    qindex = _deck_index()
    weak = []
    for r in conn.execute(
            'SELECT qid, chapitre, COUNT(*) AS n, SUM(correct) AS bonnes, '
            'AVG(duration_seconds) AS avg_dur '
            'FROM qcm_sr_reviews GROUP BY qid HAVING n >= 3').fetchall():
        taux_echec = round(100.0 * (r['n'] - (r['bonnes'] or 0)) / r['n'], 1)
        q = qindex.get(r['qid'])
        weak.append({
            'qid': r['qid'],
            'question': (q['question'][:120] if q else None),
            'chapitre': (q['chapitre'] if q else r['chapitre']) or 'Autre',
            'retiree': q is None,
            'vues': r['n'],
            'taux_echec': taux_echec,
            'avg_dur': round(r['avg_dur']) if r['avg_dur'] is not None else None,
        })
    weak.sort(key=lambda x: (-x['taux_echec'], -x['vues']))
    weak = weak[:30]

    # Retention reelle : reussite sur les revisions (hors nouvelles cartes).
    ret = conn.execute(
        'SELECT COUNT(*) AS n, SUM(correct) AS ok FROM qcm_sr_reviews WHERE was_new=0').fetchone()
    retention_reelle = round(100.0 * (ret['ok'] or 0) / ret['n'], 1) if ret['n'] else None

    # Prevision de charge : cartes dues par jour, 7 prochains jours (les
    # cartes en retard comptent pour aujourd'hui). Deck actif uniquement.
    horizon = (today + timedelta(days=6)).isoformat()
    due_rows = conn.execute(
        'SELECT qid, next_review FROM qcm_sr_state '
        'WHERE next_review IS NOT NULL AND next_review<=?', (horizon,)).fetchall()
    buckets = {(today + timedelta(days=k)).isoformat(): 0 for k in range(7)}
    for row in due_rows:
        if row['qid'] not in active_qids:
            continue
        d = row['next_review'] if row['next_review'] >= today_s else today_s
        if d in buckets:
            buckets[d] += 1
    forecast = [{'d': d, 'n': n} for d, n in sorted(buckets.items())]

    conn.close()
    return jsonify({'ok': True, 'deck': _deck_label(), 'students': students,
                    'chapters': chapters, 'questions': weak,
                    'retention_reelle': retention_reelle, 'forecast': forecast})
