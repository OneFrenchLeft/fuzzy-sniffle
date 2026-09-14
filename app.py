from flask import Flask, render_template, request, jsonify, session, send_from_directory, make_response, abort, redirect
from functools import wraps
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import sqlite3, json, os, secrets, csv, io, random, time, hmac, hashlib, re

from fsrs import apply_review, preview_all_grades, humanize_interval, DEFAULT_WEIGHTS

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')

TEMPLATES = BASE / 'templates'
STATIC = BASE / 'static'
UPLOADS = BASE / 'uploads' / 'fiche'
DATA = BASE / 'data'
DB_PATH = DATA / 'forgecards.db'
PARAMS_PATH = DATA / 'params.json'
QCM_FILES = {
    'physique': DATA / 'qcm_physique.json',
    'chimie': DATA / 'qcm_chimie.json',
    'maths': DATA / 'qcm_maths.json',
}
QCM_MIN_TIME_S = 10
QCM_MAX_TIME_S = 300
QCM_DEFAULT_TIME_S = 30

QCM_THEME_PREFIX = {'physique': 'p', 'chimie': 'c', 'maths': 'm'}
QID_RE = re.compile(r'^[a-z]-[0-9a-f]{12}-[0-9a-f]{8}$')

def qid_for(theme, chapitre, question):
    prefix = QCM_THEME_PREFIX.get(theme, 'x')
    ch = hashlib.sha1(chapitre.strip().encode('utf-8')).hexdigest()[:12]
    qh = hashlib.sha1(question.strip().encode('utf-8')).hexdigest()[:8]
    return f'{prefix}-{ch}-{qh}'


TZ_PARIS = ZoneInfo('Europe/Paris')

def now_paris():
    return datetime.now(TZ_PARIS).replace(tzinfo=None)


ADMIN_PASSWORD = os.environ.get('MADEC_ADMIN_PASSWORD')
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_hex(16)
    print('!' * 60)
    print('MADEC_ADMIN_PASSWORD non defini dans .env')
    print('Mot de passe admin temporaire pour cette session :', ADMIN_PASSWORD)
    print('Definis MADEC_ADMIN_PASSWORD dans .env pour le rendre persistant.')
    print('!' * 60)

DEFAULT_PARAMS = {
    'max_active_num': 36,
    'max_hors_serie_num': 6,
    'fsrs_retention': 0.90,
    'daily_new_limit': 3,
    'daily_review_limit': 3,
    "previous_chapter_bonus": 0.2,
    "last_chapter_bonus": 0.9,
    "teacher_difficulty_weight": 0.15
}


EMOJI_KEYPAD = ['1', '2', '3', '4', '5', '6', '7', '8']
EMOJI_PW_LENGTH = 4
EMOJI_SEP = '|'

MAX_NOTE_LENGTH = 2000
MAX_REVIEW_DURATION_SECONDS = 6 * 3600

DRAW_HISTORY_KEEP = 200

def gen_emoji_password():
    return [secrets.choice(EMOJI_KEYPAD) for _ in range(EMOJI_PW_LENGTH)]

CHAPITRES = [
    'Systèmes ouverts',
    'Diffusion thermique',
    'Diffusion de particules',
    'Rayonnement thermique',
    'Référentiels non galiléens',
    'Fluides visqueux',
    'Fluides parfaits',
    'Bilans macroscopiques',
    'Optique scalaire',
    'Interférences à division du front d\'onde',
    'Interférences à division d\'amplitude',
    'Sources du champ EM',
    'Electrostatique',
    'Magnétostatique',
    'Equations de Maxwell',
    'Equation de d\'Alembert',
    'Ondes acoustiques',
    'Ondes EM dans le vide',
    'Dispersion et absorption',
    'Ondes EM dans les milieux matériels',
    'Physique des lasers',
    'Mécanique quantique',
    'Première année - Mécanique',
    'Première année - Optique géométrique',
    'Première année - Induction',
    'Première année - Thermodynamique',
    'Première année - Electricité',
    'Autre'
]

ALLOWED_EXTENSIONS = {'pdf'}
MAX_CONTENT_LENGTH = 15 * 1024 * 1024

app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC), static_url_path='/static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    print('AVERTISSEMENT : FLASK_SECRET_KEY non defini -> cle aleatoire (sessions invalidees a chaque redemarrage).')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=48)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_COOKIE_SECURE', '1') != '0'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=0)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

UPLOADS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)


from flask_socketio import SocketIO
socketio = SocketIO(app, async_mode='gevent')
import qcm_engine
qcm_engine.register(socketio, DATA,
                    on_answer=lambda *a, **k: record_qcm_answer(*a, **k),
                    on_game_end=lambda *a, **k: record_qcm_game(*a, **k))

_login_attempts = {}
RATE_LIMIT_WINDOW = 300
RATE_LIMIT_MAX = 8

@app.route('/qcm')
def qcm_page():
    if not session.get('sr_user'):
        return render_template('index.html')
    return render_template('qcm.html', prenom=session['sr_user'])

@app.route('/qcm-images/<path:filename>')
def qcm_image(filename):
    root = (DATA / 'qcm_images').resolve()
    safe = (root / filename).resolve()
    if not str(safe).startswith(str(root) + os.sep) or not safe.is_file():
        abort(404)
    return send_from_directory(safe.parent, safe.name)

def rate_limited(key):
    now = time.time()
    attempts = _login_attempts.get(key, [])
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        _login_attempts[key] = attempts
        return True
    attempts.append(now)
    _login_attempts[key] = attempts

    if len(_login_attempts) > 1000:
        for k in [k for k, v in _login_attempts.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            del _login_attempts[k]
    return False

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'

    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-src https://www.youtube.com; "
        "frame-ancestors 'none'"
    )
    return resp

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_teacher_difficulty(raw):

    if raw in (None, ''):
        return None
    try:
        return min(max(float(raw), 1.0), 5.0)
    except (TypeError, ValueError):
        return None

def parse_duration_seconds(raw):

    if raw is None:
        return None
    try:
        return min(max(int(raw), 0), MAX_REVIEW_DURATION_SECONDS)
    except (TypeError, ValueError):
        return None

def csv_safe(value):

    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value

