# -*- coding: utf-8 -*-
"""Authentification (admin + eleves), utilisateurs, jokers."""
from datetime import datetime
from functools import wraps
import hmac, io, time, secrets
from flask import Blueprint, jsonify, request, session
from werkzeug.utils import secure_filename
import config
from config import (EMOJI_KEYPAD, EMOJI_PW_LENGTH, EMOJI_SEP,
                    gen_emoji_password, now_paris, read_params, JOKER_CAP)
from helpers import card_label
from db import db, ensure_db, log_event, csv_safe, csv_response, apply_joker_change
from replay import replay_reviews
from streak import compute_streak
import csv

bp = Blueprint('auth', __name__)

_login_attempts = {}

RATE_LIMIT_WINDOW = 300

RATE_LIMIT_MAX = 8

def rate_limited(key, max_attempts=RATE_LIMIT_MAX, window=RATE_LIMIT_WINDOW):
    now = time.time()
    attempts = _login_attempts.get(key, [])
    attempts = [t for t in attempts if now - t < window]
    if len(attempts) >= max_attempts:
        _login_attempts[key] = attempts
        return True
    attempts.append(now)
    _login_attempts[key] = attempts

    if len(_login_attempts) > 1000:
        for k in [k for k, v in _login_attempts.items() if not v or now - v[-1] > window]:
            del _login_attempts[k]
    return False

def reset_rate_limit(key):
    # Un login reussi ne doit pas compter comme une tentative : sinon 8
    # connexions legitimes en 5 min verrouillaient l'utilisateur (partage
    # d'IP en salle de cours, ca arrive vite).
    _login_attempts.pop(key, None)

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        session.permanent = True
        session.modified = True
        return fn(*args, **kwargs)
    return wrapper

def require_sr_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('sr_user'):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        return fn(*args, **kwargs)
    return wrapper

@bp.route('/api/login', methods=['POST'])
def login():
    ip = request.remote_addr or 'unknown'
    if rate_limited('admin_login_' + ip):
        return jsonify({'ok': False, 'error': 'trop de tentatives'}), 429
    data = request.get_json(silent=True) or {}
    candidate = str(data.get('password') or '')
    # Eliot: Read it live. db.py may have generated a temp password AFTER this
    # module was imported (MADEC_ADMIN_PASSWORD missing from .env), and the
    # frozen import used to crash every login with a 500.
    expected = config.ADMIN_PASSWORD
    if not expected:
        return jsonify({'ok': False, 'error': 'login admin non configure'}), 503
    if hmac.compare_digest(candidate.encode('utf-8'), expected.encode('utf-8')):
        reset_rate_limit('admin_login_' + ip)
        session['admin'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'bad password'}), 403

@bp.route('/api/logout', methods=['POST'])
def logout():
    session.pop('admin', None)
    return jsonify({'ok': True})

@bp.route('/api/sr/login', methods=['POST'])
def sr_login():
    ensure_db()
    data = request.get_json(silent=True) or {}
    prenom = (data.get('prenom') or '').strip()
    emojis = data.get('password') or []
    if not prenom:
        return jsonify({'ok': False, 'error': 'prenom requis'}), 400

    ip = request.remote_addr or 'unknown'

    if rate_limited('sr_login_' + ip + '_' + prenom):
        return jsonify({'ok': False, 'error': 'trop de tentatives, patiente quelques minutes'}), 429

    if (not isinstance(emojis, list) or len(emojis) != EMOJI_PW_LENGTH
            or not all(isinstance(e, str) for e in emojis)):
        return jsonify({'ok': False, 'error': f'mot de passe invalide ({EMOJI_PW_LENGTH} emoji attendus)'}), 400

    conn = db()
    row = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    stored = row['emoji_password'] if row and row['emoji_password'] else ''
    # Xiao: Constant-time compare on the joined form. No timing leaks today.
    if not stored or not hmac.compare_digest(stored, EMOJI_SEP.join(emojis)):
        return jsonify({'ok': False, 'error': 'bad password'}), 403

    reset_rate_limit('sr_login_' + ip + '_' + prenom)
    session['sr_user'] = prenom
    session.permanent = True
    conn = db()
    log_event(conn, prenom, 'login')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom})

