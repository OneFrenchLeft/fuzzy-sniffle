# -*- coding: utf-8 -*-
"""Weighted random draw of cards."""
from flask import Blueprint, jsonify, request, session
import random
from config import read_params, DRAW_HISTORY_KEEP, now_paris
from db import db, ensure_db, log_event
from helpers import card_label, interleave_by_chapitre
from auth import rate_limited

bp = Blueprint('draw', __name__)

# Impulse: The draw page is public by design, but every draw writes to
# draw_history (weekly recap stats) and events. A generous per-IP budget
# stops script spam without ever bothering a real colle session.
DRAW_RATE_MAX = 40
DRAW_RATE_WINDOW = 300


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


def draw_candidates(conn, params, kholle_mode=False, hs_mode=False):
    """Pool de cartes tirables, avec les memes filtres que le vrai tirage.

    En mode kholle, les fiches marquees kholle_enabled = 0 sont exclues de la
    simulation (ca ne change rien a la repetition espacee, qui lit sr_state_user).
    """
    max_active = int(params.get("max_active_num", 36))
    max_hs = int(params.get("max_hors_serie_num", 0))
    max_kholle = int(params.get("max_kholle_num", 0))
    plafond = max_kholle if (kholle_mode and max_kholle > 0) else max_active
    kholle_filter = " AND kholle_enabled = 1" if kholle_mode else ""

    rows = conn.execute(
        "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
        "FROM forgecards WHERE numero <= ? AND hors_serie = 0" + kholle_filter + " ORDER BY numero ASC",
        (plafond,)
    ).fetchall()

    if hs_mode and max_hs > 0:
        rows += conn.execute(
            "SELECT numero, code, titre, chapitre, fiche_file, correction_file, bareme_file, indices, teacher_difficulty, hors_serie "
            "FROM forgecards WHERE hors_serie = 1" + kholle_filter + " ORDER BY numero ASC LIMIT ?",
            (max_hs,)
        ).fetchall()

    cards = [dict(r) for r in rows]
    for c in cards:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    return cards


def compute_weights(cards, pool, params):
    """Poids du tirage pondere : bonus chapitre precedent / dernier chapitre,
    influence de la difficulte professeur. Meme regle que le tirage reel."""
    previous_bonus = float(params.get("previous_chapter_bonus", 0.2))
    last_bonus = float(params.get("last_chapter_bonus", 0.4))
    diff_weight_param = float(params.get("teacher_difficulty_weight", 0.15))

    DIFF_MID = 2.5
    DIFF_HALF_RANGE = 2.5

    # Ethan: Le « dernier chapitre » suit la progression du cours, donc les
    # fiches normales uniquement. draw_candidates append les hors-serie a la
    # fin de la liste : sans ce garde, inclure H1 faisait basculer le bonus
    # « dernier chapitre » sur le chapitre de H1 (souvent celui de la fiche 1,
    # p. ex. « Introduction »), au lieu de juste diluer les probabilités.
    sequence = [c for c in cards if not c.get("hors_serie")] or cards
    last_chapter = sequence[-1]["chapitre"]
    previous_chapter = None
    for c in reversed(sequence):
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
    return weights


@bp.route('/api/draw', methods=['GET'])
def api_draw():
    ip = request.remote_addr or 'unknown'
    if rate_limited('draw_' + ip, max_attempts=DRAW_RATE_MAX, window=DRAW_RATE_WINDOW):
        return jsonify({'ok': False, 'error': 'trop de tirages, patiente un peu'}), 429
    ensure_db()
    params = read_params()

    try:
        n = max(1, min(10, int(request.args.get("count", 8))))
    except (TypeError, ValueError):
        # Small: Invalid count? Eight it is. No need to start a crisis.
        n = 8

    hs_mode = request.args.get("hors_serie") == "1"
    kholle_mode = request.args.get("kholle") == "1"

    conn = db()
    cards = draw_candidates(conn, params, kholle_mode=kholle_mode, hs_mode=hs_mode)

    if not cards:
        conn.close()
        return jsonify([])

    # Ethan: Keep recently drawn cards out when possible.
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
        # Flying: Not enough cards left? Reuse the full pool rather than return nothing.
        pool = cards

    weights = compute_weights(cards, pool, params)

    k = min(n, len(pool))
    chosen = weighted_sample_without_replacement(pool, weights, k)

    # Eliot: Random first, chapter-friendly ordering after. That's deliberate.
    reordered = interleave_by_chapitre(chosen)

    # Flying: Paris time, like every other table — the UTC default used to
    # shift the weekly recap's "tirages de la semaine" around midnight.
    now_iso = now_paris().isoformat()
    conn.executemany(
        "INSERT INTO draw_history(numero, created_at) VALUES(?,?)",
        [(c["numero"], now_iso) for c in reordered],
    )

    conn.execute(
        "DELETE FROM draw_history WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM draw_history)",
        (DRAW_HISTORY_KEEP,),
    )

    log_event(
        conn,
        session.get('sr_user', 'anonyme'),
        'draw',
        ','.join(str(c['numero']) for c in reordered)
    )

    conn.commit()
    conn.close()

    return jsonify(reordered)


# ---------- Page « Probabilites » (simulation Monte Carlo du tirage kholle) ----------

SIM_DEFAULT = 10000
SIM_MIN, SIM_MAX = 1000, 50000


@bp.route('/api/probabilites/data', methods=['GET'])
def api_probabilites_data():
    """Frequences observees sur N simulations du VRAI tirage kholle.

    Meme pool (kholle_enabled = 1), memes poids (compute_weights), meme
    echantillonnage sans remise (weighted_sample_without_replacement).
    Seul le filtre anti-repetition d'historique est ignore : il depend de la
    session, pas de la probabilite de base qu'on veut visualiser.
    Publique (la page /probabilites est accessible par URL, sans login).
    """
    ip = request.remote_addr or 'unknown'
    if rate_limited('proba_' + ip, max_attempts=60, window=DRAW_RATE_WINDOW):
        return jsonify({'ok': False, 'error': 'trop de simulations, patiente un peu'}), 429
    ensure_db()
    params = read_params()
    hs_mode = request.args.get('hors_serie') == '1'
    try:
        count = max(1, min(10, int(request.args.get('count', 1))))
    except (TypeError, ValueError):
        count = 1
    try:
        n_sims = max(SIM_MIN, min(SIM_MAX, int(request.args.get('sims', SIM_DEFAULT))))
    except (TypeError, ValueError):
        n_sims = SIM_DEFAULT

    conn = db()
    cards = draw_candidates(conn, params, kholle_mode=True, hs_mode=hs_mode)
    conn.close()
    if not cards:
        return jsonify({'ok': True, 'sims': n_sims, 'count': count, 'rows': []})

    weights = compute_weights(cards, cards, params)
    k = min(count, len(cards))
    hits = {c['numero']: 0 for c in cards}
    for _ in range(n_sims):
        for c in weighted_sample_without_replacement(cards, weights, k):
            hits[c['numero']] += 1

    rows = [{
        'numero': c['numero'],
        'label': c['label'],
        'chapitre': c.get('chapitre') or 'Autre',
        'hors_serie': bool(c.get('hors_serie')),
        'pct': round(100.0 * hits[c['numero']] / n_sims, 2),
    } for c in cards]
    rows.sort(key=lambda r: -r['pct'])
    return jsonify({'ok': True, 'sims': n_sims, 'count': count, 'rows': rows})
