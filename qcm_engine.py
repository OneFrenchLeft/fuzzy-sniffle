import itertools
import hashlib
import json
import random
import re
import time
from pathlib import Path

from flask import request, session
from flask_socketio import join_room, leave_room

from gevent.event import Event

QCM_DEFAULT_TIME_S = 30
QCM_MIN_TIME_S, QCM_MAX_TIME_S = 10, 300
QCM_READ_TIME_S = 4
QCM_REVEAL_TIME_S = 5
QCM_QUESTIONS_PER_MATCH = 5
QCM_MIN_QUESTIONS, QCM_MAX_QUESTIONS = 1, 20
QCM_MAX_POINTS = 1000
QCM_MIN_POINTS = 500
QCM_MIN_PLAYERS = 1
QCM_MAX_PLAYERS = 40
QCM_MAX_LOBBIES = 5
QCM_OFFLINE_GRACE_S = 60
QCM_START_DELAY_S = 2

QCM_THEME_PREFIX = {'physique': 'p', 'chimie': 'c', 'maths': 'm'}
QID_RE = re.compile(r'^[a-z]-[0-9a-f]{12}-[0-9a-f]{8}$')

def qid_for(theme, chapitre, question):
    prefix = QCM_THEME_PREFIX.get(theme, 'x')
    ch = hashlib.sha1(chapitre.strip().encode('utf-8')).hexdigest()[:12]
    qh = hashlib.sha1(question.strip().encode('utf-8')).hexdigest()[:8]
    return f'{prefix}-{ch}-{qh}'

QCM_THEMES = ('physique', 'chimie', 'maths')
QCM_DEFAULT_THEME = QCM_THEMES[0]

QCM_FILES = {}

PRESENT = {}
LOBBIES = {}
LOBBY_BY_PLAYER = {}
GAMES = {}
GAME_BY_PLAYER = {}
LOBBY_COUNTER = itertools.count(1)
GAME_COUNTER = itertools.count(1)
OFFLINE_TIMERS = {}

_QUESTIONS_CACHE = {}

io = None

_record_answer = None
_record_game = None
_review_query = None

def normalize_choice(choice):
    if isinstance(choice, dict):
        return {
            'text': str(choice.get('texte', choice.get('text', ''))).strip(),
            'image': str(choice.get('image', '')).strip(),
        }
    return {'text': str(choice).strip(), 'image': ''}

def read_qcm_questions(path, theme=None):
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cache_key = str(path) + '#' + (theme or '')
    cached = _QUESTIONS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            if not isinstance(item, dict):
                continue
            question = str(item.get('question', '')).strip()
            choices = [normalize_choice(c) for c in item.get('choix', [])]
            choices = [c for c in choices if c['text'] or c['image']]
            answer = int(item.get('reponse')) - 1
            timer = int(item.get('temps', QCM_DEFAULT_TIME_S))
            timer = max(QCM_MIN_TIME_S, min(QCM_MAX_TIME_S, timer))
            image = str(item.get('image_complete', item.get('image', ''))).strip()
            chapitre = str(item.get('chapitre', '')).strip() or 'Autre'
            if question and 2 <= len(choices) <= 4 and 0 <= answer < len(choices):
                entry = {'question': question, 'choices': choices,
                         'answer': answer, 'time': timer, 'image': image,
                         'chapitre': chapitre,
                         'explication': str(item.get('explication', '')).strip()}
                if theme:
                    qid = str(item.get('qid', '')).strip()
                    if not QID_RE.match(qid):
                        qid = qid_for(theme, chapitre, question)
                    entry['theme'] = theme
                    entry['qid'] = qid
                out.append(entry)
        except Exception:
            continue
    _QUESTIONS_CACHE[cache_key] = (mtime, out)
    return out

def available_chapters(themes):
    if not themes:
        themes = list(QCM_FILES)
    elif isinstance(themes, str):
        themes = [themes]
    seen = []
    for theme in themes:
        path = QCM_FILES.get(theme)
        if path is None:
            continue
        for q in read_qcm_questions(path):
            pair = [theme, q.get('chapitre', 'Autre')]
            if pair not in seen:
                seen.append(pair)
    return sorted(seen, key=lambda p: (p[1] == 'Autre', p[0], p[1].lower()))

