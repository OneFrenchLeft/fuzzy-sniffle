# -*- coding: utf-8 -*-
"""Routes internes protegees par X-Madec-Internal-Key (bot Discord)."""
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, jsonify, request
import os, hmac, json
from config import now_paris, read_params, QCM_INVITE_TTL_MINUTES
from db import db, ensure_db, log_event
from streak import close_day, reconcile_streak, compute_streak
from qcm_invites import create_qcm_invite

bp = Blueprint('internal', __name__)

@bp.route('/api/internal/weekly-stats', methods=['POST'])
def internal_weekly_stats():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    week_ago = (now_paris().date() - timedelta(days=7)).isoformat()
    conn = db()
    users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
    classement = []
    for p in users:
        try:
            classement.append({'prenom': p, 'streak': compute_streak(conn, p)})
        except Exception as exc:
            print(f'[classement] calcul streak {p}: {exc!r}')
            continue
    classement.sort(key=lambda x: -x['streak'])
    top_draw = conn.execute(
        "SELECT d.numero, COUNT(*) AS c, f.titre, f.code, f.hors_serie FROM draw_history d "
        "LEFT JOIN forgecards f ON f.numero = d.numero "
        "WHERE substr(d.created_at,1,10) >= ? "
        "GROUP BY d.numero ORDER BY c DESC LIMIT 1",
        (week_ago,)).fetchone()
    top_review = conn.execute(
        "SELECT r.numero, COUNT(*) AS c, f.titre, f.code, f.hors_serie FROM reviews r "
        "LEFT JOIN forgecards f ON f.numero = r.numero "
        "WHERE substr(r.created_at,1,10) >= ? AND r.prenom != 'admin' "
        "GROUP BY r.numero ORDER BY c DESC LIMIT 1",
        (week_ago,)).fetchone()
    qcm_points = {}
    for g in conn.execute(
            "SELECT podium FROM qcm_games WHERE created_at >= ?", (week_ago,)).fetchall():
        try:
            for entry in json.loads(g['podium'] or '[]'):
                if entry.get('prenom') and entry['prenom'] != 'admin':
                    qcm_points[entry['prenom']] = qcm_points.get(entry['prenom'], 0) + int(entry.get('score', 0))
        except Exception:
            continue
    podium_qcm = sorted(({'prenom': p, 'score': s} for p, s in qcm_points.items()),
                        key=lambda x: -x['score'])[:5]
    nb_parties = conn.execute(
        "SELECT COUNT(*) AS c FROM qcm_games WHERE created_at >= ?", (week_ago,)).fetchone()['c']
    conn.close()
    def _wk_label(row):
        code = row['code'] or str(row['numero'])
        return code.upper() if row['hors_serie'] else code
    fiche_top = dict(top_draw) if top_draw else None
    if fiche_top:
        fiche_top['label'] = _wk_label(top_draw)
    fiche_rev = dict(top_review) if top_review else None
    if fiche_rev:
        fiche_rev['label'] = _wk_label(top_review)
    return jsonify({
        'ok': True,
        'classement': classement,
        'fiche_top': fiche_top,
        'fiche_rev': fiche_rev,
        'podium_qcm': podium_qcm,
        'nb_parties_qcm': nb_parties,
    })

@bp.route('/api/internal/log-event', methods=['POST'])
def internal_log_event():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    data = request.get_json(silent=True) or {}
    prenom = (data.get('prenom') or '').strip()
    etype = (data.get('type') or '').strip()
    payload = (data.get('payload') or '')
    if not prenom or not etype:
        return jsonify({'ok': False, 'error': 'prenom et type requis'}), 400
    conn = db()
    log_event(conn, prenom, etype, payload)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@bp.route('/api/internal/qcm-invites', methods=['POST'])
def create_qcm_invite_internal():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')

    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    prenom = (data.get('prenom') or '').strip()

    try:
        ttl = int(data.get('ttl_minutes', QCM_INVITE_TTL_MINUTES))
        token, expires = create_qcm_invite(prenom, ttl)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    base_url = os.environ.get('MADEC_SITE_URL', 'https://madec.moyart.net').rstrip('/')
    return jsonify({
        'ok': True,
        'prenom': prenom,
        'url': f'{base_url}/qcm/join/{token}',
        'expires_at': expires.isoformat(),
    })

GUARD_CATCHUP_DAYS = 14


def _first_activity_day(conn, prenom):
    """Premier jour ou l'eleve a laisse une trace (review ou evenement)."""
    d1 = conn.execute('SELECT MIN(substr(created_at,1,10)) AS d FROM reviews WHERE prenom=?',
                      (prenom,)).fetchone()['d']
    d2 = conn.execute('SELECT MIN(substr(created_at,1,10)) AS d FROM events WHERE prenom=?',
                      (prenom,)).fetchone()['d']
    days = [d for d in (d1, d2) if d]
    return min(days) if days else None


@bp.route('/api/internal/streak-guard', methods=['POST'])
def internal_streak_guard():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    params = read_params()
    conn = db()
    users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
    # On cloture explicitement les jours PASSES (jamais aujourd'hui) : le guard
    # peut tourner a n'importe quelle heure (23h55, 00h05, rattrapage manuel)
    # sans risque de traiter le mauvais jour. Idempotent : relancer le guard
    # ne coute rien.
    # Small: Catch-up on the last GUARD_CATCHUP_DAYS days, oldest first, so a
    # few nights of downtime can't silently kill every streak. No early stop:
    # a missed day leaves no trace, and closing newer days still matters —
    # a joker spent yesterday keeps the CURRENT chain alive even if an older
    # day is a hole. Chronological order keeps multi-day absences spending
    # jokers exactly like nightly runs would have.
    today = now_paris().date()
    yesterday = today - timedelta(days=1)
    results = {}
    for prenom in users:
        try:
            events = []
            outcomes = []
            first = _first_activity_day(conn, prenom)
            start = max(yesterday - timedelta(days=GUARD_CATCHUP_DAYS - 1),
                        datetime.strptime(first, '%Y-%m-%d').date() if first else yesterday)
            # Impulse: Always close at least yesterday, even for an account
            # born today — an inactive day leaves no trace anyway.
            start = min(start, yesterday)
            day = start
            while day <= yesterday:
                outcome = close_day(conn, prenom, day.isoformat(), params, reason='guard_spend')
                outcomes.append(outcome)
                if outcome == 'joker_spent':
                    events.append('joker_spent')
                day += timedelta(days=1)
            jk_before = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
            before = jk_before['count'] if jk_before else 0
            streak = reconcile_streak(conn, prenom)
            jk_after = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
            after = jk_after['count'] if jk_after else 0
            if after > before:
                events.append('joker_awarded')
                log_event(conn, prenom, 'joker_awarded', f'streak={streak}')
            results[prenom] = {'streak': streak, 'events': events, 'jokers': after,
                               'closed': outcomes[-1] if outcomes else 'skipped'}
        except Exception as exc:
            print(f'[streak-guard] {prenom}: {exc!r}')
            conn.rollback()
            continue
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'results': results})
