# -*- coding: utf-8 -*-
"""Acces SQLite : connexion, schema, maintenance, journal, CSV."""
import sqlite3, json, io, csv, shutil
from datetime import datetime, timedelta
from flask import make_response
from config import ADMIN_PASSWORD, DB_PATH, PARAMS_PATH, UPLOADS, DATA, now_paris
from helpers import filenames_for, hs_sort_key
import secrets


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def _backfill_codes(conn):
    """Migre les anciens codes sans écraser un PDF ni inventer de correction."""
    created, obsolete = [], set()
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('SELECT numero, hors_serie, code, fiche_file, correction_file, bareme_file '
                            'FROM forgecards ORDER BY numero').fetchall()
        reserved = {r['code'].lower() for r in rows if r['code']}
        hs_idx = max((hs_sort_key(r['code']) for r in rows if r['hors_serie'] and r['code']), default=0)
        for row in rows:
            if row['code']:
                continue
            if row['hors_serie']:
                while True:
                    hs_idx += 1
                    code = f'h{hs_idx}'
                    if code not in reserved and not any((UPLOADS / name).exists() for name in filenames_for(code)):
                        break
            else:
                code = str(row['numero'])
            reserved.add(code)
            old_files = [row['fiche_file'], row['correction_file'], row['bareme_file']]
            new_files = [name if old else '' for old, name in zip(old_files, filenames_for(code))]
            for old_name, new_name in zip(old_files, new_files):
                source = UPLOADS / old_name if old_name else None
                if source and old_name != new_name and source.exists():
                    target = UPLOADS / new_name
                    # Création exclusive : un fichier existant ne peut jamais être remplacé.
                    with target.open('xb') as output:
                        created.append(target)
                        with source.open('rb') as stream:
                            shutil.copyfileobj(stream, output)
                    obsolete.add(source)
            conn.execute('UPDATE forgecards SET code=?, fiche_file=?, correction_file=?, bareme_file=? WHERE numero=?',
                         (code, *new_files, row['numero']))
        conn.commit()
    except Exception:
        conn.rollback()
        for target in created:
            target.unlink(missing_ok=True)
        conn.close()
        raise
    # Ne retirer les anciennes copies qu'après l'enregistrement réussi des références.
    referenced = {UPLOADS / name for row in conn.execute('SELECT fiche_file,correction_file,bareme_file FROM forgecards')
                  for name in row if name}
    for source in obsolete - referenced:
        try:
            source.unlink(missing_ok=True)
        except OSError as exc:
            print(f'[db] ancienne copie PDF conservée : {exc!r}')

