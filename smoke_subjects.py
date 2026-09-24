# -*- coding: utf-8 -*-
"""Smoke test multi-matieres : migration v1 -> v2, scoping des routes,
QCM communs, jokers par matiere, exports, streak-guard.

Lance depuis la racine du depot :
    python smoke_subjects.py
"""
import os, sys, io, json, sqlite3, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)

# Environnement de test : chimie activee, cookies non securises (test_client).
os.environ['CHIMIE'] = '1'
os.environ['MATHS'] = '0'
os.environ['MADEC_ADMIN_PASSWORD'] = 'test-admin-pw'
os.environ['FLASK_SECRET_KEY'] = 'test-secret'
os.environ['MADEC_INTERNAL_API_KEY'] = 'test-internal-key'
os.environ['MADEC_COOKIE_SECURE'] = '0'

import config
DB_PATH = config.DB_PATH
DATA = config.DATA
DATA.mkdir(parents=True, exist_ok=True)
config.UPLOADS.mkdir(parents=True, exist_ok=True)

# --- 1. Base v1 pre-migration, avec donnees ---
if DB_PATH.exists():
    DB_PATH.unlink()
for bak in DATA.glob('forgecards.bak-*'):
    bak.unlink()

v1 = sqlite3.connect(DB_PATH)
v1.executescript('''
CREATE TABLE forgecards (numero INTEGER PRIMARY KEY, fiche_file TEXT NOT NULL,
  correction_file TEXT NOT NULL, bareme_file TEXT DEFAULT '', titre TEXT DEFAULT '',
  indices TEXT DEFAULT '', chapitre TEXT DEFAULT 'Autre', teacher_difficulty REAL,
  hors_serie INTEGER DEFAULT 0, code TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE users (prenom TEXT PRIMARY KEY, emoji_password TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_jokers (prenom TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0,
  last_milestone INTEGER DEFAULT 0);
CREATE TABLE joker_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, prenom TEXT NOT NULL,
  day TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
  balance_after INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE sr_state_user (prenom TEXT NOT NULL, numero INTEGER NOT NULL,
  stability REAL, difficulty REAL, state TEXT DEFAULT 'new', last_review TEXT,
  next_review TEXT, repetitions INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0,
  last_retrievability REAL, PRIMARY KEY (prenom, numero));
CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, numero INTEGER NOT NULL,
  prenom TEXT NOT NULL, result TEXT NOT NULL, note TEXT DEFAULT '',
  duration_seconds INTEGER, was_new INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE sr_daily_streak (prenom TEXT NOT NULL, day TEXT NOT NULL,
  validated INTEGER DEFAULT 0, PRIMARY KEY (prenom, day));
CREATE TABLE draw_history (id INTEGER PRIMARY KEY AUTOINCREMENT, numero INTEGER NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE sr_weights_user (prenom TEXT PRIMARY KEY, weights_json TEXT NOT NULL,
  nb_reviews_used INTEGER DEFAULT 0, loss_before REAL, loss_after REAL,
  trained_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, prenom TEXT NOT NULL,
  type TEXT NOT NULL, payload TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE qcm_invites (token TEXT PRIMARY KEY, prenom TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE qcm_answers (id INTEGER PRIMARY KEY AUTOINCREMENT, qid TEXT NOT NULL,
  theme TEXT NOT NULL, chapitre TEXT DEFAULT '', question TEXT DEFAULT '',
  prenom TEXT DEFAULT '', ok INTEGER NOT NULL DEFAULT 0, elapsed REAL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE qcm_games (id INTEGER PRIMARY KEY AUTOINCREMENT, gid TEXT NOT NULL,
  themes TEXT DEFAULT '', nb_questions INTEGER DEFAULT 0, nb_players INTEGER DEFAULT 0,
  podium TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE reviews_archive (id INTEGER PRIMARY KEY AUTOINCREMENT, numero INTEGER NOT NULL,
  prenom TEXT NOT NULL, result TEXT NOT NULL, note TEXT DEFAULT '',
  duration_seconds INTEGER, was_new INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  note_masquee INTEGER DEFAULT 0, archived_at TEXT DEFAULT CURRENT_TIMESTAMP);
INSERT INTO forgecards(numero, fiche_file, correction_file, bareme_file, titre, chapitre, code)
  VALUES (1, '1.pdf', '1-c.pdf', '', 'Fiche physique 1', 'Optique scalaire', '1');
INSERT INTO forgecards(numero, fiche_file, correction_file, bareme_file, titre, chapitre, code)
  VALUES (2, '2.pdf', '2-c.pdf', '', 'Fiche physique 2', 'Electrostatique', '2');
INSERT INTO users(prenom, emoji_password) VALUES ('Alice', '1|2|3|4');
''')
v1.commit()
# Alice a AUSSI une review datée d'aujourd'hui : sa streak physique est de 1.
from config import now_paris
today_iso = now_paris().date().isoformat()
v1.execute("INSERT INTO reviews(numero, prenom, result, was_new, created_at) VALUES (1, 'Alice', 'good', 1, ?)",
           (today_iso + 'T10:00:00',))
