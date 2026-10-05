# -*- coding: utf-8 -*-
"""Test : la formule du `remaining` du bot (cas Eddie, issue #7).

bot.py importe discord (absent de l'environnement de test) : on injecte des
stubs minimaux dans sys.modules AVANT l'import, puis on monte une base
SQLite temporaire et on verifie compute_daily :

  - cas Eddie : 1 review faite sur 1 carte due, quota 3 -> il reste 1 carte
    (l'ancienne formule min(limit, du) - fait renvoyait 0 a tort) ;
  - eleve a jour -> 0 ;
  - eleve qui n'a rien fait -> plafonne au quota ;
  - quota nouveau deja consomme mais reviews dues -> seulement les reviews
    restantes comptent ;
  - les reviews de hors-serie ne comptent pas dans les quotas.
"""
import sys, os, types, sqlite3, tempfile, asyncio
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

# --- Stubs discord / dotenv (bot.py n'est pas importable sans le paquet) ---
discord = types.ModuleType('discord')


class _Intents:
    @staticmethod
    def default():
        return _Intents()


class _Client:
    def __init__(self, **kw):
        pass

    def event(self, f):
        return f

    def get_channel(self, *a):
        return None


discord.Interaction = object
discord.Member = object
discord.Intents = _Intents
discord.Client = _Client
discord.File = lambda *a, **k: None
discord.Forbidden = type('Forbidden', (Exception,), {})
discord.CustomActivity = lambda **k: types.SimpleNamespace(**k)
discord.Streaming = lambda **k: types.SimpleNamespace(**k)
discord.ButtonStyle = types.SimpleNamespace(success=1, danger=2, secondary=3)
discord.Object = lambda **k: types.SimpleNamespace(**k)
discord.ui = types.SimpleNamespace(
    View=object, Button=object,
    button=lambda *a, **k: (lambda f: f))
discord.utils = types.SimpleNamespace(get=lambda *a, **k: None)

app_commands = types.ModuleType('discord.app_commands')
app_commands.CheckFailure = type('CheckFailure', (Exception,), {})
app_commands.AppCommandError = Exception
app_commands.Interaction = object
app_commands.check = lambda pred: (lambda f: f)
app_commands.describe = lambda **k: (lambda f: f)


class _CommandTree:
    def __init__(self, *a):
        pass

    def add_command(self, *a):
        pass

    def error(self, f):
        return f

    def copy_global_to(self, **k):
        pass

    async def sync(self, *a):
        return []


class _Group:
    def __init__(self, *a, **k):
        pass

    def command(self, *a, **k):
        return lambda f: f


app_commands.CommandTree = _CommandTree
app_commands.Group = _Group
discord.app_commands = app_commands

ext = types.ModuleType('discord.ext')
tasks = types.ModuleType('discord.ext.tasks')


class _Loop:
    """Imite discord.ext.tasks.Loop : decorateur .error + restart()."""
    def __init__(self, coro):
        self.coro = coro
        self.error_handler = None
        self.restarted = 0

    def error(self, f):
        self.error_handler = f
        return f

    def start(self):
        pass

    def is_running(self):
        return False

    def restart(self):
        self.restarted += 1


tasks.loop = lambda *a, **k: (lambda f: _Loop(f))
tasks.Loop = _Loop
ext.tasks = tasks

dotenv = types.ModuleType('dotenv')
dotenv.load_dotenv = lambda *a, **k: None

requests_stub = types.ModuleType('requests')
requests_stub.post = lambda *a, **k: None
requests_stub.RequestException = Exception

for _name, _mod in [('discord', discord), ('discord.app_commands', app_commands),
                    ('discord.ext', ext), ('discord.ext.tasks', tasks),
                    ('dotenv', dotenv), ('requests', requests_stub)]:
    sys.modules[_name] = _mod

# Base SQLite temporaire AVANT l'import de bot (FORGECARDS_DB lu a l'import)
tmp = Path(tempfile.mkdtemp())
dbfile = tmp / 'forgecards.db'
os.environ['FORGECARDS_DB'] = str(dbfile)
conn = sqlite3.connect(dbfile)
conn.execute('CREATE TABLE forgecards(numero INTEGER PRIMARY KEY, hors_serie INTEGER NOT NULL DEFAULT 0)')
conn.execute('CREATE TABLE sr_state_user(prenom TEXT, numero INTEGER, repetitions INTEGER DEFAULT 0, '
             'next_review TEXT, PRIMARY KEY(prenom, numero))')
conn.execute('CREATE TABLE reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, numero INTEGER, '
             'prenom TEXT, was_new INTEGER, created_at TEXT)')
conn.execute('CREATE TABLE sr_daily_streak(prenom TEXT, day TEXT, validated INTEGER, PRIMARY KEY(prenom, day))')
conn.execute('CREATE TABLE user_jokers(prenom TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_milestone INTEGER DEFAULT 0)')
conn.commit(); conn.close()

