"""Lancer : venv/bin/python checks/qcm_runtime_checks.py (donnees temporaires)."""
import os
import ast
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ['MADEC_ADMIN_PASSWORD'] = 'qcm-checks-only'
# Ne lit ni le .env ni les donnees de production, meme a l'import.
with patch('dotenv.load_dotenv', return_value=False):
    import config

initial = tempfile.TemporaryDirectory(prefix='forgecards-qcm-import-')
config.DATA = Path(initial.name)
config.DB_PATH = config.DATA / 'checks.db'
config.PARAMS_PATH = config.DATA / 'params.json'
config.UPLOADS = config.DATA / 'uploads'

import db as database
import auth
import qcm_engine as engine
import qcm_invites
from flask import Flask, session
from flask_socketio import SocketIO


class Task:
    def kill(self):
        pass


class QcmRuntime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='forgecards-qcm-case-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config.DATA = self.root
        config.PARAMS_PATH = self.root / 'params.json'
        config.UPLOADS = self.root / 'uploads'
        config.UPLOADS.mkdir()
        database.DB_PATH = self.root / 'checks.db'
        database.UPLOADS = config.UPLOADS
        database._db_ready = False
        database.init_db()
        conn = database.db()
        conn.execute("INSERT INTO users(prenom,emoji_password) VALUES ('Élève','1|2|3|4')")
        conn.commit()
        conn.close()
        for state in (engine.PRESENT, engine.LOBBIES, engine.LOBBY_BY_PLAYER,
                      engine.GAMES, engine.GAME_BY_PLAYER, engine.OFFLINE_TIMERS):
            state.clear()
        self.app = Flask(__name__, template_folder=str(config.TEMPLATES))
        self.app.secret_key = 'qcm-checks-only'
        self.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.app.register_blueprint(auth.bp)
        self.app.register_blueprint(qcm_invites.bp)
        self.io = SocketIO(self.app, async_mode='gevent')
        self.recorded = []
        engine.register(self.io, self.root, on_answer=lambda *row: self.recorded.append(row))
        self.question = {'qid': 'p-000000000000-00000000', 'theme': 'physique',
                         'chapitre': 'Optique', 'question': 'Question 1',
                         'choices': [{'text': 'Oui', 'image': ''}, {'text': 'Non', 'image': ''}],
                         'answer': 0, 'time': 30, 'image': '', 'explication': 'Explication'}
        self.students = patch.object(engine, 'student_questions',
                                     side_effect=lambda theme: [self.question] if theme == 'physique' else [])
        self.students.start()
        self.addCleanup(self.students.stop)
        self.tasks = patch.object(self.io, 'start_background_task', return_value=Task())
        self.tasks_mock = self.tasks.start()
        self.addCleanup(self.tasks.stop)
        self.sleep = patch.object(self.io, 'sleep', return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.http = self.app.test_client()
        with self.app.test_request_context('/'):
            self.assertTrue(auth.bind_sr_user('Élève'))
            identity = dict(session)
        with self.http.session_transaction() as state:
            state.update(identity)
        self.client = self.io.test_client(self.app, flask_test_client=self.http)
        self.addCleanup(lambda: self.client.disconnect() if self.client.is_connected() else None)
        self.client.emit('qcm_join_page')
        self.client.get_received()

    def emit(self, event, payload=None):
        return self.client.emit(event, payload, callback=True)

    def lobby(self):
        result = self.emit('qcm_lobby_create', {'theme': 'physique'})
        self.assertTrue(result['ok'])
        return engine.LOBBIES[result['lobby']['id']]

    def game(self, count=2):
        lobby = self.lobby()
        game = engine.Game('runtime-game', lobby,
                           [dict(self.question, question='Question ' + str(n + 1)) for n in range(count)])
        lobby.game = game
        lobby.etat = 'partie'
        engine.GAMES[game.gid] = game
        engine.GAME_BY_PLAYER['Élève'] = game.gid
        self.client.emit('qcm_join_page')
        self.client.get_received()
        game.perms['Élève'] = {n: [1, 0] for n in range(count)}
        return game

    def open(self, game, index=0):
        game.qindex = index
        game.phase = 'ouverture'
        game.opened = True
        game.qstart = time.monotonic()
        game.open_end = time.time() + 20

    def test_invitation_renders_and_binds_stable_account_identity(self):
        token, _ = qcm_invites.create_qcm_invite('Élève')
        anonymous = self.app.test_client()
        response = anonymous.get('/qcm/join/' + token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(('action="/qcm/join/' + token + '"').encode(), response.data)
        response = anonymous.post('/qcm/join/' + token)
        self.assertEqual(response.status_code, 302)
        with anonymous.session_transaction() as state:
            self.assertEqual(state['sr_user'], 'Élève')
            self.assertTrue(state['sr_account_identity'])
        self.assertEqual(anonymous.post('/qcm/join/' + token).status_code, 410)

    def test_review_chapter_pairs_filter_without_type_error(self):
        other = dict(self.question, qid='p-111111111111-11111111', chapitre='Mécanique')
        with patch.object(engine, '_review_query', return_value=[
                {'qid': self.question['qid'], 'created_at': '2026-01-01'},
                {'qid': other['qid'], 'created_at': '2026-01-02'}]), \
                patch.object(engine, 'student_questions', return_value=[self.question, other]):
            questions = engine.review_questions('Élève', ['physique'], 5, [['physique', 'Optique']])
        self.assertEqual([q['chapitre'] for q in questions], ['Optique'])
        self.assertEqual(questions[0]['time'], 600)

    def test_review_socket_events_keep_selected_chapter(self):
        other = dict(self.question, qid='p-111111111111-11111111', chapitre='Mécanique')
        with patch.object(engine, '_review_query', return_value=[
                {'qid': self.question['qid'], 'created_at': '2026-01-01'},
                {'qid': other['qid'], 'created_at': '2026-01-02'}]), \
                patch.object(engine, 'student_questions',
                             side_effect=lambda theme: [self.question, other] if theme == 'physique' else []):
            lobby = self.lobby()
            self.assertTrue(self.emit('qcm_lobby_chapitre', {'chapitres': [['physique', 'Optique']]})['ok'])
            self.emit('qcm_lobby_review', {'review': True})
            self.assertTrue(lobby.review_mode)
            self.emit('qcm_lobby_ready', {'pret': True})
            self.assertEqual([q['chapitre'] for q in lobby.game.questions], ['Optique'])

    def test_answer_requires_current_game_and_question_without_changing_score(self):
        game = self.game()
        self.open(game, 1)
        for payload in ({'choice': 1}, {'choice': 1, 'gid': 'old-game', 'q': 2},
                        {'choice': 1, 'gid': game.gid, 'q': 1},
                        {'choice': 1.5, 'gid': game.gid, 'q': 2},
                        {'choice': True, 'gid': game.gid, 'q': 2}):
            self.assertFalse(self.emit('qcm_answer', payload)['ok'])
            self.assertFalse(game.answers)
        self.assertTrue(self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': 2})['ok'])
        self.assertEqual(game.answers['Élève'][0], 0)
        self.assertFalse(self.emit('qcm_answer', {'choice': 0, 'gid': game.gid, 'q': 2})['ok'])

    def test_correction_rejects_answers_and_resynchronises_same_question(self):
        game = self.game()
        snapshots = []

        class AnswerEvent:
            def set(event):
                pass

            def wait(event, timeout=None):
                ack = self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': game.qindex + 1})
                self.assertTrue(ack['ok'])

        def sleep(seconds):
            if seconds != engine.QCM_REVEAL_TIME_S:
                return
            self.assertFalse(game.opened)
            self.assertEqual(game.phase, 'correction')
            expected = game.qindex + 1
            self.assertEqual(game.current_payload('Élève')['q'], expected)
            self.assertFalse(self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': expected})['ok'])
            self.client.get_received()
            self.client.emit('qcm_join_page')
            messages = {m['name']: m['args'][0] for m in self.client.get_received()}
            self.assertEqual(messages['qcm_question']['q'], expected)
            self.assertEqual(messages['qcm_question']['phase'], 'correction')
            self.assertEqual(messages['qcm_question']['choice'], 1)
            self.assertEqual(messages['qcm_reveal']['q'], expected)
            self.assertEqual(len(messages['qcm_debut']['history']), expected)
            self.assertEqual(messages['qcm_debut']['history'][-1]['ma'], 1)
            snapshots.append(expected)

        with patch.object(engine, 'Event', AnswerEvent), patch.object(self.io, 'sleep', side_effect=sleep):
            game.run()
        self.assertEqual(snapshots, [1, 2])
        self.assertEqual(len(self.recorded), 2)
        self.assertTrue(all(row[5] for row in self.recorded))
        self.assertGreaterEqual(game.scores['Élève'], 1900)

    def test_open_resync_restores_deadline_and_only_own_choice(self):
        game = self.game()
        self.open(game)
        game.answers = {'Élève': (0, 3), 'Autre': (1, 4)}
        self.client.emit('qcm_join_page')
        messages = {m['name']: m['args'][0] for m in self.client.get_received()}
        payload = messages['qcm_question']
        self.assertEqual(payload['phase'], 'ouverture')
        self.assertEqual(payload['ends_at'], game.open_end)
        self.assertTrue(payload['deja_repondu'])
        self.assertEqual(payload['choice'], 1)
        self.assertNotIn('answers', payload)

    def test_review_still_reasks_a_mistake_once_after_correction(self):
        game = self.game(count=1)
        game.review = True
        game.delayed = True

        class AnswerEvent:
            def set(event):
                pass

            def wait(event, timeout=None):
                original = 1 if game.qindex == 0 else 0
                choice = game._perm_for('Élève', game.qindex).index(original)
                self.assertTrue(self.emit('qcm_answer', {'choice': choice, 'gid': game.gid,
                                                       'q': game.qindex + 1})['ok'])

        with patch.object(engine, 'Event', AnswerEvent):
            game.run()
        self.assertEqual(len(game.questions), 2)
        self.assertTrue(game.questions[1]['_reasked'])
        self.assertEqual(game.questions[1]['time'], 3600)
        self.assertEqual([row[5] for row in self.recorded], [False, True])
        self.assertEqual(len([m for m in self.client.get_received() if m['name'] == 'qcm_reveal']), 2)

    def test_engineio_rejects_foreign_origins_with_application_policy(self):
        tree = ast.parse((REPO / 'app.py').read_text())
        constructor = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == 'socketio' for t in n.targets))
        policy = ast.literal_eval(next(k.value for k in constructor.keywords if k.arg == 'cors_allowed_origins'))
        app = Flask('origin-check')
        SocketIO(app, async_mode='gevent', cors_allowed_origins=policy)
        client = app.test_client()
        url = '/socket.io/?EIO=4&transport=polling'
        self.assertEqual(client.get(url, headers={'Origin': 'http://localhost'}).status_code, 200)
        self.assertEqual(client.get(url, headers={'Origin': 'https://other.class.example'}).status_code, 400)

    def test_old_tab_cannot_mutate_the_replacement_connection(self):
        lobby = self.lobby()
        other = self.io.test_client(self.app, flask_test_client=self.http)
        self.addCleanup(lambda: other.disconnect() if other.is_connected() else None)
        other.emit('qcm_join_page')
        sid = engine.PRESENT['Élève']['sid']
        self.client.emit('qcm_lobby_leave', {})
        self.client.emit('qcm_leave_page')
        self.assertIn(lobby.lid, engine.LOBBIES)
        self.assertEqual(engine.PRESENT['Élève']['sid'], sid)
        other.emit('qcm_lobby_leave', {})
        self.assertNotIn(lobby.lid, engine.LOBBIES)

    def test_malformed_lobby_settings_return_structured_errors(self):
        self.lobby()
        for event in ('qcm_lobby_review', 'qcm_lobby_feedback', 'qcm_lobby_theme',
                      'qcm_lobby_chapitre', 'qcm_lobby_length', 'qcm_lobby_kick'):
            with self.subTest(event=event): self.assertFalse(self.emit(event, ['invalid'])['ok'])
        self.assertFalse(self.emit('qcm_lobby_length', {'nb': float('inf')})['ok'])

    def test_delayed_resync_preserves_private_history_without_revealing_answer(self):
        game = self.game()
        game.delayed = True
        game.phase = 'correction'
        game.answers = {'Élève': (1, 3), 'Autre': (0, 4)}
        game.completed = [{'index': 0, 'answers': dict(game.answers)}]
        game.reveal = {'detail': [], 'cloturee_tot': True}
        game.scores['Élève'] = 1000
        self.client.emit('qcm_join_page')
        messages = {m['name']: m['args'][0] for m in self.client.get_received()}
        self.assertEqual(messages['qcm_debut']['scores']['Élève'], 0)
        history = messages['qcm_debut']['history']
        self.assertEqual(history[0]['ma'], 0)
        self.assertIsNone(history[0]['correct'])
        self.assertEqual(history[0]['explication'], '')
        self.assertNotIn('qcm_reveal', messages)
        self.assertNotIn('Autre', str(history))

    def test_disabling_module_blocks_new_games_but_preserves_active_game(self):
        game = self.game()
        self.open(game)
        config.write_params({'module_qcm': 0})
        for event, data in (('qcm_lobby_create', {}), ('qcm_lobby_join', {'id': game.lobby.lid}),
                            ('qcm_lobby_ready', {'pret': True})):
            self.assertFalse(self.emit(event, data)['ok'])
        self.assertTrue(self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': 1})['ok'])
        self.client.get_received()
        self.client.emit('qcm_join_page')
        self.assertIn('qcm_question', [m['name'] for m in self.client.get_received()])

    def test_disabled_module_does_not_admit_a_player_without_active_game(self):
        config.write_params({'module_qcm': 0})
        engine.PRESENT.clear()
        self.assertFalse(self.client.emit('qcm_join_page', callback=True))
        self.assertFalse(engine.PRESENT)

    def test_disabling_during_start_delay_cancels_before_game_run(self):
        lobby = self.lobby()
        with patch.object(self.io, 'sleep', side_effect=lambda seconds: config.write_params({'module_qcm': 0})):
            self.emit('qcm_lobby_ready', {'pret': True})
        self.assertEqual(lobby.etat, 'attente')
        self.assertIsNone(lobby.game)
        self.assertFalse(engine.GAMES)
        self.assertFalse(engine.GAME_BY_PLAYER)
        self.tasks_mock.assert_not_called()

    def test_deleted_or_recreated_account_cannot_use_existing_socket(self):
        game = self.game()
        self.open(game)
        conn = database.db()
        conn.execute("DELETE FROM users WHERE prenom='Élève'")
        conn.execute("DELETE FROM events WHERE prenom='Élève'")
        conn.commit()
        conn.close()
        self.assertFalse(self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': 1})['ok'])
        self.assertFalse(game.answers)
        conn = database.db()
        conn.execute("INSERT INTO users(prenom,emoji_password) VALUES ('Élève','4|3|2|1')")
        conn.commit()
        conn.close()
        self.assertFalse(self.emit('qcm_answer', {'choice': 1, 'gid': game.gid, 'q': 1})['ok'])
        self.assertFalse(game.answers)

    def test_account_deletion_revokes_game_before_a_namesake_can_join(self):
        game = self.game()
        self.open(game)
        with self.http.session_transaction() as state:
            state['admin'] = True
        response = self.http.delete('/api/users/Élève')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.is_connected())
        self.assertNotIn('Élève', engine.PRESENT)
        self.assertNotIn('Élève', engine.LOBBY_BY_PLAYER)
        self.assertNotIn('Élève', engine.GAME_BY_PLAYER)
        self.assertNotIn('Élève', game.joueurs)
        self.assertNotIn('Élève', game.scores)
        conn = database.db()
        conn.execute("INSERT INTO users(prenom,emoji_password) VALUES ('Élève','4|3|2|1')")
        conn.commit()
        conn.close()
        fresh_http = self.app.test_client()
        with self.app.test_request_context('/'):
            self.assertTrue(auth.bind_sr_user('Élève'))
            identity = dict(session)
        with fresh_http.session_transaction() as state:
            state.update(identity)
        fresh_client = self.io.test_client(self.app, flask_test_client=fresh_http)
        self.addCleanup(lambda: fresh_client.disconnect() if fresh_client.is_connected() else None)
        fresh_client.emit('qcm_join_page')
        self.assertNotIn('qcm_debut', [m['name'] for m in fresh_client.get_received()])
        self.assertTrue(fresh_client.emit('qcm_lobby_create', {'theme': 'physique'}, callback=True)['ok'])
        game.run()
        self.assertFalse(self.recorded)

    def test_revoke_player_tolerates_engine_before_registration(self):
        with patch.object(engine, 'io', None):
            engine.revoke_player('Absent')


if __name__ == '__main__':
    unittest.main()
