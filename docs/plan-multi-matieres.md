# Plan — Deux matières en parallèle (physique + chimie)

Objectif : faire tourner la chimie à côté de la physique sur la même instance,
sans second déploiement ni second bot.

**Statut : implémenté sur `feat/multi-matieres` (backend, frontend, admin,
bot). Reste la recette manuelle en conditions réelles (dernière section).**

## Décisions actées (ne pas rouvrir)

* **Un seul login par élève.** Le compte, le mot de passe emoji, la session :
  inchangés. La session est **commune** : le switch matière ne fait que changer
  un contexte de navigation (`session['subject']`), jamais l'authentification.

* **Un switch physique ⇄ chimie** dans le header, dans l'esprit du toggle
  jour/nuit (`POST /api/subject`, liste des matières via `/api/subjects`).

* **Deux pages admin séparées** : `/admin` (physique) et `/cadmin` (chimie),
  même template, même mot de passe admin. Chaque page ne voit et ne modifie
  que sa matière — **sauf les QCM, communs** : l'éditeur QCM garde son
  sélecteur de matière et chaque admin voit/enregistre les QCM de **toutes**
  les matières depuis les deux pages. Les stats de questions QCM (`weak`,
  `games`, exports `qcm-par-mois`) restent communes aussi.

* **Ce qui est séparé par matière** : fiches (`forgecards`), numérotation
  (repart de 1 en chimie), historique de tirages (`draw_history`), options de
  tirage et paramètres (`params_<matiere>.json` : max tirable, quotas/jour,
  rétention FSRS, bonus de chapitre), révisions (`reviews`, FSRS, poids
  entraînés), streaks (`sr_daily_streak`), jokers (`user_jokers` +
  `joker_ledger`), stats admin (overview, exports, suivi élèves), events.
  **Pages élèves séparées** : tirage, liste, SR, compte (dashboard), suivent
  la matière active de la session.

* Le QCM est **déjà multi-thèmes** (`qcm_physique.json`, `qcm_chimie.json`,
  `qcm_maths.json`) : inchangé, et volontairement **hors scope matière**.

## Architecture retenue : une seule app, une dimension `subject`

Pas de deuxième instance Flask, pas de deuxième base. Colonne `subject`
(`'physique'` / `'chimie'`) sur les tables métier, toutes les requêtes sont
scopées. La donnée existante devient `subject='physique'`.

### 1. Base de données (migration) — fait

Schéma v2 en place (`init_db`) : `forgecards` PK `(subject, numero)`,
`sr_state_user` PK `(prenom, subject, numero)`, `sr_daily_streak` PK
`(prenom, subject, day)`, `user_jokers` PK `(prenom, subject)`,
`sr_weights_user` PK `(prenom, subject)` ; colonne `subject` sur `reviews`,
`reviews_archive`, `draw_history`, `events`, `joker_ledger`. `users` et les
tables QCM inchangées. Migration automatique au démarrage : snapshot
horodaté `forgecards.bak-<ts>.db`, tables à PK composée renommées puis
recréées, données recopiées en `subject='physique'`, `ALTER TABLE` pour les
autres. Idempotent. *(Validée par le smoke test : migration d'une base v1 de
référence, données intactes.)*

### 2. Configuration — fait

* `SUBJECT_META` + flags `.env` (`CHIMIE=True`, plus tard `MATHS=True`) :
  `subject_enabled()`, `ENABLED_SUBJECTS`, `/api/subjects` (élèves : matières
  activées uniquement ; admin : toutes via `all_subjects_payload()`).
* `CHAPITRES_BY_SUBJECT` + `chapitres_for(subject)` (`CHAPITRES` reste la
  liste physique ; chimie/maths = `['Autre']` à remplir avant le lancement).
* `params_path/read_params/write_params(subject)` : `params.json` conservé
  pour la physique (zéro migration), `params_chimie.json`, …
* Uploads : physique garde ses noms plats historiques (`1.pdf`, `1-c.pdf`…,
  **zéro migration de fichiers ni de URLs** déjà publiées) ; les autres
  matières vivent dans `uploads/fiche/<matiere>/…` (`filenames_for(code,
  subject)`) pour ne pas collisionner les numéros.