def review_questions(prenom, themes, n, chapitres=None):
    # La connexion DB est fournie par app.py via le callback review_query.
    if _review_query is None:
        return []
    rows = _review_query(prenom)
    if not rows:
        return []
    wrong_qids = {r['qid'] for r in rows}
    last_wrong = {r['qid']: r['created_at'] for r in rows}
    pool = []
    for name in (themes or QCM_FILES):
        if name not in QCM_FILES:
            continue
        for q in read_qcm_questions(QCM_FILES[name], theme=name):
            if q.get('qid') in wrong_qids:
                q = dict(q)
                q['time'] = 600
                pool.append(q)
    if chapitres:
        wanted = set(chapitres)
        pool = [q for q in pool if (q.get('theme'), q.get('chapitre', 'Autre')) in wanted]
    pool.sort(key=lambda q: last_wrong.get(q.get('qid'), ''), reverse=True)
    return pool[:n]

def pick_questions(themes=None, n=QCM_QUESTIONS_PER_MATCH, chapitres=None):
    if not themes:
        themes = list(QCM_FILES)
    pool = []
    for name in themes:
        if name not in QCM_FILES:
            continue
        for q in read_qcm_questions(QCM_FILES[name], theme=name):
            pool.append((name, q))
    if chapitres:
        wanted = set()
        for pair in chapitres:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                wanted.add((str(pair[0]), str(pair[1])))
        pool = [(t, q) for t, q in pool if (t, q.get('chapitre', 'Autre')) in wanted]
    questions = [q for _, q in pool]
    random.shuffle(questions)
    return questions[:n]

def kahoot_points(elapsed_s, timer_s):
    ratio = max(0.0, min(1.0, elapsed_s / timer_s))
    return round(QCM_MAX_POINTS - (QCM_MAX_POINTS - QCM_MIN_POINTS) * ratio)

def media_refs(question):
    refs = []
    main = str(question.get('image') or '').strip()
    if main:
        refs.append(main)
    for choice in question.get('choices', []):
        if isinstance(choice, dict):
            ref = str(choice.get('image') or '').strip()
            if ref:
                refs.append(ref)
    return refs

def questions_media(questions):
    refs, seen = [], set()
    for question in questions:
        for ref in media_refs(question):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs

def lobby_payload(lobby):
    return {
        'id': lobby.lid,
        'nom': lobby.nom,
        'leader': lobby.leader,
        'themes': list(lobby.themes),
        'chapitres_sel': list(lobby.chapitres),
        'chapitres': available_chapters(lobby.themes),
        'nb_questions': lobby.nb_questions,
        'review_mode': lobby.review_mode,
        'delayed_feedback': lobby.delayed_feedback,
        'etat': lobby.etat,
        'joueurs': [
            {'prenom': p, 'pret': lobby.ready.get(p, False),
             'en_ligne': bool(PRESENT.get(p, {}).get('connected'))}
            for p in lobby.players
        ],
    }

def push_lobbies():
    io.emit('qcm_lobbies', {
        'lobbies': [lobby_payload(l) for l in LOBBIES.values()],
        'max': QCM_MAX_LOBBIES,
    }, room='qcm_lobby')

def cancel_offline_timer(prenom):
    timer = OFFLINE_TIMERS.pop(prenom, None)
    if timer is not None:
        try:
            timer.kill()
        except Exception:
            pass

class Lobby:
    def __init__(self, leader, theme=QCM_DEFAULT_THEME):
        self.lid = f'lobby_{next(LOBBY_COUNTER)}'
        self.nom = f'Lobby de {leader}'
        self.leader = leader
        self.players = [leader]
        self.ready = {leader: False}
        self.themes = [theme] if theme in QCM_FILES else []
        self.chapitres = []
        self.review_mode = False
        self.delayed_feedback = False
        self.nb_questions = QCM_QUESTIONS_PER_MATCH
        self.etat = 'attente'
        self.game = None

    def room(self):
        return f'room_{self.lid}'

    def add_player(self, prenom):
        if prenom not in self.players:
            self.players.append(prenom)
            self.ready[prenom] = False
            self.review_mode = False

    def remove_player(self, prenom):
        if prenom not in self.players:
            return
        self.players.remove(prenom)
        self.ready.pop(prenom, None)
        if self.leader == prenom and self.players:
            self.leader = self.players[0]
            self.ready[self.leader] = False
            self.nom = f'Lobby de {self.leader}'
            io.emit('qcm_error',
                    {'message': f'👑 {self.leader} devient le leader du lobby.'},
                    room=self.room())

    def set_ready(self, prenom, pret):
        if prenom in self.ready:
            self.ready[prenom] = bool(pret)

    def everyone_ready(self):
        return bool(self.players) and all(self.ready.get(p, False) for p in self.players)

