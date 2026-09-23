# -*- coding: utf-8 -*-
"""Logique pure des series (streaks) et jokers + cloture idempotente des jours.

Tout est scopé par matière (`subject`) : chaque élève a une streak, des
jokers et des poids FSRS indépendants par matière. Les sessions/comptes,
elles, restent communes (table `users`).

Regles metier :
  - un jour compte si l'eleve a revise OU s'il est valide (gratuit ou joker) ;
  - validation gratuite : deck vide, OU rien a faire + activite reelle le jour la
    (l'activite ne conditionne QUE la gratuite, jamais la depense de joker :
    le joker "sauve" meme un jour d'absence totale) ;
  - cartes dues + pas de revision -> un joker est consomme s'il y en a un,
    sinon la streak casse ;
  - toute depense est atomique et idempotente par (prenom, subject, day) :
    BEGIN IMMEDIATE + INSERT OR IGNORE (rowcount) + decrement conditionnel ;
  - chaque mouvement de stock est trace dans joker_ledger (append-only).
"""
from datetime import datetime, timedelta
from config import JOKER_CAP, JOKER_EVERY, now_paris
from db import log_event, apply_joker_change


def activity_days(conn, prenom, subject='physique'):
    # Small: Don't forget validated days; reviews aren't the only source.
    days = {r[0] for r in conn.execute(
        'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=? AND subject=?',
        (prenom, subject)).fetchall()}
    days |= {r[0] for r in conn.execute(
        'SELECT day FROM sr_daily_streak WHERE prenom=? AND subject=? AND validated=1',
        (prenom, subject)).fetchall()}
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


def compute_streak(conn, prenom, subject='physique'):
    # Flying: This stays read-only. Steph, please don't "fix" the DB here again.
    days = activity_days(conn, prenom, subject)
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


def close_day(conn, prenom, day, params, reason='guard_spend', subject='physique'):
    if conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND subject=? AND day=? AND validated=1',
                    (prenom, subject, day)).fetchone():
        return 'already'
    if conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND subject=? AND substr(created_at,1,10)=? LIMIT 1',
                    (prenom, subject, day)).fetchone():
        conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, subject, day, validated) VALUES(?,?,?,1)',
                     (prenom, subject, day))
        conn.commit()
        return 'review'
    deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=? AND subject=?',
                        (prenom, subject)).fetchone()['c']
    due = 0
    if deck:
        max_active = int(params.get('max_active_num', 36))
        due = conn.execute(
            'SELECT COUNT(*) AS c FROM forgecards f JOIN sr_state_user s '
            'ON s.numero=f.numero AND s.prenom=? AND s.subject=? '
            'WHERE f.subject=? AND f.numero<=? AND (s.next_review IS NULL OR s.next_review<=?)',
            (prenom, subject, subject, max_active, day)).fetchone()['c']
    has_activity = conn.execute(
        "SELECT 1 FROM events WHERE prenom=? AND subject=? AND substr(created_at,1,10)=? "
        "AND type IN ('sr_open','login','review') LIMIT 1", (prenom, subject, day)).fetchone()
    if deck == 0 or (due == 0 and has_activity):
        conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, subject, day, validated) VALUES(?,?,?,1)',
                     (prenom, subject, day))
        conn.commit()
        return 'validated_free'
    if due == 0:
        return 'missed'
    conn.execute('BEGIN IMMEDIATE')
    try:
        jk = conn.execute('SELECT count FROM user_jokers WHERE prenom=? AND subject=?',
                          (prenom, subject)).fetchone()
        if not jk or jk['count'] <= 0:
            conn.commit()
            return 'missed'
        cur = conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, subject, day, validated) VALUES(?,?,?,1)',
                           (prenom, subject, day))
        if cur.rowcount == 1:
            apply_joker_change(conn, prenom, -1, reason, day, subject=subject)
            log_event(conn, prenom, 'joker_spent', f'{reason}:{day}', subject=subject)
            conn.commit()
            return 'joker_spent'
        conn.commit()
        return 'already'
    except Exception:
        conn.rollback()
        raise


def streak_verdict(conn, prenom, params, when=None, prediction=False, subject='physique'):
    # Eliot : Same issue as before but with streak_verdict: the guard and the UI can disagree on streak status if the guard has not yet closed the day. This is a problem because the UI may show "validated" while the guard has not yet processed the day, leading to confusion.
    when = when or now_paris()
    today = when.date().isoformat()
    if prediction and when.hour == 23 and when.minute >= 55:
        return 'too_late'
    if conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND subject=? AND day=? AND validated=1',
                    (prenom, subject, today)).fetchone():
        return 'done'
    if conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND subject=? AND substr(created_at,1,10)=? LIMIT 1',
                    (prenom, subject, today)).fetchone():
        return 'done'
    deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=? AND subject=?',
                        (prenom, subject)).fetchone()['c']
    if deck == 0:
        return 'validated'
    max_active = int(params.get('max_active_num', 36))
    due = conn.execute(
        'SELECT COUNT(*) AS c FROM forgecards f JOIN sr_state_user s '
        'ON s.numero=f.numero AND s.prenom=? AND s.subject=? '
        'WHERE f.subject=? AND f.numero<=? AND (s.next_review IS NULL OR s.next_review<=?)',
        (prenom, subject, subject, max_active, today)).fetchone()['c']
    if due > 0:
        return 'due'
    has_activity = conn.execute(
        "SELECT 1 FROM events WHERE prenom=? AND subject=? AND substr(created_at,1,10)=? "
        "AND type IN ('sr_open','login','review') LIMIT 1", (prenom, subject, today)).fetchone()
    return 'validated' if has_activity else 'inactive'


def reconcile_streak(conn, prenom, subject='physique'):
    # Steph: Reconciliation can award milestones, but it must never spend a joker.
    days = activity_days(conn, prenom, subject)
    if not days:
        # Eliot: Reset this or an old milestone can block future rewards.
        conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=? AND subject=?",
                     (prenom, subject))
        conn.commit()
        return 0
    cursor = now_paris().date()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
        if cursor.isoformat() not in days:
            # Trou dans la serie : on reset les paliers, AUCUN depense ici.
            conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=? AND subject=?",
                         (prenom, subject))
            conn.commit()
            return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    if streak >= JOKER_EVERY:
        milestone = streak // JOKER_EVERY
        jrow = conn.execute('SELECT count, last_milestone FROM user_jokers WHERE prenom=? AND subject=?',
                            (prenom, subject)).fetchone()
        current = jrow['count'] if jrow else 0
        last_ms = jrow['last_milestone'] if jrow else 0
        if milestone > last_ms:
            gained = max(0, min(milestone - last_ms, JOKER_CAP - current))
            if gained > 0:
                apply_joker_change(conn, prenom, gained, 'milestone_award', subject=subject)
            conn.execute("UPDATE user_jokers SET last_milestone=? WHERE prenom=? AND subject=?",
                         (milestone, prenom, subject))
            conn.commit()
    return streak
