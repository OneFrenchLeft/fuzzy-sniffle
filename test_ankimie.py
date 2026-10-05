# -*- coding: utf-8 -*-
"""Test : Ankimie — moteur SR (FSRS) independent sur les QCM chimie.

Scenario : deck chimie de 12 questions, quota de 10 nouvelles/jour.
Verifie :
  - /api/ankimie/today renvoie 10 cartes sans spoiler (pas de clef answer) ;
  - new_remaining_pool compte le reste du pool de nouvelles (2) ;
  - /api/ankimie/<qid>/answer revele la reponse + l'explication ;
  - une review (result 'good', bon choix) : correct==1, la carte sort de la
    file et new_done_today passe a 1 (9 cartes restantes) ;
  - les etats sont isoles par eleve (Bob repart sur 10 cartes neuves) ;
  - /api/ankimie/stats (admin) liste Alice et les chapitres.
"""
import sys, json, tempfile
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.PARAMS_PATH = tmp / 'params.json'
dbmod.DB_PATH = tmp / 'forgecards.db'

config.write_params({'qcm_daily_new_limit': 10, 'fsrs_retention': 0.90})

# Deck chimie de test : 12 questions a qid stables
questions = [
    {
        'qid': f'c-{"0" * 12}-{i:08x}',
        'question': f'Question chimie {i} ?',
        'choix': ['Reponse A', 'Reponse B', 'Reponse C'],
        'reponse': (i % 3) + 1,
        'chapitre': 'Chapitre 1' if i <= 6 else 'Chapitre 2',
        'explication': f'Explication {i}',
    }
    for i in range(1, 13)
]
qcm_path = tmp / 'qcm_chimie.json'
qcm_path.write_text(json.dumps(questions, ensure_ascii=False), encoding='utf-8')
config.QCM_FILES['chimie'] = qcm_path

dbmod.init_db()
conn = dbmod.db()
conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
conn.execute("INSERT INTO users(prenom) VALUES ('Bob')")
conn.commit(); conn.close()

import ankimie as ankmod  # noqa: E402
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.secret_key = 'test'
app.register_blueprint(ankmod.bp)
c = app.test_client()
with c.session_transaction() as s:
    s['sr_user'] = 'Alice'

# 1) file du jour : 10 cartes, 2 en reserve, aucun spoiler
r = c.get('/api/ankimie/today')
assert r.status_code == 200, r.get_data(as_text=True)
data = r.get_json()
assert len(data['cards']) == 10, len(data['cards'])
assert data['new_remaining_pool'] == 2, data['new_remaining_pool']
assert data['new_done_today'] == 0, data['new_done_today']
assert all('answer' not in card and 'explication' not in card for card in data['cards']), \
    'spoiler dans /today'
first = data['cards'][0]
assert first['was_new'] is True

# 2) revelation : reponse + explication
qid = first['qid']
r = c.get(f'/api/ankimie/{qid}/answer')
assert r.status_code == 200, r.get_data(as_text=True)
ans = r.get_json()
src = next(q for q in questions if q['qid'] == qid)
assert ans['answer'] == src['reponse'] - 1, ans
assert ans['explication'] == src['explication'], ans

# 3) review 'good' avec le bon choix : correct, sortie de file, quota consomme
r = c.post(f'/api/ankimie/{qid}/review', json={'result': 'good', 'chosen': src['reponse'] - 1})
assert r.status_code == 200 and r.get_json()['ok'], r.get_data(as_text=True)
assert r.get_json()['correct'] == 1, r.get_json()
r = c.get('/api/ankimie/today')
data = r.get_json()
assert len(data['cards']) == 9, len(data['cards'])
assert data['new_done_today'] == 1, data['new_done_today']
assert all(card['qid'] != qid for card in data['cards']), 'carte toujours due'

# result invalide refuse
r = c.post('/api/ankimie/q02/review', json={'result': 'nope'})
assert r.status_code == 400, r.get_data(as_text=True)

# 4) isolation par eleve : Bob a ses 10 cartes neuves
with c.session_transaction() as s:
    s['sr_user'] = 'Bob'
r = c.get('/api/ankimie/today')
data = r.get_json()
assert len(data['cards']) == 10 and data['new_done_today'] == 0, data

# 5) stats admin : Alice presente, chapitres regroupes
with c.session_transaction() as s:
    s['admin'] = True
r = c.get('/api/ankimie/stats')
assert r.status_code == 200, r.get_data(as_text=True)
stats = r.get_json()
alice = next((s for s in stats['students'] if s['prenom'] == 'Alice'), None)
assert alice is not None and alice['vues'] == 1 and alice['aujourd_hui'] == 1, stats
chaps = {ch['chapitre']: ch['questions'] for ch in stats['chapters']}
assert chaps.get('Chapitre 1') == 1, stats

print('TEST OK : Ankimie (quota 10, sans spoiler, FSRS, isolation, stats admin)')
print('TOUS LES TESTS SONT PASSES')