def remove_player_from_lobby(prenom):
    lid = LOBBY_BY_PLAYER.pop(prenom, None)
    if not lid:
        return
    lobby = LOBBIES.get(lid)
    if lobby is None:
        return
    if lobby.etat == 'partie' and lobby.game is not None:
        game = lobby.game
        game.remove_player(prenom)
        GAME_BY_PLAYER.pop(prenom, None)
        sid = PRESENT.get(prenom, {}).get('sid')
        if sid:
            try:
                leave_room(game.gid, sid=sid)
            except Exception:
                pass
        if game.opened and len(game.answers) >= len(game.joueurs):
            game.answer_event.set()
        io.emit('qcm_error', {'message': f'{prenom} a quitté la partie.'}, room=game.gid)
    lobby.remove_player(prenom)
    if not lobby.players:
        del LOBBIES[lid]
        print(f'[qcm-web] {lid} supprimé (vide)')

class Game:
    def __init__(self, gid, lobby, questions):
        self.gid = gid
        self.lobby = lobby
        self.joueurs = list(lobby.players)
        self.theme = ' + '.join(lobby.themes) if lobby.themes else 'tout'
        self.questions = questions
        self.scores = {p: 0 for p in self.joueurs}
        self.answers = {}
        self.qindex = 0
        self.qstart = None
        self.opened = False
        self.open_end = 0.0
        self.alive = True
        self.answer_event = Event()
        self.review = getattr(lobby, 'review_mode', False)
        self.delayed = getattr(lobby, 'delayed_feedback', False)
        self.retry_queue = []
        self.feedback_buffer = []

    def send_all(self, event, payload):
        io.emit(event, payload, room=self.gid)

    def remove_player(self, prenom):
        if prenom in self.joueurs:
            self.joueurs.remove(prenom)
            self.answers.pop(prenom, None)
            self.scores.pop(prenom, None)

    def current_payload(self):
        if not (0 <= self.qindex < len(self.questions)):
            return None
        question = self.questions[self.qindex]
        return {
            'gid': self.gid, 'q': self.qindex + 1, 'total': len(self.questions),
            'question': question['question'], 'choix': question['choices'],
            'temps': question['time'], 'image': question['image'],
            'phase': 'ouverture' if self.opened else 'lecture',
            'ends_at': self.open_end, 'now': time.time(),
        }

    def run(self):
        try:
            io.sleep(1)
            self.send_all('qcm_debut', {
                'gid': self.gid, 'joueurs': self.joueurs, 'scores': self.scores,
                'total': len(self.questions), 'theme': self.theme,
                'review': self.review,
                'media': questions_media(self.questions),
            })
            print(f'[qcm-web] {self.gid} début: {len(self.questions)} questions, joueurs={self.joueurs}')
            io.sleep(3)

            while self.alive and self.joueurs and self.qindex < len(self.questions):
                question = self.questions[self.qindex]
                self.answers = {}
                self.answer_event = Event()
                self.opened = False

                self.open_end = time.time() + QCM_READ_TIME_S + question['time']

                self.send_all('qcm_question', {**self.current_payload(), 'phase': 'lecture'})
                print(f'[qcm-web] {self.gid} q{self.qindex + 1}/{len(self.questions)} lecture')
                io.sleep(QCM_READ_TIME_S)
                if not self.alive or not self.joueurs:
                    break

                self.opened = True
                self.qstart = time.monotonic()

                self.open_end = time.time() + question['time']
                self.send_all('qcm_question', {**self.current_payload(), 'phase': 'ouverture'})
                print(f'[qcm-web] {self.gid} q{self.qindex + 1} ouverture ({question["time"]} s)')

                remaining = self.open_end - time.time()
                if remaining > 0:
                    self.answer_event.wait(timeout=remaining)
                if not self.alive or not self.joueurs:
                    break

                cloturee_tot = (len(self.answers) >= len(self.joueurs)
                                and time.time() < self.open_end - 0.5)

                correct = question['answer']
                detail = []
                for player in self.joueurs:
                    answer = self.answers.get(player)
                    if answer is None:
                        points, ok, elapsed = 0, False, None
                    else:
                        choice, elapsed = answer
                        ok = choice == correct
                        if (self.review and not ok and not question.get('_reasked')
                                and self.qindex not in self.retry_queue):
                            self.retry_queue.append(self.qindex)
                        points = kahoot_points(elapsed, question['time']) if ok else 0
                        self.scores[player] = self.scores.get(player, 0) + points
                    detail.append({'prenom': player, 'ok': ok, 'pts': points, 'elapsed': elapsed})
                    if _record_answer is not None:
                        try:
                            _record_answer(
                                question.get('qid', ''),
                                question.get('theme', ''),
                                question.get('chapitre', 'Autre'),
                                question.get('question', ''),
                                player, ok, elapsed,
                            )
                        except Exception:
                            pass

                if self.delayed and not self.review:
                    self.feedback_buffer.append({
                        'q': self.qindex + 1, 'correct': correct,
                        'explication': question.get('explication', ''),
                    })
                else:
                    self.send_all('qcm_reveal', {
                        'gid': self.gid, 'q': self.qindex + 1, 'correct': correct,
                        'bonne': question['choices'][correct], 'detail': detail,
                        'scores': self.scores,
                        'cloturee_tot': cloturee_tot,
                        'explication': question.get('explication', ''),
                    })
                    print(f'[qcm-web] {self.gid} q{self.qindex + 1} reveal '
                          f'(anticipée={cloturee_tot}), scores={self.scores}')
                self.qindex += 1
                if self.review and self.qindex >= len(self.questions) and self.retry_queue:
                    # Micro-spacing : les questions ratees sont reposees une fois en fin de session.
                    retry = []
                    for idx in self.retry_queue:
                        qretry = dict(self.questions[idx])
                        qretry['_reasked'] = True
                        qretry['time'] = 3600
                        retry.append(qretry)
                    self.questions.extend(retry)
                    self.retry_queue = []
                io.sleep(0 if (self.delayed and not self.review) else QCM_REVEAL_TIME_S)

            classement = sorted(self.scores.items(), key=lambda item: -item[1])
            if _record_game is not None:
                try:
                    _record_game(self.gid, self.theme, len(self.questions),
                                 len(self.scores),
                                 [{'prenom': p, 'score': s} for p, s in classement])
                except Exception:
                    pass
            self.send_all('qcm_fin', {
                'gid': self.gid,
                'podium': [{'prenom': p, 'score': s} for p, s in classement],
                'corrections': self.feedback_buffer,
            })
            print(f'[qcm-web] {self.gid} fin: {classement}')
        except Exception as exc:
            import traceback
            traceback.print_exc()
            try:
                self.send_all('qcm_error', {'message': f'Partie interrompue ({exc}).'})
            except Exception:
                pass
            print(f'[qcm-web] {self.gid} partie interrompue: {exc!r}')
        finally:
            lobby = self.lobby
            for player in list(GAME_BY_PLAYER):
                if GAME_BY_PLAYER.get(player) == self.gid:
                    GAME_BY_PLAYER.pop(player, None)
            GAMES.pop(self.gid, None)

            if lobby and lobby.lid in LOBBIES:
                lobby.etat = 'attente'
                lobby.game = None
                lobby.ready = {p: False for p in lobby.players}
            push_lobbies()

