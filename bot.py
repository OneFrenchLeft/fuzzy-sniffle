import asyncio
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bot Discord Forgecards — lit la base du site.
# mode=rw necessaire pour la validation de streak 23h55 et les jokers.
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
FORGECARDS_DB = Path(os.environ.get('FORGECARDS_DB', BASE / 'data' / 'forgecards.db'))
PARAMS_PATH = Path(os.environ.get('FORGECARDS_PARAMS', BASE / 'data' / 'params.json'))

BOT_DB = BASE / 'data' / 'bot.db'
TZ_PARIS = ZoneInfo('Europe/Paris')
NOTIF_ROLE_NAME = os.environ.get('MADEC_NOTIF_ROLE', 'Notifications')
REMINDER_HOUR = int(os.environ.get('MADEC_REMINDER_HOUR', '21'))
GUILD_ID = os.environ.get('MADEC_GUILD_ID')
SITE_URL = os.environ.get('MADEC_SITE_URL', 'https://madec.moyart.net')
DEFAULT_PARAMS = {'max_active_num': 36, 'daily_new_limit': 3, 'daily_review_limit': 3}
JOKER_CAP = 2
JOKER_EVERY = 4
BOT_EXCLUDED_PRENOMS = frozenset({'admin'})

# Matieres : memes flags que le site (CHIMIE=True, MATHS=True dans le .env).
# Physique est toujours active. A garder en sync avec config.py.
SUBJECT_META = {
    'physique': {'label': 'Physique', 'env': None},
    'chimie': {'label': 'Chimie', 'env': 'CHIMIE'},
    'maths': {'label': 'Maths', 'env': 'MATHS'},
}


def subject_enabled(key):
    meta = SUBJECT_META.get(key)
    if not meta:
        return False
    if meta['env'] is None:
        return True
    return os.environ.get(meta['env'], '').strip().lower() in ('1', 'true', 'yes', 'on')


ENABLED_SUBJECTS = [k for k in SUBJECT_META if subject_enabled(k)]

PARAMS_PATHS = {
    'physique': PARAMS_PATH,
    'chimie': BASE / 'data' / 'params_chimie.json',
    'maths': BASE / 'data' / 'params_maths.json',
}



INTERNAL_API_KEY = os.environ.get('MADEC_INTERNAL_API_KEY')
QCM_INVITE_API_URL = os.environ.get(
    'MADEC_INTERNAL_API_URL',
    'http://127.0.0.1:5000/api/internal/qcm-invites'
)


# ---------- Acces donnees ----------
def prenom_for_discord(discord_id):
    conn = bot_db()
    row = conn.execute('SELECT prenom FROM links WHERE discord_id=?', (int(discord_id),)).fetchone()
    conn.close()
    return row[0] if row else None


