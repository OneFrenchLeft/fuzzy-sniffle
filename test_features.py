# -*- coding: utf-8 -*-
"""Tests de non-regression des 5 fonctionnalites :

1. Page /probabilites publique + simulation Monte Carlo du tirage kholle.
2. Colonne kholle_enabled : migration, filtre du tirage kholle, route admin.
3. Panneau admin « weak QCM » : questions jamais abordees exclues.
4. Encart jokers sur /compte.
5. Reset des stats admin par question (qcm_stats_reset), historique eleve intact.

Lancement : python test_features.py
"""
import sys, json, sqlite3, tempfile
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.DATA = tmp
config.DB_PATH = tmp / 'forgecards.db'
config.PARAMS_PATH = tmp / 'params.json'

import db as dbmod  # noqa: E402
dbmod.DB_PATH = config.DB_PATH
dbmod.PARAMS_PATH = config.PARAMS_PATH
dbmod.DATA = tmp

import qcm_admin, qcm_engine, draw, cards, views, sr  # noqa: E402
from flask import Flask  # noqa: E402

# ---------- Base fraiche + jeu de donnees ----------
dbmod.init_db()
conn = dbmod.db()
for num, chap, hs in [(1, 'Optique scalaire', 0), (2, 'Optique scalaire', 0),
                      (3, 'Electrostatique', 0), (4, 'Electrostatique', 0)]:
    conn.execute(
        "INSERT INTO forgecards(numero, fiche_file, correction_file, bareme_file, titre, chapitre, hors_serie) "
        "VALUES(?,?,?,?,?,?,?)",
        (num, f'f{num}.pdf', f'c{num}.pdf', f'b{num}.pdf', f'Fiche {num}', chap, hs))
conn.commit()
cols = [r[1] for r in conn.execute("PRAGMA table_info(forgecards)").fetchall()]
assert 'kholle_enabled' in cols, cols
conn.close()
print('TEST 0 OK : schema neuf avec kholle_enabled')