def try_start_lobby(lobby):
    if lobby.etat != 'attente' or not lobby.everyone_ready():
        return
    if not PRESENT.get(lobby.leader, {}).get('connected'):
        return
    if lobby.review_mode:
        questions = review_questions(lobby.leader, lobby.themes, lobby.nb_questions, lobby.chapitres)
    else:
        questions = pick_questions(lobby.themes, lobby.nb_questions, lobby.chapitres)
    if not questions:
        cible = ' + '.join(lobby.themes) if lobby.themes else 'tous thèmes'
        if lobby.chapitres:
            cible += ' · ' + ' + '.join(f'{t} : {c}' for t, c in lobby.chapitres)
        io.emit('qcm_error',
                {'message': f"Aucune question valide pour {cible}."},
                room=lobby.room())
        return
    lobby.etat = 'partie'
    gid = f'qcm_{next(GAME_COUNTER)}'
    game = Game(gid, lobby, questions)
    lobby.game = game
    GAMES[gid] = game
    for player in list(game.joueurs):
        GAME_BY_PLAYER[player] = gid
        sid = PRESENT.get(player, {}).get('sid')
        if sid:
            join_room(gid, sid=sid)
    print(f'[qcm-web] {gid} lancé depuis {lobby.lid} '
          f'(thèmes={lobby.themes or "tous"}, chapitres={lobby.chapitres or "tous"}, n={len(questions)})')
    push_lobbies()
    io.sleep(QCM_START_DELAY_S)
    if lobby.etat != 'partie' or lobby.game is not game or not lobby.everyone_ready() or not game.joueurs:
        GAMES.pop(gid, None)
        for player in list(game.joueurs):
            GAME_BY_PLAYER.pop(player, None)
        lobby.game = None
        if lobby.etat == 'partie':
            lobby.etat = 'attente'
        push_lobbies()
        return
    io.start_background_task(game.run)

