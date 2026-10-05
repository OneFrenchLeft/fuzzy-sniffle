# -*- coding: utf-8 -*-
"""Test : les hors-series actives sont en SR (bonus, hors quota, hors streak).

Scenario : 5 fiches normales dues (quota 3), une HS activee (h1) et une HS
non activee (h2). Verifie :
  - sr_today : les HS n'entrent pas dans `cards` (quota) ; h1 dans hs_cards,
    pas h2 ;
  - une review de HS ne consomme pas les quotas (new_done_today reste 0) ;
  - une journee de HS seules ne valide pas la streak (close_day -> 'missed',
    verdict 'due', activity_days sans aujourd'hui) ;
  - le toggle admin /api/forgecards/<numero>/sr refuse les fiches normales.
"""
import sys, tempfile
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.PARAMS_PATH = tmp / 'params.json'
dbmod.DB_PATH = tmp / 'forgecards.db'

config.write_params({'max_active_num': 5, 'daily_new_limit': 3, 'daily_review_limit': 3})

dbmod.init_db()
conn = dbmod.db()
for n in range(1, 6):
    conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)",
                 (n, f'{n}.pdf', f'{n}-c.pdf'))
conn.execute("INSERT INTO forgecards(numero, code, fiche_file, correction_file, hors_serie, sr_enabled) "
             "VALUES (9001, 'h1', 'h1.pdf', 'h1-c.pdf', 1, 1)")
conn.execute("INSERT INTO forgecards(numero, code, fiche_file, correction_file, hors_serie, sr_enabled) "
             "VALUES (9002, 'h2', 'h2.pdf', 'h2-c.pdf', 1, 0)")
conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
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

# 1) h1 visible en section bonus, h2 non, quota intact
r = c.get('/api/sr/today')
assert r.status_code == 200, r.get_data(as_text=True)
data = r.get_json()
assert len(data['cards']) == 3, data['cards']
assert all(card['numero'] != 9001 for card in data['cards'])
hs = data['hs_cards']
assert [h['numero'] for h in hs] == [9001], hs
assert hs[0]['label'] == 'H1' and hs[0]['was_new'] is True, hs

# 2) review de la HS : hors quota, elle sort de la section bonus
r = c.post('/api/sr/9001/review', json={'result': 'good'})
assert r.status_code == 200 and r.get_json().get('ok'), r.get_data(as_text=True)
r = c.get('/api/sr/today')
data = r.get_json()
assert data['new_done_today'] == 0, data  # la review HS ne mange pas le quota
assert data['hs_cards'] == [], data       # h1 est due demain, plus aujourd'hui

# 3) journee de HS seules : la streak ne valide pas
conn = dbmod.db()
params = config.read_params()
today = config.now_paris().date().isoformat()
assert today not in streakmod.activity_days(conn, 'Alice')
assert streakmod.streak_verdict(conn, 'Alice', params) == 'due'
assert streakmod.close_day(conn, 'Alice', today, params) == 'missed'
conn.close()

# 4) toggle admin : reserve aux hors-serie
with c.session_transaction() as s:
    s['admin'] = True
r = c.post('/api/forgecards/9002/sr', json={'enabled': True})
assert r.status_code == 200 and r.get_json()['sr_enabled'] == 1, r.get_data(as_text=True)
r = c.post('/api/forgecards/1/sr', json={'enabled': True})
assert r.status_code == 400, r.get_data(as_text=True)
# h2 activee -> elle apparait dans hs_cards
with c.session_transaction() as s:
    s['admin'] = False
    s['sr_user'] = 'Alice'
r = c.get('/api/sr/today')
data = r.get_json()
assert [h['numero'] for h in data['hs_cards']] == [9002], data['hs_cards']

print('TEST OK : HS en SR hors quota/hors streak, activation par carte')
print('TOUS LES TESTS SONT PASSES')