### 3. Backend — scoping des routes — fait

* `current_subject()` (élèves, session, défaut physique) et `admin_subject()`
  (`?subject=` validé contre **toutes** les matières connues — l'admin prépare
  une matière avant son activation ; repli sur la matière de session).
  Règle simple : **aucune requête métier sans filtre `subject`**.
* Élèves : `draw.py`, `sr.py`, `auth.py` (deck/export élève), `views.py`
  (`/api/params`, `/api/chapitres`, `/api/subjects`, `/api/subject`) — tout
  passe par `current_subject()`.
* Admin : `cards.py`, `auth.py` (stats, jokers, deck, export), exports CSV —
  tout passe par `admin_subject()`. `?subject=` absent → matière de session.
* `log_event(..., subject=)` écrit la colonne `subject`.
* `qcm_admin.py`, `qcm_engine.py`, `qcm_invites.py` : **non scopés** (QCM
  communs, voir décisions).

### 4. Frontend — le switch — fait

* Toggle matière dans le header (à côté du jour/nuit) : chargé depuis
  `/api/subjects`, caché si une seule matière activée, libellé coloré par
  matière. Clic → `POST /api/subject` → rechargement (la session serveur fait
  foi) ; `localStorage` ne sert que l'affichage immédiat.
* `admin.html` reçoit `data-subject` (via `{% block body_attrs %}`) ;
  `app.js` ajoute `?subject=` à tous ses appels admin (`adminUrl()`), sauf
  QCM. Les routes `/admin` et `/cadmin` rendent le même template avec
  `subject_key` physique/chimie.
* Le thème jour/nuit reste global.

### 5. Bot Discord — fait

* Mêmes flags lus dans le `.env` (`SUBJECT_META` dupliqué dans `bot.py`, à
  garder en sync avec `config.py`), `read_params(subject)`,
  `compute_daily/compute_remaining/compute_streak/get_jokers` scopés.
* `streak-guard` 23h55 : le site boucle sur `ENABLED_SUBJECTS`
  (`results[matiere][prenom]`), le bot mentionne la matière dans ses DM
  (joker dépensé / gagné par matière).
* Rappel quotidien : un DM par matière ayant des cartes restantes, avec le
  libellé de la matière.
* Recap du dimanche : **une section par matière** (tirages, révisions,
  streak record, cartes les plus tirées/révisées de la matière) + un bloc
  QCM commun. `weekly-stats` prend `?subject=`.
* `watch_new_cards` : drop annoncé par matière (clé de suivi
  `last_announced_num:<matiere>`).
* `/madec streak` et `/madec progress` : une ligne par matière.
* Les duels restent en physique pour l'instant (seule piscine de fiches).

## Recette avant activation (CHIMIE=True en prod)

1. Remplir `CHAPITRES_BY_SUBJECT['chimie']` et uploader les fiches via
   `/cadmin` (numérotation chimie indépendante, repart de 1).
2. Régler `params_chimie.json` via `/cadmin` (max tirable, quotas, rétention).
3. Vérifier qu'avec `CHIMIE` absent du `.env` **rien ne change** côté élèves
   (smoke test : le switch est caché, tout reste en physique).
4. Allumer `CHIMIE=True`, parcours complet : tirage, liste, SR (quotas
   chimie), streak/jokers indépendants, switch aller-retour, exports,
   guard 23h55 sur les deux matières.
5. Mettre à jour le `.env` du bot (même flag) et redémarrer le bot.

## Points de vigilance

* **Fuite croisée** : le risque n°1 reste une requête oubliée sans filtre
  `subject`. Le smoke test couvre les routes principales ; toute nouvelle
  route métier doit prendre `current_subject()` ou `admin_subject()`.
* **Numérotation** : un `numero` se lit toujours avec sa matière ; les
  URL/logs d'admin affichent les deux via `?subject=`.
* **QCM communs** : ne jamais scoper `qcm_answers`/`qcm_games` par matière —
  les deux admins partagent ces stats.
* **Bot/site sync** : `SUBJECT_META`/flags dupliqués dans `bot.py` ; un oubli
  de mise à jour d'un côté se voit (matière absente des DM/recaps).
