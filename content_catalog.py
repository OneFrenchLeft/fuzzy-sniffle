"""Chapter availability and Ankimie collections, separate from the QCM bank."""
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


def memberships():
    ensure_db()
    conn = db()
    try:
        return {(r['theme'], r['chapitre'], r['qid']): bool(r['included'])
                for r in conn.execute('SELECT * FROM ankimie_memberships')}
    finally:
        conn.close()


def qcm_is_active(theme, chapter, configuration):
    return bool(configuration.get((theme, chapter_name(chapter)), {}).get('qcm_active', 1))


def collection_questions(theme, chapter, questions, associations):
    """Legacy chemistry membership is implicit; explicit removals override it."""
    return [q for q in questions if associations.get(
        (theme, chapter, q['qid']),
        theme == 'chimie' and chapter_name(q.get('chapitre')) == chapter)]


def chapter_overview(theme, questions):
    configuration, associations = settings(), memberships()
    chapters = {chapter_name(q.get('chapitre')) for q in questions}
    chapters.update(ch for t, ch in configuration if t == theme)
    chapters.update(ch for t, ch, _ in associations if t == theme)
    out = []
    for ch in sorted(chapters, key=str.casefold):
        flags = configuration.get((theme, ch), {})
        out.append({'chapitre': ch,
                    'qcm_count': sum(chapter_name(q.get('chapitre')) == ch for q in questions),
                    'ankimie_count': len(collection_questions(theme, ch, questions, associations)),
                    'qcm_active': bool(flags.get('qcm_active', 1)),
                    'ankimie_active': bool(flags.get('ankimie_active', theme == 'chimie'))})
    return out


def ankimie_questions(banks, active_only=True):
    configuration, associations = settings(), memberships()
    selected = {}
    for theme, questions in banks.items():
        chapters = {chapter_name(q.get('chapitre')) for q in questions}
        chapters.update(ch for t, ch, _ in associations if t == theme)
        for ch in sorted(chapters, key=str.casefold):
            if active_only and not configuration.get((theme, ch), {}).get('ankimie_active', theme == 'chimie'):
                continue
            for q in collection_questions(theme, ch, questions, associations):
                # One FSRS state per question, even if it belongs to two collections.
                selected.setdefault(q['qid'], {**q, 'chapitre': ch})
    return list(selected.values())
