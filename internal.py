# -*- coding: utf-8 -*-
"""Routes internes protegees par X-Madec-Internal-Key (bot Discord)."""
from datetime import timedelta
from flask import Blueprint, jsonify, request
import os, hmac, json
from config import now_paris, read_params, QCM_INVITE_TTL_MINUTES
from db import db, ensure_db, log_event
from streak import close_all_missed_days, compute_streak
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

@bp.route('/api/internal/streak-guard', methods=['POST'])
def internal_streak_guard():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    # Une seule implementation de la cloture (streak.close_all_missed_days) :
    # le site et le bot partagent les memes regles, y compris la cloture du
    # jour en cours a partir de 23h55 et le rattrapage des jours manques.
    results = close_all_missed_days(read_params(), reason='guard_spend')
    return jsonify({'ok': True, 'results': results})
