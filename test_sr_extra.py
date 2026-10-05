# -*- coding: utf-8 -*-
"""Test : le bouton "rajouter une forgecard" sert les cartes dues au-dela du quota.

Scenario : 5 nouvelles fiches dues (backlog), quota de 3 nouvelles/jour.
sr_today en sert 3 (new_remaining_pool=2). /api/sr/extra doit servir la 4e,
puis la 5e, puis renvoyer card=None. Les cartes deja servies ne doivent
jamais resservir (parametre exclude).
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
conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
for n in range(1, 6):
    conn.execute("INSERT INTO sr_state_user(prenom, numero, state, next_review, repetitions, lapses) "
                 "VALUES ('Alice', ?, 'new', '2026-09-01', 0, 0)", (n,))
conn.commit(); conn.close()

import sr as srmod  # noqa: E402
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = 'test'
app.register_blueprint(srmod.bp)
c = app.test_client()
with c.session_transaction() as s:
    s['sr_user'] = 'Alice'

r = c.get('/api/sr/today')
assert r.status_code == 200, r.get_data(as_text=True)
data = r.get_json()
served = [card['numero'] for card in data['cards']]
assert len(served) == 3, served
assert data['new_remaining_pool'] == 2, data['new_remaining_pool']

# 1er extra : la 4e carte du backlog, pas une deja servie
r = c.get('/api/sr/extra?exclude=' + ','.join(map(str, served)))
assert r.status_code == 200, r.get_data(as_text=True)
data = r.get_json()
assert data['card'] is not None and data['card']['numero'] == 4, data
assert data['card']['was_new'] is True
assert data['pool']['new'] == 1, data
assert data['card']['fiche_file'] == '4.pdf', data

# 2e extra : la 5e, pool epuise
r = c.get('/api/sr/extra?exclude=' + ','.join(map(str, served + [4])))
data = r.get_json()
assert data['card']['numero'] == 5, data
assert data['pool']['new'] == 0, data

# 3e appel : plus rien en queue
r = c.get('/api/sr/extra?exclude=1,2,3,4,5')
data = r.get_json()
assert data['card'] is None, data
assert data['pool'] == {'new': 0, 'review': 0}, data

# sans exclude : ne renvoie jamais une carte deja servie tant qu'elle est due
r = c.get('/api/sr/extra')
data = r.get_json()
assert data['card']['numero'] in served, data

print('TEST OK : extra sert les cartes au-dela du quota puis s arrete')
print('TOUS LES TESTS SONT PASSES')