def register(socketio, data_dir, on_answer=None, on_game_end=None, review_query=None):
    global io, QCM_FILES, _record_answer, _record_game, _review_query
    from config import ENABLED_SUBJECTS
    io = socketio
    # Seules les matieres activees par les flags du .env sont jouables :
    # un client ne peut pas forcer une partie sur une matiere cachee.
    QCM_FILES = {name: Path(data_dir) / f'qcm_{name}.json'
                 for name in QCM_THEMES if name in ENABLED_SUBJECTS}
    _record_answer = on_answer
    _record_game = on_game_end
    _review_query = review_query

    @io.on('qcm_ping')
    def on_ping(data):
        return {'now': time.time()}

    @io.on('qcm_join_page')
    def on_join_page():
        prenom = session.get('sr_user')
        print(f'[qcm-web] join: prenom={prenom!r} sid={request.sid}')
        if not prenom:
            return False
        cancel_offline_timer(prenom)
        old = PRESENT.get(prenom)
        if old and old.get('sid') and old['sid'] != request.sid:
            try:
                leave_room('qcm_lobby', sid=old['sid'])
            except Exception:
                pass
        PRESENT[prenom] = {'sid': request.sid, 'connected': True}
        join_room('qcm_lobby')

        lid = LOBBY_BY_PLAYER.get(prenom)
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is not None:
            join_room(lobby.room())

        gid = GAME_BY_PLAYER.get(prenom)
        if gid and gid in GAMES:
            join_room(gid)
            game = GAMES[gid]
            io.emit('qcm_debut', {
                'gid': game.gid, 'joueurs': game.joueurs, 'scores': game.scores,
                'total': len(game.questions), 'theme': game.theme,
                'media': questions_media(game.questions), 'resync': True,
            }, room=request.sid)
            payload = game.current_payload()
            if payload:
                payload['deja_repondu'] = prenom in game.answers
                io.emit('qcm_question', payload, room=request.sid)
            print(f'[qcm-web] {prenom} resynchronisé sur {gid}')
        push_lobbies()

    @io.on('disconnect')
    def on_disconnect():
        for prenom, info in list(PRESENT.items()):
            if info.get('sid') == request.sid:
                info['connected'] = False
                cancel_offline_timer(prenom)

                def expire(prenom=prenom):
                    try:
                        io.sleep(QCM_OFFLINE_GRACE_S)
                        if PRESENT.get(prenom, {}).get('connected'):
                            return
                        PRESENT.pop(prenom, None)
                        remove_player_from_lobby(prenom)
                        push_lobbies()
                    except Exception:
                        pass

                OFFLINE_TIMERS[prenom] = io.start_background_task(expire)
                push_lobbies()
                break

    @io.on('qcm_leave_page')
    def on_leave_page():
        prenom = session.get('sr_user')
        if not prenom or GAME_BY_PLAYER.get(prenom):
            return
        PRESENT.pop(prenom, None)
        cancel_offline_timer(prenom)
        remove_player_from_lobby(prenom)
        push_lobbies()

    @io.on('qcm_lobby_create')
    def on_lobby_create(data):
        prenom = session.get('sr_user')
        data = data or {}
        if not prenom or not PRESENT.get(prenom, {}).get('connected'):
            return
        if LOBBY_BY_PLAYER.get(prenom):
            return {'ok': False, 'message': 'Tu es déjà dans un lobby.'}
        if len(LOBBIES) >= QCM_MAX_LOBBIES:
            return {'ok': False, 'message': 'Nombre maximum de lobbies atteint.'}
        if GAME_BY_PLAYER.get(prenom):
            return {'ok': False, 'message': 'Tu es déjà en partie.'}
        theme = str(data.get('theme') or QCM_DEFAULT_THEME)
        lobby = Lobby(prenom, theme)
        LOBBIES[lobby.lid] = lobby
        LOBBY_BY_PLAYER[prenom] = lobby.lid
        join_room(lobby.room())
        print(f'[qcm-web] {lobby.lid} créé par {prenom} (thèmes={lobby.themes or "tous"})')
        push_lobbies()
        return {'ok': True, 'lobby': lobby_payload(lobby)}

    @io.on('qcm_lobby_join')
    def on_lobby_join(data):
        prenom = session.get('sr_user')
        data = data or {}
        if not prenom or not PRESENT.get(prenom, {}).get('connected'):
            return
        lid = str(data.get('id') or '')
        lobby = LOBBIES.get(lid)
        if lobby is None:
            return {'ok': False, 'message': 'Lobby introuvable.'}
        if lobby.etat == 'partie':
            return {'ok': False, 'message': 'La partie de ce lobby est en cours.'}
        if LOBBY_BY_PLAYER.get(prenom):
            return {'ok': False, 'message': 'Tu es déjà dans un lobby.'}
        if GAME_BY_PLAYER.get(prenom):
            return {'ok': False, 'message': 'Tu es déjà en partie.'}
        if len(lobby.players) >= QCM_MAX_PLAYERS:
            return {'ok': False, 'message': 'Lobby plein.'}
        lobby.add_player(prenom)
        LOBBY_BY_PLAYER[prenom] = lobby.lid
        join_room(lobby.room())
        print(f'[qcm-web] {prenom} rejoint {lobby.lid}')
        push_lobbies()
        return {'ok': True, 'lobby': lobby_payload(lobby)}

    @io.on('qcm_lobby_leave')
    def on_lobby_leave(data):
        prenom = session.get('sr_user')
        if not prenom:
            return
        lid = LOBBY_BY_PLAYER.get(prenom)
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is not None:
            try:
                leave_room(lobby.room())
            except Exception:
                pass
        remove_player_from_lobby(prenom)
        push_lobbies()
        return {'ok': True}

    @io.on('qcm_lobby_ready')
    def on_lobby_ready(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        pret = bool(data.get('pret', True))
        lobby.set_ready(prenom, pret)
        print(f'[qcm-web] {lobby.lid} {prenom} prêt={pret}')
        push_lobbies()
        try_start_lobby(lobby)

    @io.on('qcm_lobby_review')
    def on_lobby_review(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader peut activer le mode revue'}
        if len(lobby.players) != 1:
            return {'ok': False, 'message': 'Le mode revue est solo'}
        review = bool(data.get('review', False))
        if review:
            qs = review_questions(prenom, lobby.themes, lobby.nb_questions, lobby.chapitres)
            if not qs:
                return {'ok': False, 'message': 'Aucune erreur a revoir'}
        lobby.review_mode = review
        if review:
            lobby.delayed_feedback = True
        print(f'[qcm-web] {lobby.lid} mode revue={review}')
        push_lobbies()

    @io.on('qcm_lobby_feedback')
    def on_lobby_feedback(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader peut changer cette option'}
        lobby.delayed_feedback = bool(data.get('delayed', False))
        print(f'[qcm-web] {lobby.lid} feedback retarde={lobby.delayed_feedback}')
        push_lobbies()

    @io.on('qcm_lobby_theme')
    def on_lobby_theme(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader choisit le thème.'}
        raw = data.get('themes')
        if isinstance(raw, list):
            themes = [str(t) for t in raw if str(t) in QCM_FILES]
        else:
            t = str(data.get('theme') or '')
            themes = [t] if t in QCM_FILES else []
        if not themes or len(themes) >= len(QCM_FILES):
            themes = []
        lobby.themes = themes

        lobby.chapitres = []

        lobby.ready = {p: False for p in lobby.players}
        print(f'[qcm-web] {lobby.lid} thèmes -> {themes or "tous"}')
        push_lobbies()
        return {'ok': True}

    @io.on('qcm_lobby_chapitre')
    def on_lobby_chapitre(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader choisit le chapitre.'}
        raw = data.get('chapitres')
        chapitres = []
        if isinstance(raw, list):
            for pair in raw:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    t, c = str(pair[0]), str(pair[1]).strip()
                    if t in QCM_FILES and c:
                        chapitres.append([t, c])
        else:
            single = str(data.get('chapitre') or '').strip()
            if single:
                for t, c in available_chapters(lobby.themes):
                    if c == single:
                        chapitres.append([t, c])
        valid = available_chapters(lobby.themes)
        chapitres = [p for p in chapitres if p in valid]

        if not chapitres or len(chapitres) >= len(valid):
            chapitres = []
        lobby.chapitres = chapitres

        lobby.ready = {p: False for p in lobby.players}
        print(f'[qcm-web] {lobby.lid} chapitres -> {lobby.chapitres or "tous"}')
        push_lobbies()
        return {'ok': True}

    @io.on('qcm_lobby_length')
    def on_lobby_length(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader choisit la longueur.'}
        try:
            nb = int(data.get('nb'))
        except (TypeError, ValueError):
            return {'ok': False, 'message': 'Nombre de questions invalide.'}
        lobby.nb_questions = max(QCM_MIN_QUESTIONS, min(QCM_MAX_QUESTIONS, nb))
        print(f'[qcm-web] {lobby.lid} longueur -> {lobby.nb_questions}')
        push_lobbies()
        return {'ok': True, 'nb_questions': lobby.nb_questions}

    @io.on('qcm_lobby_kick')
    def on_lobby_kick(data):
        prenom = session.get('sr_user')
        data = data or {}
        lid = LOBBY_BY_PLAYER.get(prenom or '')
        lobby = LOBBIES.get(lid) if lid else None
        if lobby is None or lobby.etat != 'attente':
            return
        if prenom != lobby.leader:
            return {'ok': False, 'message': 'Seul le leader peut expulser.'}
        cible = str(data.get('prenom') or '')
        if cible == lobby.leader:
            return {'ok': False, 'message': 'Le leader ne peut pas s’expulser.'}
        if cible not in lobby.players:
            return {'ok': False, 'message': 'Joueur introuvable dans le lobby.'}
        sid = PRESENT.get(cible, {}).get('sid')
        remove_player_from_lobby(cible)
        if sid:
            try:
                leave_room(lobby.room(), sid=sid)
            except Exception:
                pass
            io.emit('qcm_kicked', {'lobby': lobby.nom}, room=sid)
        print(f'[qcm-web] {lobby.lid} {cible} expulsé par {prenom}')
        push_lobbies()
        return {'ok': True}

    @io.on('qcm_answer')
    def on_answer(data):
        prenom = session.get('sr_user')
        if not isinstance(data, dict):
            return {'ok': False, 'raison': 'choix invalide'}
        gid = GAME_BY_PLAYER.get(prenom or '')
        game = GAMES.get(gid) if gid else None
        if game is None or not game.alive:
            return {'ok': False, 'raison': 'pas de partie en cours'}
        try:
            choice = int((data or {}).get('choice'))
        except (TypeError, ValueError):
            return {'ok': False, 'raison': 'choix invalide'}
        if not (0 <= game.qindex < len(game.questions)):
            return {'ok': False, 'raison': 'question terminée'}
        question = game.questions[game.qindex]
        if not game.opened or game.qstart is None:
            return {'ok': False, 'raison': 'phase de lecture'}
        if prenom in game.answers:
            return {'ok': False, 'raison': 'déjà répondu'}
        if not (0 <= choice < len(question['choices'])):
            return {'ok': False, 'raison': 'choix invalide'}
        elapsed = time.monotonic() - game.qstart
        if elapsed > question['time'] + 1.5:
            return {'ok': False, 'raison': 'trop tard'}
        game.answers[prenom] = (choice, elapsed)
        print(f'[qcm-web] {game.gid} réponse de {prenom}: choix {choice} en {elapsed:.1f} s')

        if len(game.answers) >= len(game.joueurs):
            game.answer_event.set()
        return {'ok': True}

    @io.on('qcm_quit')
    def on_quit(data):
        prenom = session.get('sr_user')
        if not prenom:
            return
        gid = GAME_BY_PLAYER.get(prenom)
        if gid:
            try:
                leave_room(gid)
            except Exception:
                pass
        remove_player_from_lobby(prenom)
        push_lobbies()
        return {'ok': True}