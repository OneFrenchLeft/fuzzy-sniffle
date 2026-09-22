from werkzeug.utils import secure_filename
from config import UPLOADS, ALLOWED_EXTENSIONS, MAX_REVIEW_DURATION_SECONDS


def current_subject():
    """Matiere active lue dans la session ; physique par defaut, y compris
    hors contexte requete (bot, scripts). Les requetes SQL se brancheront
    dessus en phase 3 — pour l'instant tout reste en physique."""
    from flask import session
    from config import ENABLED_SUBJECTS, DEFAULT_SUBJECT
    try:
        s = session.get('subject')
    except RuntimeError:
        return DEFAULT_SUBJECT
    return s if s in ENABLED_SUBJECTS else DEFAULT_SUBJECT


def allowed_file(filename):
    # Xiao: Extension check. Please don't trust the filename itself.
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def is_real_pdf(file_storage):
    # Xiao: An extension proves nothing. Check the %PDF- magic bytes instead.
    try:
        head = file_storage.stream.read(5)
        file_storage.stream.seek(0)
    except Exception:
        return False
    return head == b'%PDF-'


def parse_teacher_difficulty(raw):
    # Small: Clamp it here so nobody has to remember the range later.
    if raw in (None, ''):
        return None
    try:
        return min(max(float(raw), 1.0), 5.0)
    except (TypeError, ValueError):
        return None


def parse_duration_seconds(raw):
    # Ethan: Same idea. Bad input gets rejected quietly instead of breaking the route.
    if raw is None:
        return None
    try:
        return min(max(int(raw), 0), MAX_REVIEW_DURATION_SECONDS)
    except (TypeError, ValueError):
        return None


def hs_sort_key(code):
    # Flying: If this isn't numeric, send it to the end. Nice and boring.
    try:
        return int(str(code)[1:])
    except (TypeError, ValueError):
        return 10 ** 9


def card_label(code, hors_serie):
    # Steph: Uppercase hors-série labels, because apparently consistency was optional.
    code = code or ''
    return code.upper() if hors_serie else code


def filenames_for(code):
    # Eliot: Keep filenames generated here. One source of truth, please.
    return (
        secure_filename(f'{code}.pdf'),
        secure_filename(f'{code}-c.pdf'),
        secure_filename(f'{code}-b.pdf'),
    )


def remove_pdf_if_exists(filename):
    if not filename:
        return
    path = UPLOADS / filename
    if path.exists():
        path.unlink()


def card_exists(conn, numero):
    return conn.execute(
        'SELECT 1 FROM forgecards WHERE numero=?',
        (numero,)
    ).fetchone() is not None


def interleave_by_chapitre(cards):
    # Xiao: Avoid repeating the same chapter when another card is available.
    # Small: "Available" being the important part. Don't invent cards.
    reordered = []
    remaining = cards[:]
    last_chapitre = None

    while remaining:
        idx = None

        for i, c in enumerate(remaining):
            if c.get('chapitre') != last_chapitre:
                idx = i
                break

        if idx is None:
            # Ethan: Fine, we're out of alternatives. Pick one and move on.
            idx = 0

        picked = remaining.pop(idx)
        reordered.append(picked)
        last_chapitre = picked.get('chapitre')

    return reordered