def init_db():
    conn = db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS forgecards ("
        "numero INTEGER PRIMARY KEY,"
        "fiche_file TEXT NOT NULL,"
        "correction_file TEXT NOT NULL,"
        "bareme_file TEXT DEFAULT '',"
        "titre TEXT DEFAULT '',"
        "indices TEXT DEFAULT '',"
        "chapitre TEXT DEFAULT 'Autre',"
        "teacher_difficulty REAL,"
        "kholle_enabled INTEGER NOT NULL DEFAULT 1,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "prenom TEXT PRIMARY KEY,"
        "emoji_password TEXT,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute('''CREATE TABLE IF NOT EXISTS user_jokers (
        prenom TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0,
        last_milestone INTEGER DEFAULT 0,
        milestone_chain_start TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS joker_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prenom TEXT NOT NULL,
        day TEXT NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT NOT NULL,
        balance_after INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_joker_ledger_prenom ON joker_ledger(prenom, day)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_state_user ("
        "prenom TEXT NOT NULL,"
        "numero INTEGER NOT NULL,"
        "stability REAL,"
        "difficulty REAL,"
        "state TEXT DEFAULT 'new',"
        "last_review TEXT,"
        "next_review TEXT,"
        "repetitions INTEGER DEFAULT 0,"
        "lapses INTEGER DEFAULT 0,"
        "last_retrievability REAL,"
        "PRIMARY KEY (prenom, numero)"
        ")"
    )
    # Ankimie : SR (FSRS) sur les questions QCM — etats et reviews propres,
    # independants de la SR Forgecards (seule la retention cible est partagee).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_sr_state ("
        "prenom TEXT NOT NULL,"
        "qid TEXT NOT NULL,"
        "stability REAL,"
        "difficulty REAL,"
        "state TEXT DEFAULT 'new',"
        "last_review TEXT,"
        "next_review TEXT,"
        "repetitions INTEGER DEFAULT 0,"
        "lapses INTEGER DEFAULT 0,"
        "last_retrievability REAL,"
        "PRIMARY KEY (prenom, qid)"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qcm_sr_state_next_review ON qcm_sr_state(next_review)")
    # Availability is independent of the bank and of student review history.
    # Missing chapter rows retain the historical behaviour (active by default).
    conn.execute('''CREATE TABLE IF NOT EXISTS content_chapters (
        theme TEXT NOT NULL, chapitre TEXT NOT NULL,
        qcm_active INTEGER NOT NULL DEFAULT 1,
        ankimie_active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(theme, chapitre)
    )''')
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_sr_reviews ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "qid TEXT NOT NULL,"
        "prenom TEXT NOT NULL,"
        "chapitre TEXT DEFAULT '',"
        "result TEXT NOT NULL,"
        "grade INTEGER NOT NULL,"
        "correct INTEGER NOT NULL DEFAULT 0,"
        "was_new INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviews ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "numero INTEGER NOT NULL,"
        "prenom TEXT NOT NULL,"
        "result TEXT NOT NULL,"
        "note TEXT DEFAULT '',"
        "duration_seconds INTEGER,"
        "was_new INTEGER DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_daily_streak ("
        "prenom TEXT NOT NULL,"
        "day TEXT NOT NULL,"
        "validated INTEGER DEFAULT 0,"
        "PRIMARY KEY (prenom, day)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS draw_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "numero INTEGER NOT NULL,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sr_weights_user ("
        "prenom TEXT PRIMARY KEY,"
        "weights_json TEXT NOT NULL,"
        "nb_reviews_used INTEGER DEFAULT 0,"
        "loss_before REAL,"
        "loss_after REAL,"
        "trained_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qcm_invites (
            token TEXT PRIMARY KEY,
            prenom TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qcm_invites_expires "
        "ON qcm_invites(expires_at)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_answers ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "qid TEXT NOT NULL,"
        "theme TEXT NOT NULL,"
        "chapitre TEXT DEFAULT '',"
        "question TEXT DEFAULT '',"
        "prenom TEXT DEFAULT '',"
        "ok INTEGER NOT NULL DEFAULT 0,"
        "choice INTEGER,"
        "elapsed REAL,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qcm_answers_qid ON qcm_answers(qid)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "prenom TEXT NOT NULL,"
        "type TEXT NOT NULL,"
        "payload TEXT DEFAULT '',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_prenom ON events(prenom, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at)")

    # Marqueur de reinitialisation ADMIN des stats QCM : les vues admin ne
    # comptent que les reponses posterieures a reset_at. qcm_answers n'est
    # JAMAIS vide — les vues eleves ignorent cette table et lisent tout.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_stats_reset ("
        "qid TEXT NOT NULL,"
        "theme TEXT NOT NULL,"
        "reset_at TEXT NOT NULL,"
        "PRIMARY KEY (qid, theme)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS qcm_games ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "gid TEXT NOT NULL,"
        "themes TEXT DEFAULT '',"
        "nb_questions INTEGER DEFAULT 0,"
        "nb_players INTEGER DEFAULT 0,"
        "podium TEXT DEFAULT '',"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_prenom_created ON reviews(prenom, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_result_note ON reviews(result, note)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_numero ON reviews(numero)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviews_archive ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "numero INTEGER NOT NULL,"
        "prenom TEXT NOT NULL,"
        "result TEXT NOT NULL,"
        "note TEXT DEFAULT '',"
        "duration_seconds INTEGER,"
        "was_new INTEGER DEFAULT 0,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "note_masquee INTEGER DEFAULT 0,"
        "archived_at TEXT DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sr_state_user_next_review ON sr_state_user(next_review)")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(forgecards)").fetchall()]
    if 'titre' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN titre TEXT DEFAULT ''")
    if 'indices' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN indices TEXT DEFAULT ''")
    if 'bareme_file' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN bareme_file TEXT DEFAULT ''")
    if 'chapitre' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN chapitre TEXT DEFAULT 'Autre'")
    if 'teacher_difficulty' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN teacher_difficulty REAL")
    if 'hors_serie' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN hors_serie INTEGER DEFAULT 0")
    if 'code' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN code TEXT DEFAULT ''")
    # Exclusion de la simulation de kholle uniquement — la repetition espacee
    # (deck SR, FSRS) n'est pas touchee. 1 = incluse (defaut), 0 = hors simulation.
    if 'kholle_enabled' not in cols:
        conn.execute("ALTER TABLE forgecards ADD COLUMN kholle_enabled INTEGER NOT NULL DEFAULT 1")
    _backfill_codes(conn)

    user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'emoji_password' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN emoji_password TEXT")

    if 'sr_hs_enabled' not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN sr_hs_enabled INTEGER NOT NULL DEFAULT 0")

    sr_cols = [r[1] for r in conn.execute("PRAGMA table_info(sr_state_user)").fetchall()]
    if 'last_retrievability' not in sr_cols:
        conn.execute("ALTER TABLE sr_state_user ADD COLUMN last_retrievability REAL")

    rev_cols = [r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()]
    if 'note' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN note TEXT DEFAULT ''")
    if 'duration_seconds' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN duration_seconds INTEGER")
    if 'was_new' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN was_new INTEGER DEFAULT 0")

    joker_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_jokers)").fetchall()]
    if 'last_milestone' not in joker_cols:
        conn.execute("ALTER TABLE user_jokers ADD COLUMN last_milestone INTEGER DEFAULT 0")
    if 'milestone_chain_start' not in joker_cols:
        conn.execute("ALTER TABLE user_jokers ADD COLUMN milestone_chain_start TEXT")
    if 'note_masquee' not in rev_cols:
        conn.execute("ALTER TABLE reviews ADD COLUMN note_masquee INTEGER DEFAULT 0")

    qcm_ans_cols = [r[1] for r in conn.execute("PRAGMA table_info(qcm_answers)").fetchall()]
    if 'choice' not in qcm_ans_cols:
        conn.execute("ALTER TABLE qcm_answers ADD COLUMN choice INTEGER")

    qcm_rev_cols = [r[1] for r in conn.execute("PRAGMA table_info(qcm_sr_reviews)").fetchall()]
    if 'duration_seconds' not in qcm_rev_cols:
        conn.execute("ALTER TABLE qcm_sr_reviews ADD COLUMN duration_seconds INTEGER")

    # Undo Ankimie : etat FSRS precedent la derniere review de chaque eleve
    # (une seule profondeur) pour "annuler la derniere note".
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ankimie_undo ("
        "prenom TEXT PRIMARY KEY,"
        "review_id INTEGER NOT NULL,"
        "qid TEXT NOT NULL,"
        "prev_stability REAL, prev_difficulty REAL, prev_state TEXT,"
        "prev_last_review TEXT, prev_next_review TEXT,"
        "prev_repetitions INTEGER, prev_lapses INTEGER,"
        "prev_last_retrievability REAL,"
        "created_at TEXT NOT NULL"
        ")"
    )

    conn.execute('CREATE INDEX IF NOT EXISTS idx_reviews_prenom_date ON reviews(prenom, created_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_qcm_prenom_ok ON qcm_answers(prenom, ok)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_streak_prenom_day ON sr_daily_streak(prenom, day)')
    conn.commit()
    conn.close()

