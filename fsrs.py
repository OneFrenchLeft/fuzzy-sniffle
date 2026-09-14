import math
import random
from datetime import datetime, timedelta


# Poids FSRS-4.5 (defauts ajustes pour Forgecards).
# A retention 0.80 : premieres apparitions 1j / 3j / 8j / 21j.
# A retention 0.90 : 1j / 1j / 3j / 9j (l'intervalle initial ~ la stabilite en jours).
DEFAULT_WEIGHTS = [
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975,
    0.031, 1.6474, 0.10, 1.0461, 2.1072, 0.0793, 0.3246,
    1.587, 0.15, 1.5,
]


DECAY = -0.5
FACTOR = 19 / 81
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0
MIN_STABILITY = 0.01
MAX_INTERVAL_DAYS = 50


DEFAULT_RETENTION = 0.90
MIN_RETENTION = 0.80
MAX_RETENTION = 0.99


GRADE_AGAIN = 1
GRADE_HARD = 2
GRADE_GOOD = 3
GRADE_EASY = 4


GRADE_LABELS = {
    GRADE_AGAIN: "PAS_REUSSI",
    GRADE_HARD: "DIFFICILE",
    GRADE_GOOD: "J_AI_DU_REFLECHIR",
    GRADE_EASY: "ULTRA_FACILE",
}


# Influence de la difficulte professeur (echelle 1-5) :
# - a l'initialisation : D0 est tire vers 2*pd (mix) et S0 est reduit (S_BASE - SLOPE*pd)
# - a CHAQUE review : le gain de stabilite est reduit de PROF_REVIEW_SLOPE par point
#   au-dessus de 3/5, et la difficulte FSRS est tiree de PROF_DIFFICULTY_PULL vers 2*pd,
#   ce qui empeche la mean reversion d'effacer le signal prof au fil des reviews.
# => plus une carte est difficile, plus elle revient vite, durablement.
PROF_DIFFICULTY_MIX = 0.45
PROF_DIFFICULTY_S_BASE = 1.25
PROF_DIFFICULTY_SLOPE = 0.10
PROF_REVIEW_SLOPE = 0.05
PROF_DIFFICULTY_PULL = 0.10


# Bornes de l'optimiseur : chaque borne doit contenir le poids par defaut,
# sinon L-BFGS-B demarre hors domaine et l'entrainement echoue silencieusement.
WEIGHT_BOUNDS = [
    (0.01, 15), (0.01, 15), (0.01, 30), (0.1, 40), (1, 10),
    (0.01, 5), (0.01, 5), (0, 0.8), (0.01, 2.5), (0.01, 0.5),
    (0.01, 5), (0.01, 5), (0.01, 0.3), (0.01, 5), (0.01, 5),
    (0, 5), (0.01, 5),
]


MIN_REVIEWS_FOR_OPTIMIZATION = 400


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def retrievability(t_days, stability):
    if stability is None:
        return 0.0
    stability = max(stability, MIN_STABILITY)
    if t_days <= 0:
        return 1.0
    return (1 + FACTOR * t_days / stability) ** DECAY


def next_interval(stability, requested_retention, fuzz=True):
    r = clamp(requested_retention, MIN_RETENTION, MAX_RETENTION)
    raw = (stability / FACTOR) * (r ** (1 / DECAY) - 1)
    interval = max(1.0, raw)
    if fuzz and interval >= 2.5:
        fuzz_range = interval * 0.05
        interval += random.uniform(-fuzz_range, fuzz_range)
    return int(round(clamp(interval, 1, MAX_INTERVAL_DAYS)))


def prof_stability_factor(prof_difficulty):
    """Facteur multiplicatif sur le gain de stabilite a chaque review
    (1.0 = neutre jusqu'a 3/5 ; 0.95 a 4/5 ; 0.90 a 5/5)."""
    if prof_difficulty is None:
        return 1.0
    pd = clamp(float(prof_difficulty), 1, 5)
    return max(0.5, min(1.0, 1.0 - PROF_REVIEW_SLOPE * (pd - 3)))


def initial_difficulty(grade, w=DEFAULT_WEIGHTS, prof_difficulty=None):
    d0 = w[4] - (grade - 3) * w[5]
    if prof_difficulty is not None:
        pd = clamp(float(prof_difficulty), 1, 5)
        d0 = (1 - PROF_DIFFICULTY_MIX) * d0 + PROF_DIFFICULTY_MIX * 2 * pd
    return clamp(d0, MIN_DIFFICULTY, MAX_DIFFICULTY)


