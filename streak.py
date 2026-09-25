# -*- coding: utf-8 -*-
"""Logique pure des series (streaks) et jokers + cloture idempotente des jours.

Regles metier :
  - un jour compte si l'eleve a revise OU s'il est valide (gratuit ou joker) ;
  - validation gratuite : deck vide, OU rien a faire + activite reelle le jour la
    (l'activite ne conditionne QUE la gratuite, jamais la depense de joker :
    le joker "sauve" meme un jour d'absence totale) ;
  - jour manque (cartes dues sans revision, OU absence sans activite) -> un
    joker est consomme s'il y en a un, sinon la streak casse ;
  - toute depense est atomique et idempotente par (prenom, day) :
    BEGIN IMMEDIATE + INSERT OR IGNORE (rowcount) + decrement conditionnel ;
  - chaque mouvement de stock est trace dans joker_ledger (append-only).
"""
from datetime import datetime, timedelta
from config import JOKER_CAP, JOKER_EVERY, now_paris, read_params
from db import db, log_event, apply_joker_change


def activity_days(conn, prenom):
    # Small: Don't forget validated days; reviews aren't the only source.
    days = {r[0] for r in conn.execute(
        'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=?', (prenom,)).fetchall()}
    days |= {r[0] for r in conn.execute(
        'SELECT day FROM sr_daily_streak WHERE prenom=? AND validated=1', (prenom,)).fetchall()}
    return days


def compute_record(days):
    # Ethan: Sorted dates make the consecutive-day check deterministic.
    record, run, prev = 0, 0, None
    for d in sorted(days):
        cur = datetime.strptime(d, '%Y-%m-%d').date()
        run = run + 1 if prev and (cur - prev).days == 1 else 1
        record = max(record, run)
        prev = cur
    return record


def compute_streak(conn, prenom):
    # Flying: This stays read-only. Steph, please don't "fix" the DB here again.
    days = activity_days(conn, prenom)
    if not days:
        return 0
    cursor = now_paris().date()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _due_count(conn, prenom, day, params):
    """Cartes dues pour `day` — la meme definition partout (guard + verdict UI).

    Hors serie exclus (jamais comptes dans la streak). Les fiches rendues
    tirables par un increment de max_active le jour `day` (ou apres) sont
    exclues : elles ne deviennent dues que le lendemain de l'increment,
    sinon un increment a 23h crame des jokers pour des cartes jamais vues.
    """
    max_active = int(params.get('max_active_num', 36))
    increment = params.get('_max_active_increment') or {}
    inc_date = str(increment.get('date', ''))
    try:
        inc_from = int(increment.get('from', max_active))
    except (TypeError, ValueError):
        inc_from = max_active
    return conn.execute(
        'SELECT COUNT(*) AS c FROM forgecards f JOIN sr_state_user s '
        'ON s.numero=f.numero AND s.prenom=? '
        'WHERE f.numero<=? AND f.hors_serie=0 '
        'AND (s.next_review IS NULL OR s.next_review<=?) '
        'AND NOT (f.numero > ? AND ? >= ?)',
        (prenom, max_active, day, inc_from, inc_date, day)).fetchone()['c']


def close_day(conn, prenom, day, params, reason='guard_spend'):
    if conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND day=? AND validated=1',
                    (prenom, day)).fetchone():
        return 'already'
    if conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND substr(created_at,1,10)=? LIMIT 1',
                    (prenom, day)).fetchone():
        conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)',
                     (prenom, day))
        conn.commit()
        return 'review'
    deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=?',
                        (prenom,)).fetchone()['c']
    due = _due_count(conn, prenom, day, params) if deck else 0
    has_activity = conn.execute(
        "SELECT 1 FROM events WHERE prenom=? AND substr(created_at,1,10)=? "
        "AND type IN ('sr_open','login','review') LIMIT 1", (prenom, day)).fetchone()
    if deck == 0 or (due == 0 and has_activity):
        conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)',
                     (prenom, day))
        conn.commit()
        return 'validated_free'
    # Jour manque : le joker sauve la serie, qu'il y ait des cartes dues ou
    # non (un eleve a jour qui ne se connecte pas ne doit pas perdre sa
    # streak alors qu'il a un joker — c'est precisement son role).
    conn.execute('BEGIN IMMEDIATE')
    try:
        jk = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
        if not jk or jk['count'] <= 0:
            conn.commit()
            return 'missed'
        cur = conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)',
                           (prenom, day))
        if cur.rowcount == 1:
            apply_joker_change(conn, prenom, -1, reason, day)
            log_event(conn, prenom, 'joker_spent', f'{reason}:{day}')
            conn.commit()
            return 'joker_spent'
        conn.commit()
        return 'already'
    except Exception:
        conn.rollback()
        raise


def streak_verdict(conn, prenom, params, when=None, prediction=False):
    # Eliot : Same issue as before but with streak_verdict: the guard and the UI can disagree on streak status if the guard has not yet closed the day. This is a problem because the UI may show "validated" while the guard has not yet processed the day, leading to confusion.
    when = when or now_paris()
    today = when.date().isoformat()
    if prediction and when.hour == 23 and when.minute >= 55:
        return 'too_late'
    if conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND day=? AND validated=1',
                    (prenom, today)).fetchone():
        return 'done'
    if conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND substr(created_at,1,10)=? LIMIT 1',
                    (prenom, today)).fetchone():
        return 'done'
    deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=?',
                        (prenom,)).fetchone()['c']
    if deck == 0:
        return 'validated'
    if _due_count(conn, prenom, today, params) > 0:
        return 'due'
    has_activity = conn.execute(
        "SELECT 1 FROM events WHERE prenom=? AND substr(created_at,1,10)=? "
        "AND type IN ('sr_open','login','review') LIMIT 1", (prenom, today)).fetchone()
    return 'validated' if has_activity else 'inactive'


