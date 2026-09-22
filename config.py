# -*- coding: utf-8 -*-
"""Constantes, chemins, fuseau horaire, parametres."""
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os, re, secrets, json

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

TZ_PARIS = ZoneInfo('Europe/Paris')

def now_paris():
    return datetime.now(TZ_PARIS).replace(tzinfo=None)

ADMIN_PASSWORD = os.environ.get('MADEC_ADMIN_PASSWORD')

# --- Matieres : feature flags du .env ---
# Physique est la matiere de base, toujours active. Les autres se reveillent
# avec une variable d'environnement (CHIMIE=True, MATHS=True...). Tant que le
# flag est absent ou False, la matiere n'existe nulle part dans l'interface.
SUBJECT_META = {
    'physique': {'label': 'Physique', 'color': '#01696f', 'env': None},
    'chimie': {'label': 'Chimie', 'color': '#8e44ad', 'env': 'CHIMIE'},
    'maths': {'label': 'Maths', 'color': '#2980b9', 'env': 'MATHS'},
}

DEFAULT_SUBJECT = 'physique'


def subject_enabled(key):
    meta = SUBJECT_META.get(key)
    if not meta:
        return False
    if meta['env'] is None:
        return True
    return os.environ.get(meta['env'], '').strip().lower() in ('1', 'true', 'yes', 'on')


ENABLED_SUBJECTS = [k for k in SUBJECT_META if subject_enabled(k)]


def subjects_payload():
    """Liste publique des matieres actives, pour l'UI (switch, selects QCM)."""
    return [{'key': k, 'label': SUBJECT_META[k]['label'], 'color': SUBJECT_META[k]['color']}
            for k in ENABLED_SUBJECTS]


def all_subjects_payload():
    """Toutes les matieres connues, actives ou non — reserve a l'admin, qui
    doit pouvoir preparer une matiere avant de l'activer."""
    return [{'key': k, 'label': SUBJECT_META[k]['label'], 'color': SUBJECT_META[k]['color']}
            for k in SUBJECT_META]

DEFAULT_PARAMS = {
    'max_active_num': 36,
    'max_hors_serie_num': 6,
    'max_kholle_num': 12,
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

# Chapitres par matiere. CHAPITRES reste la liste physique (compat : tous les
# imports actuels pointent dessus). Les listes chimie/maths sont a remplir
# avant le lancement de la matiere.
CHAPITRES_BY_SUBJECT = {
    'physique': CHAPITRES,
    'chimie': ['Autre'],
    'maths': ['Autre'],
}


def chapitres_for(subject=DEFAULT_SUBJECT):
    return CHAPITRES_BY_SUBJECT.get(subject, CHAPITRES_BY_SUBJECT[DEFAULT_SUBJECT])

ALLOWED_EXTENSIONS = {'pdf'}

MAX_CONTENT_LENGTH = 15 * 1024 * 1024

JOKER_CAP = 2

JOKER_EVERY = 4

QCM_WEAK_LIMIT = 10

QCM_INVITE_TTL_MINUTES = 15

GRADE_MAP_FR = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}

# Un fichier de parametres par matiere. La physique garde le chemin
# historique (params.json) : zero migration sur une instance existante.
PARAMS_PATHS = {
    'physique': PARAMS_PATH,
    'chimie': DATA / 'params_chimie.json',
    'maths': DATA / 'params_maths.json',
}


def params_path(subject=DEFAULT_SUBJECT):
    return PARAMS_PATHS.get(subject, PARAMS_PATH)


def read_params(subject=DEFAULT_SUBJECT):


    path = params_path(subject)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    out = DEFAULT_PARAMS.copy()
    for k, default in DEFAULT_PARAMS.items():
        try:
            if k in ('max_active_num', 'daily_new_limit', 'daily_review_limit', 'max_hors_serie_num', 'max_kholle_num'):
                out[k] = max(0, int(data.get(k, default)))
            elif k == 'fsrs_retention':
                out[k] = min(max(float(data.get(k, default)), 0.80), 0.97)
            else:
                out[k] = float(data.get(k, default))
        except (TypeError, ValueError):
            out[k] = default
    if not path.exists():
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    return out

def write_params(data, subject=DEFAULT_SUBJECT):
    merged = read_params(subject)
    for k in DEFAULT_PARAMS:
        if k in data:
            merged[k] = data[k]
    params_path(subject).write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')

    return read_params(subject)

def gen_emoji_password():
    return [secrets.choice(EMOJI_KEYPAD) for _ in range(EMOJI_PW_LENGTH)]