v1.execute("INSERT INTO sr_state_user(prenom, numero, state, next_review, repetitions) VALUES ('Alice', 1, 'review', ?, 1)",
           (today_iso,))
v1.execute("INSERT INTO sr_daily_streak(prenom, day, validated) VALUES ('Alice', ?, 1)", (today_iso,))
v1.executescript('''
INSERT INTO reviews(numero, prenom, result, was_new, created_at)
  VALUES (1, 'Alice', 'good', 1, '2026-09-20T10:00:00');
INSERT INTO sr_daily_streak(prenom, day, validated) VALUES ('Alice', '2026-09-20', 1);
INSERT INTO draw_history(numero, created_at) VALUES (1, '2026-09-20T10:05:00');
INSERT INTO user_jokers(prenom, count) VALUES ('Alice', 1);
INSERT INTO joker_ledger(prenom, day, delta, reason, balance_after)
  VALUES ('Alice', '2026-09-20', 1, 'milestone_award', 1);
INSERT INTO events(prenom, type, created_at) VALUES ('Alice', 'login', '2026-09-20T09:00:00');
INSERT INTO sr_weights_user(prenom, weights_json) VALUES ('Alice', '[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17]');
''')
v1.commit()
v1.close()

# --- 2. Import de l'app (declenche la migration) ---
import app as app_module
app = app_module.app
app.config['TESTING'] = True
client = app.test_client()

FAILED = []
def check(name, cond, extra=''):
    status = 'ok ' if cond else 'Echec'
    print(f"[{status}] {name}" + (f" — {extra}" if extra and not cond else ''))
    if not cond:
        FAILED.append(name)

def pdf_bytes(label):
    return io.BytesIO(b'%PDF-1.4\n%' + label.encode() + b'\nfake pdf content')

# --- 3. Migration v1 -> v2 ---
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
fc_cols = [r[1] for r in conn.execute('PRAGMA table_info(forgecards)').fetchall()]
check("migration : colonne subject dans forgecards", 'subject' in fc_cols)
row = conn.execute("SELECT subject, titre FROM forgecards WHERE numero=1").fetchone()
check("migration : donnees physique conservees", row and row['subject'] == 'physique' and 'physique 1' in row['titre'],
      str(dict(row) if row else None))
check("migration : reviews conservees en physique",
      conn.execute("SELECT COUNT(*) c FROM reviews WHERE subject='physique' AND prenom='Alice'").fetchone()['c'] == 2)
check("migration : user_jokers PK composee",
      conn.execute("SELECT COUNT(*) c FROM user_jokers WHERE prenom='Alice' AND subject='physique' AND count=1").fetchone()['c'] == 1)
