"""Chapter availability, separate from the QCM bank.

Ankimie ne gere plus de collections : elle pioche automatiquement dans
toutes les questions des chapitres actifs en QCM. L'interrupteur
qcm_active pilote donc les deux applications d'un coup.
"""
from db import db, ensure_db


def chapter_name(value):
    return str(value or '').strip() or 'Autre'


def settings():
    ensure_db()
    conn = db()
    try:
        return {(r['theme'], r['chapitre']): dict(r)
                for r in conn.execute('SELECT * FROM content_chapters')}
    finally:
        conn.close()


def qcm_is_active(theme, chapter, configuration):
    return bool(configuration.get((theme, chapter_name(chapter)), {}).get('qcm_active', 1))


def chapter_overview(theme, questions):
    configuration = settings()
    chapters = {chapter_name(q.get('chapitre')) for q in questions}
    chapters.update(ch for t, ch in configuration if t == theme)
    out = []
    for ch in sorted(chapters, key=str.casefold):
        flags = configuration.get((theme, ch), {})
        count = sum(chapter_name(q.get('chapitre')) == ch for q in questions)
        active = bool(flags.get('qcm_active', 1))
        out.append({'chapitre': ch,
                    'qcm_count': count,
                    # Collection Ankimie = le chapitre entier, chimie uniquement.
                    'ankimie_count': count if theme == 'chimie' else 0,
                    'qcm_active': active,
                    'ankimie_active': active if theme == 'chimie' else False})
    return out


def ankimie_questions(banks, active_only=True):
    """Toutes les questions CHIMIE des chapitres actifs en QCM, sans gestion.

    Ankimie est et reste un deck chimie : les autres matieres ne sont jamais
    piochees, meme si leurs chapitres sont actifs en QCM.
    """
    configuration = settings()
    selected = {}
    for theme, questions in banks.items():
        if theme != 'chimie':
            continue
        for q in questions:
            ch = chapter_name(q.get('chapitre'))
            if active_only and not qcm_is_active(theme, ch, configuration):
                continue
            # One FSRS state per question, even if it belongs to two themes.
            selected.setdefault(q['qid'], {**q, 'chapitre': ch})
    return list(selected.values())
