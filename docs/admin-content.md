# Administration des fiches, des QCM et d’Ankimie

## Retrouver une action

Le menu de l’administration est organisé par intention :

- **Contenu** : « Fiches PDF » pour importer et gérer les fichiers ; « Questions
  QCM » pour éditer la banque et activer les chapitres.
- **Réglages** : « Réglages de l’application » rassemble les options ; « Vérifier
  le tirage » simule la répartition des fiches sans lancer de tirage élève.
- **Suivi des élèves** : comptes et progression, notes de blocage, résultats QCM,
  suivi Ankimie, bilans de la classe et exports.

Les réglages comportent cinq sections numérotées, sans autre niveau de menu :
modules et personnalisation ; fiches disponibles et chapitres ; révision des
fiches ; révision Ankimie ; rappel Discord. « Enregistrer tous les réglages »
enregistre l’ensemble. Les réglages Ankimie ne sont pas dupliqués dans le suivi :
le bouton de ce dernier ouvre directement la section correspondante.

## Gérer la banque QCM

Dans **Contenu → Questions QCM → Gérer les questions**, choisir la matière puis
le chapitre dans la liste de gauche. Le chemin « Matière > Chapitre » rappelle
le périmètre courant. La recherche filtre les questions et les réponses du
périmètre ; changer de chapitre efface ce filtre.

« Nouvelle question », « Modifier » et « Dupliquer » ouvrent le même formulaire.
Dans un chapitre sélectionné, la destination est automatique et le sélecteur de
chapitre est masqué. Dans « Tous les chapitres », choisir la destination dans le
formulaire. Les identifiants sont générés automatiquement ; une duplication
reçoit un nouvel identifiant. Deux à quatre réponses sont requises, avec une
bonne réponse cochée. Illustration, LaTeX et explication sont facultatifs.

« Supprimer » est disponible dans la liste et dans le formulaire d’une question
existante. La confirmation indique que la question sera retirée de QCM et
d’Ankimie, tandis que ses révisions historiques seront conservées. Une nouvelle
question ou une duplication non enregistrée ne possède pas ce bouton.

Le champ « Nouveau chapitre », sous la liste des chapitres, crée un chapitre
**inactif par défaut**. L’interrupteur de chaque chapitre contrôle sa disponibilité
pour les élèves. Désactiver conserve les questions et leurs historiques.
L’aide « Comment fonctionne l’activation ? » rappelle ce comportement au besoin.

Le bouton de suppression d’un chapitre ouvre une confirmation. Si le chapitre
contient des questions, choisir où les conserver : elles sont déplacées avec leurs
identifiants, puis suivent l’activation du chapitre de destination. « Autre »
(ou « À classer » pour supprimer « Autre ») peut être créé automatiquement,
inactif par défaut. Un chapitre vide ne demande pas de destination. Supprimer un
chapitre conserve les historiques des questions déplacées.

## Choisir les cartes Ankimie

Ankimie utilise automatiquement **toutes les questions des chapitres QCM actifs**
dans les matières cochées sous **Réglages de l’application → 4. Révision Ankimie**.
La matière choisie par défaut est la chimie. Aucune gestion de collections ni
activation indépendante Ankimie n’est nécessaire. Ajouter une matière rend
éligibles ses chapitres QCM actifs ; décocher une matière conserve les états de
révision, qui redeviennent utilisables si elle est réactivée.

Les états FSRS et le quota Ankimie restent distincts des fiches PDF Forgecards.
Chaque question conserve un seul état par élève, lié à son identifiant stable.
Le nombre quotidien de nouvelles cartes peut être zéro ; les révisions déjà
acquises restent indépendantes de ce quota. La rétention cible et le quota se
règlent dans la même section Ankimie. Le suivi ne sert qu’à consulter les résultats.

## Importer un tableur

« Importer » propose un modèle CSV. Choisir d’abord la matière et le chapitre
destinataire, exporter le tableur en CSV, puis vérifier l’aperçu. Les colonnes sont :
Question, Réponse A/B/C/D, Bonne réponse (A/B/C/D), Temps (secondes), Explication,
Image et Formule. Deux à quatre réponses sont requises ; Temps peut être laissé
vide pour 30 secondes. Explication, Image et Formule sont facultatifs.
Les fichiers UTF-8, Windows-1252 et UTF-16 avec BOM sont acceptés.