check("migration : sr_daily_streak conserve",
      conn.execute("SELECT COUNT(*) c FROM sr_daily_streak WHERE prenom='Alice' AND subject='physique'").fetchone()['c'] == 2)
check("migration : draw_history conserve",
      conn.execute("SELECT COUNT(*) c FROM draw_history WHERE subject='physique'").fetchone()['c'] == 1)
check("migration : events conserve",
      conn.execute("SELECT COUNT(*) c FROM events WHERE subject='physique'").fetchone()['c'] == 1)
check("migration : snapshot cree", len(list(DATA.glob('forgecards.bak-*'))) == 1)
conn.close()

# --- 4. Switch matiere + liste publique sans fuite ---
r = client.get('/api/subjects')
d = r.get_json()
check("subjects : physique + chimie activees",
      [s['key'] for s in d['subjects']] == ['physique', 'chimie'], str(d))
check("subjects : current defaut physique", d['current'] == 'physique')

r = client.get('/api/forgecards/public')
check("public : 2 fiches physique (session par defaut)", len(r.get_json()) == 2)
r = client.post('/api/subject', json={'subject': 'chimie'})
check("switch chimie ok", r.get_json().get('ok') is True)
r = client.get('/api/forgecards/public')
check("public : 0 fiche chimie (pas de fuite physique)", len(r.get_json()) == 0)
r = client.post('/api/subject', json={'subject': 'maths'})
check("switch maths refuse (flag off)", r.status_code == 400)
client.post('/api/subject', json={'subject': 'physique'})

# --- 5. Admin : login + uploads par matiere ---
r = client.post('/api/login', json={'password': 'test-admin-pw'})
check("admin login", r.get_json().get('ok') is True)

def upload(numero, subject, titre):
    return client.post('/api/forgecards/upload?subject=' + subject, data={
        'numero': str(numero), 'titre': titre, 'chapitre': 'Autre',
        'fiche_pdf': (pdf_bytes('fiche'), 'f.pdf'),
        'correction_pdf': (pdf_bytes('corr'), 'c.pdf'),
    }, content_type='multipart/form-data')

r = upload(3, 'physique', 'Fiche physique 3')
check("upload physique 3", r.get_json().get('ok') is True, r.get_json())
r = upload(1, 'chimie', 'Fiche chimie 1')
check("upload chimie 1", r.get_json().get('ok') is True and r.get_json().get('subject') == 'chimie', r.get_json())
r = upload(2, 'chimie', 'Fiche chimie 2')
check("upload chimie 2", r.get_json().get('ok') is True, r.get_json())

r = client.get('/api/forgecards').get_json()
check("admin liste sans subject = physique", len(r) == 3 and all('physique' in c['titre'] for c in r))
r = client.get('/api/forgecards?subject=chimie').get_json()
check("admin liste chimie = 2 fiches chimie", len(r) == 2 and all('chimie' in c['titre'] for c in r), str(r))
check("fichiers chimie sous-dossier", all(c['fiche_file'].startswith('chimie/') for c in r))

# Chapitres scopés
r = client.get('/api/chapitres?subject=chimie').get_json()
check("chapitres chimie (placeholder)", r == ['Autre'], str(r))
r = client.get('/api/chapitres').get_json()
check("chapitres physique intacts", len(r) == len(config.CHAPITRES), str(len(r)))

# Params par matiere
client.post('/api/params?subject=chimie', json={'max_active_num': 2, 'daily_new_limit': 1, 'daily_review_limit': 1})
r = client.get('/api/params?subject=chimie').get_json()
check("params chimie independants", r['max_active_num'] == 2)
r = client.get('/api/params').get_json()
check("params physique inchanges", r['max_active_num'] == 36)

# --- 6. Eleve : login, SR, reviews par matiere ---
r = client.post('/api/users', json={'prenom': 'Bob'})
pw = r.get_json().get('password')
check("creation eleve Bob", bool(pw))
r = client.post('/api/sr/login', json={'prenom': 'Bob', 'password': pw})
check("login eleve Bob", r.get_json().get('ok') is True)

