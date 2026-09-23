# -*- coding: utf-8 -*-
"""Weighted random draw of cards, scoped on the session subject."""
from flask import Blueprint, jsonify, request, session
import random
from config import read_params, DRAW_HISTORY_KEEP, now_paris
from db import db, ensure_db, log_event
from helpers import card_label, interleave_by_chapitre, current_subject

bp = Blueprint('draw', __name__)


def weighted_sample_without_replacement(pool, weights, k):
    # Xiao: Weighted draw without putting the same card back.
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


@bp.route('/api/draw', methods=['GET'])
def api_draw():
    ensure_db()
    subject = current_subject()
    params = read_params(subject)

    max_active = int(params.get("max_active_num", 36))

    try:
        n = max(1, min(10, int(request.args.get("count", 8))))
    except (TypeError, ValueError):
        # Small: Invalid count? Eight it is. No need to start a crisis.
        n = 8

    previous_bonus = float(params.get("previous_chapter_bonus", 0.2))
    last_bonus = float(params.get("last_chapter_bonus", 0.4))
    diff_weight_param = float(params.get("teacher_difficulty_weight", 0.15))

    DIFF_MID = 2.5
    DIFF_HALF_RANGE = 2.5

    hs_mode = request.args.get("hors_serie") == "1"
    kholle_mode = request.args.get("kholle") == "1"
    max_hs = int(params.get("max_hors_serie_num", 0))
    max_kholle = int(params.get("max_kholle_num", 0))

    conn = db()

    plafond = max_kholle if (kholle_mode and max_kholle > 0) else max_active

    rows = conn.execute(
        "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
        "FROM forgecards WHERE subject=? AND numero <= ? AND hors_serie = 0 ORDER BY numero ASC",
        (subject, plafond)
    ).fetchall()

    if hs_mode and max_hs > 0:
        rows += conn.execute(
            "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
            "FROM forgecards WHERE subject=? AND hors_serie = 1 ORDER BY numero ASC LIMIT ?",
            (subject, max_hs)
        ).fetchall()

    cards = [dict(r) for r in rows]

    for c in cards:
        c['label'] = card_label(c.get('code'), c['hors_serie'])

    if not cards:
        conn.close()
        return jsonify([])

    # Ethan: Keep recently drawn cards out when possible.
    recent = conn.execute(
        """
        SELECT numero
        FROM draw_history
        WHERE subject = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (subject, max(n * 2, 5)),
    ).fetchall()

    recent_numbers = {r["numero"] for r in recent}
    pool = [c for c in cards if c["numero"] not in recent_numbers]

    if len(pool) < n:
        # Flying: Not enough cards left? Reuse the full pool rather than return nothing.
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
            # Steph: Hors-série gets the same baseline as a difficulty-5 card.
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

    # Eliot: Random first, chapter-friendly ordering after. That's deliberate.
    reordered = interleave_by_chapitre(chosen)

    # Flying: Paris time, like every other table — the UTC default used to
    # shift the weekly recap's "tirages de la semaine" around midnight.
    now_iso = now_paris().isoformat()
    conn.executemany(
        "INSERT INTO draw_history(subject, numero, created_at) VALUES(?,?,?)",
        [(subject, c["numero"], now_iso) for c in reordered],
    )

    conn.execute(
        "DELETE FROM draw_history WHERE subject=? AND id < (SELECT COALESCE(MAX(id), 0) - ? FROM draw_history WHERE subject=?)",
        (subject, DRAW_HISTORY_KEEP, subject),
    )

    log_event(
        conn,
        session.get('sr_user', 'anonyme'),
        'draw',
        ','.join(str(c['numero']) for c in reordered),
        subject=subject
    )

    conn.commit()
    conn.close()

    return jsonify(reordered)