def csv_response(output, filename):

    resp = make_response('\ufeff' + output.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    conn = db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS forgecards ("
        "numero INTEGER PRIMARY KEY,"
        "fiche_file TEXT NOT NULL,"
        "correction_file TEXT NOT NULL,"
        "bareme_file TEXT DEFAULT '',"
        "titre TEXT DEFAULT '',"
        "indices TEXT DEFAULT '',"
        "chapitre TEXT DEFAULT 'Autre',"
        "teacher_difficulty REAL,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "prenom TEXT PRIMARY KEY,"
        "emoji_password TEXT,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute('''CREATE TABLE IF NOT EXISTS user_jokers (
        prenom TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0,
        last_milestone INTEGER DEFAULT 0
    )''')
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_state_user ("
        "prenom TEXT NOT NULL,"
        "numero INTEGER NOT NULL,"
        "stability REAL,"
        "difficulty REAL,"
        "state TEXT DEFAULT 'new',"
        "last_review TEXT,"
        "next_review TEXT,"
        "repetitions INTEGER DEFAULT 0,"
        "lapses INTEGER DEFAULT 0,"
        "last_retrievability REAL,"
        "PRIMARY KEY (prenom, numero)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviews ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "numero INTEGER NOT NULL,"
        "prenom TEXT NOT NULL,"
        "result TEXT NOT NULL,"
        "note TEXT DEFAULT '',"
        "duration_seconds INTEGER,"
        "was_new INTEGER DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_daily_streak ("
        "prenom TEXT NOT NULL,"
        "day TEXT NOT NULL,"
        "validated INTEGER DEFAULT 0,"
        "PRIMARY KEY (prenom, day)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS draw_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "numero INTEGER NOT NULL,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_weights_user ("
        "prenom TEXT PRIMARY KEY,"
        "weights_json TEXT NOT NULL,"
        "nb_reviews_used INTEGER DEFAULT 0,"
        "loss_before REAL,"
        "loss_after REAL,"
        "trained_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qcm_invites (
            token TEXT PRIMARY KEY,
            prenom TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qcm_invites_expires "
        "ON qcm_invites(expires_at)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_answers ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "qid TEXT NOT NULL,"
        "theme TEXT NOT NULL,"
        "chapitre TEXT DEFAULT '',"
        "question TEXT DEFAULT '',"
        "prenom TEXT DEFAULT '',"
        "ok INTEGER NOT NULL DEFAULT 0,"
        "elapsed REAL,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qcm_answers_qid ON qcm_answers(qid)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "prenom TEXT NOT NULL,"
        "type TEXT NOT NULL,"
        "payload TEXT DEFAULT '',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_prenom ON events(prenom, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_games ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "gid TEXT NOT NULL,"
        "themes TEXT DEFAULT '',"
        "nb_questions INTEGER DEFAULT 0,"
        "nb_players INTEGER DEFAULT 0,"
        "podium TEXT DEFAULT '',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_prenom_created ON reviews(prenom, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_result_note ON reviews(result, note)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_numero ON reviews(numero)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sr_state_user_next_review ON sr_state_user(next_review)")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(forgecards)").fetchall()]
    if 'titre' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN titre TEXT DEFAULT ''")
    if 'indices' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN indices TEXT DEFAULT ''")
    if 'bareme_file' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN bareme_file TEXT DEFAULT ''")
    if 'chapitre' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN chapitre TEXT DEFAULT 'Autre'")
    if 'teacher_difficulty' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN teacher_difficulty REAL")
    if 'hors_serie' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN hors_serie INTEGER DEFAULT 0")
    if 'code' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN code TEXT DEFAULT ''")
    # backfill du code public : numero pour le pool normal, h1/h2... pour les hors-serie
    hs_idx = 0
    for row in conn.execute("SELECT numero, hors_serie, code, fiche_file, correction_file, bareme_file FROM forgecards ORDER BY numero ASC").fetchall():
        if row['code']:
            if row['hors_serie']:
                hs_idx = max(hs_idx, hs_sort_key(row['code']))
            continue
        if row['hors_serie']:
            hs_idx += 1
            code = f'h{hs_idx}'
        else:
            code = str(row['numero'])
        old_files = [row['fiche_file'], row['correction_file'], row['bareme_file']]
        new_files = filenames_for(code)
        for old_name, new_name in zip(old_files, new_files):
            old_path = UPLOADS / old_name if old_name else None
            if old_name and old_name != new_name and old_path.exists():
                old_path.replace(UPLOADS / new_name)
        conn.execute(
            'UPDATE forgecards SET code=?, fiche_file=?, correction_file=?, bareme_file=? WHERE numero=?',
            (code, new_files[0], new_files[1], new_files[2] if row['bareme_file'] else '', row['numero'])
        )

    user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'emoji_password' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN emoji_password TEXT")

    sr_cols = [r[1] for r in conn.execute("PRAGMA table_info(sr_state_user)").fetchall()]
    if 'last_retrievability' not in sr_cols:
        conn.execute("ALTER TABLE sr_state_user ADD COLUMN last_retrievability REAL")

    rev_cols = [r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()]
    if 'note' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN note TEXT DEFAULT ''")
    if 'duration_seconds' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN duration_seconds INTEGER")
    if 'was_new' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN was_new INTEGER DEFAULT 0")

    joker_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_jokers)").fetchall()]
    if 'last_milestone' not in joker_cols:
        conn.execute("ALTER TABLE user_jokers ADD COLUMN last_milestone INTEGER DEFAULT 0")
    if 'note_masquee' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN note_masquee INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


_db_ready = False

def ensure_db():
    global _db_ready
    if _db_ready:
        return
    init_db()
    try:
        conn = db()
        cutoff = (now_paris() - timedelta(days=400)).isoformat()
        conn.execute('DELETE FROM events WHERE created_at < ?', (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    _db_ready = True

def log_event(conn, prenom, etype, payload=''):
    if not prenom:
        prenom = 'anonyme'
    try:
        conn.execute('INSERT INTO events(prenom, type, payload, created_at) VALUES(?,?,?,?)',
                     (prenom, etype, str(payload)[:500], now_paris().isoformat()))
    except Exception as exc:
        print(f'[events] log impossible: {exc!r}')

def read_params():


    data = {}
    if PARAMS_PATH.exists():
        try:
            data = json.loads(PARAMS_PATH.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    out = DEFAULT_PARAMS.copy()
    for k, default in DEFAULT_PARAMS.items():
        try:
            if k in ('max_active_num', 'daily_new_limit', 'daily_review_limit', 'max_hors_serie_num'):
                out[k] = max(0, int(data.get(k, default)))
            elif k == 'fsrs_retention':
                out[k] = min(max(float(data.get(k, default)), 0.80), 0.97)
            else:
                out[k] = float(data.get(k, default))
        except (TypeError, ValueError):
            out[k] = default
    if not PARAMS_PATH.exists():
        PARAMS_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    return out

def write_params(data):
    merged = read_params()
    for k in DEFAULT_PARAMS:
        if k in data:
            merged[k] = data[k]
    PARAMS_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')

    return read_params()

def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        session.permanent = True
        session.modified = True
        return fn(*args, **kwargs)
    return wrapper

def normalize_qcm_choice(value):

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('un choix texte ne peut pas être vide')
        return text

    if not isinstance(value, dict):
        raise ValueError('un choix doit être une chaîne ou un objet')

    out = {}
    text = str(value.get('texte', value.get('text', ''))).strip()
    image = str(value.get('image', '')).strip()
    latex = str(value.get('latex', '')).strip()

    if text:
        out['texte'] = text
    if image:
        out['image'] = image
    if latex:
        out['latex'] = latex

    if not out:
        raise ValueError('un choix riche doit contenir texte, image ou latex')

    return out

def validate_qcm_questions(raw, theme=None):

    if not isinstance(raw, list):
        raise ValueError('le fichier QCM doit contenir une liste')

    clean = []

    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'question {position} : objet JSON attendu')

        question = str(item.get('question', '')).strip()
        if not question:
            raise ValueError(f'question {position} : champ question requis')

        choix_raw = item.get('choix')
        if not isinstance(choix_raw, list):
            raise ValueError(f'question {position} : tableau choix requis')

        choix = [
            normalize_qcm_choice(choice)
            for choice in choix_raw
        ]

        if not 2 <= len(choix) <= 4:
            raise ValueError(
                f'question {position} : il faut entre 2 et 4 choix'
            )

        try:
            reponse = int(item.get('reponse'))
        except (TypeError, ValueError):
            raise ValueError(
                f'question {position} : reponse doit être un entier'
            )

        if not 1 <= reponse <= len(choix):
            raise ValueError(
                f'question {position} : reponse doit être comprise entre '
                f'1 et {len(choix)}'
            )

        try:
            temps = int(item.get('temps', QCM_DEFAULT_TIME_S))
        except (TypeError, ValueError):
            raise ValueError(
                f'question {position} : temps doit être un entier'
            )

        if not QCM_MIN_TIME_S <= temps <= QCM_MAX_TIME_S:
            raise ValueError(
                f'question {position} : temps entre '
                f'{QCM_MIN_TIME_S} et {QCM_MAX_TIME_S} secondes'
            )

        normalized = {
            'question': question,
            'choix': choix,
            'reponse': reponse,
            'temps': temps,
        }

        image_complete = str(
            item.get('image_complete', item.get('image', ''))
        ).strip()
        latex = str(item.get('latex', '')).strip()

        if image_complete:
            normalized['image_complete'] = image_complete
        if latex:
            normalized['latex'] = latex
        chapitre = str(item.get('chapitre', '')).strip()
        if chapitre:
            normalized['chapitre'] = chapitre
        explication = str(item.get('explication', '')).strip()
        if explication:
            normalized['explication'] = explication

        qid = str(item.get('qid', '')).strip()
        if not QID_RE.match(qid):
            qid = qid_for(theme, chapitre or 'Autre', question) if theme else ''
        if qid:
            normalized['qid'] = qid

        clean.append(normalized)

    return clean

def get_qcm_path(theme):
    path = QCM_FILES.get(theme)
    if path is None:
        abort(404)
    return path

@app.route('/api/admin/qcm/<theme>', methods=['GET'])
@require_admin
def admin_get_qcm(theme):
    path = get_qcm_path(theme)
    if not path.exists():
        return jsonify({'ok': True, 'theme': theme, 'raw': '[]', 'mtime': None})
    try:
        raw = path.read_text(encoding='utf-8')
        mtime = path.stat().st_mtime
    except OSError as e:
        return jsonify({'ok': False, 'error': f'lecture impossible : {e}'}), 500
    return jsonify({'ok': True, 'theme': theme, 'raw': raw, 'mtime': mtime})

@app.route('/api/admin/qcm/<theme>', methods=['PUT'])
@require_admin
def admin_save_qcm(theme):
    path = get_qcm_path(theme)
    payload = request.get_json(silent=True) or {}
    raw = payload.get('raw')
    if not isinstance(raw, str):
        return jsonify({'ok': False, 'error': 'champ raw requis'}), 400
    try:
        parsed = json.loads(raw)
        questions = validate_qcm_questions(parsed, theme=theme)
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'JSON invalide : {e}'}), 400
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    normalized_raw = json.dumps(questions, ensure_ascii=False, indent=2) + '\n'
    try:
        temporary.write_text(normalized_raw, encoding='utf-8')
        temporary.replace(path)
    except OSError as e:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({'ok': False, 'error': f'impossible d’enregistrer : {e}'}), 500
    return jsonify({'ok': True, 'theme': theme, 'count': len(questions)})


GRADE_MAP_FR = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}

def replay_reviews(conn, prenom, rows):
    """Rejoue l'historique FSRS d'un eleve : pour chaque revision (dans l'ordre),
    l'etat de la fiche APRES cette revision + la retrievabilite avant.
    rows : lignes avec cles created_at, numero, result (triees chronologiquement)."""
    prof_by_num = {r['numero']: r['teacher_difficulty']
                   for r in conn.execute('SELECT numero, teacher_difficulty FROM forgecards').fetchall()}
    weights = get_user_weights(conn, prenom)
    retention = float(read_params().get('fsrs_retention', 0.90))
    state_by_card = {}
    out = []
    for rv in rows:
        numero = rv['numero']
        s_prev, d_prev, last_prev, reps, lapses = state_by_card.get(numero, (None, None, None, 0, 0))
        grade = GRADE_MAP_FR.get(rv['result'])
        try:
            when = datetime.fromisoformat(rv['created_at'])
        except (TypeError, ValueError):
            when = now_paris()
        snap = {'difficulty': None, 'stability': None, 'repetitions': 0, 'lapses': 0,
                'next_review': None, 'state': None, 'r_before': None, 'elapsed_days': None}
        if grade is not None:
            try:
                r = apply_review(s_prev, d_prev, last_prev, grade, when, retention,
                                 w=weights, fuzz=False, prof_difficulty=prof_by_num.get(numero))
                snap.update({'difficulty': r['stability'] and r['difficulty'], 'stability': r['stability'],
                             'next_review': r['next_review'], 'state': r['state'],
                             'r_before': r['retrievability_before'], 'elapsed_days': r['elapsed_days']})
                state_by_card[numero] = (r['stability'], r['difficulty'], when.isoformat(), reps + 1, lapses + (1 if grade == 1 else 0))
            except Exception:
                state_by_card[numero] = (s_prev, d_prev, last_prev, reps + 1, lapses + (1 if grade == 1 else 0))
        else:
            state_by_card[numero] = (s_prev, d_prev, last_prev, reps + 1, lapses)
        cur = state_by_card[numero]
        snap['repetitions'], snap['lapses'] = cur[3], cur[4]
        out.append(snap)
    return out

def record_qcm_game(gid, theme, nb_questions, nb_players, podium):
    try:
        ensure_db()
        conn = db()
        conn.execute(
            'INSERT INTO qcm_games(gid, themes, nb_questions, nb_players, podium, created_at) '
            'VALUES(?,?,?,?,?,?)',
            (gid, theme, nb_questions, nb_players, json.dumps(podium, ensure_ascii=False),
             now_paris().isoformat())
        )
        for entry in podium:
            if entry.get('prenom'):
                log_event(conn, entry['prenom'], 'qcm_played',
                          f"{gid}:{entry.get('score', 0)}")
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f'[qcm-games] enregistrement impossible: {exc!r}')


def record_qcm_answer(qid, theme, chapitre, question, prenom, ok, elapsed):
    if not qid:
        return
    try:
        ensure_db()
        conn = db()
        conn.execute(
            'INSERT INTO qcm_answers(qid, theme, chapitre, question, prenom, ok, elapsed, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (qid, theme, chapitre, (question or '')[:300], prenom,
             1 if ok else 0, elapsed, now_paris().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f'[qcm-stats] enregistrement impossible: {exc!r}')


@app.route('/api/admin/qcm/purge-retired', methods=['POST'])
@require_admin
def admin_qcm_purge_retired():
    # Supprime de qcm_answers les lignes dont la question n'existe plus
    # dans les fichiers (theme/chapitre filtres respectes).
    ensure_db()
    theme = request.args.get('theme', '')
    chapitre = request.args.get('chapitre', '')
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    active_qids = set()
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            if chapitre and (q.get('chapitre') or 'Autre') != chapitre:
                continue
            if q.get('qid'):
                active_qids.add(q['qid'])
    clauses, args = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        args.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        args.append(chapitre)
    where = (' AND '.join(clauses)) if clauses else '1=1'
    conn = db()
    rows = conn.execute(f'SELECT DISTINCT qid FROM qcm_answers WHERE {where}', args).fetchall()
    qids = [r['qid'] for r in rows if r['qid'] not in active_qids]
    deleted = 0
    if qids:
        marks = ','.join('?' * len(qids))
        cur = conn.execute(
            f'DELETE FROM qcm_answers WHERE qid IN ({marks}) AND {where}',
            qids + args)
        deleted = cur.rowcount
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted, 'questions': len(qids)})

@app.route('/api/admin/qcm/games', methods=['GET'])
@require_admin
def admin_qcm_games():
    ensure_db()
    conn = db()
    rows = conn.execute(
        "SELECT gid, themes, nb_questions, nb_players, podium, created_at "
        "FROM qcm_games ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            podium = json.loads(r['podium'] or '[]')
        except Exception:
            podium = []
        out.append({'gid': r['gid'], 'themes': r['themes'], 'nb_questions': r['nb_questions'],
                    'nb_players': r['nb_players'], 'podium': podium[:3], 'created_at': r['created_at']})
    return jsonify({'ok': True, 'rows': out})

@app.route('/api/admin/qcm/weak', methods=['GET'])
@require_admin
def admin_qcm_weak():
    ensure_db()
    theme = request.args.get('theme', '')
    chapitre = request.args.get('chapitre', '')
    catalog = {}
    chapitres_disponibles = set()
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            chapitres_disponibles.add(q.get('chapitre', 'Autre'))
            if q.get('qid'):
                catalog[q['qid']] = q
    sql = ("SELECT qid, theme, chapitre, question, COUNT(*) AS n, "
           "SUM(ok) AS bonnes, MAX(created_at) AS derniere "
           "FROM qcm_answers")
    clauses, params_sql = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        params_sql.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        params_sql.append(chapitre)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' GROUP BY qid'
    conn = db()
    stats = {r['qid']: r for r in conn.execute(sql, params_sql).fetchall()}
    conn.close()
    out, seen = [], set()
    for qid, q in catalog.items():
        if chapitre and q.get('chapitre') != chapitre:
            continue
        seen.add(qid)
        s = stats.get(qid)
        n = s['n'] if s else 0
        bonnes = (s['bonnes'] or 0) if s else 0
        out.append({
            'qid': qid, 'theme': q.get('theme', ''),
            'chapitre': q.get('chapitre', 'Autre'),
            'question': q.get('question', ''),
            'sorties': n,
            'taux_echec': round(100 * (n - bonnes) / n, 1) if n else 0,
            'derniere': s['derniere'] if s else None,
            'absente': False,
        })
    for qid, s in stats.items():
        if qid in seen:
            continue
        n = s['n']
        bonnes = s['bonnes'] or 0
        out.append({
            'qid': qid, 'theme': s['theme'] or '',
            'chapitre': s['chapitre'] or 'Autre',
            'question': s['question'] or '',
            'sorties': n,
            'taux_echec': round(100 * (n - bonnes) / n, 1) if n else 0,
            'derniere': s['derniere'],
            'absente': True,
        })
    out.sort(key=lambda r: (-r['taux_echec'], -r['sorties']))
    return jsonify({
        'ok': True,
        'theme': theme,
        'chapitres': sorted(chapitres_disponibles, key=str.lower),
        'rows': out,
    })


def require_sr_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('sr_user'):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        return fn(*args, **kwargs)
    return wrapper

def hs_sort_key(code):
    try:
        return int(str(code)[1:])
    except (TypeError, ValueError):
        return 10 ** 9


def card_label(code, hors_serie):
    code = code or ''
    return code.upper() if hors_serie else code


def filenames_for(code):
    return (
        secure_filename(f'{code}.pdf'),
        secure_filename(f'{code}-c.pdf'),
        secure_filename(f'{code}-b.pdf'),
    )

def remove_pdf_if_exists(filename):
    if not filename:
        return
    path = UPLOADS / filename
    if path.exists():
        path.unlink()

def card_exists(conn, numero):
    return conn.execute('SELECT 1 FROM forgecards WHERE numero=?', (numero,)).fetchone() is not None

def interleave_by_chapitre(cards):
    reordered = []
    remaining = cards[:]
    last_chapitre = None
    while remaining:
        idx = None
        for i, c in enumerate(remaining):
            if c.get('chapitre') != last_chapitre:
                idx = i
                break
        if idx is None:
            idx = 0
        picked = remaining.pop(idx)
        reordered.append(picked)
        last_chapitre = picked.get('chapitre')
    return reordered

def get_user_weights(conn, prenom):


    row = conn.execute('SELECT weights_json FROM sr_weights_user WHERE prenom=?', (prenom,)).fetchone()
    if row and row['weights_json']:
        try:
            w = json.loads(row['weights_json'])
            if isinstance(w, list) and len(w) == len(DEFAULT_WEIGHTS) and all(isinstance(v, (int, float)) for v in w):
                return w
        except Exception:
            pass
    return list(DEFAULT_WEIGHTS)

def ensure_sr_row(conn, prenom, numero):
    row = conn.execute('SELECT * FROM sr_state_user WHERE prenom=? AND numero=?', (prenom, numero)).fetchone()
    if row is None:
        today = now_paris().date().isoformat()
        conn.execute(
            'INSERT INTO sr_state_user (prenom, numero, stability, difficulty, state, last_review, next_review, repetitions, lapses) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (prenom, numero, None, None, 'new', None, today, 0, 0)
        )
        row = conn.execute('SELECT * FROM sr_state_user WHERE prenom=? AND numero=?', (prenom, numero)).fetchone()
    return row

@app.errorhandler(413)
def too_large(e):
    return jsonify({'ok': False, 'error': 'fichier trop volumineux (max 15 Mo)'}), 413

@app.errorhandler(404)
def page_not_found(e):
    try:
        return render_template('404.html'), 404
    except Exception:

        return make_response('Page introuvable', 404)


@app.route('/')
def home():
    return render_template('index.html', active='tirage')

@app.route('/sr')
def page_sr():
    return render_template('sr.html', active='sr')

@app.route('/liste')
def page_liste():
    return render_template('liste.html', active='liste')

@app.route('/compte')
def page_compte():
    return render_template('compte.html', active='compte')

@app.route('/admin')
def page_admin():
    return render_template('admin.html', active='admin')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/login', methods=['POST'])
def login():
    ip = request.remote_addr or 'unknown'
    if rate_limited('admin_login_' + ip):
        return jsonify({'ok': False, 'error': 'trop de tentatives'}), 429
    data = request.get_json(silent=True) or {}
    candidate = str(data.get('password') or '')
    if hmac.compare_digest(candidate.encode('utf-8'), ADMIN_PASSWORD.encode('utf-8')):
        session['admin'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'bad password'}), 403

QCM_INVITE_TTL_MINUTES = 15

def create_qcm_invite(prenom, ttl_minutes=QCM_INVITE_TTL_MINUTES):

    ensure_db()

    prenom = (prenom or '').strip()
    if not prenom:
        raise ValueError('prenom requis')

    conn = db()

    exists = conn.execute(
        'SELECT 1 FROM users WHERE prenom=?',
        (prenom,)
    ).fetchone()

    if not exists:
        conn.close()
        raise ValueError('prenom inconnu')

    try:
        ttl_minutes = int(ttl_minutes)
    except (TypeError, ValueError):
        ttl_minutes = QCM_INVITE_TTL_MINUTES

    ttl_minutes = max(1, min(120, ttl_minutes))
    now = now_paris()
    expires = now + timedelta(minutes=ttl_minutes)
    token = secrets.token_urlsafe(32)

    conn.execute(
        '''
        INSERT INTO qcm_invites(token, prenom, created_at, expires_at, used_at)
        VALUES(?,?,?,?,NULL)
        ''',
        (token, prenom, now.isoformat(), expires.isoformat())
    )


    conn.execute(
        '''
        DELETE FROM qcm_invites
        WHERE expires_at < ?
           OR (used_at IS NOT NULL AND used_at < ?)
        ''',
        (
            now.isoformat(),
            (now - timedelta(days=1)).isoformat(),
        )
    )

    conn.commit()
    conn.close()

    return token, expires

def get_valid_qcm_invite(token):

    ensure_db()

    conn = db()
    row = conn.execute(
        """
        SELECT token, prenom, expires_at, used_at
        FROM qcm_invites
        WHERE token=?
        """,
        (token,)
    ).fetchone()
    conn.close()

    if not row:
        return None, "Ce lien QCM est introuvable.", 404

    if row['used_at'] is not None:
        return None, "Ce lien QCM a déjà été utilisé.", 410

    try:
        expired = datetime.fromisoformat(row['expires_at']) <= now_paris()
    except (TypeError, ValueError):
        expired = True

    if expired:
        return None, "Ce lien QCM a expiré. Demande un nouveau lien.", 410

    return row, None, 200

@app.route('/qcm/join/<token>', methods=['GET'])
def qcm_join_with_invite(token):

    row, error, status = get_valid_qcm_invite(token)

    if error:
        return render_template(
            'qcm_invite_error.html',
            message=error
        ), status

    return render_template(
        'qcm_invite_confirm.html',
        token=token,
        prenom=row['prenom']
    )

@app.route('/qcm/join/<token>', methods=['POST'])
def consume_qcm_invite(token):

    ensure_db()

    now = now_paris()
    conn = db()
    row = conn.execute(
        """
        SELECT prenom, expires_at, used_at
        FROM qcm_invites
        WHERE token=?
        """,
        (token,)
    ).fetchone()

    if not row:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM est introuvable."
        ), 404

    if row['used_at'] is not None:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM a déjà été utilisé."
        ), 410

    try:
        expired = datetime.fromisoformat(row['expires_at']) <= now
    except (TypeError, ValueError):
        expired = True

    if expired:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM a expiré. Demande un nouveau lien."
        ), 410

    cur = conn.execute(
        """
        UPDATE qcm_invites
        SET used_at=?
        WHERE token=? AND used_at IS NULL
        """,
        (now.isoformat(), token)
    )
    conn.commit()
    conn.close()

    if cur.rowcount != 1:
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM vient déjà d'être utilisé."
        ), 410

    session.clear()
    session['sr_user'] = row['prenom']
    session.permanent = True
    try:
        conn = db()
        log_event(conn, row['prenom'], 'invite_used')
        conn.commit()
        conn.close()
    except Exception:
        pass

    return redirect('/qcm')

@app.route('/api/internal/weekly-stats', methods=['POST'])
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
        except Exception:
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

@app.route('/api/internal/log-event', methods=['POST'])
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

@app.route('/api/internal/qcm-invites', methods=['POST'])
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

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('admin', None)
    return jsonify({'ok': True})

def streak_guard_user(conn, prenom, params):
    today = now_paris().date().isoformat()
    already = conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND day=?', (prenom, today)).fetchone()
    has_review = conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND substr(created_at,1,10)=? LIMIT 1', (prenom, today)).fetchone()
    events = []
    if not already and not has_review:
        deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=?', (prenom,)).fetchone()['c']
        if deck == 0:
            pass
        else:
            max_active = int(params.get('max_active_num', 36))
            due = conn.execute(
                'SELECT COUNT(*) AS c FROM forgecards f JOIN sr_state_user s ON s.numero=f.numero AND s.prenom=? '
                'WHERE f.numero<=? AND (s.next_review IS NULL OR s.next_review<=?)',
                (prenom, max_active, today)).fetchone()['c']
            if due == 0:
                conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)', (prenom, today))
                events.append('validated')
            else:
                jk = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
                if jk and jk['count'] > 0:
                    conn.execute('UPDATE user_jokers SET count=count-1 WHERE prenom=?', (prenom,))
                    conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)', (prenom, today))
                    events.append('joker_spent')
                    log_event(conn, prenom, 'joker_spent')
    jk_before = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    before = jk_before['count'] if jk_before else 0
    streak = compute_streak(conn, prenom)
    jk_after = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    after = jk_after['count'] if jk_after else 0
    if after > before:
        events.append('joker_awarded')
        log_event(conn, prenom, 'joker_awarded', f'streak={streak}')
    return streak, events, after

@app.route('/api/internal/streak-guard', methods=['POST'])
def internal_streak_guard():
    expected = os.environ.get('MADEC_INTERNAL_API_KEY')
    supplied = request.headers.get('X-Madec-Internal-Key', '')
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    ensure_db()
    params = read_params()
    conn = db()
    users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
    results = {}
    for prenom in users:
        try:
            streak, events, jokers = streak_guard_user(conn, prenom, params)
            results[prenom] = {'streak': streak, 'events': events, 'jokers': jokers}
        except Exception:
            continue
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'results': results})

@app.route('/api/sr/login', methods=['POST'])
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

    if not isinstance(emojis, list) or len(emojis) != EMOJI_PW_LENGTH:
        return jsonify({'ok': False, 'error': f'mot de passe invalide ({EMOJI_PW_LENGTH} emoji attendus)'}), 400

    conn = db()
    row = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    stored = row['emoji_password'].split(EMOJI_SEP) if row and row['emoji_password'] else None
    if stored != emojis:
        return jsonify({'ok': False, 'error': 'bad password'}), 403

    session['sr_user'] = prenom
    session.permanent = True
    conn = db()
    log_event(conn, prenom, 'login')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom})

@app.route('/api/sr/dashboard', methods=['GET'])
@require_sr_user
def sr_dashboard():
    ensure_db()
    prenom = session['sr_user']
    today = now_paris().date()
    week_ago = (today - timedelta(days=7)).isoformat()
    conn = db()
    streak = compute_streak(conn, prenom)
    jrow = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    jokers = jrow['count'] if jrow else 0
    total = conn.execute('SELECT COUNT(*) AS c FROM reviews WHERE prenom=?', (prenom,)).fetchone()['c']
    semaine = conn.execute(
        'SELECT COUNT(*) AS c FROM reviews WHERE prenom=? AND substr(created_at,1,10)>=?',
        (prenom, week_ago)).fetchone()['c']
    days = {r[0] for r in conn.execute(
        'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=?', (prenom,)).fetchall()}
    days |= {r[0] for r in conn.execute(
        'SELECT day FROM sr_daily_streak WHERE prenom=? AND validated=1', (prenom,)).fetchall()}
    record, run, prev = 0, 0, None
    for d in sorted(days):
        cur = datetime.strptime(d, '%Y-%m-%d').date()
        if prev and (cur - prev).days == 1:
            run += 1
        else:
            run = 1
        record = max(record, run)
        prev = cur
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'streak': streak, 'record': record,
                    'jokers': jokers, 'semaine': semaine, 'total': total})

@app.route('/api/sr/qcm/errors', methods=['GET'])
@require_sr_user
def sr_qcm_errors():
    # Les 10 dernieres erreurs QCM de l'eleve connecte, avec explication
    # et bonne reponse si elles existent dans le fichier QCM.
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    rows = conn.execute(
        'SELECT qid, theme, chapitre, question, ok, elapsed, created_at '
        'FROM qcm_answers WHERE prenom = ? ORDER BY id DESC LIMIT 10',
        (prenom,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = {
            'qid': r['qid'], 'theme': r['theme'], 'chapitre': r['chapitre'],
            'question': r['question'], 'ok': bool(r['ok']),
            'elapsed': r['elapsed'], 'created_at': r['created_at'],
        }
        path = QCM_FILES.get(r['theme'])
        if path and path.exists():
            for q in qcm_engine.read_qcm_questions(path, theme=r['theme']):
                if q.get('qid') == r['qid']:
                    if q.get('explication'):
                        item['explication'] = q['explication']
                    try:
                        idx = q.get('answer')
                        choix = q.get('choices') or []
                        if idx is not None and 0 <= idx < len(choix):
                            bonne = choix[idx]
                            if isinstance(bonne, dict):
                                item['bonne_reponse'] = bonne.get('texte') or bonne.get('text') or ''
                            else:
                                item['bonne_reponse'] = str(bonne)
                    except Exception:
                        pass
                    break
        out.append(item)
    return jsonify({'ok': True, 'rows': out})

@app.route('/api/sr/qcm/weak', methods=['GET'])
@require_sr_user
def sr_qcm_weak():
    ensure_db()
    prenom = session['sr_user']
    conn = db()
    rows = conn.execute(
        "SELECT question, theme, chapitre, COUNT(*) AS n, SUM(ok) AS bonnes "
        "FROM qcm_answers WHERE prenom=? GROUP BY qid "
        "HAVING n > bonnes ORDER BY (n - bonnes) * 1.0 / n DESC, n DESC LIMIT 10",
        (prenom,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        n = r['n']
        out.append({'question': r['question'], 'theme': r['theme'], 'chapitre': r['chapitre'],
                    'sorties': n, 'echecs': n - (r['bonnes'] or 0),
                    'taux_echec': round(100 * (n - (r['bonnes'] or 0)) / n, 1)})
    return jsonify({'ok': True, 'rows': out})

@app.route('/api/sr/whoami', methods=['GET'])
def sr_whoami():
    prenom = session.get('sr_user')
    if not prenom:
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'prenom': prenom})

@app.route('/api/sr/logout', methods=['POST'])
def sr_logout():
    session.pop('sr_user', None)
    return jsonify({'ok': True})

@app.route('/api/psswrd-keypad', methods=['GET'])
def emoji_keypad():

    return jsonify(EMOJI_KEYPAD)

@app.route('/api/params', methods=['GET'])
def get_params():
    return jsonify(read_params())

@app.route('/api/params', methods=['POST'])
@require_admin
def post_params():
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'params': write_params(data)})

@app.route('/api/chapitres', methods=['GET'])
def get_chapitres():
    return jsonify(CHAPITRES)


@app.route('/api/users/public', methods=['GET'])
def users_public():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT prenom FROM users ORDER BY prenom ASC').fetchall()
    conn.close()
    return jsonify([r['prenom'] for r in rows])

@app.route('/api/users', methods=['GET'])
@require_admin
def users_list():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT prenom, created_at FROM users ORDER BY prenom ASC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/users', methods=['POST'])
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

@app.route('/api/users/<prenom>/password', methods=['GET'])
@require_admin
def user_password(prenom):

    ensure_db()
    conn = db()
    row = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    if not row or not row['emoji_password']:
        return jsonify({'ok': False, 'error': 'aucun mot de passe pour cet utilisateur'}), 404
    return jsonify({'ok': True, 'prenom': prenom, 'password': row['emoji_password'].split(EMOJI_SEP)})

@app.route('/api/users/<prenom>/regen-password', methods=['POST'])
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

@app.route('/api/users/<prenom>', methods=['DELETE'])
@require_admin
def users_delete(prenom):
    ensure_db()
    conn = db()
    conn.execute('DELETE FROM users WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM sr_state_user WHERE prenom=?', (prenom,))

    conn.execute('DELETE FROM reviews WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM sr_daily_streak WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM user_jokers WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM qcm_answers WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM events WHERE prenom=?', (prenom,))
    conn.execute('DELETE FROM qcm_invites WHERE prenom=?', (prenom,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<prenom>/joker', methods=['POST'])
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
    if row and row['count'] >= 2:
        conn.close()
        return jsonify({'ok': False, 'full': True, 'error': prenom + ' a deja 2 jokers, plafond atteint.', 'jokers': row['count']}), 400
    conn.execute(
        "INSERT INTO user_jokers (prenom, count) VALUES (?,1) "
        "ON CONFLICT(prenom) DO UPDATE SET count = count + 1",
        (prenom,))
    row = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'prenom': prenom, 'jokers': row['count']})

@app.route('/api/users/stats', methods=['GET'])
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
            'todo_today': min(max(0, due_by_prenom.get(prenom, 0) - done_by_prenom.get(prenom, 0)),
                              max(0, daily_review_limit - done_by_prenom.get(prenom, 0))),
        })
    return jsonify(stats)

@app.route('/api/users/<prenom>/deck', methods=['GET'])
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

@app.route('/papayou')
def page_papayou():
    return render_template('papayou.html', active='papayou')

@app.route('/api/users/<prenom>/export', methods=['GET'])
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

@app.route('/api/forgecards/stats/export', methods=['GET'])
@require_admin
def forgecards_stats_export():
    ensure_db()
    conn = db()
    cards = conn.execute('SELECT numero, titre, chapitre FROM forgecards ORDER BY numero ASC').fetchall()

    stats_rows = conn.execute(
        "SELECT numero, "
        "COUNT(*) as total_revisions, "
        "COUNT(DISTINCT prenom) as nb_eleves_distincts, "
        "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) as nb_again, "
        "SUM(CASE WHEN result='hard' THEN 1 ELSE 0 END) as nb_hard, "
        "SUM(CASE WHEN result='good' THEN 1 ELSE 0 END) as nb_good, "
        "SUM(CASE WHEN result='easy' THEN 1 ELSE 0 END) as nb_easy, "
        "AVG(duration_seconds) as avg_duration, "
        "MIN(created_at) as premiere_revision, "
        "MAX(created_at) as derniere_revision "
        "FROM reviews GROUP BY numero"
    ).fetchall()
    stats_by_numero = {r['numero']: r for r in stats_rows}

    avg_rows = conn.execute(
        "SELECT numero, AVG(difficulty) as avg_difficulty, AVG(stability) as avg_stability, "
        "SUM(lapses) as total_lapses, COUNT(*) as nb_decks "
        "FROM sr_state_user WHERE difficulty IS NOT NULL GROUP BY numero"
    ).fetchall()
    avg_by_numero = {r['numero']: r for r in avg_rows}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Numéro de la fiche', 'Titre', 'Chapitre', 'Nb élèves différents ayant révisé', 'Nb de révisions',
        'Nb pas réussi', 'Nb difficile', 'Nb j ai dû réfléchir', 'Nb ultra facile',
        'Taux d échec (%)', 'Durée moyenne (secondes)', 'Difficulté moyenne', 'Stabilité moyenne', 'Total d oublie cumulés (implique recommencer FSRS)',
        'Première révision en date', 'Dernière révision en date'
    ])
    for c in cards:
        numero = c['numero']
        s = stats_by_numero.get(numero)
        a = avg_by_numero.get(numero)
        total = s['total_revisions'] if s else 0
        nb_again = s['nb_again'] if s else 0
        taux_echec = round(100 * nb_again / total, 1) if total else ''
        writer.writerow([
            numero, csv_safe(c['titre'] or ''), csv_safe(c['chapitre'] or ''),
            s['nb_eleves_distincts'] if s else 0,
            total, nb_again,
            s['nb_hard'] if s else 0,
            s['nb_good'] if s else 0,
            s['nb_easy'] if s else 0,
            taux_echec,
            round(s['avg_duration'], 0) if s and s['avg_duration'] is not None else '',
            round(a['avg_difficulty'], 2) if a and a['avg_difficulty'] is not None else '',
            round(a['avg_stability'], 2) if a and a['avg_stability'] is not None else '',
            a['total_lapses'] if a else 0,
            s['premiere_revision'] if s else '',
            s['derniere_revision'] if s else '',
        ])

    return csv_response(output, 'Statistiques_moyennes_par_fiche.csv')

@app.route('/api/dashboard/weak-cards', methods=['GET'])
@require_admin
def dashboard_weak_cards():

    ensure_db()
    chapitre_filter = request.args.get('chapitre')
    conn = db()
    query = (
        "SELECT f.numero, f.titre, f.chapitre, "
        "COALESCE(rv.total_revisions, 0) as total_revisions, "
        "COALESCE(rv.nb_eleves, 0) as nb_eleves, "
        "COALESCE(rv.nb_again, 0) as nb_again, "
        "rv.avg_duration as avg_duration, "
        "s.avg_difficulty as avg_difficulty "
        "FROM forgecards f "
        "LEFT JOIN ("
        "SELECT numero, COUNT(*) as total_revisions, COUNT(DISTINCT prenom) as nb_eleves, "
        "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) as nb_again, "
        "AVG(duration_seconds) as avg_duration "
        "FROM reviews WHERE prenom != 'admin' GROUP BY numero"
        ") rv ON rv.numero = f.numero "
        "LEFT JOIN ("
        "SELECT numero, AVG(difficulty) as avg_difficulty "
        "FROM sr_state_user WHERE difficulty IS NOT NULL AND prenom != 'admin' GROUP BY numero"
        ") s ON s.numero = f.numero "
    )
    params_sql = []
    if chapitre_filter:
        query += "WHERE f.chapitre = ? "
        params_sql.append(chapitre_filter)
    query += "ORDER BY f.numero ASC"
    rows = conn.execute(query, params_sql).fetchall()
    conn.close()

    out = []
    for r in rows:
        total = r['total_revisions'] or 0
        taux_echec = round(100 * (r['nb_again'] or 0) / total, 1) if total else 0
        out.append({
            'numero': r['numero'], 'titre': r['titre'], 'chapitre': r['chapitre'],
            'total_revisions': total, 'nb_eleves': r['nb_eleves'] or 0,
            'taux_echec_pct': taux_echec,
            'avg_duration_seconds': round(r['avg_duration'], 0) if r['avg_duration'] is not None else None,
            'avg_difficulty': round(r['avg_difficulty'], 2) if r['avg_difficulty'] is not None else None,
        })
    out.sort(key=lambda c: (-c['taux_echec_pct'], -c['total_revisions']))
    return jsonify(out)

@app.route('/api/dashboard/failure-notes', methods=['GET'])
@require_admin
def dashboard_failure_notes():

    ensure_db()
    conn = db()
    rows = conn.execute(
        "SELECT rv.id, rv.created_at, rv.numero, f.titre, rv.prenom, rv.note, rv.note_masquee "
        "FROM reviews rv LEFT JOIN forgecards f ON f.numero = rv.numero "
        "WHERE rv.result = 'again' AND rv.note != '' "
        "ORDER BY rv.created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/reviews/<int:review_id>/note', methods=['DELETE'])
@require_admin
def delete_failure_note(review_id):
    ensure_db()
    conn = db()
    cur = conn.execute("UPDATE reviews SET note='' WHERE id=?", (review_id,))
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        return jsonify({'ok': False, 'error': 'note introuvable'}), 404
    return jsonify({'ok': True})


def _set_note_masquee(review_id, valeur):
    ensure_db()
    conn = db()
    cur = conn.execute("UPDATE reviews SET note_masquee=? WHERE id=?", (valeur, review_id))
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        return jsonify({'ok': False, 'error': 'note introuvable'}), 404
    return jsonify({'ok': True})


@app.route('/api/reviews/<int:review_id>/note/masquer', methods=['POST'])
@require_admin
def masquer_failure_note(review_id):
    return _set_note_masquee(review_id, 1)

@app.route('/api/reviews/<int:review_id>/note/restaurer', methods=['POST'])
@require_admin
def restaurer_failure_note(review_id):
    return _set_note_masquee(review_id, 0)

@app.route('/api/forgecards', methods=['GET'])
@require_admin
def list_forgecards():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT numero, code, fiche_file, correction_file, bareme_file, titre, indices, chapitre, teacher_difficulty, hors_serie, created_at FROM forgecards ORDER BY numero ASC').fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    for c in out:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    return jsonify(out)

@app.route('/api/forgecards/public')
def list_forgecards_public():
    ensure_db()
    q = (request.args.get('q') or '').strip().lower()
    params = read_params()
    max_active = int(params.get('max_active_num', 36))
    max_hs = int(params.get('max_hors_serie_num', 0))
    conn = db()
    rows = conn.execute(
        "SELECT numero, code, titre, chapitre, teacher_difficulty, fiche_file, correction_file, bareme_file, indices, hors_serie FROM forgecards WHERE hors_serie = 0 AND numero <= ? ORDER BY numero ASC",
        (max_active,)
    ).fetchall()
    if max_hs > 0:
        rows += conn.execute(
            "SELECT numero, code, titre, chapitre, teacher_difficulty, fiche_file, correction_file, bareme_file, indices, hors_serie FROM forgecards WHERE hors_serie = 1 ORDER BY numero ASC LIMIT ?",
            (max_hs,)
        ).fetchall()
    conn.close()
    cards = [dict(r) for r in rows]
    for c in cards:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    if q:
        def match(c):
            label = (c.get('label') or '').lower()
            return (q in (c.get('titre') or '').lower()
                    or q == str(c.get('numero'))
                    or q == label
                    or (c.get('hors_serie') and q == label.lstrip('h'))
                    or q in (c.get('chapitre') or '').lower())
        cards = [c for c in cards if match(c)]
    return jsonify(cards)

@app.route('/api/forgecards/upload', methods=['POST'])
@require_admin
def upload_forgecard():
    ensure_db()
    numero = (request.form.get('numero') or '').strip()
    titre = (request.form.get('titre') or '').strip()
    indices = (request.form.get('indices') or '').strip()
    chapitre = (request.form.get('chapitre') or 'Autre').strip()
    fiche = request.files.get('fiche_pdf')
    correction = request.files.get('correction_pdf')
    bareme = request.files.get('bareme_pdf')
    if not fiche or not correction:
        return jsonify({'ok': False, 'error': 'missing data'}), 400
    if not allowed_file(fiche.filename) or not allowed_file(correction.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if bareme and not allowed_file(bareme.filename):
        return jsonify({'ok': False, 'error': 'bareme pdf only'}), 400
    if chapitre not in CHAPITRES:
        chapitre = 'Autre'
    teacher_difficulty = parse_teacher_difficulty(request.form.get('difficulty'))
    hors_serie = 1 if (request.form.get('hors_serie') or '') == '1' else 0
    raw_num = (numero or '').strip().lower()
    hs_target = raw_num if (raw_num.startswith('h') and raw_num[1:].isdigit()) else None
    if hs_target:
        hors_serie = 1
    if not numero.isdigit():
        if not hors_serie:
            return jsonify({'ok': False, 'error': 'numero requis'}), 400
        numero = None
    else:
        numero = int(numero)
    conn = db()
    try:
        if hs_target:
            row_t = conn.execute("SELECT numero FROM forgecards WHERE code = ? AND hors_serie = 1", (hs_target,)).fetchone()
            if not row_t:
                return jsonify({'ok': False, 'error': 'fiche ' + hs_target.upper() + ' introuvable'}), 404
            numero = row_t['numero']
            code = hs_target
        elif hors_serie:
            existing_codes = [r[0] for r in conn.execute("SELECT code FROM forgecards WHERE hors_serie = 1").fetchall()]
            next_idx = max([hs_sort_key(c) for c in existing_codes] + [0]) + 1
            code = f'h{next_idx}'
            if numero is None:
                row_max = conn.execute("SELECT MAX(numero) AS m FROM forgecards").fetchone()
                numero = max(9001, (row_max['m'] or 0) + 1)
        else:
            code = str(numero)
        fiche_name, corr_name, bareme_name = filenames_for(code)
        existing = conn.execute('SELECT fiche_file, correction_file, bareme_file FROM forgecards WHERE numero=?', (numero,)).fetchone()
        if existing:
            for name in [existing['fiche_file'], existing['correction_file'], existing['bareme_file']]:
                remove_pdf_if_exists(name)
        fiche.save(UPLOADS / fiche_name)
        correction.save(UPLOADS / corr_name)
        final_bareme_name = ''
        if bareme:
            bareme.save(UPLOADS / bareme_name)
            final_bareme_name = bareme_name
        elif existing and existing['bareme_file']:
            old_bareme_path = UPLOADS / existing['bareme_file']
            if old_bareme_path.exists():
                final_bareme_name = existing['bareme_file']
        conn.execute(
            'INSERT INTO forgecards(numero, code, fiche_file, correction_file, bareme_file, titre, indices, chapitre, teacher_difficulty, hors_serie) '
            'VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(numero) DO UPDATE SET '
            'code=excluded.code, fiche_file=excluded.fiche_file, correction_file=excluded.correction_file, '
            'bareme_file=excluded.bareme_file, titre=excluded.titre, indices=excluded.indices, chapitre=excluded.chapitre, teacher_difficulty=excluded.teacher_difficulty, hors_serie=excluded.hors_serie',
            (numero, code, fiche_name, corr_name, final_bareme_name, titre, indices, chapitre, teacher_difficulty, hors_serie)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'numero': numero, 'code': code, 'label': card_label(code, hors_serie)})

@app.route('/api/forgecards/<int:numero>', methods=['DELETE'])
@require_admin
def delete_forgecard(numero):
    ensure_db()
    conn = db()
    row = conn.execute('SELECT fiche_file, correction_file, bareme_file FROM forgecards WHERE numero=?', (numero,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'not found'}), 404
    conn.execute('DELETE FROM forgecards WHERE numero=?', (numero,))
    conn.execute('DELETE FROM sr_state_user WHERE numero=?', (numero,))

    conn.execute('DELETE FROM reviews WHERE numero=?', (numero,))
    conn.execute('DELETE FROM draw_history WHERE numero=?', (numero,))
    conn.commit()
    conn.close()
    for name in [row['fiche_file'], row['correction_file'], row['bareme_file']]:
        remove_pdf_if_exists(name)
    return jsonify({'ok': True})

@app.route('/api/forgecards/<int:numero>', methods=['PATCH'])
@require_admin
def edit_forgecard(numero):
    ensure_db()
    titre = (request.form.get('titre') or '').strip()
    indices = (request.form.get('indices') or '').strip()
    chapitre = (request.form.get('chapitre') or 'Autre').strip()
    remove_bareme = (request.form.get('remove_bareme') or '') == '1'
    remove_correction = (request.form.get('remove_correction') or '') == '1'
    fiche = request.files.get('fiche_pdf')
    correction = request.files.get('correction_pdf')
    bareme = request.files.get('bareme_pdf')
    if chapitre not in CHAPITRES:
        chapitre = 'Autre'
    teacher_difficulty = parse_teacher_difficulty(request.form.get('difficulty'))
    if fiche and not allowed_file(fiche.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if correction and not allowed_file(correction.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if bareme and not allowed_file(bareme.filename):
        return jsonify({'ok': False, 'error': 'bareme pdf only'}), 400
    if correction and remove_correction:
        return jsonify({'ok': False, 'error': 'choisis soit remplacer soit retirer la correction, pas les deux'}), 400
    hors_serie_edit = 1 if (request.form.get('hors_serie') or '') == '1' else 0
    conn = db()
    try:
        row = conn.execute('SELECT fiche_file, correction_file, bareme_file, code, hors_serie FROM forgecards WHERE numero=?', (numero,)).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        code = row['code'] or str(numero)
        if hors_serie_edit and not row['hors_serie']:
            existing_codes = [r[0] for r in conn.execute("SELECT code FROM forgecards WHERE hors_serie = 1").fetchall()]
            code = f"h{max([hs_sort_key(c) for c in existing_codes] + [0]) + 1}"
        elif not hors_serie_edit and row['hors_serie']:
            code = str(numero)
        if code != (row['code'] or str(numero)):
            for old_name, new_name in zip(
                    [row['fiche_file'], row['correction_file'], row['bareme_file']],
                    filenames_for(code)):
                old_path = UPLOADS / old_name if old_name else None
                if old_name and old_name != new_name and old_path.exists():
                    old_path.replace(UPLOADS / new_name)
            conn.execute(
                'UPDATE forgecards SET fiche_file=?, correction_file=?, bareme_file=? WHERE numero=?',
                (filenames_for(code)[0],
                 filenames_for(code)[1] if row['correction_file'] else '',
                 filenames_for(code)[2] if row['bareme_file'] else '',
                 numero))
            row = conn.execute('SELECT fiche_file, correction_file, bareme_file, code, hors_serie FROM forgecards WHERE numero=?', (numero,)).fetchone()
        fiche_name, corr_name, bareme_name = filenames_for(code)
        final_fiche_name = row['fiche_file']
        final_corr_name = row['correction_file']
        final_bareme_name = row['bareme_file']
        if fiche:
            fiche.save(UPLOADS / fiche_name)
            final_fiche_name = fiche_name
        if correction:
            correction.save(UPLOADS / corr_name)
            final_corr_name = corr_name
        elif remove_correction:
            remove_pdf_if_exists(row['correction_file'])
            final_corr_name = ''
        if bareme:
            bareme.save(UPLOADS / bareme_name)
            final_bareme_name = bareme_name
        elif remove_bareme:
            remove_pdf_if_exists(row['bareme_file'])
            final_bareme_name = ''
        conn.execute(
            'UPDATE forgecards SET titre=?, indices=?, chapitre=?, fiche_file=?, correction_file=?, bareme_file=?, teacher_difficulty=?, hors_serie=?, code=? WHERE numero=?',
            (titre, indices, chapitre, final_fiche_name, final_corr_name, final_bareme_name, teacher_difficulty,
             hors_serie_edit, code, numero)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'numero': numero, 'code': code, 'label': card_label(code, hors_serie_edit)})


def compute_streak(conn, prenom):
    rows = conn.execute(
        'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=?', (prenom,)
    ).fetchall()
    days = {r[0] for r in rows}
    rows = conn.execute(
        'SELECT day FROM sr_daily_streak WHERE prenom=? AND validated=1', (prenom,)
    ).fetchall()
    days |= {r[0] for r in rows}
    if not days:
        return 0
    cursor = now_paris().date()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    if cursor.isoformat() not in days:


        jrow = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
        if jrow and jrow['count'] > 0:
            conn.execute('UPDATE user_jokers SET count = count - 1 WHERE prenom=?', (prenom,))
            conn.execute('INSERT OR IGNORE INTO sr_daily_streak (prenom, day, validated) VALUES (?,?,1)',
                         (prenom, cursor.isoformat()))
            conn.commit()
            days.add(cursor.isoformat())
        else:

            conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=?", (prenom,))
            conn.commit()
            return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)


    if streak >= 8:
        milestone = streak // 8
        jrow = conn.execute('SELECT count, last_milestone FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
        current = jrow['count'] if jrow else 0
        last_ms = jrow['last_milestone'] if jrow else 0
        if milestone > last_ms:
            gained = max(0, min(milestone - last_ms, 2 - current))
            conn.execute(
                "INSERT INTO user_jokers (prenom, count, last_milestone) VALUES (?,?,?) "
                "ON CONFLICT(prenom) DO UPDATE SET count=count+?, last_milestone=?",
                (prenom, gained, milestone, gained, milestone))
            conn.commit()
    return streak

@app.route('/api/sr/today', methods=['GET'])
@require_sr_user
def sr_today():
    ensure_db()
    prenom = session['sr_user']
    try:
        conn0 = db()
        log_event(conn0, prenom, 'sr_open')
        conn0.commit()
        conn0.close()
    except Exception:
        pass
    today = now_paris().date().isoformat()
    params = read_params()
    max_active = int(params.get('max_active_num', 36))
    new_limit = int(params.get('daily_new_limit', 3))
    review_limit = int(params.get('daily_review_limit', 3))

    conn = db()
    all_cards = conn.execute('SELECT numero FROM forgecards WHERE numero <= ? AND hors_serie = 0', (max_active,)).fetchall()
    for c in all_cards:
        ensure_sr_row(conn, prenom, c['numero'])
    conn.commit()

    done_today = conn.execute(
        "SELECT numero, was_new FROM reviews WHERE prenom=? AND substr(created_at,1,10)=?",
        (prenom, today)
    ).fetchall()
    new_done_today = sum(1 for r in done_today if r['was_new'])
    review_done_today = sum(1 for r in done_today if not r['was_new'])
    new_slots_left = max(0, new_limit - new_done_today)
    review_slots_left = max(0, review_limit - review_done_today)

    rows = conn.execute(
        'SELECT f.numero, f.fiche_file, f.correction_file, f.bareme_file, f.titre, f.indices, f.chapitre, '
        's.difficulty, s.stability, s.next_review, s.last_review, s.repetitions '
        'FROM forgecards f '
        'JOIN sr_state_user s ON s.numero = f.numero AND s.prenom = ? '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY f.numero ASC',
        (prenom, max_active)
    ).fetchall()
    streak = compute_streak(conn, prenom)
    conn.close()

    due = [dict(r) for r in rows if (r['next_review'] is None or r['next_review'] <= today)]
    due.sort(key=lambda c: (c.get('next_review') or '', -(c.get('difficulty') or 5)))

    new_due = [c for c in due if c.get('repetitions', 0) == 0]
    review_due = [c for c in due if c.get('repetitions', 0) > 0]

    selected_new = new_due[:new_slots_left]
    selected_review = review_due[:review_slots_left]
    selected = selected_review + selected_new


    interleaved = interleave_by_chapitre(selected)
    for chosen in interleaved:
        chosen['was_new'] = chosen.get('repetitions', 0) == 0

    tomorrow = (now_paris().date() + timedelta(days=1)).isoformat()
    tomorrow_cards = [c for c in [dict(r) for r in rows] if c.get('next_review') == tomorrow]
    nb_new_tomorrow = sum(1 for c in tomorrow_cards if c.get('repetitions', 0) == 0)
    nb_review_tomorrow = sum(1 for c in tomorrow_cards if c.get('repetitions', 0) > 0)
    tomorrow_forecast = {
        'total': nb_new_tomorrow + nb_review_tomorrow,
        'nb_new': nb_new_tomorrow,
        'nb_review': nb_review_tomorrow,
    }
    auto_validated_streak = len(due) == 0 and len(all_cards) > 0
    conn = db()
    jrow = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    jokers = jrow['count'] if jrow else 0

    return jsonify({
        'cards': interleaved,
        'streak': streak,
        'new_done_today': new_done_today,
        'new_limit': new_limit,
        'review_done_today': review_done_today,
        'review_limit': review_limit,
        'new_remaining_pool': max(0, len(new_due) - len(selected_new)),
        'review_remaining_pool': max(0, len(review_due) - len(selected_review)),
        'tomorrow_forecast': tomorrow_forecast,
        'auto_validated_streak': auto_validated_streak,
        'jokers': jokers,
    })

@app.route('/api/sr/<int:numero>/preview', methods=['GET'])
@require_sr_user
def sr_preview(numero):
    ensure_db()
    prenom = session['sr_user']
    now = now_paris()
    params = read_params()
    retention = float(params.get('fsrs_retention', 0.90))
    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    row = ensure_sr_row(conn, prenom, numero)
    prof_row = conn.execute('SELECT teacher_difficulty FROM forgecards WHERE numero=?', (numero,)).fetchone()
    prof_difficulty = prof_row['teacher_difficulty'] if prof_row else None
    weights = get_user_weights(conn, prenom)
    conn.close()


    preview = preview_all_grades(row['stability'], row['difficulty'], row['last_review'], now, retention,
                                 w=weights, prof_difficulty=prof_difficulty)
    for label, info in preview.items():
        info['label'] = humanize_interval(info['interval_days'])

    return jsonify({'numero': numero, 'preview': preview})

@app.route('/api/sr/<int:numero>/review', methods=['POST'])
@require_sr_user
def sr_review(numero):
    ensure_db()
    prenom = session['sr_user']
    data = request.get_json(silent=True) or {}
    result = data.get('result')
    note = (data.get('note') or '').strip()[:MAX_NOTE_LENGTH]
    duration = parse_duration_seconds(data.get('duration_seconds'))

    GRADE_MAP = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}
    grade = GRADE_MAP.get(result)
    if grade is None:
        return jsonify({'ok': False, 'error': 'result invalide: again/hard/good/easy'}), 400
    if grade == 1 and len(note) < 10:
        return jsonify({'ok': False, 'error': "Decris ton blocage (10 caracteres min) : items du bareme rates, endroit ou tu as bloque..."}), 400

    now = now_paris()
    params = read_params()
    retention = float(params.get('fsrs_retention', 0.90))

    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    prof_row = conn.execute('SELECT teacher_difficulty FROM forgecards WHERE numero=?', (numero,)).fetchone()
    prof_difficulty = prof_row['teacher_difficulty'] if prof_row else None
    row = ensure_sr_row(conn, prenom, numero)
    was_new = 1 if (row['repetitions'] or 0) == 0 else 0
    reps = (row['repetitions'] or 0) + 1
    lapses = (row['lapses'] or 0) + (1 if grade == 1 else 0)
    weights = get_user_weights(conn, prenom)
    r = apply_review(row['stability'], row['difficulty'], row['last_review'], grade, now, retention,
                     w=weights, prof_difficulty=prof_difficulty)

    conn.execute(
        'UPDATE sr_state_user SET stability=?, difficulty=?, state=?, last_review=?, next_review=?, '
        'repetitions=?, lapses=?, last_retrievability=? WHERE prenom=? AND numero=?',
        (r['stability'], r['difficulty'], r['state'], now.isoformat(), r['next_review'],
         reps, lapses, r['retrievability_before'], prenom, numero)
    )
    conn.execute(
        'INSERT INTO reviews(numero, prenom, result, note, duration_seconds, was_new, created_at) VALUES(?,?,?,?,?,?,?)',
        (numero, prenom, result, note, duration, was_new, now.isoformat())
    )
    log_event(conn, prenom, 'review', f'{numero}:{result}')
    conn.commit()
    streak = compute_streak(conn, prenom)
    conn.close()
    return jsonify({'ok': True, 'numero': numero, **r, 'repetitions': reps, 'lapses': lapses, 'streak': streak})

@app.route('/api/sr/<int:numero>/advance', methods=['POST'])
@require_sr_user
def sr_advance(numero):
    ensure_db()
    prenom = session['sr_user']
    data = request.get_json(silent=True) or {}
    try:
        days = max(1, min(30, int(data.get('days', 1))))
    except (TypeError, ValueError):
        days = 1
    conn = db()
    if not card_exists(conn, numero):
        conn.close()
        return jsonify({'ok': False, 'error': 'fiche introuvable'}), 404
    row = ensure_sr_row(conn, prenom, numero)
    base_date = now_paris().date()
    if row['next_review']:
        try:
            base_date = max(base_date, datetime.strptime(row['next_review'], '%Y-%m-%d').date())
        except Exception:
            pass
    new_next = (base_date + timedelta(days=days)).isoformat()
    conn.execute('UPDATE sr_state_user SET next_review=? WHERE prenom=? AND numero=?', (new_next, prenom, numero))
    log_event(conn, prenom, 'advance', f'{numero}:+{days}j')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'numero': numero, 'next_review': new_next})

@app.route('/api/admin/stats/overview', methods=['GET'])
@require_admin
def admin_stats_overview():
    ensure_db()
    conn = db()
    today = now_paris().date()
    eight_weeks_ago = (today - timedelta(days=56)).isoformat()

    users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin' ORDER BY prenom").fetchall()]
    active_days = {}
    for r in conn.execute(
            "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n FROM events "
            "WHERE type IN ('login','sr_open','review') AND created_at >= ? GROUP BY prenom, d",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['d'])
    for r in conn.execute(
            "SELECT prenom, substr(created_at,1,10) AS d FROM reviews WHERE created_at >= ? GROUP BY prenom, d",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['d'])
    for r in conn.execute(
            "SELECT prenom, day FROM sr_daily_streak WHERE validated=1 AND day >= ?",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['day'])
    weeks = [(today - timedelta(days=7 * k)).isoformat() for k in range(8)][::-1]
    assiduite = []
    for p in users:
        days = active_days.get(p, set())
        row = {'prenom': p, 'total': len(days)}
        for wk in weeks:
            w0 = datetime.strptime(wk, '%Y-%m-%d').date()
            row[wk] = sum(1 for d in days if w0 - timedelta(days=6) <= datetime.strptime(d, '%Y-%m-%d').date() <= w0)
        assiduite.append(row)

    heures = [0] * 24
    for r in conn.execute(
            "SELECT CAST(substr(created_at,12,2) AS INTEGER) AS h, COUNT(*) AS n "
            "FROM reviews GROUP BY h").fetchall():
        if r['h'] is not None:
            heures[r['h']] = r['n']

    qcm_mois = [dict(r) for r in conn.execute(
        "SELECT substr(created_at,1,7) AS mois, chapitre, COUNT(*) AS n, SUM(ok) AS bonnes "
        "FROM qcm_answers GROUP BY mois, chapitre ORDER BY mois, chapitre").fetchall()]

    ret = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok "
        "FROM reviews WHERE was_new = 0").fetchone()
    retention_reelle = round(100 * (ret['ok'] or 0) / ret['n'], 1) if ret['n'] else None

    two_months_ago = (today - timedelta(days=60)).isoformat()
    ret_rows = conn.execute(
        "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n, "
        "SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok "
        "FROM reviews WHERE was_new = 0 AND created_at >= ? "
        "GROUP BY d ORDER BY d",
        (two_months_ago,)
    ).fetchall()
    retention_jours = [
        {'d': r['d'], 'n': r['n'], 'taux': round(100 * (r['ok'] or 0) / r['n'], 1)}
        for r in ret_rows if r['n']
    ]

    conn.close()
    return jsonify({'ok': True, 'semaines': weeks, 'assiduite': assiduite,
                    'heures': heures, 'qcm_mois': qcm_mois,
                    'retention_reelle': retention_reelle,
                    'retention_jours': retention_jours})

@app.route('/api/admin/stats/export/<kind>', methods=['GET'])
@require_admin
def admin_stats_export(kind):
    ensure_db()
    conn = db()
    output = io.StringIO()
    writer = csv.writer(output)

    if kind == 'activite-quotidienne':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        rev = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n, "
                "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) AS again, "
                "SUM(CASE WHEN result='hard' THEN 1 ELSE 0 END) AS hard, "
                "SUM(CASE WHEN result='good' THEN 1 ELSE 0 END) AS good, "
                "SUM(CASE WHEN result='easy' THEN 1 ELSE 0 END) AS easy, "
                "SUM(duration_seconds) AS duree "
                "FROM reviews GROUP BY prenom, d").fetchall():
            rev[(r['prenom'], r['d'])] = r
        ev = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, type, COUNT(*) AS n "
                "FROM events GROUP BY prenom, d, type").fetchall():
            ev.setdefault((r['prenom'], r['d']), {})[r['type']] = r['n']
        qcm = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n, SUM(ok) AS bonnes "
                "FROM qcm_answers GROUP BY prenom, d").fetchall():
            qcm[(r['prenom'], r['d'])] = r
        writer.writerow(['Eleve', 'Date', 'Revisions', 'Echecs', 'Difficiles', 'Reussies', 'Automatiques',
                         'Duree totale (s)', 'Connexions', 'Ouvertures SR', 'Tirages', 'Reports',
                         'Jokers depenses', 'Relances recues', 'Reponses QCM', 'Bonnes QCM'])
        jours = sorted({d for (p, d) in rev} | {d for (p, d) in ev} | {d for (p, d) in qcm})
        for p in users:
            for d in jours:
                rv, e, q = rev.get((p, d)), ev.get((p, d), {}), qcm.get((p, d))
                if rv is None and not e and q is None:
                    continue
                writer.writerow([csv_safe(p), d,
                                 rv['n'] if rv else 0, rv['again'] if rv else 0,
                                 rv['hard'] if rv else 0, rv['good'] if rv else 0,
                                 rv['easy'] if rv else 0,
                                 rv['duree'] if rv and rv['duree'] else '',
                                 e.get('login', 0), e.get('sr_open', 0), e.get('draw', 0),
                                 e.get('advance', 0), e.get('joker_spent', 0), e.get('reminder', 0),
                                 q['n'] if q else 0, (q['bonnes'] or 0) if q else 0])
        conn.close()
        return csv_response(output, 'activite_quotidienne.csv')

    if kind == 'qcm-par-mois':
        writer.writerow(['Mois', 'Theme', 'Chapitre', 'Question', 'Sorties', 'Bonnes', 'Taux echec (%)'])
        for r in conn.execute(
                "SELECT substr(created_at,1,7) AS mois, theme, chapitre, question, "
                "COUNT(*) AS n, SUM(ok) AS bonnes FROM qcm_answers "
                "GROUP BY mois, qid ORDER BY mois, theme, chapitre").fetchall():
            n = r['n']
            writer.writerow([r['mois'], csv_safe(r['theme'] or ''), csv_safe(r['chapitre'] or ''),
                             csv_safe(r['question'] or '')[:120], n, r['bonnes'] or 0,
                             round(100 * (n - (r['bonnes'] or 0)) / n, 1) if n else ''])
        conn.close()
        return csv_response(output, 'qcm_par_mois.csv')

    if kind == 'synthese-eleves':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        writer.writerow(['Eleve', 'Jours actifs', 'Plus longue serie', 'Serie en cours',
                         'Revisions totales', 'Taux reussite (%)', 'Temps total (min)',
                         'Jokers gagnes', 'Jokers depenses', 'Relances recues',
                         'Parties QCM jouees', 'Reponses QCM', 'Taux reussite QCM (%)'])
        for p in users:
            days = {r[0] for r in conn.execute(
                'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=?', (p,)).fetchall()}
            days |= {r[0] for r in conn.execute(
                'SELECT day FROM sr_daily_streak WHERE prenom=? AND validated=1', (p,)).fetchall()}
            record, run, prev = 0, 0, None
            for d in sorted(days):
                cur = datetime.strptime(d, '%Y-%m-%d').date()
                run = run + 1 if prev and (cur - prev).days == 1 else 1
                record = max(record, run)
                prev = cur
            streak = compute_streak(conn, p)
            rv = conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok, "
                "SUM(duration_seconds) AS duree FROM reviews WHERE prenom=?", (p,)).fetchone()
            ev = {r['type']: r['n'] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM events WHERE prenom=? GROUP BY type", (p,)).fetchall()}
            q = conn.execute(
                "SELECT COUNT(*) AS n, SUM(ok) AS bonnes FROM qcm_answers WHERE prenom=?", (p,)).fetchone()
            writer.writerow([csv_safe(p), len(days), record, streak,
                             rv['n'], round(100 * (rv['ok'] or 0) / rv['n'], 1) if rv['n'] else '',
                             round((rv['duree'] or 0) / 60, 1) if rv['duree'] else '',
                             ev.get('joker_awarded', 0), ev.get('joker_spent', 0), ev.get('reminder', 0),
                             ev.get('qcm_played', 0), q['n'],
                             round(100 * (q['bonnes'] or 0) / q['n'], 1) if q['n'] else ''])
        conn.close()
        return csv_response(output, 'synthese_eleves.csv')

    if kind == 'etude-revisions':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        writer.writerow(['Eleve', 'Date', 'Numero fiche', 'Chapitre', 'Resultat', 'Duree (s)',
                         'Jours depuis la revision precedente', 'Retrievabilite FSRS avant (%)',
                         'Stabilite apres (j)', 'Difficulte apres', 'N-ieme revision de la fiche'])
        for p in users:
            rows = conn.execute(
                'SELECT rv.created_at, rv.numero, f.chapitre, rv.result, rv.duration_seconds '
                'FROM reviews rv LEFT JOIN forgecards f ON f.numero = rv.numero '
                'WHERE rv.prenom = ? ORDER BY rv.created_at ASC, rv.id ASC', (p,)).fetchall()
            for r, snap in zip(rows, replay_reviews(conn, p, rows)):
                writer.writerow([csv_safe(p), r['created_at'], r['numero'], csv_safe(r['chapitre'] or ''),
                                 r['result'], r['duration_seconds'] if r['duration_seconds'] is not None else '',
                                 snap['elapsed_days'] if snap['elapsed_days'] is not None else '',
                                 round(100 * snap['r_before'], 1) if snap['r_before'] is not None else '',
                                 round(snap['stability'], 2) if snap['stability'] is not None else '',
                                 round(snap['difficulty'], 2) if snap['difficulty'] is not None else '',
                                 snap['repetitions']])
        conn.close()
        return csv_response(output, 'etude_revisions.csv')

    conn.close()
    return jsonify({'ok': False, 'error': 'export inconnu'}), 404

@app.route('/api/sr/export', methods=['GET'])
@require_admin
def sr_export():
    ensure_db()
    max_active = int(read_params().get('max_active_num', 36))
    conn = db()
    rows = conn.execute(
        'SELECT s.prenom, f.code AS numero, s.difficulty, s.stability, s.last_review, s.next_review, s.repetitions, s.lapses, s.last_retrievability '
        'FROM sr_state_user s JOIN forgecards f ON f.numero = s.numero '
        'WHERE f.numero <= ? AND f.hors_serie = 0 '
        'ORDER BY s.prenom ASC, s.numero ASC',
        (max_active,)
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Prénom', 'Numéro', 'Difficulté', 'Stabilité', 'Dernière révision', 'Prochaine révision', 'Répétitions', "Nombres d'oublie (implique recommencer FSRS)", 'Dernière retention testée'])
    for r in rows:
        writer.writerow([csv_safe(r['prenom']), r['numero'], r['difficulty'], r['stability'], r['last_review'], r['next_review'], r['repetitions'], r['lapses'], r['last_retrievability']])
    return csv_response(output, 'Statistiques_FSRS.csv')

@app.route('/api/sr/train', methods=['POST'])
@require_admin
def sr_train():

    from fsrs import build_sequences_from_reviews, train_weights, MIN_REVIEWS_FOR_OPTIMIZATION
    ensure_db()
    conn = db()
    users = [r['prenom'] for r in conn.execute('SELECT prenom FROM users').fetchall()]
    results = {}
    for prenom in users:
        rows = conn.execute(
            "SELECT numero, result, created_at FROM reviews WHERE prenom=? ORDER BY created_at ASC",
            (prenom,)
        ).fetchall()
        seqs = build_sequences_from_reviews(rows)
        weights, n, loss_before, loss_after = train_weights(seqs, get_user_weights(conn, prenom))
        if n >= MIN_REVIEWS_FOR_OPTIMIZATION and loss_after < loss_before:
            conn.execute(
                'INSERT INTO sr_weights_user (prenom, weights_json, nb_reviews_used, loss_before, loss_after, trained_at) '
                'VALUES (?,?,?,?,?,?) '
                'ON CONFLICT(prenom) DO UPDATE SET weights_json=excluded.weights_json, nb_reviews_used=excluded.nb_reviews_used, '
                'loss_before=excluded.loss_before, loss_after=excluded.loss_after, trained_at=excluded.trained_at',
                (prenom, json.dumps(weights), n, loss_before, loss_after, now_paris().isoformat())
            )
            results[prenom] = {'reviews_used': n, 'loss_before': round(loss_before, 4), 'loss_after': round(loss_after, 4)}
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'results': results})

@app.route("/api/forgecards/<int:numero>/reset-stats", methods=["POST"])
@require_admin
def reset_forgecard_stats(numero):
    ensure_db()
    conn = db()

    exists = conn.execute(
        "SELECT 1 FROM forgecards WHERE numero = ?",
        (numero,),
    ).fetchone()

    if not exists:
        conn.close()
        return jsonify(ok=False, error="fiche introuvable"), 404

    conn.execute(
        "DELETE FROM reviews WHERE numero = ?",
        (numero,),
    )

    conn.execute(
        "DELETE FROM sr_state_user WHERE numero = ?",
        (numero,),
    )

    conn.commit()
    conn.close()

    return jsonify(ok=True, numero=numero)

def weighted_sample_without_replacement(pool, weights, k):
    pool = list(pool)
    weights = list(weights)
    chosen = []
    total = sum(weights)
    for _ in range(min(k, len(pool))):
        r = random.random() * total
        acc, idx = 0.0, 0
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                idx = i
                break
        chosen.append(pool[idx])
        total -= weights[idx]
        pool.pop(idx)
        weights.pop(idx)
    return chosen

@app.route('/api/draw', methods=['GET'])
def api_draw():
    ensure_db()
    params = read_params()

    max_active = int(params.get("max_active_num", 36))
    try:
        n = max(1, min(10, int(request.args.get("count", 8))))
    except (TypeError, ValueError):
        n = 8

    previous_bonus = float(params.get("previous_chapter_bonus", 0.2))
    last_bonus = float(params.get("last_chapter_bonus", 0.4))
    diff_weight_param = float(params.get("teacher_difficulty_weight", 0.15))


    DIFF_MID = 2.5
    DIFF_HALF_RANGE = 2.5

    hs_mode = request.args.get("hors_serie") == "1"
    max_hs = int(params.get("max_hors_serie_num", 0))

    conn = db()

    rows = conn.execute(
        "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
        "FROM forgecards WHERE numero <= ? AND hors_serie = 0 ORDER BY numero ASC",
        (max_active,)
    ).fetchall()
    if hs_mode and max_hs > 0:
        rows += conn.execute(
            "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
            "FROM forgecards WHERE hors_serie = 1 ORDER BY numero ASC LIMIT ?",
            (max_hs,)
        ).fetchall()

    cards = [dict(r) for r in rows]
    for c in cards:
        c['label'] = card_label(c.get('code'), c['hors_serie'])

    if not cards:
        conn.close()
        return jsonify([])


    recent = conn.execute(
        """
        SELECT numero
        FROM draw_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(n * 2, 5),),
    ).fetchall()

    recent_numbers = {r["numero"] for r in recent}

    pool = [c for c in cards if c["numero"] not in recent_numbers]

    if len(pool) < n:
        pool = cards

    last_chapter = cards[-1]["chapitre"]
    previous_chapter = None

    for c in reversed(cards):
        if c["chapitre"] != last_chapter:
            previous_chapter = c["chapitre"]
            break

    weights = []

    for c in pool:
        if c.get("hors_serie"):
            # meme proba qu'une fiche normale de difficulte 5 d'un vieux chapitre
            normalized = (5.0 - DIFF_MID) / DIFF_HALF_RANGE
            weights.append(max(0.1, 1 + diff_weight_param * normalized))
            continue
        weight = 1.0

        if previous_chapter is not None and c["chapitre"] == previous_chapter:
            weight += previous_bonus

        if c["chapitre"] == last_chapter:
            weight += last_bonus

        td = c.get("teacher_difficulty")
        if td is not None and diff_weight_param:
            normalized = (float(td) - DIFF_MID) / DIFF_HALF_RANGE
            weight *= max(0.1, 1 + diff_weight_param * normalized)

        weights.append(weight)

    k = min(n, len(pool))
    chosen = weighted_sample_without_replacement(pool, weights, k)

    reordered = interleave_by_chapitre(chosen)

    for c in reordered:
        conn.execute(
            "INSERT INTO draw_history(numero) VALUES(?)",
            (c["numero"],),
        )


    conn.execute(
        "DELETE FROM draw_history WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM draw_history)",
        (DRAW_HISTORY_KEEP,),
    )

    log_event(conn, session.get('sr_user', 'anonyme'), 'draw',
              ','.join(str(c['numero']) for c in reordered))
    conn.commit()
    conn.close()

    return jsonify(reordered)

@app.route('/uploads/fiche/<filename>')
def fiche_file(filename):

    return send_from_directory(UPLOADS, filename, max_age=0)

init_db()

if __name__ == '__main__':
    app.run(debug=False)
