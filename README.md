# Forgecards

Site de forgecards : simulation de khôlle (tirage de fiches), répétition
espacée (FSRS modifié), QCM en direct façon kahoot, et bot Discord
(rappels quotidiens, streaks et jokers, duels de copies, invitations QCM).

## Composants

- `app.py` — site Flask : pages publiques, espace admin, API interne, websocket QCM
- `bot.py` — bot Discord, lit la base du site (`data/forgecards.db`) en lecture/écriture
- `qcm_engine.py` — moteur de QCM temps réel (Flask-SocketIO + gevent)
- `fsrs.py` — moteur de répétition espacée (FSRS-4.5 ajusté + influence de la difficulté professeur)
- `message.py` — envoie un message Discord depuis le terminal

## Déploiement

- Python 3.11+, `pip install -r requirements.txt`
- Configuration par variables d'environnement (fichier `.env`, ignoré par git) :
  `DISCORD_TOKEN`, `FORGECARDS_DB`, `FORGECARDS_PARAMS`, `MADEC_INTERNAL_API_KEY`,
  `MADEC_INTERNAL_API_URL`, `MADEC_SITE_URL`, `MADEC_GUILD_ID`,
  `MADEC_NOTIF_ROLE`, `MADEC_REMINDER_HOUR`, `MADEC_EXTRA_ADMINS`
- Données SQLite dans `data/` (non versionnées)
- Les assets lourds (images de fond, favicons, fonts LM Roman, MathJax,
  socket.io.min.js, médias) sont servis hors dépôt par le reverse proxy