Un import invalide est entièrement bloqué. Un import valide **ajoute** les questions
au chapitre sans remplacer la banque. L’aperçu validé reste confirmable pendant
30 minutes, tant que la banque n’a pas changé. En cas de modification, refaire
l’aperçu. Une confirmation ne peut pas être réutilisée pour importer deux fois
le même lot ni pour ne conserver qu’une partie du lot validé.

## Tester et éditer en JSON

L’onglet **Tester une question** vérifie le rendu sans enregistrer de contenu.
L’onglet **Édition avancée (JSON)** conserve l’édition en masse et le chargement
d’un fichier JSON. Son contenu correspond à la matière et au chapitre sélectionnés.
Une édition d’un chapitre préserve les autres ; « Tous les chapitres » permet de
modifier toute la matière. Les images utilisent `image` ; l’ancien champ
`image_complete` est normalisé à l’enregistrement. Les chemins locaux existants
et les URL HTTPS restent pris en charge par le rendu.

« Enregistrer » valide puis sauvegarde en une action. Une suppression de questions
est validée avant confirmation ; annuler conserve le brouillon. Une liste vide
retire les questions du périmètre sélectionné. Les modifications JSON non
sauvegardées sont prévisualisées dans la liste mais bloquent les mutations
visuelles. Changer de périmètre ou recharger demande confirmation avant de les
abandonner. Une erreur de chargement ou de sauvegarde conserve les modifications
pour correction ou nouvelle tentative. Les réponses réseau anciennes ne remplacent
pas la matière ou le chapitre actuel.

## Modules, compte et séries

Désactiver un module le retire des menus et masque ses informations dans l’espace
compte des élèves. Les données et le suivi administrateur restent conservés.
Quand Forgecards est désactivé, l’accueil ouvre l’espace compte. La déconnexion
attend la confirmation du serveur avant de revenir à l’accueil.

Une session élève est liée à l’identité stable du compte, enregistrée dans la
table `events` existante : supprimer puis recréer le même prénom ne redonne pas
accès à son ancien cookie. Un changement de mot de passe ne modifie pas cette
identité. Les sessions antérieures restent reconnues si leur compte n’a pas été
supprimé. Ce contrôle ne nécessite aucune nouvelle table ni migration de schéma.

La désactivation de Forgecards suspend ses obligations : les journées de pause
ne dépensent pas de joker et n’ajoutent pas de jour à la série. Elles relient les
journées actives de part et d’autre de la pause. Les dates de pause sont conservées
dans les paramètres partagés par le site et le bot. Le jour de réactivation est
un jour actif ; une pause ne valide pas rétroactivement une journée manquée avant
sa désactivation. Pour un ancien déploiement déjà désactivé sans date enregistrée,
la pause commence à la première lecture après mise à jour.

## Architecture et compatibilité

Les fichiers `data/qcm_physique.json`, `data/qcm_chimie.json` et
`data/qcm_maths.json` restent la banque canonique. Le validateur commun normalise
les formulaires, le JSON et les imports. Les identifiants valides sont conservés.
Les formules des questions et des réponses riches sont rendues par QCM et Ankimie.

La table SQLite `content_chapters(theme, chapitre, qcm_active, ankimie_active)`
conserve les chapitres vides et l’activation. `qcm_active` contrôle désormais les
deux activités ; l’ancienne colonne `ankimie_active` reste compatible avec les
bases existantes et ne constitue pas une seconde activation. Les anciennes données
de collections ne nécessitent aucune suppression manuelle.

Sans configuration explicite, les chapitres QCM existants restent actifs.
La sélection élève filtre les activations à la lecture ; le cache garde la banque
complète. Les parties QCM déjà lancées conservent leurs questions ; les nouvelles
sélections utilisent les activations courantes. Une question retirée pendant une
session Ankimie provoque une actualisation lors de sa prochaine utilisation.

