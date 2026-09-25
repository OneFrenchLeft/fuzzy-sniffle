# -*- coding: utf-8 -*-
"""Reproduction des bugs streak/joker + verification des corrections.

Bug A : un eleve a jour (aucune carte due) qui ne se connecte pas un jour
        perd sa streak SANS que son joker soit depense.
Bug B : le jour J n'est cloture que le lendemain 23h55 -> la streak affichee
        est fausse pendant 24h et les dues sont evaluees sur l'etat futur.
Bug C : increment de N a 23h -> les nouvelles fiches ne doivent pas etre
        dues le jour meme (ni pour le deck, ni pour le guard de streak).
"""
import sys, tempfile
from pathlib import Path
from datetime import datetime, timedelta

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import db as dbmod  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.PARAMS_PATH = tmp / 'params.json'
dbmod.DB_PATH = tmp / 'forgecards.db'

J = datetime(2026, 9, 24, 12, 0)  # jour de reference


def at(day_offset, hour, minute=0):
    return datetime(J.year, J.month, J.day, hour, minute) + timedelta(days=day_offset)


FAKE = {'now': at(0, 12)}


def fake_now():
    return FAKE['now']


config.now_paris = fake_now
dbmod.now_paris = fake_now
import streak  # noqa: E402
streak.now_paris = fake_now

dbmod.init_db()


def iso(dt):
    return dt.date().isoformat()


def setup_eleve(prenom, nb_cards=36, next_review=None, jokers=1, review_days=()):
    conn = dbmod.db()
    for n in range(1, nb_cards + 1):
        conn.execute('INSERT OR IGNORE INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)',
                     (n, f'{n}.pdf', f'{n}-c.pdf'))
        conn.execute("INSERT OR IGNORE INTO sr_state_user(prenom, numero, stability, difficulty, state, last_review, next_review, repetitions, lapses) "
                     "VALUES (?,?,1.0,5.0,'review',?,?,1,0)",
                     (prenom, n, iso(at(-3, 10)), next_review or iso(at(5, 10))))
    conn.execute('INSERT OR IGNORE INTO users(prenom) VALUES (?)', (prenom,))
    for d in review_days:
        conn.execute('INSERT INTO reviews(numero, prenom, result, was_new, created_at) VALUES (1,?,?,0,?)',
                     (prenom, 'good', at(d, 18).isoformat()))
    conn.execute("INSERT INTO user_jokers(prenom, count, last_milestone) VALUES (?,?,0) "
                 "ON CONFLICT(prenom) DO UPDATE SET count=excluded.count", (prenom, jokers))
    conn.commit()
    conn.close()


def jokers_of(prenom):
    row = dbmod.db().execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    return row['count'] if row else 0


def streak_of(prenom):
    conn = dbmod.db()
    s = streak.compute_streak(conn, prenom)
    conn.close()
    return s


# ---------- BUG A : jour d'absence sans dues -> le joker doit sauver ----------
# (streak de base 2 : apres le jour sauve elle passe a 3, sous le palier de 4
# qui offrirait un joker et fausserait le comptage)
setup_eleve('Bob', jokers=1, review_days=(-2, -1))
assert streak_of('Bob') == 2, streak_of('Bob')

# Jour J : Bob ne fait rien. Guard de J 23h55 (clot J et avant avec la correction).
FAKE['now'] = at(0, 23, 55)
streak.close_all_missed_days(config.read_params(), reason='site_guard')
assert jokers_of('Bob') == 0, f"joker non depense le jour meme: {jokers_of('Bob')}"
assert streak_of('Bob') == 3, f"streak perdue malgre joker: {streak_of('Bob')}"
print('TEST A OK : absence sans dues -> joker depense a 23h55 le jour meme, streak conservee')

# ---------- BUG C : increment a 23h -> nouvelles fiches non dues le jour J ----------
setup_eleve('Carole', nb_cards=36, jokers=0, review_days=(-1,))
# Elle a aussi ouvert le site le jour J (activite) AVANT l'increment.
conn = dbmod.db()
dbmod.log_event(conn, 'Carole', 'sr_open')
conn.commit(); conn.close()
# Increment 36 -> 40 a 23h.
FAKE['now'] = at(0, 23, 0)
config.write_params({'max_active_num': 40})
conn = dbmod.db()
for n in range(37, 41):
    conn.execute('INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?,?,?)',
                 (n, f'{n}.pdf', f'{n}-c.pdf'))
    # L'ancien code avait cree des lignes dues le jour meme : on simule une
    # ouverture a 23h30 via le meme CASE que sr.py (next_review = demain).
    conn.execute("INSERT INTO sr_state_user(prenom, numero, state, next_review, repetitions, lapses) "
                 "VALUES ('Carole', ?, 'new', ?, 0, 0)", (n, iso(at(1, 8))))
conn.commit(); conn.close()
# Guard de J 23h55 : les fiches 37-40 ne sont dues que demain -> pas de joker,
# jour valide (activite + rien de du).
FAKE['now'] = at(0, 23, 55)
streak.close_all_missed_days(config.read_params(), reason='site_guard')
assert jokers_of('Carole') == 0, f"joker brule par l increment: {jokers_of('Carole')}"
assert streak_of('Carole') == 2, f"streak Carole: {streak_of('Carole')}"
print('TEST B OK : increment 23h -> nouvelles fiches non dues le jour J, pas de joker brule')

# ---------- Le lendemain, les nouvelles fiches comptent normalement ----------
# Carole ne revient pas le J+1 : les fiches 37-40 etaient dues -> jour manque,
# elle n'a pas de joker -> 'missed'. La streak affichee tombe a 0 des J+2
# (a J+1 23h55, elle compte encore jusqu'a J par construction).
FAKE['now'] = at(1, 23, 55)
res = streak.close_all_missed_days(config.read_params(), reason='site_guard')
closed = res['Carole']['closed']
assert closed.get(iso(at(1, 12))) == 'missed', closed
FAKE['now'] = at(2, 12, 0)
assert streak_of('Carole') == 0, f"streak Carole J+2: {streak_of('Carole')}"
print('TEST C OK : le lendemain, les fiches de l increment comptent comme dues')

# ---------- BUG B : le guard ne doit pas clore aujourd'hui avant 23h55 ----------
setup_eleve('Dan', jokers=2, review_days=(-1,))
FAKE['now'] = at(0, 14, 0)  # ouverture du site a 14h : rattrapage sans clore J
conn = dbmod.db()
streak.close_missed_days(conn, 'Dan', config.read_params(), reason='sr_open_spend')
conn.commit(); conn.close()
assert jokers_of('Dan') == 2, f"joker depense a 14h: {jokers_of('Dan')}"
FAKE['now'] = at(0, 23, 55)
streak.close_all_missed_days(config.read_params(), reason='site_guard')
assert jokers_of('Dan') == 1, f"joker du soir non depense: {jokers_of('Dan')}"
print('TEST D OK : pas de cloture du jour avant 23h55, cloture a 23h55')

# ---------- Jokers epuises : la streak casse, sans depense negative ----------
setup_eleve('Eve', jokers=0, review_days=(-1,))
FAKE['now'] = at(0, 23, 55)
res = streak.close_all_missed_days(config.read_params(), reason='site_guard')
assert jokers_of('Eve') == 0
assert res['Eve']['closed'].get(iso(at(0, 12))) == 'missed', res['Eve']
FAKE['now'] = at(1, 12, 0)
assert streak_of('Eve') == 0, streak_of('Eve')
print('TEST E OK : sans joker, la streak casse proprement')

print('TOUS LES TESTS STREAK SONT PASSES')
