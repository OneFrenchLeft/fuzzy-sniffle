# Xiao: Replay logic only. Keep it deterministic.
from datetime import datetime
import json
from fsrs import apply_review, DEFAULT_WEIGHTS
from config import GRADE_MAP_FR, read_params, now_paris
from db import db

def get_user_weights(conn, prenom, subject='physique'):
    # Small: Validate the shape before trusting stored weights.
    row = conn.execute('SELECT weights_json FROM sr_weights_user WHERE prenom=? AND subject=?',
                       (prenom, subject)).fetchone()
    if row and row['weights_json']:
        try:
            w = json.loads(row['weights_json'])
            if isinstance(w, list) and len(w) == len(DEFAULT_WEIGHTS) and all(isinstance(v, (int, float)) for v in w):
                return w
        except Exception:
            # Ethan: Bad JSON should fall back to defaults, not kill the replay.
            pass
    return list(DEFAULT_WEIGHTS)

def replay_reviews(conn, prenom, rows, subject='physique'):
    """Replays a student's FSRS history: for each review, returns the card state
    AFTER that review plus retrievability BEFORE it.
    rows: rows with created_at, numero, result keys, sorted chronologically."""
    # Flying: Build this once instead of querying teacher difficulty for every review.
    prof_by_num = {r['numero']: r['teacher_difficulty']
                   for r in conn.execute('SELECT numero, teacher_difficulty FROM forgecards WHERE subject=?',
                                         (subject,)).fetchall()}
    weights = get_user_weights(conn, prenom, subject)
    retention = float(read_params(subject).get('fsrs_retention', 0.90))
    state_by_card = {}
    out = []
    for rv in rows:
        numero = rv['numero']
        s_prev, d_prev, last_prev, reps, lapses = state_by_card.get(numero, (None, None, None, 0, 0))
        grade = GRADE_MAP_FR.get(rv['result'])
        try:
            when = datetime.fromisoformat(rv['created_at'])
        except (TypeError, ValueError):
            # Steph: Corrupt timestamps should not stop the entire history replay.
            when = now_paris()
        snap = {'difficulty': None, 'stability': None, 'repetitions': 0, 'lapses': 0,
                'next_review': None, 'state': None, 'r_before': None, 'elapsed_days': None}
        if grade is not None:
            try:
                # Eliot: Same weights and teacher difficulty as the live FSRS path.
                r = apply_review(s_prev, d_prev, last_prev, grade, when, retention,
                                 w=weights, fuzz=False, prof_difficulty=prof_by_num.get(numero))
                snap.update({'difficulty': r['stability'] and r['difficulty'], 'stability': r['stability'],
                             'next_review': r['next_review'], 'state': r['state'],
                             'r_before': r['retrievability_before'], 'elapsed_days': r['elapsed_days']})
                state_by_card[numero] = (r['stability'], r['difficulty'], when.isoformat(), reps + 1, lapses + (1 if grade == 1 else 0))
            except Exception:
                # Xiao: Preserve the previous state if FSRS rejects one historical review.
                state_by_card[numero] = (s_prev, d_prev, last_prev, reps + 1, lapses + (1 if grade == 1 else 0))
        else:
            # Small: Unknown grades still count as attempts, but cannot update FSRS.
            state_by_card[numero] = (s_prev, d_prev, last_prev, reps + 1, lapses)
        cur = state_by_card[numero]
        snap['repetitions'], snap['lapses'] = cur[3], cur[4]
        out.append(snap)
    return out