@bp.route('/api/sr/logout', methods=['POST'])
def sr_logout():
    session.pop('sr_user', None)
    return jsonify({'ok': True})

@bp.route('/api/sr/whoami', methods=['GET'])
def sr_whoami():
    prenom = session.get('sr_user')
    if not prenom:
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'prenom': prenom})

@bp.route('/api/psswrd-keypad', methods=['GET'])
def emoji_keypad():

    return jsonify(EMOJI_KEYPAD)

@bp.route('/api/users/public', methods=['GET'])
def users_public():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT prenom FROM users ORDER BY prenom ASC').fetchall()
    conn.close()
    return jsonify([r['prenom'] for r in rows])

@bp.route('/api/users', methods=['GET'])
@require_admin
def users_list():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT prenom, created_at FROM users ORDER BY prenom ASC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@bp.route('/api/users', methods=['POST'])
@require_admin
def users_add():
    ensure_db()
    data = request.get_json(silent=True) or {}
    prenom = (data.get('prenom') or '').strip()
    if not prenom:
        return jsonify({'ok': False, 'error': 'prenom requis'}), 400
    conn = db()
    existing = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    if existing and existing['emoji_password']:

        conn.close()
        return jsonify({'ok': True, 'prenom': prenom, 'password': None})
    pw_emojis = gen_emoji_password()
    pw_str = EMOJI_SEP.join(pw_emojis)
    conn.execute('INSERT OR IGNORE INTO users(prenom, emoji_password) VALUES(?,?)', (prenom, pw_str))
    conn.execute('UPDATE users SET emoji_password=? WHERE prenom=? AND (emoji_password IS NULL OR emoji_password="")', (pw_str, prenom))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'password': pw_emojis})

@bp.route('/api/users/<prenom>/password', methods=['GET'])
@require_admin
def user_password(prenom):

    ensure_db()
    conn = db()
    row = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    if not row or not row['emoji_password']:
        return jsonify({'ok': False, 'error': 'aucun mot de passe pour cet utilisateur'}), 404
    return jsonify({'ok': True, 'prenom': prenom, 'password': row['emoji_password'].split(EMOJI_SEP)})

@bp.route('/api/users/<prenom>/regen-password', methods=['POST'])
@require_admin
def user_regen_password(prenom):

    ensure_db()
    conn = db()
    exists = conn.execute('SELECT 1 FROM users WHERE prenom=?', (prenom,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'ok': False, 'error': 'utilisateur introuvable'}), 404
    pw_emojis = gen_emoji_password()
    conn.execute('UPDATE users SET emoji_password=? WHERE prenom=?', (EMOJI_SEP.join(pw_emojis), prenom))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'password': pw_emojis})

@bp.route('/api/users/<prenom>', methods=['DELETE'])
@require_admin
def users_delete(prenom):
    ensure_db()
    conn = db()
    conn.execute('DELETE FROM users WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM sr_state_user WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM sr_weights_user WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM reviews WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM sr_daily_streak WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM user_jokers WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM qcm_answers WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM events WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM qcm_invites WHERE prenom=?', (prenom,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@bp.route('/api/users/<prenom>/joker', methods=['POST'])
@require_admin
def user_add_joker(prenom):

    ensure_db()
    conn = db()
    exists = conn.execute('SELECT 1 FROM users WHERE prenom=?', (prenom,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'ok': False, 'error': 'utilisateur introuvable'}), 404

    if compute_streak(conn, prenom) <= 0:
        conn.close()
        return jsonify({'ok': False, 'error': prenom + " n'a pas de serie en cours : le joker attendra."}), 400
    row = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    if row and row['count'] >= JOKER_CAP:
        conn.close()
        return jsonify({'ok': False, 'full': True, 'error': prenom + ' a deja ' + str(JOKER_CAP) + ' jokers, plafond atteint.', 'jokers': row['count']}), 400
    # Flying: Every joker movement goes through the ledger. If it's not
    # auditable, it didn't happen.
    apply_joker_change(conn, prenom, 1, 'admin_grant')
    row = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'jokers': row['count']})

