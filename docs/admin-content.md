# Gestion des QCM et des collections Ankimie

## Pour les enseignants

Dans Admin → Éditeur & aperçu, choisir une matière puis un chapitre. La banque
affiche les questions, les réponses, la bonne réponse, le temps, les illustrations
et les explications. « Ajouter », « Modifier » et « Dupliquer » ouvrent un
formulaire. Les réponses se gèrent avec des boutons et la bonne réponse se coche.
Les identifiants des questions sont automatiques.

L’aperçu des chapitres distingue le contenu **disponible** du contenu **actif pour
les élèves**. Les nouveaux chapitres sont inactifs ; cocher leur activation pour
les publier. Désactiver un chapitre ne supprime ni ses questions ni l’historique.
QCM et Ankimie ont des activations indépendantes : publier un QCM n’oblige pas à
publier les mêmes cartes en répétition espacée.

« Importer des questions » propose un modèle CSV pour un tableur. Choisir d’abord
la matière et le chapitre destinataire, exporter le tableur en CSV, puis vérifier
l’aperçu. Les colonnes sont : Question, Réponse A/B/C/D, Bonne réponse (A/B/C/D),
Temps (secondes), Explication, Image et Formule. Deux à quatre réponses sont
requises ; Temps peut être laissé vide pour 30 secondes. Les trois dernières
colonnes sont facultatives. Les fichiers UTF-8, Windows-1252 et UTF-16 avec BOM
sont acceptés. Un import invalide est entièrement bloqué. Un import valide
**ajoute** les questions au chapitre sans remplacer celles de la banque.
L’aperçu validé reste confirmable pendant 30 minutes, tant que la banque n’a pas
changé. En cas de modification, refaire l’aperçu.

Dans Admin → Ankimie, choisir une matière et un chapitre de révision. Les cartes
proviennent de la banque QCM, pas des fiches PDF Forgecards. Le sélecteur permet
de chercher une carte existante et de l’ajouter à une collection. Retirer une
carte d’une collection conserve la question, les autres collections et les
révisions. Une même question dans plusieurs collections n’est présentée qu’une
fois aux élèves et conserve un seul état de révision.

La suppression depuis la banque QCM retire définitivement la question de cette
banque et de la disponibilité des collections, après confirmation. Les historiques
restent conservés. La purge historique existante reste une action explicite,
distincte de la désactivation et du retrait d’une collection.

## Mode avancé

« Mode avancé — édition JSON » est fermé par défaut. Il conserve l’édition en
masse, le chargement d’un fichier JSON, le formatage et l’ancien test de rendu.
Le contenu montré correspond à la matière et au chapitre sélectionnés. Une
édition d’un seul chapitre préserve tous les autres chapitres. Choisir « Tous les
chapitres » permet de modifier la matière entière.

Le bouton de validation appelle le serveur. Les modifications JSON non
enregistrées apparaissent en aperçu visuel, mais bloquent les actions visuelles
jusqu’à leur enregistrement ou leur abandon. Un changement de périmètre ou un
rechargement demande confirmation avant d’abandonner un brouillon. Une sauvegarde
ne remplace jamais un contenu dont le chargement a échoué. Les réponses réseau
anciennes ne remplacent pas la matière ou le chapitre actuel.

## Architecture et compatibilité

Les fichiers `data/qcm_physique.json`, `data/qcm_chimie.json` et
`data/qcm_maths.json` restent l’unique banque canonique. Le validateur existant
normalise les données pour les formulaires, le JSON et les imports. Les
identifiants valides sont préservés ; les autres utilisent le mécanisme de
génération existant. Les formules des questions et réponses riches sont rendues
par le moteur QCM et par Ankimie.

Deux tables SQLite sont ajoutées automatiquement et sans réécrire les historiques :

- `content_chapters(theme, chapitre, qcm_active, ankimie_active)` : activation
  indépendante des deux activités et chapitres vides en préparation.
- `ankimie_memberships(theme, chapitre, qid, included)` : ajouts et retraits
  explicites dans les collections.

Sans configuration explicite, les chapitres QCM existants restent actifs et les
questions de chimie restent dans leurs chapitres Ankimie historiques. Les
questions de physique et maths ne sont ajoutées à Ankimie que volontairement et
leurs collections sont inactives par défaut. Les quotas, états et révisions FSRS
existants sont conservés. La sélection des élèves filtre les activations à la
lecture ; le cache des fichiers garde toujours la banque complète. Les parties
QCM déjà lancées gardent leurs questions ; les nouvelles sélections utilisent
les activations courantes. Une carte retirée pendant une session Ankimie provoque
un rafraîchissement du deck lors de sa prochaine révélation ou notation.