def initial_stability(grade, w=DEFAULT_WEIGHTS, prof_difficulty=None):
    s0 = max(MIN_STABILITY, w[grade - 1])
    if prof_difficulty is not None:
        pd = clamp(float(prof_difficulty), 1, 5)
        s0 = max(MIN_STABILITY, s0 * (PROF_DIFFICULTY_S_BASE - PROF_DIFFICULTY_SLOPE * pd))
    return s0


def mean_reversion_difficulty(d, grade, w=DEFAULT_WEIGHTS):
    d0_good = w[4] - (GRADE_GOOD - 3) * w[5]
    delta_d = -w[6] * (grade - 3)
    d_prime = d + delta_d * (10 - d) / 9
    d_double_prime = w[7] * d0_good + (1 - w[7]) * d_prime
    return clamp(d_double_prime, MIN_DIFFICULTY, MAX_DIFFICULTY)


def stability_after_success(d, s, r, grade, w=DEFAULT_WEIGHTS, prof_difficulty=None):
    hard_penalty = w[15] if grade == GRADE_HARD else 1.0
    easy_bonus = w[16] if grade == GRADE_EASY else 1.0
    s_inc = (
        math.exp(w[8]) * (11 - d) * (s ** -w[9]) *
        (math.exp(w[10] * (1 - r)) - 1) * hard_penalty * easy_bonus
        * prof_stability_factor(prof_difficulty) + 1
    )
    return max(MIN_STABILITY, s * s_inc)


def stability_after_forget(d, s, r, w=DEFAULT_WEIGHTS):
    s_forget = w[11] * (d ** -w[12]) * (((s + 1) ** w[13]) - 1) * math.exp(w[14] * (1 - r))
    return max(MIN_STABILITY, min(s_forget, s))


def same_day_stability(s, grade):
    # Modele maison pour les revisions a moins de 24 h d'intervalle :
    # le FSRS standard n'est pas calibre pour ce cas, on applique un facteur fixe.
    factors = {
        GRADE_AGAIN: 0.45,
        GRADE_HARD: 0.92,
        GRADE_GOOD: 1.12,
        GRADE_EASY: 1.25,
    }
    return max(MIN_STABILITY, s * factors[grade])


def apply_review(stability, difficulty, last_review_iso, grade, now,
                 requested_retention=DEFAULT_RETENTION, w=DEFAULT_WEIGHTS,
                 fuzz=True, prof_difficulty=None):
    if stability is None or difficulty is None:
        # Initialisation de la carte : la difficulte professeur module S0 et D0.
        new_difficulty = initial_difficulty(grade, w, prof_difficulty)
        new_stability = initial_stability(grade, w, prof_difficulty)
        state, elapsed_days, r_before = "review", 0.0, None
    else:
        last_review = datetime.fromisoformat(last_review_iso or now.isoformat())
        elapsed_days = max(0.0, (now - last_review).total_seconds() / 86400)
        r_before = retrievability(elapsed_days, stability)
        new_difficulty = mean_reversion_difficulty(difficulty, grade, w)
        if prof_difficulty is not None:
            # Traction de D vers la difficulte prof (2*pd sur l'echelle 1-10)
            target = clamp(2 * float(prof_difficulty), MIN_DIFFICULTY, MAX_DIFFICULTY)
            new_difficulty = clamp((1 - PROF_DIFFICULTY_PULL) * new_difficulty + PROF_DIFFICULTY_PULL * target,
                                   MIN_DIFFICULTY, MAX_DIFFICULTY)

        if elapsed_days < 1:
            new_stability = same_day_stability(stability, grade)
            state = "review" if grade != GRADE_AGAIN else "relearning"
        else:
            if grade == GRADE_AGAIN:
                new_stability = stability_after_forget(difficulty, stability, r_before, w)
                state = "relearning"
            else:
                new_stability = stability_after_success(difficulty, stability, r_before, grade, w,
                                                        prof_difficulty=prof_difficulty)
                state = "review"

    interval_days = next_interval(new_stability, requested_retention, fuzz=fuzz)
    next_review_date = (now + timedelta(days=interval_days)).date().isoformat()

    return {
        "stability": round(new_stability, 4),
        "difficulty": round(new_difficulty, 4),
        "state": state,
        "next_review": next_review_date,
        "elapsed_days": round(elapsed_days, 2),
        "retrievability_before": round(r_before, 4) if r_before is not None else None,
        "interval_days": interval_days,
    }