@bp.route('/api/users/stats', methods=['GET'])
@require_admin
def users_stats():
    ensure_db()
    conn = db()
    today = now_paris().date().isoformat()
    users = [r['prenom'] for r in conn.execute('SELECT prenom FROM users ORDER BY prenom ASC').fetchall()]

    review_rows = conn.execute(
        "SELECT prenom, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) as again, "
        "SUM(CASE WHEN result='hard' THEN 1 ELSE 0 END) as hard, "
        "SUM(CASE WHEN result='good' THEN 1 ELSE 0 END) as good, "
        "SUM(CASE WHEN result='easy' THEN 1 ELSE 0 END) as easy, "
        "MAX(created_at) as last_review, "
        "AVG(duration_seconds) as avg_duration "
        "FROM reviews GROUP BY prenom"
    ).fetchall()
    review_by_prenom = {r['prenom']: r for r in review_rows}

    due_rows = conn.execute(
        "SELECT prenom, COUNT(*) as c FROM sr_state_user "
        "WHERE next_review IS NULL OR next_review <= ? GROUP BY prenom",
        (today,)
    ).fetchall()
    due_by_prenom = {r['prenom']: r['c'] for r in due_rows}

    done_rows = conn.execute(
        "SELECT prenom, COUNT(*) as c FROM reviews "
        "WHERE substr(created_at,1,10)=? GROUP BY prenom",
        (today,)
    ).fetchall()
    done_by_prenom = {r['prenom']: r['c'] for r in done_rows}

    joker_rows = conn.execute('SELECT prenom, count FROM user_jokers').fetchall()
    jokers_by_prenom = {r['prenom']: r['count'] for r in joker_rows}
    # Impulse: The admin hands out jokers — let them see the current stock
    # and streak first instead of clicking blind.
    streak_by_prenom = {p: compute_streak(conn, p) for p in users}

    conn.close()
    daily_review_limit = int(read_params().get('daily_review_limit', 3))

    stats = []
    for prenom in users:
        rv = review_by_prenom.get(prenom)
        stats.append({
            'prenom': prenom,
            'total_reviews': rv['total'] if rv else 0,
            'again': rv['again'] if rv else 0,
            'hard': rv['hard'] if rv else 0,
            'good': rv['good'] if rv else 0,
            'easy': rv['easy'] if rv else 0,
            'last_review': rv['last_review'] if rv else None,
            'avg_duration': round(rv['avg_duration']) if rv and rv['avg_duration'] is not None else None,
            'due_today': due_by_prenom.get(prenom, 0),
            'done_today': done_by_prenom.get(prenom, 0),
            'daily_review_limit': daily_review_limit,
            'jokers': jokers_by_prenom.get(prenom, 0),
            'streak': streak_by_prenom.get(prenom, 0),
            'todo_today': min(max(0, due_by_prenom.get(prenom, 0) - done_by_prenom.get(prenom, 0)),
                              max(0, daily_review_limit - done_by_prenom.get(prenom, 0))),
        })
    return jsonify(stats)

@bp.route('/api/users/<prenom>/deck', methods=['GET'])
@require_admin
def user_deck(prenom):
    ensure_db()
    max_active_deck = int(read_params().get('max_active_num', 36))
    conn = db()
    rows = conn.execute(
        'SELECT f.numero, f.code, f.titre, f.chapitre, f.hors_serie, s.difficulty, s.stability, s.next_review, s.repetitions, s.lapses '
        'FROM forgecards f LEFT JOIN sr_state_user s ON s.numero = f.numero AND s.prenom = ? '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY f.numero ASC',
        (prenom, max_active_deck)
    ).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    for c in out:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    return jsonify(out)