Les sauvegardes valident la banque entière et utilisent un fichier temporaire,
`fsync`, un remplacement atomique et un verrou par matière. Les nouveaux chapitres
sont persistés inactifs avant l’exposition du contenu. Une révision SHA-256 protège
les clients récents contre les modifications concurrentes (HTTP 409). L’ancien
PUT complet reste compatible avec les clients sans révision. Une confirmation
d’import signée lie la matière, le chapitre, la révision et toutes les questions.

## API

Tous les endpoints d’administration conservent l’authentification admin.

| Endpoint | Rôle |
| --- | --- |
| GET `/api/admin/content/subjects` | Matières, chapitres et compteurs |
| POST/PUT `/api/admin/content/<theme>/chapters` | Créer un chapitre / modifier son activation |
| DELETE `/api/admin/content/<theme>/chapters` | Déplacer les questions puis supprimer le chapitre, avec révision obligatoire |
| GET/PUT `/api/admin/qcm/<theme>` | Lire la banque, sa révision et ses métadonnées / éditer en masse |
| POST `/api/admin/qcm/<theme>/questions` | Créer une question |
| PUT/DELETE `/api/admin/qcm/<theme>/questions/<qid>` | Modifier / supprimer la question |
| POST `/api/admin/qcm/<theme>/validate` | Valider sans enregistrer |
| GET `/api/admin/qcm/import-template.csv` | Télécharger le modèle CSV |
| POST `/api/admin/qcm/<theme>/import/preview` | Prévisualiser le lot avec les erreurs par question |
| POST `/api/admin/qcm/<theme>/import` | Confirmer le lot intégralement validé |

Les mutations visuelles envoient la révision chargée. Un HTTP 409 exige de recharger
la banque et de vérifier les changements avant de réessayer. Les imports ajoutent
au contenu ; les mutations individuelles ne remplacent que la question ciblée.
La validation est répétée avant chaque remplacement.

## Déploiement et vérification

Redéployer le dépôt et redémarrer Flask ainsi que le bot Discord. `init_db()`
préserve les données existantes ; aucun SQL manuel ni réimport des questions n’est
nécessaire. Les chemins de données et les droits d’écriture restent habituels.
Les verrous utilisent `fcntl` sous Linux. Aucune dépendance de production n’est ajoutée.

Les contrôles serveur et navigateur utilisent des bases et banques temporaires.
Ils ne modifient pas les données locales ou de production.

```sh
venv/bin/python checks/config_runtime_checks.py
venv/bin/python checks/api_integrity_checks.py
venv/bin/python checks/sr_concurrency_checks.py
venv/bin/python checks/qcm_runtime_checks.py
venv/bin/python checks/content_admin_checks.py
node --test test_qcm_editor.js test_sr_hs_ui.js checks/frontend_regression.cjs checks/qcm_client_checks.cjs
node checks/admin_content_browser.cjs
node checks/module_admin_browser.cjs
```

Les vérifications de configuration, fichiers, sessions, concurrence et QCM copient
les sources dans un dossier temporaire avant de créer leurs bases et PDF factices.
Le bot Discord est contrôlé avec des salons simulés ; aucun message réel n'est
envoyé. Les tests couvrent aussi les erreurs disque/SQL, les suppressions de
comptes, les reconnexions, les pauses et les reprises sans modifier les règles
de notation ou de quotas.

Le contrôle navigateur nécessite Playwright et Chromium déjà disponibles dans
l’environnement de vérification. Il démarre automatiquement un serveur de données
temporaires sur un port libre. `CHROMIUM_PATH` peut définir le chemin de Chromium
(défaut : `/usr/bin/chromium`) ; `CONTENT_CHECK_SCREENSHOTS` un dossier de captures.
Une URL `CONTENT_CHECK_URL` n’est acceptée que si le serveur expose le marqueur de
fixture isolée. Ne jamais pointer ce contrôle vers une instance de production.

Le contrôle couvre les formulaires CRUD et la suppression dans l’éditeur, les
réponses, l’import valide et invalide, le JSON et ses conflits de chargement,
la conservation d’un brouillon après erreur, la navigation entre réglages et suivi
Ankimie, ainsi que les tailles 320/390/768/1280 px dans les deux thèmes.