def close_missed_days(conn, prenom, params, reason='guard_spend', max_back=62, include_today=False):
    """Cloture TOUS les jours passes non clotures, pas seulement hier.

    Le site doit rester autonome : si le bot Discord est coupe (ou supprime),
    aucun jour ne doit passer entre les gouttes — sinon une absence couverte
    par un joker est perdue definitivement. Idempotent : close_day renvoie
    'already' pour les jours deja traites, relancer ne coute rien.
    include_today n'est vrai qu'au guard de 23h55 : clore aujourd'hui plus
    tot priverait l'eleve de sa journee. Clore des 23h55 (au lieu d'attendre
    le lendemain) evite 24h de streak affichee a 0 et evalue les dues sur
    l'etat du jour meme, avant que les reviews du lendemain ne les deplacent.
    Retourne {day: outcome} pour les jours reellement traites.
    """
    today = now_paris().date()
    row = conn.execute(
        "SELECT MIN(d) AS first_day FROM ("
        "  SELECT substr(created_at,1,10) AS d FROM reviews WHERE prenom=?"
        "  UNION SELECT day AS d FROM sr_daily_streak WHERE prenom=?"
        "  UNION SELECT substr(created_at,1,10) AS d FROM events WHERE prenom=?"
        ")", (prenom, prenom, prenom)).fetchone()
    first_day = row['first_day'] if row else None
    if not first_day:
        return {}
    try:
        start = datetime.strptime(first_day, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return {}
    start = max(start, today - timedelta(days=max_back))
    outcomes = {}
    cursor = today if include_today else today - timedelta(days=1)
    # Du plus recent au plus ancien : un joker depense sauve le jour le plus
    # recent possible — celui qui prolonge la serie ACTUELLE. Clore dans
    # l'ordre chronologique gaspillait les jokers sur des trous anciens deja
    # compenses par une cassure ulterieure (ex. joker brule sur un trou de
    # il y a 3 semaines alors que hier etait sauvable). Des qu'un jour est
    # definitivement manque ('missed', plus de joker), les jours plus anciens
    # ne peuvent plus rien changer a la serie en cours : on arrete.
    while cursor >= start:
        day = cursor.isoformat()
        outcome = close_day(conn, prenom, day, params, reason=reason)
        if outcome != 'already':
            outcomes[day] = outcome
        if outcome == 'missed':
            break
        cursor -= timedelta(days=1)
    return outcomes


def close_all_missed_days(params=None, reason='site_guard'):
    """Cloture rattrapante pour tous les eleves + reconciliation des paliers.

    C'est LE gardien des streaks cote site : il peut etre appele par la boucle
    interne de l'app (23h55), par une route interne, ou manuellement. Le bot
    Discord ne fait que lire les resultats pour envoyer les DM.
    Retourne {prenom: {'streak': int, 'events': [...], 'jokers': int, 'closed': {...}}}.
    """
    params = params or read_params()
    # Le guard de 23h55 clot AUSSI aujourd'hui : la journee est finie (le
    # verdict UI dit deja 'too_late' a partir de 23h55) et la streak affichee
    # est correcte des ce soir au lieu d'etre fausse pendant 24h.
    now = now_paris()
    include_today = (now.hour, now.minute) >= (23, 55)
    conn = db()
    users = [r['prenom'] for r in conn.execute(
        "SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
    results = {}
    for prenom in users:
        try:
            events = []
            outcomes = close_missed_days(conn, prenom, params, reason=reason,
                                         include_today=include_today)
            if 'joker_spent' in outcomes.values():
                events.append('joker_spent')
            jk_before = conn.execute('SELECT count FROM user_jokers WHERE prenom=?',
                                     (prenom,)).fetchone()
            before = jk_before['count'] if jk_before else 0
            streak = reconcile_streak(conn, prenom)
            jk_after = conn.execute('SELECT count FROM user_jokers WHERE prenom=?',
                                    (prenom,)).fetchone()
            after = jk_after['count'] if jk_after else 0
            if after > before:
                events.append('joker_awarded')
                log_event(conn, prenom, 'joker_awarded', f'streak={streak}')
            results[prenom] = {'streak': streak, 'events': events, 'jokers': after,
                               'closed': outcomes}
        except Exception as exc:
            print(f'[streak-guard] {prenom}: {exc!r}')
            conn.rollback()
            continue
    conn.commit()
    conn.close()
    return results


def reconcile_streak(conn, prenom):
    # Steph: Reconciliation can award milestones, but it must never spend a joker.
    days = activity_days(conn, prenom)
    if not days:
        # Eliot: Reset this or an old milestone can block future rewards.
        conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=?", (prenom,))
        conn.commit()
        return 0
    cursor = now_paris().date()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
        if cursor.isoformat() not in days:
            # Trou dans la serie : on reset les paliers, AUCUN depense ici.
            conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=?", (prenom,))
            conn.commit()
            return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    if streak >= JOKER_EVERY:
        milestone = streak // JOKER_EVERY
        jrow = conn.execute('SELECT count, last_milestone FROM user_jokers WHERE prenom=?',
                            (prenom,)).fetchone()
        current = jrow['count'] if jrow else 0
        last_ms = jrow['last_milestone'] if jrow else 0
        if milestone > last_ms:
            gained = max(0, min(milestone - last_ms, JOKER_CAP - current))
            if gained > 0:
                apply_joker_change(conn, prenom, gained, 'milestone_award')
            conn.execute("UPDATE user_jokers SET last_milestone=? WHERE prenom=?",
                         (milestone, prenom))
            conn.commit()
    return streak
