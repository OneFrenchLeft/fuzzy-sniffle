#!/usr/bin/env python3

import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
FORGECARDS_DB = BASE / 'data' / 'forgecards.db'
BOT_DB = BASE / 'data' / 'bot.db'

LINKS = {
    "Aaron": 1397116387750707214,
    "Adam": 652571455057559574,
    "Adrien": 439771511361110017,
    "Agathe": 1468944050211000516,
    "Amaury": 1409818935976656936,
    "Amel": 1459237768105427029,
    "Amir": 644613928688549929,
    "Armand": 502484050464866305,
    "Axel": 1468953051577712855,
    "Basile": 928604142983839744,
    "Camille": 409958667035738117,
    "Daphnée": 1457712113832300615,
    "Eddie": 614181305084674071,
    "Elliot": 587694136761516036,
    "Esteban": 1540375371458023586,
    "Ewann": 797134731623137302,
    "Fabio": 632627835915206656,
    "Gabrielle": 1456981475433119938,
    "Inès": 1536691604772102194,
    "Joseph": 942110781263208459,
    "Jules Auguste": 947605509127680080,
    "Maelle": 1457801736914735222,
    "Margot": 1456725297285042256,
    "Marie": 1170387244721377342,
    "Marine": 1409470321400217682,
    "Martial": 707962056787230862,
    "Mathieu": 1355182944091639870,
    "Maëlys": 1539294459835977839,
    "Noa": 1432471531325358232,
    "Noémie": 1459512542396158219,
    "Paul-Antoine": 1539206664773701665,
    "Quentin": 395590087221575680,
    "Sven": 720274428029567026,
    "Timéo": 1305246327138947082,
    "Tristan": 442099639798464522,
    "Ulysse": 725304740056924162,
    "Vincent": 712352233131343912,
    "Lowang": 320602734367735808,
    "Rayan E.": 810845591525130270,
    "Rayan F.": 585892568647204893,
    "admin": 570863003554152449,
}

SNOWFLAKE_MIN, SNOWFLAKE_MAX = 10**15, 10**20


def load_links(argv):
    """LINKS embarqué par défaut, ou un fichier JSON passé en argument."""
    for arg in argv[1:]:
        if arg.endswith('.json'):
            data = json.loads(Path(arg).read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                sys.exit(f'{arg} : le JSON doit être un objet {{"prenom": id}}.')
            return data
    if not LINKS:
        sys.exit('Le dictionnaire LINKS est vide — édite-le en haut du script '
                 'ou passe un fichier JSON en argument.')
    return LINKS


def main():
    apply = '--apply' in sys.argv
    links = load_links(sys.argv)

    # Prénoms connus du site (lecture seule)
    site = sqlite3.connect(f'file:{FORGECARDS_DB}?mode=ro', uri=True)
    known = {r[0] for r in site.execute('SELECT prenom FROM users')}
    site.close()

    bots = sqlite3.connect(BOT_DB)
    bots.execute('PRAGMA busy_timeout=5000')
    bots.execute('''CREATE TABLE IF NOT EXISTS links(
        prenom TEXT PRIMARY KEY,
        discord_id INTEGER NOT NULL UNIQUE,
        notifications INTEGER NOT NULL DEFAULT 1)''')

    ok, inconnus, invalides = [], [], []

    for raw_prenom, raw_id in links.items():
        prenom = str(raw_prenom).strip()
        if not prenom:
            invalides.append((raw_prenom, raw_id, 'prénom vide'))
            continue
        try:
            discord_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            invalides.append((raw_prenom, raw_id, 'ID Discord non numérique'))
            continue
        if not (SNOWFLAKE_MIN <= discord_id <= SNOWFLAKE_MAX):
            invalides.append((raw_prenom, raw_id, 'ID Discord improbable'))
            continue

        # Tolérance à la casse : "lea" retrouve "Lea" et le signale
        note = ''
        if prenom not in known:
            found = next((p for p in known if p.lower() == prenom.lower()), None)
            if found is None:
                inconnus.append(prenom)
                continue
            note = f'(orthographe corrigée : {found})'
            prenom = found

        if apply:
            # Même logique que la commande /madec link du bot
            bots.execute('DELETE FROM links WHERE discord_id = ?', (discord_id,))
            bots.execute(
                'INSERT INTO links(prenom, discord_id, notifications) VALUES(?,?,1) '
                'ON CONFLICT(prenom) DO UPDATE SET discord_id=excluded.discord_id',
                (prenom, discord_id))
        ok.append((prenom, discord_id, note))

    if apply:
        bots.commit()
    else:
        bots.rollback()

    total_liens = bots.execute('SELECT COUNT(*) FROM links').fetchone()[0]
    bots.close()

    print()
    for prenom, discord_id, note in ok:
        print(f'  ✔ {prenom} -> {discord_id} {note}')
    for prenom in inconnus:
        print(f'  ✘ {prenom} : inconnu sur le site (crée le compte d\'abord)')
    for prenom, raw_id, raison in invalides:
        print(f'  ✘ {raw_prenom!r} -> {raw_id!r} : {raison}')

    print()
    print(f'{len(ok)} lien(s) {"appliqués" if apply else "simulés"}, '
          f'{len(inconnus)} inconnu(s), {len(invalides)} invalide(s).')
    print(f'Total actuel dans bot.db : {total_liens} lien(s).')
    if not apply and ok:
        print('Simulation seulement — relance avec --apply pour écrire.')


if __name__ == '__main__':
    main()