r = client.get('/api/sr/today').get_json()
check("sr/today physique : 3 nouvelles (quota 3)", r['subject'] == 'physique' and len(r['cards']) == 3, str(r.get('new_remaining_pool')))
num = r['cards'][0]['numero']
r = client.post(f'/api/sr/{num}/review', json={'result': 'good'}).get_json()
check("review physique enregistree", r.get('ok') is True and r.get('subject') == 'physique')

client.post('/api/subject', json={'subject': 'chimie'})
r = client.get('/api/sr/today').get_json()
check("sr/today chimie : quota chimie 1 nouvelle", r['subject'] == 'chimie' and len(r['cards']) == 1, str(len(r['cards'])))
num_chimie = r['cards'][0]['numero']
check("numeros chimie distincts (1 ou 2)", num_chimie in (1, 2))
r = client.post(f'/api/sr/{num_chimie}/review', json={'result': 'easy'}).get_json()
check("review chimie enregistree", r.get('ok') is True and r.get('subject') == 'chimie')

conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
check("reviews bien scopees en base",
      conn.execute("SELECT COUNT(*) c FROM reviews WHERE prenom='Bob' AND subject='chimie'").fetchone()['c'] == 1
      and conn.execute("SELECT COUNT(*) c FROM reviews WHERE prenom='Bob' AND subject='physique'").fetchone()['c'] == 1)
check("sr_state scope physique/chimie",
      conn.execute("SELECT COUNT(*) c FROM sr_state_user WHERE prenom='Bob' AND subject='chimie'").fetchone()['c'] >= 2)
conn.close()

# Tirage scope
r = client.get('/api/draw?count=2').get_json()
check("draw chimie : fiches chimie uniquement", len(r) == 2 and all(c['titre'].startswith('Fiche chimie') for c in r), str(r))
client.post('/api/subject', json={'subject': 'physique'})
r = client.get('/api/draw?count=2').get_json()
check("draw physique : fiches physique uniquement", all('physique' in c['titre'] for c in r))

# Dashboard par matiere
d = client.get('/api/sr/dashboard').get_json()
check("dashboard physique (1 review)", d['subject'] == 'physique' and d['total'] == 1)
client.post('/api/subject', json={'subject': 'chimie'})
d = client.get('/api/sr/dashboard').get_json()
check("dashboard chimie (1 review, streak isolee)", d['subject'] == 'chimie' and d['total'] == 1)

# Jokers par matiere (admin). Bob vient de reviser en chimie (streak 1) ;
# Alice a une streak physique d'aujourd'hui mais rien en chimie.
r = client.post('/api/users/Bob/joker?subject=chimie').get_json()
check("joker chimie accorde (Bob a streak chimie 1)", r.get('ok') is True and r.get('jokers') == 1, str(r))
r = client.post('/api/users/Alice/joker?subject=physique').get_json()
check("joker physique accorde (streak Alice)", r.get('ok') is True and r.get('jokers') == 2, str(r))
r = client.post('/api/users/Alice/joker?subject=chimie').get_json()
check("joker chimie refuse (streak chimie 0)", r.get('ok') is False)

# --- 7. QCM communs depuis les deux pages admin ---
for theme in ('physique', 'chimie'):
    r = client.get(f'/api/admin/qcm/{theme}').get_json()
    check(f"QCM {theme} lisible depuis /cadmin et /admin", r.get('ok') is True)
r = client.put('/api/admin/qcm/chimie', json={'raw': json.dumps([
    {'question': 'Q chimie ?', 'choix': ['a', 'b'], 'reponse': 1, 'temps': 30}])})