def ensure_db():
    global _db_ready
    if _db_ready:
        return
    init_db()
    try:
        conn = db()
        cutoff = (now_paris() - timedelta(days=400)).isoformat()
        # L'identité stable du compte survit à la rétention du journal d'activité.
        conn.execute("DELETE FROM events WHERE created_at < ? AND type NOT IN ('account_identity','account_deleted')", (cutoff,))
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f'[db] purge events impossible: {exc!r}')
    _db_ready = True

def log_event(conn, prenom, etype, payload=''):
    if not prenom:
        prenom = 'anonyme'
    try:
        conn.execute('INSERT INTO events(prenom, type, payload, created_at) VALUES(?,?,?,?)',
                     (prenom, etype, str(payload)[:500], now_paris().isoformat()))
    except Exception as exc:
        print(f'[events] log impossible: {exc!r}')

def csv_safe(value):

    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value

def csv_response(output, filename):

    resp = make_response('\ufeff' + output.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp

UPLOADS.mkdir(parents=True, exist_ok=True)

DATA.mkdir(parents=True, exist_ok=True)

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_hex(16)
    # Impulse: Push it back into config so auth.py can actually see it.
    # Otherwise the temp password we print below is just decoration.
    import config as _config
    _config.ADMIN_PASSWORD = ADMIN_PASSWORD
    print('!' * 60)
    print('MADEC_ADMIN_PASSWORD non defini dans .env')
    print('Mot de passe admin temporaire pour cette session :', ADMIN_PASSWORD)
    print('Definis MADEC_ADMIN_PASSWORD dans .env pour le rendre persistant.')
    print('!' * 60)

_db_ready = False



def apply_joker_change(conn, prenom, delta, reason, day=None):
    """Toute modification du stock de jokers passe par ici (append-only).

    Met a jour user_jokers.count (borne [0, JOKER_CAP]) et ecrit la ligne
    correspondante dans joker_ledger avec le solde apres operation — ce qui
    rend le stock entierement auditable et reconstructible.
    Doit etre appelee dans une transaction (BEGIN IMMEDIATE pour les depenses).
    Retourne le solde apres operation.
    """
    from config import JOKER_CAP
    # UPDATE puis INSERT si absent : schema-agnostique. La base du serveur de
    # test a ete migree par feat/multi-matieres, ou la PK est (prenom, subject)
    # — ON CONFLICT(prenom) y leve "does not match any PRIMARY KEY".
    cur = conn.execute(
        "UPDATE user_jokers SET count = max(0, min(count + ?, ?)) WHERE prenom=?",
        (delta, JOKER_CAP, prenom))
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO user_jokers (prenom, count) VALUES (?, max(0, min(?, ?)))",
            (prenom, delta, JOKER_CAP))
    bal = conn.execute('SELECT count FROM user_jokers WHERE prenom=?', (prenom,)).fetchone()['count']
    conn.execute(
        "INSERT INTO joker_ledger(prenom, day, delta, reason, balance_after, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (prenom, day or now_paris().date().isoformat(), delta, reason, bal,
         now_paris().isoformat()))
    return bal