Les sauvegardes valident la candidature entière puis utilisent un fichier
temporaire unique, `fsync` et un remplacement atomique. Un verrou fichier par
matière sérialise les modifications entre processus. Les nouvelles activations
inactives sont persistées avant d’exposer le nouveau contenu dans le fichier,
y compris en cas d’arrêt du processus. Une révision SHA-256 du fichier protège
les clients récents contre les modifications concurrentes (réponse HTTP 409).
L’ancien PUT complet conserve sa compatibilité pour les clients sans révision.
Une confirmation d’import signée lie la matière, le chapitre, la révision et
toutes les questions validées : modifier ou réduire le lot invalide la confirmation.

## API

Tous les endpoints d’administration conservent l’authentification admin existante.

| Endpoint | Rôle |
| --- | --- |
| GET `/api/admin/content/subjects` | Matières, chapitres et compteurs |
| POST/PUT `/api/admin/content/<theme>/chapters` | Créer un chapitre / modifier ses activations |
| GET/PUT `/api/admin/qcm/<theme>` | JSON canonique, révision et métadonnées / édition en masse |
| POST `/api/admin/qcm/<theme>/questions` | Créer une question |
| PUT/DELETE `/api/admin/qcm/<theme>/questions/<qid>` | Modifier / supprimer la question |
| POST `/api/admin/qcm/<theme>/validate` | Valider sans enregistrer |
| GET `/api/admin/qcm/import-template.csv` | Modèle de tableur |
| POST `/api/admin/qcm/<theme>/import/preview` | Aperçu CSV ou JSON avec erreurs par question |
| POST `/api/admin/qcm/<theme>/import` | Confirmer le lot intégralement validé |
| GET/PUT `/api/admin/ankimie/<theme>/collections` | Lire une collection / ajouter ou retirer une carte |

Les mutations visuelles envoient la révision chargée. Un 409 exige de recharger
la banque et de vérifier les changements avant de réessayer. Les mutations de
questions ne remplacent que la question ciblée dans la dernière banque lue sous
verrou ; les imports ajoutent à cette banque. La validation est répétée avant
chaque remplacement.

## Déploiement

Redéployer les fichiers du dépôt et redémarrer Flask. `init_db()` crée les deux
tables au démarrage. Aucun SQL manuel ni réimport des questions n’est nécessaire.
Les fichiers JSON, la base et le dossier `data/` gardent les chemins et besoins
d’écriture habituels. Le verrou utilise `fcntl` sous Linux. Aucun framework ni
dépendance de production supplémentaire n’est ajouté.

## Vérification

Les contrôles serveur et navigateur utilisent des bases et banques temporaires.
Ils ne touchent pas au contenu local ou de production.

```sh
venv/bin/python checks/content_admin_checks.py
node --test test_qcm_editor.js test_sr_hs_ui.js
```

Le contrôle navigateur nécessite Playwright et Chromium. Pour une installation
locale de Playwright : `npm install --no-save --package-lock=false playwright`.
Exécuter ensuite :

```sh
node checks/admin_content_browser.cjs
```

Il démarre automatiquement un serveur de données temporaires sur un port libre.
`CHROMIUM_PATH` peut définir le chemin de Chromium (défaut : `/usr/bin/chromium`).
`CONTENT_CHECK_SCREENSHOTS` peut désigner un dossier de captures. Le contrôle
couvre les formulaires CRUD, l’ajout/retrait de réponses, les imports invalides
et valides, la synchronisation JSON, les réponses tardives, les collections et
les tailles 320/390/768/1280 px dans les deux thèmes.

## Fichiers concernés

- Données et API : `db.py`, `content_catalog.py`, `content_admin.py`,
  `qcm_admin.py`, `qcm_engine.py`, `ankimie.py`, `sr.py`, `app.py`.
- Interface : `templates/admin.html`, `templates/base.html`,
  `templates/admin_qcm_visual.html`, `templates/admin_ankimie_content.html`,
  `static/js/app.js`, `static/js/admin_content.js`, `static/css/admin_content.css`.
- Contrôles : `test_qcm_editor.js`, `checks/content_admin_checks.py`,
  `checks/admin_content_browser.cjs`.
- Documentation/outillage : `README.md`, `.gitignore`, `docs/admin-content.md`.