# Les fiches 1..6 existent (hors_serie=0) ; le JOIN de compute_daily part de
# forgecards, il faut des lignes dans les deux tables.
conn = sqlite3.connect(dbfile)
for n in range(1, 7):
    conn.execute('INSERT INTO forgecards(numero, hors_serie) VALUES (?, 0)', (n,))
conn.execute("INSERT INTO forgecards(numero, hors_serie) VALUES (9001, 1)")
conn.commit(); conn.close()

import bot  # noqa: E402

PARAMS = {'max_active_num': 36, 'daily_new_limit': 3, 'daily_review_limit': 3}
TODAY = bot.today_paris()
FUTURE = '2099-01-01'


def seed(prenom, states, reviews):
    """Etats explicites pour `states` ; les fiches 1..6 non couvertes sont
    censees 'pas encore dues' (next_review au futur) pour ne pas polluer."""
    conn = bot.fc_db()
    covered = set()
    for numero, reps, nxt in states:
        conn.execute('INSERT INTO sr_state_user(prenom, numero, repetitions, next_review) VALUES (?,?,?,?)',
                     (prenom, numero, reps, nxt))
        covered.add(numero)
    for numero in range(1, 7):
        if numero not in covered:
            conn.execute('INSERT INTO sr_state_user(prenom, numero, repetitions, next_review) VALUES (?,?,?,?)',
                         (prenom, numero, 0, FUTURE))
    for numero, was_new in reviews:
        conn.execute('INSERT INTO reviews(numero, prenom, was_new, created_at) VALUES (?,?,?,?)',
                     (numero, prenom, was_new, f'{TODAY}T12:00:00'))
    conn.commit(); conn.close()


# 1) cas Eddie : 1 new faite (carte 1 apprise), 1 review faite (carte 5),
#    mais 1 review encore due (carte 2). Ancienne formule -> 0, attendu 1.
seed('Eddie',
     states=[(1, 1, FUTURE), (2, 1, TODAY), (5, 1, FUTURE)],
     reviews=[(1, 1), (5, 0)])
d = bot.compute_daily(bot.fc_db(), 'Eddie', PARAMS)
assert d['due_new'] == 0 and d['due_review'] == 1, d
assert d['new_done'] == 1 and d['review_done'] == 1, d
assert d['remaining'] == 1, f"cas Eddie: remaining={d['remaining']}, attendu 1 (bug: double comptage)"

# 2) eleve a jour : sa seule due est reviewee (next_review repoussee) -> 0
seed('Lea', states=[(1, 1, FUTURE)], reviews=[(1, 0)])
d = bot.compute_daily(bot.fc_db(), 'Lea', PARAMS)
assert d['due_new'] == 0 and d['due_review'] == 0, d
assert d['remaining'] == 0, d

# 3) rien fait : plafonne au quota (3 news + 2 reviews dues -> 5)
seed('Max', states=[(1, 0, None), (2, 0, None), (3, 0, None), (4, 1, TODAY), (5, 1, TODAY)],
     reviews=[])
d = bot.compute_daily(bot.fc_db(), 'Max', PARAMS)
assert d['due_new'] == 3 and d['due_review'] == 2, d
assert d['remaining'] == 5, d
assert d['total'] == 5, d

# 4) quota de news consomme (3 news faites), plus de news dues,
#    3 reviews dues -> seulement le quota de reviews reste
seed('Zoe', states=[(1, 1, TODAY), (2, 1, TODAY), (3, 1, TODAY)],
     reviews=[(4, 1), (5, 1), (6, 1)])
d = bot.compute_daily(bot.fc_db(), 'Zoe', PARAMS)
assert d['due_new'] == 0 and d['due_review'] == 3, d
assert d['new_done'] == 3 and d['review_done'] == 0, d
assert d['remaining'] == 3, d  # quota reviews: 3 - 0 fait

# 5) les reviews de hors-serie ne comptent pas dans les quotas
seed('Anna', states=[(1, 1, FUTURE)], reviews=[(1, 0), (9001, 0)])
d = bot.compute_daily(bot.fc_db(), 'Anna', PARAMS)
assert d['review_done'] == 1, d  # la review HS (9001) est ignoree
assert d['remaining'] == 0, d

# 6) le wrapper resilient enregistre un handler qui relance la boucle
for loop in (bot.daily_reminder, bot.watch_new_cards, bot.rotate_status,
             bot.nightly_streak_guard, bot.weekly_recap):
    assert loop.error_handler is not None, f'{loop.coro.__name__} sans handler'
    asyncio.run(loop.error_handler(RuntimeError('boom de test')))
    assert loop.restarted == 1, f'{loop.coro.__name__} pas relancee'

print('TEST OK : remaining du bot aligne sur les quotas (cas Eddie), hors-serie exclus, boucles resilientes')
print('TOUS LES TESTS SONT PASSES')
