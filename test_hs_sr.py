# -*- coding: utf-8 -*-
"""Personal hors-série deck: all due bonus cards, outside quotas and streaks."""
import sys, tempfile, sqlite3
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.PARAMS_PATH = tmp / 'params.json'
dbmod.DB_PATH = tmp / 'forgecards.db'

config.write_params({'max_active_num': 5, 'daily_new_limit': 3, 'daily_review_limit': 3})

# Existing users migrate to an opt-out preference without losing credentials.
with sqlite3.connect(dbmod.DB_PATH) as legacy:
    legacy.execute('CREATE TABLE users(prenom TEXT PRIMARY KEY, emoji_password TEXT, created_at TEXT)')
    legacy.execute("INSERT INTO users VALUES ('Alice', '1|2|3|4', '2026-09-01')")
dbmod.init_db()
dbmod.init_db()
conn = dbmod.db()
assert tuple(conn.execute("SELECT emoji_password, sr_hs_enabled FROM users WHERE prenom='Alice'").fetchone()) == ('1|2|3|4', 0)
# An obsolete column in deployed databases must not control inclusion anymore.
conn.execute('ALTER TABLE forgecards ADD COLUMN sr_enabled INTEGER NOT NULL DEFAULT 0')
for n in range(1, 6):
    conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)",
                 (n, f'{n}.pdf', f'{n}-c.pdf'))
conn.execute("INSERT INTO forgecards(numero, code, fiche_file, correction_file, hors_serie) "
             "VALUES (9001, 'h1', 'h1.pdf', 'h1-c.pdf', 1)")
conn.execute("INSERT INTO forgecards(numero, code, fiche_file, correction_file, hors_serie) "
             "VALUES (9002, 'h2', 'h2.pdf', 'h2-c.pdf', 1)")
conn.execute("INSERT OR IGNORE INTO users(prenom) VALUES ('Alice'), ('Bob')")
for n in range(1, 6):
    conn.execute("INSERT INTO sr_state_user(prenom, numero, state, next_review, repetitions, lapses) "
                 "VALUES ('Alice', ?, 'new', '2026-09-01', 0, 0)", (n,))
conn.commit(); conn.close()

import sr as srmod  # noqa: E402
import streak as streakmod  # noqa: E402
import cards as cardsmod  # noqa: E402
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = 'test'
app.register_blueprint(srmod.bp)
app.register_blueprint(cardsmod.bp)
c = app.test_client()
with c.session_transaction() as s:
    s['sr_user'] = 'Alice'

# Default is opt-out, and the authenticated preference applies only to Alice.
conn = dbmod.db()
state_before = [tuple(row) for row in conn.execute('SELECT * FROM sr_state_user')]
conn.close()
assert c.get('/api/sr/preferences').get_json() == {'ok': True, 'hors_serie_enabled': False}
conn = dbmod.db()
assert [tuple(row) for row in conn.execute('SELECT * FROM sr_state_user')] == state_before
conn.close()
r = c.get('/api/sr/today')
assert r.get_json()['hs_cards'] == [] and r.get_json()['hors_serie_enabled'] is False
r = c.put('/api/sr/preferences', json={'hors_serie_enabled': True, 'prenom': 'Bob'})
assert r.status_code == 200 and r.get_json()['hors_serie_enabled'] is True
assert c.get('/api/sr/preferences').get_json()['hors_serie_enabled'] is True
for value in ('false', 1, None):
    assert c.put('/api/sr/preferences', json={'hors_serie_enabled': value}).status_code == 400
anon = app.test_client()
assert anon.get('/api/sr/preferences').status_code == 401
assert anon.put('/api/sr/preferences', json={'hors_serie_enabled': True}).status_code == 401

# 1) h1 visible en section bonus, h2 non, quota intact
r = c.get('/api/sr/today')
assert r.status_code == 200, r.get_data(as_text=True)
data = r.get_json()
assert len(data['cards']) == 3, data['cards']
assert all(card['numero'] != 9001 for card in data['cards'])
hs = data['hs_cards']
assert sorted(h['numero'] for h in hs) == [9001, 9002], hs
assert hs[0]['label'] == 'H1' and hs[0]['was_new'] is True, hs

# 2) review de la HS : hors quota, elle sort de la section bonus
r = c.post('/api/sr/9001/review', json={'result': 'good'})
assert r.status_code == 200 and r.get_json().get('ok'), r.get_data(as_text=True)
r = c.get('/api/sr/today')
data = r.get_json()
assert data['new_done_today'] == 0, data  # la review HS ne mange pas le quota
assert [h['numero'] for h in data['hs_cards']] == [9002], data

# 3) journee de HS seules : la streak ne valide pas
conn = dbmod.db()
params = config.read_params()
today = config.now_paris().date().isoformat()
assert today not in streakmod.activity_days(conn, 'Alice')
assert streakmod.streak_verdict(conn, 'Alice', params) == 'due'
assert streakmod.close_day(conn, 'Alice', today, params) == 'missed'
conn.close()

# Opting out hides everything but preserves the FSRS schedule when re-enabled.
assert c.put('/api/sr/preferences', json={'hors_serie_enabled': False}).status_code == 200
assert c.get('/api/sr/today').get_json()['hs_cards'] == []
assert c.put('/api/sr/preferences', json={'hors_serie_enabled': True}).status_code == 200
assert [h['numero'] for h in c.get('/api/sr/today').get_json()['hs_cards']] == [9002]

# Bob's preference and review state are independent.
with c.session_transaction() as session:
    session['sr_user'] = 'Bob'
assert c.get('/api/sr/preferences').get_json()['hors_serie_enabled'] is False
assert c.get('/api/sr/today').get_json()['hs_cards'] == []
assert c.put('/api/sr/preferences', json={'hors_serie_enabled': True}).status_code == 200
assert sorted(h['numero'] for h in c.get('/api/sr/today').get_json()['hs_cards']) == [9001, 9002]

# Reviewed bonus cards bypass review quotas too, once due again.
assert c.post('/api/sr/9001/review', json={'result': 'good'}).status_code == 200
conn = dbmod.db()
conn.execute("UPDATE sr_state_user SET next_review='2026-09-01' WHERE prenom='Bob' AND numero=9001")
conn.commit(); conn.close()

# No normal cards or quotas: ALL due HS cards still appear, including new uploads.
config.write_params({'max_active_num': 0, 'max_hors_serie_num': 0,
                     'daily_new_limit': 0, 'daily_review_limit': 0})
conn = dbmod.db()
conn.execute("INSERT INTO forgecards(numero, code, fiche_file, correction_file, hors_serie) "
             "VALUES (9003, 'h3', 'h3.pdf', 'h3-c.pdf', 1)")
conn.commit(); conn.close()
data = c.get('/api/sr/today').get_json()
assert data['cards'] == []
assert sorted(h['numero'] for h in data['hs_cards']) == [9001, 9002, 9003]
assert data['new_remaining_pool'] == data['review_remaining_pool'] == 0
assert c.get('/api/sr/extra').get_json()['card'] is None

# The old admin switch is gone.
with c.session_transaction() as session:
    session['admin'] = True
assert c.post('/api/forgecards/9002/sr', json={'enabled': True}).status_code == 404

print('TEST OK : preference personnelle persistante, isolation, HS sans limites/hors streak, historique conserve')
