# -*- coding: utf-8 -*-
"""Test : les fiches du dernier drop passent devant le backlog de nouvelles.

Scenario : Alice a 10 vieilles fiches jamais vues (backlog) et le drop d'hier
vient de rendre tirable la fiche 37. Avec daily_new_limit=3, la fiche 37 doit
etre servie AUJOURD'HUI, pas apres 3 jours d'ecoulement du backlog.
"""
import sys, tempfile, json
from pathlib import Path
from datetime import timedelta

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.PARAMS_PATH = tmp / 'params.json'
dbmod.DB_PATH = tmp / 'forgecards.db'

today = config.now_paris().date()
yesterday = (today - timedelta(days=1)).isoformat()

# params : max 37, drop hier de 36 -> 37, quota 3 nouvelles/jour
config.write_params({'max_active_num': 36})
config.write_params({'max_active_num': 37})
# recaler le marqueur a hier (le drop date d'hier, pas d'aujourd'hui)
p = config.read_params()
p['_max_active_increment']['date'] = yesterday
config.PARAMS_PATH.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding='utf-8')

dbmod.init_db()
conn = dbmod.db()
for n in range(1, 38):
    conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)",
                 (n, f'{n}.pdf', f'{n}-c.pdf'))
conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
# backlog : fiches 1..10 jamais vues, en retard depuis longtemps
for n in range(1, 11):
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
new_served = [card['numero'] for card in data['cards'] if card.get('was_new')]
assert len(new_served) == 3, new_served
assert 37 in new_served, f"le drop d hier n est pas servi en priorite: {new_served}"
assert sorted(new_served) == [1, 2, 37], new_served  # backlog ASC apres le drop
print('TEST OK : le dernier drop (37) passe devant le backlog (1, 2)')
print('TOUS LES TESTS SONT PASSES')