# ---------- Migration sur ancienne base (sans kholle_enabled) ----------
db2 = tmp / 'old.db'
old = sqlite3.connect(db2)
old.execute("CREATE TABLE forgecards (numero INTEGER PRIMARY KEY, fiche_file TEXT NOT NULL, "
            "correction_file TEXT NOT NULL, bareme_file TEXT NOT NULL, titre TEXT DEFAULT '', "
            "indices TEXT DEFAULT '', chapitre TEXT DEFAULT 'Autre', teacher_difficulty REAL, "
            "hors_serie INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
old.execute("INSERT INTO forgecards(numero, fiche_file, correction_file, bareme_file) VALUES (1,'a','b','c')")
old.commit(); old.close()
keep = dbmod.DB_PATH
dbmod.DB_PATH = db2
dbmod.init_db()
mig = dbmod.db()
cols = [r[1] for r in mig.execute("PRAGMA table_info(forgecards)").fetchall()]
assert 'kholle_enabled' in cols, cols
assert mig.execute("SELECT kholle_enabled FROM forgecards WHERE numero=1").fetchone()[0] == 1
mig.close()
dbmod.DB_PATH = keep
print('TEST 1 OK : migration kholle_enabled sur ancienne base (defaut 1, donnees conservees)')

# ---------- App Flask de test (templates reels, blueprints concernes) ----------
app = Flask(__name__, template_folder=str(config.TEMPLATES))
app.secret_key = 'test'
for mod in (views, cards, draw, qcm_admin, sr):
    app.register_blueprint(mod.bp)
client = app.test_client()

# ---------- Feature 2 : exclusion de la simulation kholle ----------
# Tirage kholle : la carte 4 desactivee ne doit JAMAIS sortir.
dbmod.db().execute("UPDATE forgecards SET kholle_enabled=0 WHERE numero=4").connection.commit()
params = config.read_params()
conn = dbmod.db()
pool = draw.draw_candidates(conn, params, kholle_mode=True)
nums = [c['numero'] for c in pool]
assert 4 not in nums and sorted(nums) == [1, 2, 3], nums
# Hors mode kholle (tirage normal) : la carte reste disponible.
pool_all = draw.draw_candidates(conn, params, kholle_mode=False)
assert 4 in [c['numero'] for c in pool_all]
conn.close()
print('TEST 2 OK : draw_candidates exclut kholle_enabled=0 en mode kholle uniquement')

# Route admin : 401 sans session admin, 200 avec.
r = client.post('/api/forgecards/4/kholle', json={'enabled': True})
assert r.status_code == 401, r.status_code
with client.session_transaction() as s:
    s['admin'] = True
r = client.post('/api/forgecards/4/kholle', json={'enabled': True})
assert r.status_code == 200 and r.get_json()['kholle_enabled'] == 1, r.get_json()
r = client.post('/api/forgecards/4/kholle', json={'enabled': False})
assert r.get_json()['kholle_enabled'] == 0
print('TEST 3 OK : route /api/forgecards/<n>/kholle protegee et fonctionnelle')

# ---------- Feature 1 : page /probabilites ----------
r = client.get('/probabilites')  # sans login
assert r.status_code == 200, r.status_code
# Aucun lien public : ni la nav, ni la page compte ne mentionnent la route.
base = (config.TEMPLATES / 'base.html').read_text(encoding='utf-8')
compte = (config.TEMPLATES / 'compte.html').read_text(encoding='utf-8')
assert '/probabilites' not in base and '/probabilites' not in compte
admin_tpl = (config.TEMPLATES / 'admin.html').read_text(encoding='utf-8')
assert '/probabilites' in admin_tpl, 'le seul lien doit etre dans admin.html'
print('TEST 4 OK : /probabilites publique, lien uniquement dans admin.html')

r = client.get('/api/probabilites/data?count=1&sims=1000')
d = r.get_json()
assert d['ok'] and d['sims'] == 1000, d
nums = [row['numero'] for row in d['rows']]
assert 4 not in nums, 'carte hors simulation presente dans les probabilites'
# count=1 : chaque simulation tire exactement 1 carte -> somme des pct ~ 100.
total = sum(row['pct'] for row in d['rows'])
assert 95.0 <= total <= 105.0, total
# Tri decroissant.
pcts = [row['pct'] for row in d['rows']]
assert pcts == sorted(pcts, reverse=True)
print('TEST 5 OK : /api/probabilites/data exclut la carte desactivee, pct coherents, tri desc')

# ---------- Features 3 + 5 : weak QCM, non-abordees, reset stats ----------
qcm_file = tmp / 'qcm_physique.json'
qcm_file.write_text(json.dumps([
    {'qid': 'p-aaaaaaaaaaaa-bbbbbbbb', 'question': 'Question abordee ?',
     'choix': ['1', '2', '3', '4'], 'reponse': 1, 'chapitre': 'Test', 'temps': 20},
    {'qid': 'p-cccccccccccc-dddddddd', 'question': 'Question jamais vue ?',
     'choix': ['1', '2', '3', '4'], 'reponse': 1, 'chapitre': 'Test', 'temps': 20},
], ensure_ascii=False), encoding='utf-8')
qcm_admin.QCM_FILES = {'physique': qcm_file}
catalog = qcm_engine.read_qcm_questions(qcm_file, theme='physique')
QID_HIT, QID_MISS = catalog[0]['qid'], catalog[1]['qid']

# Sans aucune reponse : les deux questions sont « non abordees » -> liste vide.
d = client.get('/api/admin/qcm/weak?theme=physique').get_json()
assert d['ok'] and d['rows'] == [], d['rows']
print('TEST 6 OK : questions jamais abordees exclues du panneau admin')

# Une reponse sur QID_HIT -> elle apparait, l'autre reste exclue.
qcm_admin.record_qcm_answer(QID_HIT, 'physique', 'Test', 'Question abordee ?', 'Alice', False, 5.0, 0)
d = client.get('/api/admin/qcm/weak?theme=physique').get_json()
qids = [row['qid'] for row in d['rows']]
assert qids == [QID_HIT], qids
print('TEST 7 OK : question abordee visible, non-abordee toujours exclue')

# Reset : 401 sans admin, 405 en GET, puis effet du reset.
anon = app.test_client()
assert anon.post('/api/admin/qcm/questions/reset-stats',
                 json={'questions': [{'qid': QID_HIT, 'theme': 'physique'}]}).status_code == 401
assert client.get('/api/admin/qcm/questions/reset-stats').status_code == 405
before = dbmod.db().execute("SELECT COUNT(*) FROM qcm_answers").fetchone()[0]
r = client.post('/api/admin/qcm/questions/reset-stats',
                json={'questions': [{'qid': QID_HIT, 'theme': 'physique'}]})
assert r.status_code == 200 and r.get_json()['reset'] == 1, r.get_json()
# Apres reset : la question redevient « non abordee » cote admin...
d = client.get('/api/admin/qcm/weak?theme=physique').get_json()
assert d['rows'] == [], d['rows']
# ...mais qcm_answers n'a RIEN perdu...
after = dbmod.db().execute("SELECT COUNT(*) FROM qcm_answers").fetchone()[0]
assert after == before, (before, after)
# ...et la vue eleve voit toujours son erreur.
eleve = app.test_client()
with eleve.session_transaction() as s:
    s['sr_user'] = 'Alice'
d = eleve.get('/api/sr/qcm/errors').get_json()
rows_eleve = d.get('errors') or d.get('rows') or []
assert d and any(e.get('qid') == QID_HIT for e in rows_eleve), d
print('TEST 8 OK : reset-stats admin sans toucher a l\'historique eleve')

# ---------- Feature 4 : encart jokers sur /compte ----------
r = client.get('/compte')
html = r.get_data(as_text=True)
assert r.status_code == 200 and 'jokers' in html.lower(), 'encart jokers absent de /compte'
assert '1 joker tous les 4 jours' in html and '2 au maximum' in html
print('TEST 9 OK : encart jokers present sur /compte')

print('TOUS LES TESTS SONT PASSES')
