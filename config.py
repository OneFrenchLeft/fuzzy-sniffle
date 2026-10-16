# -*- coding: utf-8 -*-
"""Constantes, chemins, fuseau horaire, parametres."""
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os, re, secrets, json, math, logging, tempfile, fcntl
from contextlib import contextmanager

BASE = Path(__file__).resolve().parent

load_dotenv(BASE / '.env')

TEMPLATES = BASE / 'templates'

STATIC = BASE / 'static'

UPLOADS = BASE / 'uploads' / 'fiche'

DATA = BASE / 'data'

DB_PATH = DATA / 'forgecards.db'

PARAMS_PATH = DATA / 'params.json'

# Registre des matieres : ajouter une matiere = ajouter une entree ici
# (cle sans accent, label affiche, couleur du lobby QCM, prefixe d'identifiant
# de question). Tout le reste (fichiers data/qcm_<cle>.json, selects admin,
# lobby, Ankimie) en decoule.
SUBJECTS = {
    'physique': {'label': 'Physique', 'color': '#01696f', 'prefix': 'p'},
    'chimie': {'label': 'Chimie', 'color': '#8e44ad', 'prefix': 'c'},
    'maths': {'label': 'Mathématiques', 'color': '#2980b9', 'prefix': 'm'},
}

QCM_FILES = {key: DATA / f'qcm_{key}.json' for key in SUBJECTS}

QCM_MIN_TIME_S = 10

QCM_MAX_TIME_S = 300

QCM_DEFAULT_TIME_S = 30

QCM_THEME_PREFIX = {key: subj['prefix'] for key, subj in SUBJECTS.items()}

QID_RE = re.compile(r'^[a-z]-[0-9a-f]{12}-[0-9a-f]{8}$')

TZ_PARIS = ZoneInfo('Europe/Paris')

def now_paris():
    return datetime.now(TZ_PARIS).replace(tzinfo=None)

ADMIN_PASSWORD = os.environ.get('MADEC_ADMIN_PASSWORD')

