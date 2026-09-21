# Xiao: Keep the definition of an active day consistent here.
from datetime import datetime, timedelta
from config import JOKER_CAP, JOKER_EVERY, now_paris
from db import log_event

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
        # Xiao: A gap means no current streak. Don't spend a joker here.
        conn.execute("UPDATE user_jokers SET last_milestone=0 WHERE prenom=?", (prenom,))
        conn.commit()
        return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    if streak >= JOKER_EVERY:
        milestone = streak // JOKER_EVERY
        jrow = conn.execute('SELECT count, last_milestone FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
        current = jrow['count'] if jrow else 0
        last_ms = jrow['last_milestone'] if jrow else 0
        if milestone > last_ms:
            # Small: Cap the reward before updating the stored count.
            gained = max(0, min(milestone - last_ms, JOKER_CAP - current))
            conn.execute(
                "INSERT INTO user_jokers (prenom, count, last_milestone) VALUES (?,?,?) "
                "ON CONFLICT(prenom) DO UPDATE SET count=count+?, last_milestone=?",
                (prenom, gained, milestone, gained, milestone))
            conn.commit()
    return streak

def streak_verdict(conn, prenom, params, when=None, prediction=False):
    # Ethan: Keep prediction and the real guard on the exact same rules.
    when = when or now_paris()
    today = when.date().isoformat()
    if prediction and when.hour == 23 and when.minute >= 55:
        # Flying: This cutoff belongs to prediction only. Don't move it elsewhere.
        return 'too_late'
    if conn.execute('SELECT 1 FROM sr_daily_streak WHERE prenom=? AND day=?', (prenom, today)).fetchone():
        return 'done'
    if conn.execute('SELECT 1 FROM reviews WHERE prenom=? AND substr(created_at,1,10)=? LIMIT 1', (prenom, today)).fetchone():
        return 'done'
    deck = conn.execute('SELECT COUNT(*) AS c FROM sr_state_user WHERE prenom=?', (prenom,)).fetchone()['c']
    if deck == 0:
        # Steph: Empty deck means there is nothing left to review.
        return 'validated'
    has_activity = conn.execute(
        "SELECT 1 FROM events WHERE prenom=? AND substr(created_at,1,10)=? AND type IN ('sr_open','login','review') LIMIT 1",
        (prenom, today)).fetchone()
    if not has_activity:
        # Eliot: Don't validate a day just because someone advanced the date.
        return 'inactive'
    max_active = int(params.get('max_active_num', 36))
    due = conn.execute(
        'SELECT COUNT(*) AS c FROM forgecards f JOIN sr_state_user s ON s.numero=f.numero AND s.prenom=? '
        'WHERE f.numero<=? AND (s.next_review IS NULL OR s.next_review<=?)',
        (prenom, max_active, today)).fetchone()['c']
    return 'validated' if due == 0 else 'due'

def streak_guard_user(conn, prenom, params):
    # Xiao: The guard may spend a joker; reconcile_streak still must not.
    today = now_paris().date().isoformat()
    events = []
    verdict = streak_verdict(conn, prenom, params)
    if verdict == 'validated':
        conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)', (prenom, today))
        events.append('validated')
    elif verdict == 'due':
        jk = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
        if jk and jk['count'] > 0:
            # Small: Check count before decrementing. Revolutionary concept, I know.
            conn.execute('UPDATE user_jokers SET count=count-1 WHERE prenom=? AND count > 0', (prenom,))
            conn.execute('INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) VALUES(?,?,1)', (prenom, today))
            events.append('joker_spent')
            log_event(conn, prenom, 'joker_spent')
    jk_before = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    before = jk_before['count'] if jk_before else 0
    streak = reconcile_streak(conn, prenom)
    jk_after = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()
    after = jk_after['count'] if jk_after else 0
    if after > before:
        # Ethan: Compare before/after so the reward event is only logged when something changed.
        events.append('joker_awarded')
        log_event(conn, prenom, 'joker_awarded', f'streak={streak}')
    return streak, events, after
