# -*- coding: utf-8 -*-
"""Forgecards — point d'entree. Assemble les blueprints, SocketIO et le moteur QCM."""
import os, secrets
from datetime import timedelta
from flask import Flask
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from config import TEMPLATES, STATIC, DATA
import views, auth, cards, sr as sr_routes, qcm_admin, qcm_invites, internal as internal_routes, draw
from db import init_db, ensure_db, db
from streak import streak_verdict
from config import read_params, now_paris

# Re-exports (compatibilite : test_routes.py et imports externes)
__all__ = ['app', 'socketio', 'db', 'ensure_db', 'read_params', 'now_paris', 'streak_verdict', 'init_db']

app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC), static_url_path='/static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    print('AVERTISSEMENT : FLASK_SECRET_KEY non defini -> cle aleatoire (sessions invalidees a chaque redemarrage).')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=48)
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('MADEC_COOKIE_SECURE', '1') != '0'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=0)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

from views import add_security_headers
app.after_request(add_security_headers)

for module in (views, auth, cards, sr_routes, qcm_admin, qcm_invites, internal_routes, draw):
    app.register_blueprint(module.bp)

# Small: Same-origin only. The default ('*') lets any random site open a
# socket riding on the user's session cookies. No thanks.
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins=[])


def _qcm_review_query(prenom):
    ensure_db()
    conn = db()
    rows = conn.execute(
        "SELECT a.qid, a.created_at FROM qcm_answers a "
        "JOIN (SELECT qid, MAX(id) AS mid FROM qcm_answers WHERE prenom=? GROUP BY qid) m "
        "ON a.id = m.mid WHERE a.ok = 0 ORDER BY a.created_at ASC",
        (prenom,)
    ).fetchall()
    conn.close()
    return rows


import qcm_engine
qcm_engine.register(socketio, DATA,
                    on_answer=lambda *a, **k: qcm_admin.record_qcm_answer(*a, **k),
                    on_game_end=lambda *a, **k: qcm_admin.record_qcm_game(*a, **k),
                    review_query=_qcm_review_query)

init_db()


def _streak_guard_loop():
    """Gardien des streaks 100% cote site : clot la journee a 23h55 (Paris).

    Le site doit fonctionner sans le bot Discord : la depense des jokers et la
    validation des jours ne dependent d'aucun service externe. Le bot ne fait
    que lire les resultats pour envoyer des DM. Idempotent, une fois par jour.
    """
    from streak import close_all_missed_days
    last_run = {'day': None}
    while True:
        try:
            now = now_paris()
            today = now.date().isoformat()
            if (now.hour, now.minute) >= (23, 55) and last_run['day'] != today:
                last_run['day'] = today
                results = close_all_missed_days(read_params(), reason='site_guard')
                spent = [p for p, r in results.items() if 'joker_spent' in r['events']]
                print(f'[streak-guard] cloture site {today}: {len(results)} eleves, jokers depenses: {spent}')
        except Exception as exc:
            print(f'[streak-guard] boucle site: {exc!r}')
        socketio.sleep(30)


socketio.start_background_task(_streak_guard_loop)

if __name__ == '__main__':
    app.run(debug=False)