def discord_for_prenom(prenom):
    conn = bot_db()
    row = conn.execute('SELECT discord_id FROM links WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    return row[0] if row else None


def emoji_password_for(prenom):
    conn = fc_db()
    row = conn.execute('SELECT emoji_password FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def fc_db():
    """Connexion lecture/ecriture a la base du site (busy_timeout pour la concurrence)."""
    conn = sqlite3.connect(f'file:{FORGECARDS_DB}?mode=rw', uri=True)
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def bot_db():
    conn = sqlite3.connect(BOT_DB)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS links ('
        'prenom TEXT PRIMARY KEY,'
        'discord_id INTEGER NOT NULL UNIQUE,'
        'notifications INTEGER NOT NULL DEFAULT 1)'
    )
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    return conn


def get_setting(key):
    conn = bot_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key, value):
    conn = bot_db()
    conn.execute('INSERT INTO settings(key, value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    conn.commit()
    conn.close()


def read_params(subject='physique'):
    path = PARAMS_PATHS.get(subject, PARAMS_PATHS['physique'])
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = {}
    out = DEFAULT_PARAMS.copy()
    for k in out:
        try:
            out[k] = int(data.get(k, out[k]))
        except (TypeError, ValueError):
            pass
    return out


def today_paris():
    return datetime.now(TZ_PARIS).date().isoformat()


def compute_daily(conn, prenom, params, subject='physique'):
    """Decompose la journee : dues, faites, total (capte par quota), restantes."""
    today = today_paris()
    max_active = params['max_active_num']
    rows = conn.execute(
        'SELECT s.repetitions, s.next_review FROM forgecards f '
        'LEFT JOIN sr_state_user s ON s.numero = f.numero AND s.prenom = ? AND s.subject = ? '
        'WHERE f.subject = ? AND f.numero <= ?',
        (prenom, subject, subject, max_active)
    ).fetchall()
    due_new = due_review = 0
    for reps, next_review in rows:
        reps = reps or 0
        if next_review is None or next_review <= today:
            if reps == 0:
                due_new += 1
            else:
                due_review += 1
    done = conn.execute(
        'SELECT was_new, COUNT(*) FROM reviews WHERE prenom=? AND subject=? AND substr(created_at,1,10)=? GROUP BY was_new',
        (prenom, subject, today)
    ).fetchall()
    new_done = sum(c for w, c in done if w)
    review_done = sum(c for w, c in done if not w)
    total = min(params['daily_new_limit'], due_new) + min(params['daily_review_limit'], due_review)
    remaining = (max(0, min(params['daily_new_limit'], due_new) - new_done)
                 + max(0, min(params['daily_review_limit'], due_review) - review_done))
    return {'due_new': due_new, 'due_review': due_review, 'new_done': new_done,
            'review_done': review_done, 'total': total, 'done': new_done + review_done,
            'remaining': remaining}


def compute_remaining(conn, prenom, params, subject='physique'):
    return compute_daily(conn, prenom, params, subject)['remaining']


def compute_streak(conn, prenom, subject='physique'):
    """Jours consecutifs avec >= 1 review OU jour valide (sr_daily_streak)."""
    rows = conn.execute(
        'SELECT DISTINCT substr(created_at,1,10) FROM reviews WHERE prenom=? AND subject=?',
        (prenom, subject)
    ).fetchall()
    days = {r[0] for r in rows}
    rows = conn.execute(
        'SELECT day FROM sr_daily_streak WHERE prenom=? AND subject=? AND validated>=1',
        (prenom, subject)
    ).fetchall()
    days |= {r[0] for r in rows}
    if not days:
        return 0
    cursor = datetime.now(TZ_PARIS).date()
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
        if cursor.isoformat() not in days:
            return 0
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def plural(n, mot, pluriel=None):
    return f"{n} {mot if n == 1 else (pluriel or mot + 's')}"


def progress_bar(done, total, width=12):
    if total <= 0:
        return '░' * width
    filled = max(0, min(width, round(width * done / total)))
    return '█' * filled + '░' * (width - filled)


def fmt_dur(s):
    m, s = divmod(int(s), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"


def fmt_grade(g):
    return str(int(g)) if float(g) == int(g) else str(g)


def ensure_joker_table():
    # Meme schema v2 que le site : stock de jokers par (prenom, subject).
    # Sans ca, un demarrage du bot avant le site recreait l'ancien schema
    # mono-matiere et cassait les PK composites. Self-heal aussi les tables
    # qui datent d'avant last_milestone (comme le faisait db.py / main).
    conn = fc_db()
    conn.execute('CREATE TABLE IF NOT EXISTS user_jokers ('
                 'prenom TEXT NOT NULL,'
                 'subject TEXT NOT NULL DEFAULT \'physique\','
                 'count INTEGER NOT NULL DEFAULT 0,'
                 'last_milestone INTEGER DEFAULT 0,'
                 'PRIMARY KEY (prenom, subject))')
    cols = [r[1] for r in conn.execute('PRAGMA table_info(user_jokers)').fetchall()]
    if 'subject' not in cols:
        # Ancien schema mono-matiere (prenom PRIMARY KEY) : on migre en v2
        # comme db.py, en rattachant l'existant a la matiere historique.
        old_cols = [r[1] for r in conn.execute('PRAGMA table_info(user_jokers)').fetchall()]
        if 'last_milestone' not in old_cols:
            conn.execute('ALTER TABLE user_jokers ADD COLUMN last_milestone INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE user_jokers RENAME TO user_jokers_old')
        conn.execute('CREATE TABLE user_jokers ('
                     'prenom TEXT NOT NULL,'
                     'subject TEXT NOT NULL DEFAULT \'physique\','
                     'count INTEGER NOT NULL DEFAULT 0,'
                     'last_milestone INTEGER DEFAULT 0,'
                     'PRIMARY KEY (prenom, subject))')
        conn.execute("INSERT INTO user_jokers(prenom, subject, count, last_milestone) "
                     "SELECT prenom, 'physique', count, last_milestone FROM user_jokers_old")
        conn.execute('DROP TABLE user_jokers_old')
    elif 'last_milestone' not in cols:
        conn.execute('ALTER TABLE user_jokers ADD COLUMN last_milestone INTEGER DEFAULT 0')
    conn.commit()
    conn.close()


def get_jokers(conn, prenom, subject='physique'):
    row = conn.execute('SELECT count FROM user_jokers WHERE prenom=? AND subject=?',
                       (prenom, subject)).fetchone()
    return row[0] if row else 0


async def dm_user(discord_id, text):
    if not discord_id:
        return
    try:
        u = await client.fetch_user(int(discord_id))
        await u.send(text)
    except Exception:
        pass


# ---------- Bot ----------

intents = discord.Intents.default()
# NB : le contenu des DM est toujours lisible par le bot ; l'intent privilegie
# message_content ne concerne que les messages des serveurs. Inutile ici.
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
madec = app_commands.Group(name='madec', description='Commandes Forgecards')
tree.add_command(madec)


class WrongChannel(app_commands.CheckFailure):
    pass


def channel_required():
    async def predicate(interaction: discord.Interaction) -> bool:
        ch = get_setting('channel_id')
        if ch is None:
            raise WrongChannel('Aucun salon configure. Un admin doit lancer /madec setchannel.')
        if interaction.channel_id != int(ch):
            raise WrongChannel(f'Pas ici. Je ne reponds que dans <#{ch}>.')
        return True
    return app_commands.check(predicate)


EXTRA_ADMIN_IDS = {int(x) for x in os.environ.get('MADEC_EXTRA_ADMINS', '').split(',') if x.strip()}


def is_madec_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in EXTRA_ADMIN_IDS:
        return True
    perms = getattr(interaction.user, 'guild_permissions', None)
    return bool(perms and perms.manage_guild)


def madec_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_madec_admin(interaction):
            return True
        raise app_commands.CheckFailure('Reserve aux admins.')
    return app_commands.check(predicate)


admin_only = madec_admin()


@tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, WrongChannel):
        msg = f'🚫 {error}'
    elif isinstance(error, app_commands.CheckFailure):
        msg = f'🔒 {error}'
    else:
        msg = 'NaN. Quelque chose a diverge; reessaie. 💥'
        print(f'[bot] erreur commande: {error!r}')
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@madec.command(name='setchannel', description='Definit le salon ou le bot parle (admin)')
@admin_only
async def setchannel(interaction: discord.Interaction):
    set_setting('channel_id', interaction.channel_id)
    await interaction.response.send_message(
        f"✅ Compris. Je ne parle que dans {interaction.channel.mention} maintenant.",
        ephemeral=True)


@madec.command(name='link', description='Relie un prenom du site a un compte Discord (admin)')
@admin_only
@channel_required()
@app_commands.describe(prenom='Prenom exact sur le site', membre='Compte Discord a relier')
async def link(interaction: discord.Interaction, prenom: str, membre: discord.Member):
    prenom = prenom.strip()
    conn = fc_db()
    exists = conn.execute('SELECT 1 FROM users WHERE prenom=?', (prenom,)).fetchone()
    conn.close()
    if not exists:
        await interaction.response.send_message(
            f"❌ `{prenom}` inconnu sur le site.", ephemeral=True)
        return
    conn = bot_db()
    conn.execute('DELETE FROM links WHERE discord_id=?', (membre.id,))
    conn.execute(
        'INSERT INTO links(prenom, discord_id, notifications) VALUES(?,?,1) '
        'ON CONFLICT(prenom) DO UPDATE SET discord_id=excluded.discord_id',
        (prenom, membre.id))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"`{prenom}` ↔ {membre.mention} : liaison etablie.", ephemeral=True)


@madec.command(name='delink', description="Retire le lien site/Discord d'un prenom (admin)")
@admin_only
@channel_required()
@app_commands.describe(prenom='Prenom sur le site')
async def delink(interaction: discord.Interaction, prenom: str):
    conn = bot_db()
    cur = conn.execute('DELETE FROM links WHERE prenom=?', (prenom.strip(),))
    conn.commit()
    conn.close()
    if cur.rowcount:
        await interaction.response.send_message(
            f"🔓 Liaison rompue pour `{prenom.strip()}`.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❌ `{prenom.strip()}` n'etait pas lie.", ephemeral=True)


@madec.command(name='password', description="Recoit ton mot de passe en DM")
@channel_required()
async def password(interaction: discord.Interaction):
    prenom = prenom_for_discord(interaction.user.id)
    if not prenom:
        await interaction.response.send_message("Tu n'es pas relié à un prenom.", ephemeral=True)
        return
    pw = emoji_password_for(prenom)
    if not pw:
        await interaction.response.send_message(
            "Aucun mot de passe enregistre pour toi. Demande a un admin d'en regenerer un.",
            ephemeral=True)
        return
    pretty = ' '.join(pw.replace('·', '|').split('|'))
    try:
        await interaction.user.send(
            f"Ton mot de passe pour le compte (**{prenom}**) :\n# {pretty}\nNe le partage pas.")
        await interaction.response.send_message('Mot de passe envoye en DM.', ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "Impossible de t'envoyer un DM, ouvre tes messages prives.", ephemeral=True)


@madec.command(name='notification', description='Active ou desactive tes rappels quotidiens')
@channel_required()
@app_commands.describe(etat='on ou off')
async def notification(interaction: discord.Interaction, etat: str):
    etat = etat.lower()
    if etat not in ('on', 'off'):
        await interaction.response.send_message("❌ `on` ou `off`.", ephemeral=True)
        return
    conn = bot_db()
    row = conn.execute('SELECT prenom FROM links WHERE discord_id=?', (interaction.user.id,)).fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message(
            "❌ Demande d'abord a un admin de te link.", ephemeral=True)
        return
    val = 1 if etat == 'on' else 0
    conn.execute('UPDATE links SET notifications=? WHERE discord_id=?', (val, interaction.user.id))
    conn.commit()
    conn.close()
    role = discord.utils.get(interaction.guild.roles, name=NOTIF_ROLE_NAME)
    if role:
        try:
            if val:
                await interaction.user.add_roles(role)
            else:
                await interaction.user.remove_roles(role)
        except discord.Forbidden:
            pass
    await interaction.response.send_message(
        "🔔 Noté, je passe te relancer." if val else "🔕 Compris, je te laisse tranquille.",
        ephemeral=True)



@madec.command(name='streak', description='Tes streaks par matiere, ou celle du membre mentionne')
@channel_required()
@app_commands.describe(membre='Optionnel : membre Discord')
async def streak(interaction: discord.Interaction, membre: discord.Member = None):
    target = membre or interaction.user
    prenom = prenom_for_discord(target.id)
    if not prenom:
        await interaction.response.send_message(
            f"❌ {target.display_name} n'est pas link.", ephemeral=True)
        return
    conn = fc_db()
    lines = [f"🔥 **{prenom}**"]
    for subject in ENABLED_SUBJECTS:
        s = compute_streak(conn, prenom, subject)
        jk = get_jokers(conn, prenom, subject)
        remaining = compute_remaining(conn, prenom, read_params(subject), subject)
        label = SUBJECT_META[subject]['label']
        line = f"• **{label}** : {plural(s, 'jour')} de suite"
        if jk:
            line += f" — 🃏×{jk}"
        if remaining:
            line += f" — encore {plural(remaining, 'carte')} aujourd'hui"
        lines.append(line)
    conn.close()
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@madec.command(name='progress', description='Ta progression du jour, par matiere')
@channel_required()
async def progress(interaction: discord.Interaction):
    prenom = prenom_for_discord(interaction.user.id)
    if not prenom:
        await interaction.response.send_message("Tu n'es pas relié à un prenom.", ephemeral=True)
        return
    conn = fc_db()
    lines = [f"📊 **{prenom}**"]
    for subject in ENABLED_SUBJECTS:
        d = compute_daily(conn, prenom, read_params(subject), subject)
        if d['total'] == 0:
            lines.append(f"• **{SUBJECT_META[subject]['label']}** : rien à faire 🎉")
            continue
        done = min(d['done'], d['total'])
        bar = progress_bar(done, d['total'])
        status = "✅ terminé !" if d['remaining'] == 0 else f"encore {plural(d['remaining'], 'carte')}"
        lines.append(f"• **{SUBJECT_META[subject]['label']}** : {done}/{d['total']} `{bar}` {status}")
    conn.close()
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)


