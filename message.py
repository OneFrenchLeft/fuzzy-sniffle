#!/usr/bin/env python3
"""
message.py — envoie un message Discord via le bot madec, depuis le terminal.

Exemples :
    venv/bin/python message.py "RDV à 14h dans le salon QCM"
        -> poste dans le salon configuré par /madec setchannel

    venv/bin/python message.py --channel 123456789 "Message"
        -> poste dans un salon précis (clic droit > Copier l'identifiant)

    venv/bin/python message.py --prenom Lea "Ton lien QCM arrive"
        -> DM au compte Discord lié à ce prénom (table links de bot.db)

    echo "Message multiligne" | venv/bin/python message.py -
        -> lit le texte depuis stdin (pratique pour les pipes)

N'interfère pas avec madecbot : la connexion est ouverte, le message envoyé,
puis fermée proprement.
"""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
TOKEN = os.environ.get('DISCORD_TOKEN')
BOT_DB = BASE / 'data' / 'bot.db'


def configured_channel():
    conn = sqlite3.connect(BOT_DB)
    row = conn.execute("SELECT value FROM settings WHERE key='channel_id'").fetchone()
    conn.close()
    return int(row[0]) if row else None


def discord_id_for(prenom):
    conn = sqlite3.connect(BOT_DB)
    row = conn.execute('SELECT discord_id FROM links WHERE prenom = ?', (prenom.strip(),)).fetchone()
    conn.close()
    return row[0] if row else None


def main():
    args = [a for a in sys.argv[1:]]
    channel_id = None
    dm_id = None

    if '--channel' in args:
        i = args.index('--channel')
        channel_id = int(args[i + 1])
        del args[i:i + 2]
    if '--prenom' in args:
        i = args.index('--prenom')
        dm_id = discord_id_for(args[i + 1])
        if dm_id is None:
            sys.exit(f'✘ {args[i + 1]} n\'est lié à aucun compte Discord.')
        del args[i:i + 2]

    if not args:
        sys.exit(__doc__)
    text = args[0] if args[0] != '-' else sys.stdin.read()
    text = text.strip()
    if not text:
        sys.exit('✘ message vide.')
    if len(text) > 2000:
        sys.exit('✘ Discord limite à 2000 caractères (là : %d).' % len(text))

    if dm_id is None and channel_id is None:
        channel_id = configured_channel()
        if channel_id is None:
            sys.exit('✘ aucun salon configuré — utilise --channel ou /madec setchannel.')

    if not TOKEN:
        sys.exit('✘ DISCORD_TOKEN manquant dans .env')

    import discord

    async def send():
        client = discord.Client(intents=discord.Intents.default())

        @client.event
        async def on_ready():
            try:
                if dm_id is not None:
                    user = await client.fetch_user(dm_id)
                    await user.send(text)
                    print(f'✔ envoyé en DM ({dm_id})')
                else:
                    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
                    await channel.send(text)
                    print(f'✔ envoyé dans #{getattr(channel, "name", channel_id)}')
            except discord.Forbidden:
                print('✘ impossible (DM fermés ou salon inaccessible au bot).')
            except Exception as e:
                print(f'✘ {e!r}')
            finally:
                await client.close()

        await client.start(TOKEN)

    asyncio.run(send())


if __name__ == '__main__':
    main()
