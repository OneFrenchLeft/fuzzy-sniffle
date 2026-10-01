# -*- coding: utf-8 -*-
"""Test : nouvelles fiches dues le lendemain d'un increment de N + drop
annonce a 6h07 (retenu la veille)."""
import sys, sqlite3, tempfile, json
from pathlib import Path
from datetime import datetime, timedelta

WS = Path(__file__).parent
REPO = WS / 'fuzzy-sniffle'
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
PARAMS = tmp / 'params.json'
DB = tmp / 'forgecards.db'
config.PARAMS_PATH = PARAMS
dbmod.DB_PATH = DB

today = config.now_paris().date().isoformat()
tomorrow = (config.now_paris().date() + timedelta(days=1)).isoformat()

# ---------- 1. Marqueur d'increment dans les params ----------
p = config.read_params()
assert '_max_active_increment' not in p
config.write_params({'max_active_num': 36})
p = config.read_params()
assert '_max_active_increment' not in p, p  # pas de changement -> pas de marqueur
config.write_params({'max_active_num': 40})
p = config.read_params()
assert p['_max_active_increment'] == {'date': today, 'from': 36}, p
config.write_params({'daily_new_limit': 4})  # ecriture sans toucher au max : conserve
p = config.read_params()
assert p['_max_active_increment'] == {'date': today, 'from': 36}, p
config.write_params({'max_active_num': 38})  # reduction -> on efface
p = config.read_params()
assert '_max_active_increment' not in p, p
print('TEST 1 OK : marqueur increment pose/conserve/efface')

# ---------- 2. sr_today : fiches de l'increment dues DEMAIN ----------
dbmod.init_db()
conn = dbmod.db()
for n in range(1, 6):
    conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)",
                 (n, f'{n}.pdf', f'{n}-c.pdf'))
conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
conn.commit(); conn.close()

import sr as srmod  # noqa: E402
from flask import Flask  # noqa: E402