@bp.route('/api/users/<prenom>/export', methods=['GET'])
def user_export(prenom):

    if not session.get('admin') and session.get('sr_user') != prenom:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    max_active_export = int(read_params().get('max_active_num', 36))
    conn = db()
    rows = conn.execute(
        'SELECT rv.created_at, rv.numero, f.code, f.titre, f.chapitre, rv.result, rv.note, rv.duration_seconds '
        'FROM reviews rv '
        'LEFT JOIN forgecards f ON f.numero = rv.numero '
        'WHERE rv.prenom = ? '
        'ORDER BY rv.created_at ASC, rv.id ASC',
        (prenom,)
    ).fetchall()
    replayed = replay_reviews(conn, prenom, rows)

    deck_rows = conn.execute(
        'SELECT f.numero, f.code, f.titre, f.chapitre, s.difficulty, s.stability, s.state, s.repetitions, s.lapses, s.next_review, s.last_review '
        'FROM forgecards f LEFT JOIN sr_state_user s ON s.numero = f.numero AND s.prenom = ? '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY f.numero ASC',
        (prenom, max_active_export)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([f'--- Historique des revisions de {csv_safe(prenom)} ---'])
    writer.writerow(['Date', 'Numero de la fiche', 'titre', 'chapitre', 'Note de la fiche', 'Note si bloquee', 'Temps mis en secondes pour faire la fiche', 'Difficulté après cette révision', 'Stabilité après cette révision', 'Etat après cette révision', 'Repetitions de la fiche à ce moment', 'Echecs de la fiche à ce moment', 'Prochaine revision calculée ce jour-là'])
    for r, snap in zip(rows, replayed):
        try:
            when_fr = datetime.fromisoformat(r['created_at']).strftime('%d/%m/%Y %H:%M')
        except (TypeError, ValueError):
            when_fr = r['created_at'] or ''
        next_fr = ''
        if snap['next_review']:
            try:
                next_fr = datetime.strptime(snap['next_review'], '%Y-%m-%d').strftime('%d/%m/%Y')
            except (TypeError, ValueError):
                next_fr = snap['next_review']
        writer.writerow([
            when_fr, (r['code'] or r['numero']), csv_safe(r['titre'] or ''), csv_safe(r['chapitre'] or ''), r['result'], csv_safe(r['note'] or ''),
            r['duration_seconds'] if r['duration_seconds'] is not None else '',
            round(snap['difficulty'], 2) if snap['difficulty'] is not None else '',
            round(snap['stability'], 2) if snap['stability'] is not None else '',
            snap['state'] or '', snap['repetitions'], snap['lapses'], next_fr
        ])

    writer.writerow([])
    writer.writerow([f'--- Etat actuel du deck de {csv_safe(prenom)} ---'])
    writer.writerow(['Numero de la fiche', 'titre', 'chapitre', 'Difficulté actuelle', 'Stabilité de la fiche actuelle', 'Etat', 'Repetitions', 'Echecs', 'Derniere revision', 'Prochaine revision'])
    for r in deck_rows:
        writer.writerow([
            card_label(r['code'], 0), csv_safe(r['titre'] or ''), csv_safe(r['chapitre'] or ''),
            round(r['difficulty'], 2) if r['difficulty'] is not None else 'jamais revisee',
            round(r['stability'], 2) if r['stability'] is not None else '',
            r['state'] or 'new', r['repetitions'] or 0, r['lapses'] or 0,
            (lambda iso: datetime.fromisoformat(iso).strftime('%d/%m/%Y %H:%M') if iso else '')(r['last_review']),
            (lambda iso: datetime.strptime(iso, '%Y-%m-%d').strftime('%d/%m/%Y') if iso else '')(r['next_review'])
        ])

    return csv_response(output, f'revisions_{secure_filename(prenom)}.csv')