check("QCM chimie enregistrable", r.get_json().get('ok') is True)
r = client.get('/api/admin/qcm/weak?theme=chimie').get_json()
check("stats QCM communes par theme", r.get('ok') is True and len(r.get('rows', [])) == 1)

# --- 8. Stats admin scopées ---
r = client.get('/api/users/stats?subject=chimie').get_json()
bob_chimie = [s for s in r if s['prenom'] == 'Bob']
check("users/stats chimie : Bob a 1 review chimie", bob_chimie and bob_chimie[0]['total_reviews'] == 1, str(r))
r = client.get('/api/users/stats').get_json()
bob_phys = [s for s in r if s['prenom'] == 'Bob']
check("users/stats physique : Bob a 1 review physique", bob_phys and bob_phys[0]['total_reviews'] == 1)

r = client.get('/api/admin/stats/overview?subject=chimie').get_json()
check("overview chimie scope + qcm_mois commun", r.get('subject') == 'chimie' and r.get('qcm_mois') is not None)

# --- 9. Exports ---
r = client.get('/api/users/Bob/export?subject=chimie')
check("export CSV Bob chimie", r.status_code == 200 and 'Fiche chimie' in r.get_data(as_text=True))
r = client.get('/api/users/Bob/export?subject=physique')
check("export CSV Bob physique (?subject= explicite)",
      r.status_code == 200 and 'physique' in r.get_data(as_text=True))
r = client.get('/api/admin/stats/export/synthese-eleves?subject=chimie')
check("export synthese chimie nomme", 'synthese_eleves_chimie' in r.headers.get('Content-Disposition', ''))
r = client.get('/api/sr/export?subject=chimie')
check("export FSRS chimie", r.status_code == 200 and 'chimie' in r.get_data(as_text=True))

# --- 10. Routes internes bot ---
r = client.post('/api/internal/weekly-stats?subject=chimie',
                headers={'X-Madec-Internal-Key': 'test-internal-key'})
d = r.get_json()
check("weekly-stats chimie", d.get('ok') is True and d.get('subject') == 'chimie')
r = client.post('/api/internal/weekly-stats', headers={'X-Madec-Internal-Key': 'test-internal-key'})
check("weekly-stats sans subject refuse", r.status_code == 400)

r = client.post('/api/internal/streak-guard', headers={'X-Madec-Internal-Key': 'test-internal-key'})
d = r.get_json()
check("streak-guard nested par matiere",
      d.get('ok') is True and 'physique' in d.get('results', {}) and 'chimie' in d.get('results', {}),
      str(list(d.get('results', {}).keys())))
check("streak-guard : Bob present dans les deux matieres",
      'Bob' in d['results']['physique'] and 'Bob' in d['results']['chimie'])

r = client.post('/api/internal/log-event', json={'prenom': 'Bob', 'type': 'reminder', 'subject': 'chimie'},
                headers={'X-Madec-Internal-Key': 'test-internal-key'})
check("log-event interne avec subject", r.get_json().get('ok') is True)

# --- 11. /cadmin ---
r = client.get('/cadmin')
check("/cadmin rend admin.html avec data-subject chimie",
      r.status_code == 200 and 'data-subject="chimie"' in r.get_data(as_text=True))
r = client.get('/admin')
check("/admin rend data-subject physique", 'data-subject="physique"' in r.get_data(as_text=True))

# --- 12. Suppression de fiche scopee ---
r = client.delete('/api/forgecards/2?subject=chimie')
check("delete fiche chimie 2", r.get_json().get('ok') is True)
r = client.get('/api/forgecards?subject=chimie').get_json()
check("chimie il reste 1 fiche", len(r) == 1)
r = client.get('/api/forgecards?subject=physique').get_json()
check("physique toujours 3 fiches (pas de fuite)", len(r) == 3)

print()
if FAILED:
    print(f"ECHECS ({len(FAILED)}):")
    for f in FAILED:
        print(" -", f)
    sys.exit(1)
print("TOUS LES TESTS SONT PASSES")