# ---------- Duels ----------

DUELS = {}
DUEL_BY_USER = {}
_next_duel_id = [1]

DUEL_SEND_WINDOW_S = 90     # 1 min 30 pour envoyer les copies
DUEL_SEND_REMIND_S = 60     # rappel 30 s avant la fin de la fenetre d'envoi
DUEL_GRADE_WINDOW_S = 600   # 10 min pour donner ET confirmer sa note
DUEL_MAX_GRADE = 20


def parse_grade(text):
    """Accepte '14', '12,5', '12.5', '14/20'. Retourne None si invalide/hors bareme."""
    m = re.match(r'^\s*(\d+(?:[.,]\d+)?)\s*(?:/\s*\d+\s*)?$', text or '')
    if not m:
        return None
    try:
        g = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    return g if 0 <= g <= DUEL_MAX_GRADE else None


class Duel:
    """Etats : pending -> active -> sending -> grading -> done/null (+ expired)."""

    def __init__(self, challenger, opponent, channel_id):
        self.id = _next_duel_id[0]
        _next_duel_id[0] += 1
        self.challenger = challenger
        self.opponent = opponent
        self.channel_id = channel_id
        self.numero = None
        self.state = 'pending'
        self.start_mono = None
        self.finisher = None
        self.finish_s = None
        self.copies = {challenger.id: [], opponent.id: []}        # user_id -> [(filename, bytes)]
        self.copy_locked = {challenger.id: False, opponent.id: False}
        self.pending_grade = {challenger.id: None, opponent.id: None}  # proposee, pas confirmee
        self.locked_grade = {challenger.id: None, opponent.id: None}   # confirmee = definitive
        self._tasks = []

    def players(self):
        return (self.challenger, self.opponent)

    def other(self, user):
        return self.opponent if user.id == self.challenger.id else self.challenger

    async def send_invite(self):
        await self.opponent.send(
            f"⚔️ **{self.challenger.display_name}** te convie à un duel de forgecarde ! "
            f"Tu as 2 minutes pour accepter.",
            view=InviteView(self))

    async def start(self):
        conn = fc_db()
        # Les duels restent en physique pour l'instant (une seule piscine de
        # fiches) ; les URL plates physique ({numero}.pdf) restent valides.
        row = conn.execute('SELECT numero FROM forgecards WHERE subject=? AND numero <= ? ORDER BY RANDOM() LIMIT 1',
                           ('physique', read_params('physique')['max_active_num'])).fetchone()
        conn.close()
        if row is None:
            await self.cancel('aucune forgecard active')
            return
        self.numero = row[0]
        self.state = 'active'
        self.start_mono = time.monotonic()
        fiche_url = f"{SITE_URL}/uploads/fiche/{self.numero}.pdf"
        for p in self.players():
            try:
                await p.send(
                    f"⚔️ **Duel lancé !** Fiche n°{self.numero}\n📄 {fiche_url}\n"
                    f"Clique ✅ « J'ai fini » quand ta copie est prête. À ce moment-là, vous aurez "
                    f"**1 min 30** pour m'envoyer vos copies, puis vous noterez celle de l'autre : "
                    f"c'est la **meilleure note** qui gagne, pas le plus rapide.",
                    view=FinishView(self))
            except discord.Forbidden:
                await self.cancel(f"DM fermés pour {p.display_name}")
                return

    # ----- phase d'envoi des copies -----
    async def finish(self, user):
        if self.state != 'active':
            return False
        self.state = 'sending'
        self.finisher = user
        self.finish_s = time.monotonic() - self.start_mono
        for p in self.players():
            try:
                await p.send(
                    f"🏁 **{user.display_name}** a posé le stylo en **{fmt_dur(self.finish_s)}**.\n"
                    f"📤 Vous avez **1 min 30** pour m'envoyer vos copies **ici, en DM** (photos).\n"
                    f"Je garde les copies sous le coude et je ne les transmets à l'adversaire "
                    f"qu'une fois les **deux** prêtes !\n"
                    f"Clique ✅ « J'ai tout envoyé » quand ta copie est complète.",
                    view=CopyDoneView(self))
            except discord.Forbidden:
                pass
        self._tasks.append(asyncio.create_task(self._send_reminder()))
        self._tasks.append(asyncio.create_task(self._send_timeout()))
        return True

    async def receive_copies(self, user, images):
        """images : liste de (filename, bytes). Retourne un accuse a renvoyer, ou None."""
        if self.state != 'sending' or user.id not in self.copies:
            return None
        if self.copy_locked[user.id]:
            return "🔒 Ta copie est déjà verrouillée (« J'ai tout envoyé »)."
        self.copies[user.id].extend(images)
        n = len(self.copies[user.id])
        return (f"📸 Bien reçu : {plural(n, 'page')} au total. "
                f"Clique ✅ « J'ai tout envoyé » quand c'est complet.")

    async def mark_copy_done(self, user):
        """Clic sur « J'ai tout envoyé ». Retourne le message d'accuse."""
        if self.state != 'sending':
            return "Trop tard : la phase d'envoi des copies est terminée."
        if self.copy_locked.get(user.id, True):
            return "Tu as déjà verrouillé ta copie."
        if not self.copies[user.id]:
            return ("⚠️ Tu n'as encore rien envoyé ! Envoie au moins une photo, "
                    "sinon le duel sera nul à la fin du chrono.")
        self.copy_locked[user.id] = True
        if all(self.copy_locked.values()):
            await self._deliver_and_grade()
            return "Copie verrouillée. Les copies sont échangées, place à la correction !"
        return (f"Copie verrouillée ({plural(len(self.copies[user.id]), 'page')}). "
                f"En attente de {self.other(user).display_name}…")

    async def _send_reminder(self):
        await asyncio.sleep(DUEL_SEND_REMIND_S)
        if self.state != 'sending':
            return
        for p in self.players():
            if self.copy_locked[p.id]:
                continue
            n = len(self.copies[p.id])
            try:
                if n == 0:
                    await p.send("⏳ **Plus que 30 s** et tu n'as encore rien envoyé ! "
                                 "Sans copie, le duel sera **nul**.")
                else:
                    await p.send(f"⏳ Plus que 30 s ! ({plural(n, 'page')} reçue(s), "
                                 f"pense à cliquer ✅ « J'ai tout envoyé ».)")
            except Exception:
                pass

    async def _send_timeout(self):
        await asyncio.sleep(DUEL_SEND_WINDOW_S)
        if self.state != 'sending':
            return
        for p in self.players():
            if not self.copies[p.id]:
                await self.cancel(f"{p.display_name} n'a envoyé aucune page")
                return
        for uid in self.copy_locked:
            self.copy_locked[uid] = True
        await self._deliver_and_grade()

    # ----- phase de correction croisee -----
    async def _deliver_and_grade(self):
        if self.state != 'sending':
            return
        self.state = 'grading'
        corr_url = f"{SITE_URL}/uploads/fiche/{self.numero}-c.pdf"
        bareme_url = f"{SITE_URL}/uploads/fiche/{self.numero}-b.pdf"
        for p in self.players():
            other = self.other(p)
            files = [discord.File(io.BytesIO(data), filename=name)
                     for name, data in self.copies[other.id]]
            try:
                for i in range(0, max(1, len(files)), 10):
                    if i == 0:
                        await p.send(f"📝 **Correction croisée !** Voici la copie de "
                                     f"**{other.display_name}** :", files=files[:10])
                    else:
                        await p.send(files=files[i:i + 10])
                await p.send(
                    f"📄 Corrigé : {corr_url}\n📏 Barème : {bareme_url}\n\n"
                    f"Note la copie de **{other.display_name}** et envoie-moi simplement "
                    f"le nombre (ex : `14`). Tu as **10 minutes**, et tu devras "
                    f"**confirmer** ta note pour qu'elle compte.\n"
                    f"*Fair-play : un duel se gagne avec une meilleure copie, pas avec "
                    f"une note sévère. Note l'autre comme tu aimerais être noté.*")
            except Exception:
                pass
        self._tasks.append(asyncio.create_task(self._grade_timeout()))

    async def propose_grade(self, user, value):
        if self.state != 'grading' or user.id not in self.locked_grade:
            return
        if self.locked_grade[user.id] is not None:
            await user.send("Ta note est déjà confirmée, tu ne peux plus la changer.")
            return
        self.pending_grade[user.id] = value
        other = self.other(user)
        await user.send(
            f"Tu t'apprêtes à donner **{fmt_grade(value)}** "
            f"à la copie de **{other.display_name}**.\n"
            f"Dernière réflexion : sois honnête, et sache reconnaître une belle copie, "
            f"même adverse. C'est un jeu !",
            view=GradeConfirmView(self, user))

    async def confirm_grade(self, user):
        if self.state != 'grading' or self.pending_grade.get(user.id) is None:
            return None
        if self.locked_grade[user.id] is not None:
            return "Ta note est déjà confirmée."
        self.locked_grade[user.id] = self.pending_grade[user.id]
        if all(g is not None for g in self.locked_grade.values()):
            await self._resolve_grades()
            return None
        return (f"Note **{fmt_grade(self.locked_grade[user.id])}** confirmée. "
                f"En attente de la note de {self.other(user).display_name}…")

    async def modify_grade(self, user):
        if self.state != 'grading' or self.locked_grade.get(user.id) is not None:
            return "Impossible de modifier maintenant."
        self.pending_grade[user.id] = None
        return "OK, renvoie-moi ta note (un simple nombre)."

    async def _resolve_grades(self):
        # La note d'un joueur = celle donnee PAR l'autre sur sa copie.
        grade_challenger = self.locked_grade[self.opponent.id]
        grade_opponent = self.locked_grade[self.challenger.id]
        tie = grade_challenger == grade_opponent
        self.state = 'null' if tie else 'done'
        pairs = ((self.challenger, grade_challenger, grade_opponent),
                 (self.opponent, grade_opponent, grade_challenger))
        if tie:
            for p, own, given in pairs:
                try:
                    await p.send(
                        f"**Égalité parfaite** sur la forgecard n°{self.numero} !\n"
                        f"Ta copie : **{fmt_grade(own)}**; "
                        f"tu avais donné **{fmt_grade(given)}**.")
                except Exception:
                    pass
        else:
            winner = self.challenger if grade_challenger > grade_opponent else self.opponent
            for p, own, given in pairs:
                result = "🏆 Tu remportes" if p.id == winner.id else " Tu perds"
                try:
                    await p.send(
                        f"{result} le duel sur la forgecard n°{self.numero} !\n"
                        f"Ta copie : **{fmt_grade(own)}**; "
                        f"tu avais donné **{fmt_grade(given)}**.")
                except Exception:
                    pass
            channel = client.get_channel(self.channel_id)
            if channel:
                await channel.send(
                    f"🏆 **{winner.display_name}** remporte le duel sur la forgecard n°{self.numero} "
                    f"! GG")
        self._cleanup()

    async def _grade_timeout(self):
        await asyncio.sleep(DUEL_GRADE_WINDOW_S)
        if self.state != 'grading':
            return
        await self.cancel("Correction expirée (note non donnée ou non confirmée à temps)")

    # ----- fins de duel -----
    async def cancel(self, reason):
        if self.state in ('done', 'null', 'expired'):
            return
        self.state = 'null'
        for p in self.players():
            try:
                await p.send(f"❌ Duel nul ({reason}).")
            except Exception:
                pass
        self._cleanup()

    async def expire_invite(self):
        if self.state != 'pending':
            return
        self.state = 'expired'
        await dm_user(self.challenger.id, f"⌛ {self.opponent.display_name} n'a pas repondu. Duel expire.")
        await dm_user(self.opponent.id, "⌛ L'invitation au duel a expiré.")
        self._cleanup()

    def _cleanup(self):
        for t in self._tasks:
            t.cancel()
        DUELS.pop(self.id, None)
        for p in self.players():
            DUEL_BY_USER.pop(p.id, None)