def open_today():
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(srmod.bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s['sr_user'] = 'Alice'
    return c.get('/api/sr/today')

# remettre a 36 d'abord (le test 1 avait reduit a 38, sinon le marqueur
# partirait de 38 et les fiches 36-38 passeraient dues aujourd'hui)
config.write_params({'max_active_num': 36})
config.write_params({'max_active_num': 40})  # marqueur from=36, date=today
conn = dbmod.db()  # une seule connexion : db() en ouvre une nouvelle a chaque appel
conn.execute("UPDATE forgecards SET numero = numero + 35")  # 36..40
conn.commit(); conn.close()
# les 5 fiches (36..40) viennent d'un increment 36 -> 40 aujourd'hui
r = open_today()
assert r.status_code == 200, r.get_data(as_text=True)
rows = dbmod.db().execute(
    "SELECT numero, next_review FROM sr_state_user WHERE prenom='Alice' ORDER BY numero").fetchall()
assert len(rows) == 5, rows
due_today = [row['numero'] for row in rows if row['next_review'] == today]
due_tmrw = [row['numero'] for row in rows if row['next_review'] == tomorrow]
# 36 etait deja tirable avant l'increment (marqueur from=36) -> due aujourd'hui ;
# 37..40 rendues tirable par l'increment -> dues DEMAIN seulement
assert due_today == [36], rows
assert due_tmrw == [37, 38, 39, 40], rows
print('TEST 2a OK : nouvelles fiches de l increment dues demain, anciennes aujourd hui')

# Le lendemain (marqueur date < today simule) : tout devient du aujourd'hui
conn = dbmod.db()
conn.execute("DELETE FROM sr_state_user")
conn.commit(); conn.close()
config.write_params({'max_active_num': 39})  # efface le marqueur (reduction)
config.write_params({'max_active_num': 40})  # re-increment : marqueur from=39, date=today
r = open_today()
rows = dbmod.db().execute(
    "SELECT numero, next_review FROM sr_state_user WHERE prenom='Alice' ORDER BY numero").fetchall()
due_today = [row['numero'] for row in rows if row['next_review'] == today]
due_tmrw = [row['numero'] for row in rows if row['next_review'] == tomorrow]
assert due_today == [36, 37, 38, 39], rows  # anciennes dues aujourd'hui
assert due_tmrw == [40], rows               # seule la nouvelle attend demain
print('TEST 2b OK : anciennes fiches dues aujourd hui, nouvelle seulement demain')

# ---------- 3. Drop annonce a 6h07 ----------
import types  # noqa: E402

# bot.py importe discord au niveau module : stubs minimaux pour le tester hors ligne
class _Meta(type):
    def __getattr__(cls, name):
        return _Any


class _Any(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _ANY

    def __getattr__(self, name):
        return _ANY


_ANY = _Any()


def _mod_getattr(name):
    return _Any


class _CheckFailure(Exception):
    pass


class _AppCommandError(Exception):
    pass


discord_stub = types.ModuleType('discord')
discord_stub.__getattr__ = _mod_getattr
app_commands_stub = types.ModuleType('discord.app_commands')
app_commands_stub.__getattr__ = _mod_getattr
app_commands_stub.CheckFailure = _CheckFailure
app_commands_stub.AppCommandError = _AppCommandError
ext_stub = types.ModuleType('discord.ext')
tasks_stub = types.ModuleType('discord.ext.tasks')


class _Intents:
    @staticmethod
    def default():
        return _Intents()


class _Client:
    def __init__(self, *a, **k):
        pass

    def event(self, fn):
        return fn


def _deco_factory(*a, **k):
    def deco(fn):
        return fn
    return deco


class _CommandTree:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _deco_factory


class _Group:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _deco_factory


def _loop(*a, **k):
    def deco(fn):
        return fn
    return deco


discord_stub.Intents = _Intents
discord_stub.Client = _Client
discord_stub.app_commands = app_commands_stub
discord_stub.ext = ext_stub
app_commands_stub.Group = _Group
app_commands_stub.CommandTree = _CommandTree
ext_stub.tasks = tasks_stub
tasks_stub.loop = _loop
sys.modules['discord'] = discord_stub
sys.modules['discord.app_commands'] = app_commands_stub
sys.modules['discord.ext'] = ext_stub
sys.modules['discord.ext.tasks'] = tasks_stub

import bot  # noqa: E402

H = lambda h, m: datetime(2026, 9, 24, h, m)
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3], None, '', H(23, 0))
assert a == 'init' and last == 3, (a, ann, pend, last)
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3], '3', '', H(23, 0))
assert a == 'sync' and last == 3, (a, ann, pend, last)
# increment a 23h -> retenu
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3, 4, 5], '3', '', H(23, 0))
assert a == 'hold' and pend == '4,5' and last == 3, (a, ann, pend, last)
# 6h05 : toujours retenu
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3, 4, 5], '3', '4,5', H(6, 5))
assert a == 'hold' and pend == '4,5', (a, ann, pend, last)
# 6h07 : annonce du lot, suivi du max tirable
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3, 4, 5], '3', '4,5', H(6, 7))
assert a == 'announce' and ann == [4, 5] and pend == '' and last == 5, (a, ann, pend, last)
# increment en journee (apres 6h07) : annonce directe
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3, 4], '3', '', H(15, 0))
assert a == 'announce' and ann == [4], (a, ann, pend, last)
# fiche retiree pendant la nuit : exclue de l'annonce, suivi quand meme
a, ann, pend, last = bot.plan_drop_announcement([1, 2, 3, 4], '3', '4,5', H(6, 8))
assert a == 'announce' and ann == [4] and last == 4, (a, ann, pend, last)
print('TEST 3 OK : drop retenu avant 6h07, annonce au passage')

print('TOUS LES TESTS SONT PASSES')