# Liste par defaut des chapitres de forgecards. L'admin la modifie sans toucher
# au code (onglet Parametres) : elle est stockee dans params.json ('chapitres')
# et cette constante ne sert que de valeur initiale / de repli.
CHAPITRES = [
    'Systèmes ouverts',
    'Diffusion thermique',
    'Diffusion de particules',
    'Rayonnement thermique',
    'Référentiels non galiléens',
    'Cinématique des fluides',
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

DEFAULT_PARAMS = {
    'max_active_num': 36,
    'max_hors_serie_num': 6,
    'max_kholle_num': 12,
    'fsrs_retention': 0.90,
    'daily_new_limit': 3,
    'daily_review_limit': 3,
    'qcm_daily_new_limit': 10,
    # Retention cible propre a Ankimie (le deck QCM a des cartes plus petites
    # que les forgecards : on peut viser un rythme different).
    'ankimie_retention': 0.90,
    # Heure du rappel Discord quotidien (lues par le bot a chaque declenchement).
    'reminder_hour': 21,
    # Interrupteurs de modules (export multi-classes : chaque classe active
    # ce qu'elle utilise). 1 = actif, 0 = desactive.
    'module_forgecards': 1,
    'module_ankimie': 1,
    'module_qcm': 1,
    # Matieres piochees par Ankimie (sous-ensemble des cles de QCM_FILES).
    'ankimie_subjects': ['chimie'],
    # Chapitres proposés a l'import / a l'edition des forgecards (editables
    # dans l'onglet Parametres).
    'chapitres': CHAPITRES,
    # Textes affiches (nom du site dans la nav, nom du tirage de fiches).
    'site_name': 'Forgecards',
    'tirage_label': 'Simulation de khôlle',
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

ALLOWED_EXTENSIONS = {'pdf'}

MAX_CONTENT_LENGTH = 15 * 1024 * 1024

JOKER_CAP = 2

JOKER_EVERY = 4

QCM_WEAK_LIMIT = 10

QCM_INVITE_TTL_MINUTES = 15

GRADE_MAP_FR = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}

SQLITE_INT_MAX = 2 ** 63 - 1
_params_log = logging.getLogger(__name__)


def _read_params_file(strict=False):
    if not PARAMS_PATH.exists():
        return {}
    try:
        data = json.loads(PARAMS_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('un objet JSON est attendu')
        return data
    except (OSError, ValueError, UnicodeError) as exc:
        _params_log.warning('Lecture des paramètres impossible : %s', exc)
        if strict:
            raise ValueError('Les paramètres existants sont illisibles ; aucune modification enregistrée.') from exc
        return {}


def _normalise_params(data, strict=False):
    out = DEFAULT_PARAMS.copy()
    for k, default in DEFAULT_PARAMS.items():
        try:
            if k in ('max_active_num', 'daily_new_limit', 'daily_review_limit', 'max_hors_serie_num', 'max_kholle_num', 'qcm_daily_new_limit'):
                value = int(data.get(k, default))
                if not 0 <= value <= SQLITE_INT_MAX:
                    raise ValueError('entier positif hors plage SQLite')
                out[k] = value
            elif k in ('fsrs_retention', 'ankimie_retention'):
                value = float(data.get(k, default))
                if not math.isfinite(value):
                    raise ValueError('nombre fini requis')
                out[k] = min(max(value, 0.80), 0.97)
            elif k == 'reminder_hour':
                out[k] = min(max(int(data.get(k, default)), 0), 23)
            elif k in ('module_forgecards', 'module_ankimie', 'module_qcm'):
                out[k] = 1 if int(data.get(k, default)) else 0
            elif k == 'ankimie_subjects':
                raw = data.get(k, default)
                if not isinstance(raw, list):
                    raw = default
                # Ne garder que les matieres connues ; liste vide = deck vide
                # (choix admin explicite), valeur invalide = repli au defaut.
                out[k] = [t for t in raw if t in QCM_FILES]
            elif k == 'chapitres':
                raw = data.get(k, default)
                if not isinstance(raw, list):
                    raw = default
                # Chaines non vides, sans doublons, ordre conserve. 'Autre'
                # est force : c'est la valeur de repli des imports/editions.
                seen = []
                for item in raw:
                    item = str(item).strip()
                    if item and item not in seen:
                        seen.append(item)
                if 'Autre' not in seen:
                    seen.append('Autre')
                out[k] = seen or list(default)
            elif k in ('site_name', 'tirage_label'):
                raw = str(data.get(k, default)).strip()
                out[k] = raw[:60] if raw else default
            else:
                value = float(data.get(k, default))
                if not math.isfinite(value):
                    raise ValueError('nombre fini requis')
                out[k] = value
        except (TypeError, ValueError, OverflowError):
            if strict and k in data:
                raise ValueError('Valeur invalide pour « ' + k + ' ».') from None
            out[k] = default
    # Marqueur prive : date et ancienne valeur du dernier increment de
    # max_active_num. sr.py s'en sert pour ne rendre les nouvelles fiches
    # dues que le lendemain de l'increment (sinon le garde de streak de
    # 23h55 les compte et crame des jokers). Cle '_' : jamais exposee a
    # l'admin, jamais dans DEFAULT_PARAMS.
    inc = data.get('_max_active_increment')
    if isinstance(inc, dict):
        try:
            first = int(inc.get('from', 0))
            if not 0 <= first <= SQLITE_INT_MAX:
                raise ValueError('seuil hors plage SQLite')
            out['_max_active_increment'] = {
                'date': datetime.strptime(inc.get('date', ''), '%Y-%m-%d').date().isoformat(),
                'from': first,
            }
        except (TypeError, ValueError, OverflowError):
            pass
    pauses = []
    raw_pauses = data.get('_forgecards_pauses', [])
    for pause in raw_pauses if isinstance(raw_pauses, list) else []:
        if not isinstance(pause, dict):
            continue
        try:
            start = datetime.strptime(pause['start'], '%Y-%m-%d').date().isoformat()
            end = pause.get('end')
            if end is not None:
                end = datetime.strptime(end, '%Y-%m-%d').date().isoformat()
                if end < start:
                    continue
            pauses.append({'start': start, 'end': end})
        except (KeyError, TypeError, ValueError):
            continue
    if pauses:
        out['_forgecards_pauses'] = pauses
    return out


@contextmanager
def _params_lock():
    PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PARAMS_PATH.with_suffix(PARAMS_PATH.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic_params_write(data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=PARAMS_PATH.parent,
                                         prefix=PARAMS_PATH.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(PARAMS_PATH)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ensure_pause(params):
    # Un ancien déploiement désactivé commence sa pause à la première lecture ;
    # aucune date de désactivation passée n'est inventée.
    pauses = params.setdefault('_forgecards_pauses', [])
    if not params['module_forgecards'] and not any(p['end'] is None for p in pauses):
        pauses.append({'start': now_paris().date().isoformat(), 'end': None})
        return True
    return False


def read_params():
    out = _normalise_params(_read_params_file())
    missing_pause = not out['module_forgecards'] and not any(
        p['end'] is None for p in out.get('_forgecards_pauses', []))
    if not PARAMS_PATH.exists() or missing_pause:
        with _params_lock():
            out = _normalise_params(_read_params_file())
            changed = _ensure_pause(out)
            if not PARAMS_PATH.exists() or changed:
                _atomic_params_write(out)
    return out


def write_params(data):
    if not isinstance(data, dict):
        raise ValueError('Un objet JSON est attendu pour les paramètres.')
    with _params_lock():
        previous = _normalise_params(_read_params_file(strict=True))
        _ensure_pause(previous)
        candidate = {**previous, **{k: data[k] for k in DEFAULT_PARAMS if k in data}}
        merged = _normalise_params(candidate, strict=True)
        today = now_paris().date().isoformat()
        prev_max = previous['max_active_num']
        new_max = merged['max_active_num']
        increment = previous.get('_max_active_increment', {})
        # Plusieurs hausses le même jour conservent le premier seuil différé.
        if increment.get('date') == today:
            floor = min(prev_max, increment['from'])
            if new_max > floor:
                merged['_max_active_increment'] = {'date': today, 'from': floor}
            else:
                merged.pop('_max_active_increment', None)
        elif new_max > prev_max:
            merged['_max_active_increment'] = {'date': today, 'from': prev_max}
        elif new_max < prev_max:
            merged.pop('_max_active_increment', None)
        if previous['module_forgecards'] and not merged['module_forgecards']:
            _ensure_pause(merged)
        elif not previous['module_forgecards'] and merged['module_forgecards']:
            for pause in merged.get('_forgecards_pauses', []):
                if pause['end'] is None:
                    pause['end'] = today
        _atomic_params_write(merged)
        return merged


def forgecards_paused(day, params=None):
    """Journées sans obligation, fin de pause exclusive (ni jour offert ni joker)."""
    params = params if params is not None else read_params()
    day = day.isoformat() if hasattr(day, 'isoformat') else str(day)
    pauses = params.get('_forgecards_pauses', [])
    pauses = pauses if isinstance(pauses, list) else []
    if any(isinstance(p, dict) and isinstance(p.get('start'), str)
           and p['start'] <= day and (p.get('end') is None
                                     or isinstance(p['end'], str) and day < p['end'])
           for p in pauses):
        return True
    # Compatibilité avec les paramètres du bot ou d'un ancien déploiement.
    return not params.get('module_forgecards', 1) and day == now_paris().date().isoformat()

def gen_emoji_password():
    return [secrets.choice(EMOJI_KEYPAD) for _ in range(EMOJI_PW_LENGTH)]


def module_enabled(name):
    """Interrupteur de module ('forgecards', 'ankimie', 'qcm'). Defaut : actif."""
    try:
        return bool(int(read_params().get('module_' + name, 1)))
    except (TypeError, ValueError):
        return True


def get_chapitres():
    """Chapitres de forgecards courants (params.json, repli : CHAPITRES)."""
    chaps = read_params().get('chapitres')
    return chaps if isinstance(chaps, list) and chaps else list(CHAPITRES)


def subject_list():
    """Matieres connues, pretes pour les templates/API : key, label, color."""
    return [{'key': k, 'label': v['label'], 'color': v['color']}
            for k, v in SUBJECTS.items()]