def preview_all_grades(stability, difficulty, last_review_iso, now,
                       requested_retention=DEFAULT_RETENTION,
                       w=DEFAULT_WEIGHTS, prof_difficulty=None):
    preview = {}
    for grade, label in GRADE_LABELS.items():
        result = apply_review(
            stability, difficulty, last_review_iso, grade, now,
            requested_retention, w, fuzz=False,
            prof_difficulty=prof_difficulty,
        )
        preview[label] = {
            key: result[key]
            for key in ("next_review", "interval_days", "stability", "difficulty")
        }
    return preview


def humanize_interval(days):
    if days < 1:
        return "< 1 j"
    if days < 30:
        return f"{days} j"
    if days < 365:
        months = days / 30.44
        return f"{months:.1f} mois"
    years = days / 365.25
    return f"{years:.1f} an" + ("s" if years >= 2 else "")


def build_sequences_from_reviews(rows):
    from collections import defaultdict
    GRADE_MAP = {"again": 1, "hard": 2, "good": 3, "easy": 4}
    by_card = defaultdict(list)
    for r in rows:
        by_card[r["numero"]].append(r)
    sequences = []
    for numero, revs in by_card.items():
        revs = sorted(revs, key=lambda r: r["created_at"])
        seq = []
        prev_dt = None
        for r in revs:
            dt = datetime.fromisoformat(r["created_at"])
            grade = GRADE_MAP.get(r["result"])
            if grade is None:
                continue
            elapsed = 0.0 if prev_dt is None else max(0.0, (dt - prev_dt).total_seconds() / 86400)
            seq.append((elapsed, grade))
            prev_dt = dt
        if len(seq) >= 2:
            sequences.append(seq)
    return sequences


def _fsrs_loss(x, sequences):
    # NB : entraine le FSRS sans l'influence prof (a priori pedagogue, pas une
    # propriete de l'eleve) ; les poids appris restent transferables.
    w = list(x)
    total_loss = 0.0
    count = 0
    for seq in sequences:
        stability = None
        difficulty = None
        for elapsed, grade in seq:
            if stability is None:
                difficulty = initial_difficulty(grade, w)
                stability = initial_stability(grade, w)
                continue
            new_difficulty = mean_reversion_difficulty(difficulty, grade, w)
            if elapsed < 1:
                # Revision intra-journee : pas de terme de perte fiable,
                # on met juste a jour la stabilite comme apply_review.
                stability = same_day_stability(stability, grade)
                difficulty = new_difficulty
                continue
            r = retrievability(elapsed, stability)
            label = 1.0 if grade > 1 else 0.0
            p = clamp(r, 1e-6, 1 - 1e-6)
            total_loss += -(label * math.log(p) + (1 - label) * math.log(1 - p))
            count += 1
            if grade == GRADE_AGAIN:
                stability = stability_after_forget(difficulty, stability, r, w)
            else:
                stability = stability_after_success(difficulty, stability, r, grade, w)
            difficulty = new_difficulty
    if count == 0:
        return 999.0
    return total_loss / count


def train_weights(sequences, init_weights=None):
    from scipy.optimize import minimize

    x0 = list(init_weights) if init_weights else list(DEFAULT_WEIGHTS)
    nb_reviews = sum(max(0, len(seq) - 1) for seq in sequences)
    loss_before = _fsrs_loss(x0, sequences)

    if nb_reviews < MIN_REVIEWS_FOR_OPTIMIZATION:
        return list(x0), nb_reviews, loss_before, loss_before

    result = minimize(
        _fsrs_loss, x0, args=(sequences,), method="L-BFGS-B",
        bounds=WEIGHT_BOUNDS, options={"maxiter": 300}
    )
    loss_after = _fsrs_loss(result.x, sequences)

    if loss_after >= loss_before or not result.success:
        return list(x0), nb_reviews, loss_before, loss_before

    return [round(v, 4) for v in result.x], nb_reviews, loss_before, loss_after