class InviteView(discord.ui.View):
    def __init__(self, duel):
        super().__init__(timeout=120)
        self.duel = duel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.duel.opponent.id:
            await interaction.response.send_message("Ce duel n'est pas pour toi.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Duel accepté ! Préparation…", view=None)
        await self.duel.start()

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Duel refusé.", view=None)
        await self.duel.cancel("refusé")

    async def on_timeout(self):
        await self.duel.expire_invite()


class FinishView(discord.ui.View):
    def __init__(self, duel):
        super().__init__(timeout=3600)
        self.duel = duel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.duel.challenger.id, self.duel.opponent.id):
            await interaction.response.send_message("Tu n'es pas dans ce duel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="J'ai fini", style=discord.ButtonStyle.success, emoji="✅")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = await self.duel.finish(interaction.user)
        if ok:
            await interaction.response.edit_message(view=None)
        else:
            await interaction.response.send_message(
                "La phase d'envoi des copies est déjà ouverte.", ephemeral=True)

    async def on_timeout(self):
        if self.duel.state == 'active':
            await self.duel.cancel("temps écoulé")


class CopyDoneView(discord.ui.View):
    def __init__(self, duel):
        super().__init__(timeout=DUEL_SEND_WINDOW_S + 60)
        self.duel = duel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.duel.challenger.id, self.duel.opponent.id):
            await interaction.response.send_message("Tu n'es pas dans ce duel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="J'ai tout envoyé", style=discord.ButtonStyle.success, emoji="✅")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = await self.duel.mark_copy_done(interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)


class GradeConfirmView(discord.ui.View):
    def __init__(self, duel, grader):
        super().__init__(timeout=DUEL_GRADE_WINDOW_S)
        self.duel = duel
        self.grader = grader

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.grader.id:
            await interaction.response.send_message("Ce n'est pas ta note.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmer ma note", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = await self.duel.confirm_grade(interaction.user)
        await interaction.response.edit_message(content=msg or "✅ Note confirmée.", view=None)

    @discord.ui.button(label="Modifier", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def modify(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = await self.duel.modify_grade(interaction.user)
        await interaction.response.edit_message(content=msg, view=None)


@madec.command(name='duel', description='Defie un membre sur une forgecard')
@channel_required()
@app_commands.describe(adversaire='Membre à defier')
async def duel(interaction: discord.Interaction, adversaire: discord.Member):
    challenger = interaction.user
    if adversaire.id == challenger.id:
        await interaction.response.send_message("❌ Tu ne peux pas te defier toi-meme.", ephemeral=True)
        return
    if adversaire.bot:
        await interaction.response.send_message("❌ Pas contre un bot.", ephemeral=True)
        return
    if challenger.id in DUEL_BY_USER or adversaire.id in DUEL_BY_USER:
        await interaction.response.send_message("❌ L'un de vous est deja en duel.", ephemeral=True)
        return
    if not prenom_for_discord(challenger.id):
        await interaction.response.send_message("❌ Tu n'es pas link.", ephemeral=True)
        return
    if not prenom_for_discord(adversaire.id):
        await interaction.response.send_message(f"❌ {adversaire.display_name} n'est pas link.", ephemeral=True)
        return
    d = Duel(challenger, adversaire, interaction.channel_id)
    try:
        await d.send_invite()
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Impossible de DM {adversaire.display_name} (DM fermes).", ephemeral=True)
        return
    DUELS[d.id] = d
    DUEL_BY_USER[challenger.id] = d.id
    DUEL_BY_USER[adversaire.id] = d.id
    await interaction.response.send_message(
        f"⚔️ Defi envoyé à {adversaire.mention} ! Il a 2 min pour accepter.", ephemeral=True)


@client.event
async def on_message(message):
    """Reception des DM : copies (images) pendant 'sending', notes (nombres) pendant 'grading'."""
    if message.author.bot or message.guild is not None:
        return
    duel = DUELS.get(DUEL_BY_USER.get(message.author.id))
    if duel is None:
        return
    if duel.state == 'sending':
        images = []
        for a in message.attachments:
            if a.content_type and a.content_type.startswith('image/'):
                try:
                    images.append((a.filename, await a.read()))
                except Exception:
                    pass
        if images:
            ack = await duel.receive_copies(message.author, images)
            if ack:
                await message.channel.send(ack)
        elif message.content.strip():
            await message.channel.send(
                "📎 Envoie des **images** de ta copie, puis clique ✅ « J'ai tout envoyé ».")
    elif duel.state == 'grading':
        if not message.content.strip():
            return
        g = parse_grade(message.content)
        if g is None:
            await message.channel.send(
                f"Envoie-moi une **note** entre 0 et {DUEL_MAX_GRADE} (ex : `14` ou `12,5`).")
            return
        await duel.propose_grade(message.author, g)


@madec.command(
    name='qcm',
    description='Reçois ton lien personnel pour rejoindre le QCM'
)
@channel_required()
async def qcm(interaction: discord.Interaction):
    prenom = prenom_for_discord(interaction.user.id)

    if not prenom:
        await interaction.response.send_message(
            "Tu n'es pas relié à un prénom Forgecards. "
            "Demande à un admin de te relier.",
            ephemeral=True
        )
        return

    if not INTERNAL_API_KEY:
        await interaction.response.send_message(
            "MADEC_INTERNAL_API_KEY manque dans le .env du bot.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    headers = {
        'X-Madec-Internal-Key': INTERNAL_API_KEY,
        'Content-Type': 'application/json',
    }
    body = {
        'prenom': prenom,
        'ttl_minutes': 15,
    }

    try:
        print(f'[qcm-link] appel API: {QCM_INVITE_API_URL}')

        # requests est bloquant : on l'exécute hors de la boucle asyncio Discord.
        response = await asyncio.to_thread(
            requests.post,
            QCM_INVITE_API_URL,
            headers=headers,
            json=body,
            timeout=10,
        )

    except requests.RequestException as e:
        print(f'[qcm-link] erreur réseau: {e!r}')

        await interaction.followup.send(
            "Impossible de joindre le site pour créer le lien QCM.",
            ephemeral=True
        )
        return

    print(
        f'[qcm-link] réponse: status={response.status_code}, '
        f'url={response.url!r}, '
        f'content_type={response.headers.get("Content-Type")!r}, '
        f'body={response.text[:500]!r}'
    )

    try:
        payload = response.json()
    except ValueError:
        await interaction.followup.send(
            f"Le site a répondu dans un format invalide "
            f"(HTTP {response.status_code}).",
            ephemeral=True
        )
        return

    if not response.ok or not payload.get('ok'):
        await interaction.followup.send(
            f"Impossible de créer le lien : "
            f"{payload.get('error', f'HTTP {response.status_code}')}.",
            ephemeral=True
        )
        return

    message = (
        "⚡ Ton lien personnel pour rejoindre le QCM :\n"
        f"{payload['url']}\n\n"
        "Valable 15 minutes et utilisable une seule fois."
    )

    try:
        await interaction.user.send(message)
        confirmation = "Je t'ai envoyé ton lien QCM en message privé."
    except discord.Forbidden:
        confirmation = (
            "Tes messages privés sont fermés. Voici ton lien personnel :\n\n"
            f"{payload['url']}\n\n"
            "Valable 15 minutes et utilisable une seule fois."
        )

    await interaction.followup.send(
        confirmation,
        ephemeral=True
    )

# ---------- Taches planifiees ----------

# Eliot: _post is a helper, NOT a loop. The @tasks.loop belongs on
# daily_reminder below — it once sat on _post, which turned _post into an
# uncallable Loop and crashed on_ready before ANY scheduled task could start
# (no reminders, no 23h55 streak guard, no weekly recap). Dark times.
async def _post(path, payload, timeout=10):
    """POST interne non bloquant : requests est synchrone, on le pousse dans un thread."""
    def _do():
        r = requests.post(f"{SITE_URL}{path}",
                          headers={"X-Madec-Internal-Key": INTERNAL_API_KEY},
                          json=payload, timeout=timeout)
        return r.json()
    return await asyncio.to_thread(_do)


@tasks.loop(time=dtime(hour=REMINDER_HOUR, minute=1, tzinfo=TZ_PARIS))
async def daily_reminder():
    conn = fc_db()
    bots = bot_db()
    for prenom, discord_id in bots.execute(
        'SELECT prenom, discord_id FROM links WHERE notifications=1 AND prenom != \'admin\''
    ).fetchall():
        for subject in ENABLED_SUBJECTS:
            try:
                remaining = compute_remaining(conn, prenom, read_params(subject), subject)
            except Exception:
                continue
            if remaining <= 0:
                continue
            label = SUBJECT_META[subject]['label']
            await dm_user(discord_id,
                          f"⏰ **{label}** — il te reste **{plural(remaining, 'carte')}** aujourd'hui.\n{SITE_URL}")
            try:
                await _post('/api/internal/log-event',
                            {'prenom': prenom, 'type': 'reminder',
                             'payload': f'daily:{subject}', 'subject': subject})
            except Exception:
                pass
    conn.close()
    bots.close()


@tasks.loop(minutes=2)
async def watch_new_cards():
    ch_id = get_setting('channel_id')
    if not ch_id:
        return
    conn = fc_db()
    drops = []
    for subject in ENABLED_SUBJECTS:
        max_active = read_params(subject)['max_active_num']
        rows = conn.execute(
            'SELECT numero FROM forgecards WHERE subject=? AND numero <= ? ORDER BY numero',
            (subject, max_active)).fetchall()
        nums = [r[0] for r in rows]

        # Cle de suivi par matiere : les numerotations sont independantes.
        key = f'last_announced_num:{subject}'
        last = get_setting(key)
        if last is None:
            # premier demarrage : tout ce qui est deja tirable est considere connu
            set_setting(key, max(nums) if nums else 0)
            continue

        new = [n for n in nums if n > int(last)]
        if not new:
            # Suit AUSSI les reductions du max tirable : si tu re-elargis ensuite,
            # les fiches redevenues disponibles seront annoncees comme un drop.
            set_setting(key, max(nums) if nums else 0)
            continue
        set_setting(key, max(new))
        drops.append((subject, new))
    conn.close()
    if not drops:
        return

    channel = client.get_channel(int(ch_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(ch_id))
        except Exception:
            return
    role = discord.utils.get(channel.guild.roles, name=NOTIF_ROLE_NAME)
    mention = f'{role.mention} ' if role else ''
    for subject, new in drops:
        label = SUBJECT_META[subject]['label']
        if len(new) <= 5:
            detail = ', '.join(f'n°{n}' for n in new)
            await channel.send(
                f"📦 {mention}**Drop {label}** : {plural(len(new), 'nouvelle forgecard')} "
                f"maintenant tirables ({detail}) !")
        else:
            await channel.send(
                f"📦 {mention}**Gros drop {label}** : {plural(len(new), 'nouvelle forgecard')} "
                f"maintenant tirables (jusqu'à la n°{max(new)}) !")


@tasks.loop(seconds=15)
async def rotate_status():
    now = datetime.now(TZ_PARIS)
    if now.hour >= 23 or now.hour < 7:
        await client.change_presence(activity=discord.Streaming(name='📹 les forgecards que tu n\'as pas faites', url='https://www.youtube.com/watch?v=E4WlUXrJgy4'))
        return
    # Xiao: This loop fires every 15s — nobody needs the count to be that
    # fresh. Cache it 5 minutes and save thousands of queries a day.
    # Multi-subject: somme des restants sur chaque matiere activee.
    import time as _time
    cache = getattr(rotate_status, '_cache', None)
    if cache and _time.time() - cache[0] < 300:
        n = cache[1]
    else:
        conn = fc_db()
        bots = bot_db()
        n = 0
        for prenom, _ in bots.execute("SELECT prenom, discord_id FROM links WHERE prenom != 'admin'").fetchall():
            try:
                remaining_total = sum(
                    compute_remaining(conn, prenom, read_params(subject), subject)
                    for subject in ENABLED_SUBJECTS
                )
                if remaining_total > 0:
                    n += 1
            except Exception:
                continue
        conn.close()
        bots.close()
        rotate_status._cache = (_time.time(), n)
    if n == 0:
        msg_count = 'Tout le monde est à jour. Suspect.'
    elif n == 1:
        msg_count = "1 personne n'a pas fait ses cartes aujourd'hui"
    else:
        msg_count = f"{n} personnes n'ont pas fait leurs cartes aujourd'hui"
    messages = [msg_count, 'Va bosser → https://madec.moyart.net']
    rotate_status.i = (getattr(rotate_status, 'i', 0) + 1) % len(messages)
    await client.change_presence(activity=discord.CustomActivity(name=messages[rotate_status.i]))


@tasks.loop(time=dtime(hour=23, minute=55, tzinfo=TZ_PARIS))
async def nightly_streak_guard():
    try:
        resp = await _post('/api/internal/streak-guard', {}, timeout=30)
    except Exception as e:
        print(f'[streak-guard] appel impossible: {e!r}')
        return
    if not isinstance(resp, dict) or not resp.get('ok'):
        print(f'[streak-guard] reponse invalide: {resp!r}')
        return
    bots = bot_db()
    links = {p: d for p, d in bots.execute("SELECT prenom, discord_id FROM links WHERE prenom != 'admin'").fetchall()}
    bots.close()
    # results est nested par matiere : {subject: {prenom: {...}}}
    for subject, per_prenom in resp.get('results', {}).items():
        label = SUBJECT_META.get(subject, {}).get('label', subject)
        for prenom, info in per_prenom.items():
            for ev in info['events']:
                if ev == 'joker_spent':
                    await dm_user(links.get(prenom),
                                  f"🃏 **{label}** — joker utilisé ! Ta streak est sauvée. "
                                  f"Il te reste {info['jokers']} joker(s).")
                    try:
                        await _post('/api/internal/log-event',
                                    {'prenom': prenom, 'type': 'reminder',
                                     'payload': f'joker:{subject}', 'subject': subject})
                    except Exception:
                        pass
                elif ev == 'joker_awarded':
                    await dm_user(links.get(prenom),
                                  f"🃏 **{label}** — streak de {info['streak']} jours ! "
                                  f"Tu gagnes un joker (total {info['jokers']}). "
                                  f"Il sauvera ta streak si tu oublies un jour.")


@tasks.loop(time=dtime(hour=20, minute=0, tzinfo=TZ_PARIS))
async def weekly_recap():
    """Recap du dimanche 20h : une section par matiere (tirages, revisions,
    streak record), plus un bloc QCM commun aux matieres."""
    if datetime.now(TZ_PARIS).weekday() != 6:
        return
    ch_id = get_setting('channel_id')
    if not ch_id:
        return
    conn = fc_db()
    week_ago = (datetime.now(TZ_PARIS).date() - timedelta(days=7)).isoformat()
    per_subject = {}
    for subject in ENABLED_SUBJECTS:
        reviews = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE subject=? AND substr(created_at,1,10) >= ? AND prenom != 'admin'",
            (subject, week_ago)).fetchone()[0]
        total_reviews = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE subject=? AND prenom != 'admin'",
            (subject,)).fetchone()[0]
        draws = conn.execute(
            'SELECT COUNT(*) FROM draw_history WHERE subject=? AND substr(created_at,1,10) >= ?',
            (subject, week_ago)).fetchone()[0]
        per_subject[subject] = {'reviews': reviews, 'total_reviews': total_reviews, 'draws': draws}
    conn.close()
    classements, fiches = {}, {}
    qcm_podium, nb_parties = [], 0
    for subject in ENABLED_SUBJECTS:
        try:
            resp = await _post(f'/api/internal/weekly-stats?subject={subject}', {}, timeout=30)
            if resp and resp.get('ok'):
                classements[subject] = resp.get('classement', [])
                fiches[subject] = (resp.get('fiche_top'), resp.get('fiche_rev'))
                if subject == ENABLED_SUBJECTS[0]:
                    # Le podium QCM est commun : une seule section le reprend.
                    qcm_podium = resp.get('podium_qcm', [])
                    nb_parties = resp.get('nb_parties_qcm', 0)
        except Exception as e:
            print('weekly-stats:', e)
    channel = client.get_channel(int(ch_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(ch_id))
        except Exception:
            return
    blocks = []
    for subject in ENABLED_SUBJECTS:
        label = SUBJECT_META[subject]['label']
        stats = per_subject[subject]
        lines = [
            f"📊 **Recap de la semaine — {label}**",
            "",
            f"🎲 Tirages de la semaine : {stats['draws']}",
            f"🔁 Révisions de la semaine : {stats['reviews']}",
            f"📚 Révisions depuis la rentrée : {stats['total_reviews']}",
            "",
        ]
        # Xiao: One line for the class record, everyone tied at the top gets named.
        actifs = [c for c in classements.get(subject, []) if c['streak'] > 0]
        if actifs:
            best = actifs[0]['streak']
            names = ', '.join(c['prenom'] for c in actifs if c['streak'] == best)
            lines.append(f"🔥 Streak la plus longue : {plural(best, 'jour')} — {names}")
        else:
            lines.append("🔥 Aucune streak en cours. Le désert.")
        fiche_top, fiche_rev = fiches.get(subject, (None, None))
        if fiche_top:
            titre = f"« {fiche_top['titre']} »" if fiche_top.get('titre') else ''
            lines.append(f"🃏 Carte la plus tirée : {fiche_top.get('label') or fiche_top['numero']} {titre} ({plural(fiche_top['c'], 'tirage')})")
        if fiche_rev:
            titre = f"« {fiche_rev['titre']} »" if fiche_rev.get('titre') else ''
            lines.append(f"📖 Carte la plus révisée : {fiche_rev.get('label') or fiche_rev['numero']} {titre} ({plural(fiche_rev['c'], 'revision')})")
        blocks.append('\n'.join(lines))
    qcm_lines = []
    if qcm_podium:
        podium_txt = ' · '.join(f"**{p['prenom']}** ({p['score']})" for p in qcm_podium)
        qcm_lines.append(f"🎮 **QCM** (toutes matières) : {plural(nb_parties, 'partie')} cette semaine — {podium_txt}")
        blocks.append('\n'.join(qcm_lines))
    await channel.send('\n\n'.join(blocks))


# ---------- Demarrage ----------

@client.event
async def on_ready():
    try:
        ensure_joker_table()
    except Exception as e:
        print('Joker table:', e)
    if GUILD_ID:
        g = discord.Object(id=int(GUILD_ID))
        try:
            tree.copy_global_to(guild=g)
            synced = await tree.sync(guild=g)
            print(f'Synced {len(synced)} commandes (guild {GUILD_ID}).')
        except Exception as e:
            print('Sync fail:', e)
    else:
        try:
            synced = await tree.sync()
            print(f'Synced {len(synced)} commandes (global).')
        except Exception as e:
            print('Sync fail:', e)
    for t in (daily_reminder, watch_new_cards, rotate_status, nightly_streak_guard, weekly_recap):
        if not t.is_running():
            t.start()
    print('Connecte:', client.user)


if __name__ == '__main__':
    if not DISCORD_TOKEN:
        raise SystemExit('DISCORD_TOKEN manquant dans .env')
    client.run(DISCORD_TOKEN)
