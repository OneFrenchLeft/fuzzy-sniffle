# Plan — Deux matières en parallèle (physique + chimie)

Objectif : faire tourner la chimie à côté de la physique sur la même instance,
sans second déploiement ni second bot.

## Décisions actées (ne pas rouvrir)

- **Un seul login par élève.** Le compte, le mot de passe emoji, la session : inchangés.
- **Un switch physique ⇄ chimie** dans le header, dans l'esprit du toggle jour/nuit.
- **Deux pages admin séparées** : `/admin` (physique) et `/cadmin` (chimie).
- **Un seul mot de passe admin**, partagé par les deux pages.
- Le QCM est **déjà multi-matières** (`qcm_physique.json`, `qcm_chimie.json`,
  `qcm_maths.json`) : on n'y touche pas.

## Architecture retenue : une seule app, une dimension `subject`

Pas de deuxième instance Flask, pas de deuxième base. On ajoute une colonne
`subject` (`'physique'` / `'chimie'`) aux tables métier et on scope toutes les
requêtes. La donnée existante devient `subject='physique'`.

### 1. Base de données (migration)

Ajouter `subject TEXT NOT NULL DEFAULT 'physique'` et adapter les clés :

| Table | Changement |
| --- | --- |
| `forgecards` | PK `(numero)` → `(subject, numero)` — les numéros de fiches repartent de 1 en chimie |
| `sr_state_user` | PK `(prenom, numero)` → `(prenom, subject, numero)` |
| `sr_daily_streak` | PK `(prenom, day)` → `(prenom, subject, day)` |
| `user_jokers` | PK `(prenom)` → `(prenom, subject)` — jokers et streaks **par matière** |
| `joker_ledger` | colonne `subject` (audit) |
| `reviews` / `reviews_archive` | colonne `subject` |
| `draw_history` | colonne `subject` |
| `sr_weights_user` | PK `(prenom)` → `(prenom, subject)` — poids FSRS entraînés par matière |
| `events` | colonne `subject` (stats admin par matière) |
| `users` | **inchangée** — un seul compte |
| tables QCM | **inchangées** — déjà multi-thèmes/matières |

Migration = script `migrate_subjects.py` à lancer une fois : SQLite ne sait pas
modifier une PK en place, il faut `CREATE TABLE ... AS` + renommage pour les
tables à clé composée. Snapshot de `data/forgecards.db` avant, évidemment.

### 2. Configuration

- `SUBJECTS = ('physique', 'chimie')` dans `config.py`.
- `CHAPITRES` devient un dict par matière (`CHAPITRES['physique']` = liste
  actuelle, `CHAPITRES['chimie']` = à remplir).
- `params.json` → un fichier par matière (`params_physique.json`,
  `params_chimie.json`) : rétention FSRS, limites/jour et numéros max
  indépendants. `read_params(subject)` / `write_params(subject, data)`.
- Uploads : `uploads/fiche/<subject>/...` (sous-dossier par matière, les PDF
  actuels sont déplacés dans `physique/`).

### 3. Backend — scoping des routes

- `session['subject']`, défaut `'physique'`, modifié via
  `POST /api/subject {subject: 'chimie'}` (valide contre `SUBJECTS`).
- Helper `current_subject()` dans `helpers.py`, utilisé partout où une requête
  SQL touche une table migrée. Règle simple : **aucune requête métier sans
  filtre `subject`**, sinon fuite de données entre matières.
- Routes concernées : `cards.py`, `sr.py`, `draw.py`, `auth.py` (stats, deck,
  jokers), `views.py` (`/api/params`, `/api/chapitres`, `/uploads/fiche/...`),
  `internal.py` (weekly-stats, streak-guard), exports CSV.
- Le login élève (`/api/sr/login`) ne change pas : la matière est un contexte
  de navigation, pas un périmètre d'authentification.

### 4. Frontend — le switch

- Toggle dans le header à côté du jour/nuit : deux états (⚛️ Physique / ⚗️
  Chimie), même mécanique que le thème (clic → `POST /api/subject` →
  rechargement des données de la vue courante).
- Affichage immédiat via `localStorage` en attendant la réponse, la session
  serveur fait foi au chargement.
- Titre de page et libellés adaptés à la matière active.
- Le thème jour/nuit reste global (pas par matière).

### 5. Admin — `/cadmin`

- Nouvelle route `GET /cadmin` dans `views.py` qui rend **le même**
  `admin.html` avec `subject='chimie'` exposé au JS
  (`<body data-subject="chimie">` ou variable injectée).
- `session['admin']` existante : même mot de passe, une connexion admin ouvre
  les deux pages (décision actée).
- Le JS admin lit `data-subject` et passe `?subject=` (ou un header) à tous
  ses appels `/api/admin/*` et `/api/forgecards/*`. `/admin` sans paramètre =
  physique, comportement actuel préservé.
- Chaque page admin ne voit et ne modifie que sa matière : paramètres, fiches,
  élèves (suivi par matière), stats, exports.

### 6. Bot Discord

- `streak-guard`, `daily_reminder`, `weekly_recap` itèrent sur `SUBJECTS`.
- Streaks et jokers **par matière** : un élève peut avoir une streak physique
  de 12 et une streak chimie de 3, avec des jokers indépendants.
- Le recap du dimanche affiche deux sections (📊 Recap physique / 📊 Recap
  chimie) dans le même message, au format actuel.
- Le rappel quotidien mentionne la matière concernée.

### 7. Phases de mise en œuvre

1. **Config** : `SUBJECTS`, chapitres par matière, chemins uploads/params.
2. **Migration DB** : script + snapshot + vérification que la physique
   fonctionne exactement comme avant (régression zéro).
3. **Scoping backend** : `current_subject()` + propagation route par route,
   tests du smoke test étendus aux deux matières.
4. **Switch frontend** : toggle header + rechargement des vues.
5. **`/cadmin`** : route + `data-subject` + paramétrage des appels JS.
6. **Bot** : boucle sur les matières + recap en deux sections.
7. **Recette** : parcours complet sur les deux matières (tirage, SR, streak,
   joker, admin, exports), dont un passage du guard à 23h55 sur les deux.

### Points de vigilance

- **Fuite croisée** : le risque n°1 est une requête oubliée sans filtre
  `subject` (une fiche de chimie qui sort en physique). Le helper centralisé
  et la revue route par route sont là pour ça.
- **PK composites** : la migration SQLite doit recréer les tables, pas les
  altérer — tester le script sur une copie avant la prod.
- **Numérotation** : les fiches chimie repartent à n°1, toutes les URLs et
  logs qui affichent un `numero` doivent être lus avec leur matière.
- **Charge du guard** : deux matières = deux clôtures par élève et par nuit ;
  le rattrapage sur 14 jours reste borné, pas de souci de performance à cette
  échelle.
