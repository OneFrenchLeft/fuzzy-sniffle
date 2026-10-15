"""Verifications SR/bot isolees : venv/bin/python checks/sr_concurrency_checks.py."""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import asyncio

REPO = Path(__file__).resolve().parents[1]
SOURCES = tempfile.TemporaryDirectory(prefix='forgecards-sr-sources-')
for source in REPO.glob('*.py'):
    shutil.copyfile(source, Path(SOURCES.name) / source.name)
sys.path.insert(0, SOURCES.name)
os.environ['MADEC_ADMIN_PASSWORD'] = 'sr-checks-only'
os.environ['FLASK_SECRET_KEY'] = 'sr-checks-only'
import config
import db as database
import sr
import ankimie
import streak
import qcm_engine
import auth
from flask import Flask, session

TODAY = datetime(2026, 10, 14, 23, 56)
QID = 'c-000000000001-00000001'
OTHER_QID = 'c-000000000002-00000002'


class ConnectionProxy:
    """Signale la tentative concurrente sans modifier les donnees SQLite."""
    def __init__(self, connection, observed):
        self.connection = connection
        self.observed = observed

    def execute(self, sql, *args):
        self.observed(sql)
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


class SRConsistency(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='forgecards-sr-case-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config.PARAMS_PATH = root / 'params.json'
        database.DB_PATH = root / 'checks.db'
        database.UPLOADS = root / 'uploads'
        database.UPLOADS.mkdir()
        database._db_ready = False
        config.QCM_FILES.clear()
        config.QCM_FILES.update({theme: root / ('qcm_' + theme + '.json')
                                for theme in ('physique', 'chimie', 'maths')})
        qcm_engine._QUESTIONS_CACHE.clear()
        for theme, path in config.QCM_FILES.items():
            questions = [] if theme != 'chimie' else [
                {'qid': qid, 'question': 'Question ' + str(i), 'chapitre': 'Test',
                 'choix': ['A', 'B'], 'reponse': 1, 'temps': 30}
                for i, qid in enumerate((QID, OTHER_QID))]
            path.write_text(json.dumps(questions), encoding='utf-8')
        database.ensure_db()
        with database.db() as conn:
            conn.execute("INSERT INTO users(prenom,emoji_password) VALUES('Eleve','1|2|3|4')")
            for numero in (1, 2, 3):
                conn.execute('INSERT INTO forgecards(numero,fiche_file,correction_file) VALUES(?,?,?)',
                             (numero, str(numero) + '.pdf', str(numero) + '-c.pdf'))
        self.app = Flask('sr-checks')
        self.app.secret_key = 'sr-checks-only'
        self.app.config['TESTING'] = True
        self.app.register_blueprint(auth.bp)
        self.app.register_blueprint(sr.bp)
        self.app.register_blueprint(ankimie.bp)
        self.clock = ExitStack()
        self.addCleanup(self.clock.close)
        self.now = TODAY
        for module in (config, sr, ankimie, streak, database, auth):
            self.clock.enter_context(patch.object(module, 'now_paris', lambda: self.now))
        config.read_params()
        # L'identite est liee avant le test : la migration d'un vieux cookie
        # ne doit pas prendre un verrou d'ecriture pendant la synchronisation.
        with self.app.test_request_context('/'):
            self.assertTrue(auth.bind_sr_user('Eleve'))
            self.identity = session['sr_account_identity']

    def client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['sr_user'] = 'Eleve'
            session['sr_account_identity'] = self.identity
            session['admin'] = True
        return client

    def rows(self, sql, params=()):
        conn = database.db()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def review(self, qid=QID, result='good'):
        response = self.client().post('/api/ankimie/' + qid + '/review', json={'result': result})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def add_review_day(self, day, user='Eleve'):
        with database.db() as conn:
            conn.execute('INSERT INTO reviews(prenom,numero,result,created_at) VALUES(?,1,?,?)',
                         (user, 'good', day + 'T12:00:00'))

    def bot_namespace(self):
        # Extraire les parties pures evite d'initialiser le client Discord.
        parsed = ast.parse((REPO / 'bot.py').read_text(encoding='utf-8'))
        selected = [node for node in parsed.body
                    if ((isinstance(node, ast.FunctionDef) and node.name in ('compute_daily', 'compute_streak'))
                        or (isinstance(node, ast.ClassDef) and node.name == 'Duel'))]
        current = self
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return current.now
        namespace = {'datetime': FixedDateTime, 'timedelta': timedelta,
                     'TZ_PARIS': config.TZ_PARIS,
                     'today_paris': lambda: self.now.date().isoformat(),
                     'forgecards_paused': config.forgecards_paused,
                     'read_params': config.read_params}
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'bot.py', 'exec'), namespace)
        return namespace

    def concurrent_reviews(self, module, endpoint, table, prepare):
        prepare()
        entered = threading.Event()
        attempted = threading.Event()
        first_ident = []
        original_db = module.db
        original_apply = module.apply_review

        def observe(sql):
            if threading.get_ident() != first_ident[0] and (
                    sql.startswith('BEGIN IMMEDIATE') or sql.startswith('SELECT * FROM ' + table)):
                attempted.set()

        def connection():
            return ConnectionProxy(original_db(), observe)

        def apply(*args, **kwargs):
            if threading.get_ident() == first_ident[0]:
                entered.set()
                self.assertTrue(attempted.wait(5), 'La deuxieme requete doit atteindre SQLite.')
            return original_apply(*args, **kwargs)

        def first():
            first_ident.append(threading.get_ident())
            return self.client().post(endpoint, json={'result': 'good'})

        with patch.object(module, 'db', connection), patch.object(module, 'apply_review', apply):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_request = pool.submit(first)
                self.assertTrue(entered.wait(5))
                second_request = pool.submit(lambda: self.client().post(endpoint, json={'result': 'good'}))
                self.assertEqual(first_request.result(timeout=10).status_code, 200)
                self.assertEqual(second_request.result(timeout=10).status_code, 200)
        self.assertEqual(self.rows('SELECT repetitions FROM ' + table + ' WHERE prenom=?', ('Eleve',))[0]['repetitions'], 2)

    def test_sr_reviews_serialize_state_and_history(self):
        def prepare():
            with database.db() as conn:
                sr.ensure_sr_row(conn, 'Eleve', 1)
        self.concurrent_reviews(sr, '/api/sr/1/review', 'sr_state_user', prepare)
        self.assertEqual(len(self.rows('SELECT id FROM reviews')), 2)
        self.assertEqual(sum(row['was_new'] for row in self.rows('SELECT was_new FROM reviews')), 1)

    def test_ankimie_reviews_serialize_state_and_history(self):
        def prepare():
            with database.db() as conn:
                ankimie._ensure_states(conn, 'Eleve', self.now.date().isoformat())
        self.concurrent_reviews(ankimie, '/api/ankimie/' + QID + '/review', 'qcm_sr_state', prepare)
        self.assertEqual(len(self.rows('SELECT id FROM qcm_sr_reviews')), 2)
        self.assertEqual(sum(row['was_new'] for row in self.rows('SELECT was_new FROM qcm_sr_reviews')), 1)

    def test_undo_targets_expected_review_and_only_once(self):
        first = self.review()
        second = self.review(OTHER_QID)
        client = self.client()
        self.assertEqual(client.post('/api/ankimie/undo', json={}).status_code, 400)
        self.assertEqual(client.post('/api/ankimie/undo', json={'review_id': first['review_id']}).status_code, 409)
        self.assertEqual(len(self.rows('SELECT id FROM qcm_sr_reviews')), 2)
        response = client.post('/api/ankimie/undo', json={'review_id': second['review_id']})
        self.assertEqual(response.get_json(), {'ok': True, 'qid': OTHER_QID, 'review_id': second['review_id']})
        self.assertEqual(client.post('/api/ankimie/undo', json={'review_id': second['review_id']}).status_code, 404)
        self.assertEqual(self.rows('SELECT repetitions FROM qcm_sr_state WHERE qid=?', (QID,))[0]['repetitions'], 1)

    def test_concurrent_undos_do_not_both_cancel_the_same_review(self):
        latest = self.review()
        entered = threading.Event()
        attempted = threading.Event()
        first_ident = []
        original_db = ankimie.db

        def observe(sql):
            if threading.get_ident() == first_ident[0] and sql.startswith('SELECT * FROM ankimie_undo'):
                entered.set()
                self.assertTrue(attempted.wait(5))
            elif threading.get_ident() != first_ident[0] and (
                    sql.startswith('BEGIN IMMEDIATE') or sql.startswith('SELECT * FROM ankimie_undo')):
                attempted.set()

        def undo(first=False):
            if first:
                first_ident.append(threading.get_ident())
            return self.client().post('/api/ankimie/undo', json={'review_id': latest['review_id']})

        with patch.object(ankimie, 'db', lambda: ConnectionProxy(original_db(), observe)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(undo, True)
                self.assertTrue(entered.wait(5))
                second = pool.submit(undo)
                self.assertEqual(first.result(timeout=10).status_code, 200)
                self.assertEqual(second.result(timeout=10).status_code, 404)
        self.assertEqual(self.rows('SELECT id FROM qcm_sr_reviews'), [])
        self.assertEqual(self.rows('SELECT repetitions FROM qcm_sr_state WHERE qid=?', (QID,))[0]['repetitions'], 0)

    def test_concurrent_initial_deck_loads_keep_one_state_per_question(self):
        barrier = threading.Barrier(2)

        class FirstLoadConnection(ConnectionProxy):
            def executemany(self, sql, args):
                if 'INTO qcm_sr_state' in sql:
                    barrier.wait(timeout=5)
                return self.connection.executemany(sql, args)

        def load():
            connection = FirstLoadConnection(database.db(), lambda _: None)
            try:
                ankimie._ensure_states(connection, 'Eleve', self.now.date().isoformat())
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in (pool.submit(load), pool.submit(load)):
                future.result(timeout=10)
        self.assertEqual(len(self.rows('SELECT qid FROM qcm_sr_state')), 2)

    def test_reset_student_and_question_invalidate_undo(self):
        for endpoint in ('/api/admin/ankimie/reset/Eleve', '/api/admin/ankimie/reset-question/' + QID):
            with self.subTest(endpoint=endpoint):
                self.review()
                latest = self.review()
                self.assertEqual(self.client().post(endpoint).status_code, 200)
                self.assertEqual(self.client().post('/api/ankimie/undo', json={'review_id': latest['review_id']}).status_code, 404)
                state = self.rows('SELECT repetitions,stability FROM qcm_sr_state WHERE qid=?', (QID,))[0]
                self.assertEqual(state, {'repetitions': 0, 'stability': None})

    def test_guard_processes_next_student_after_joker_award(self):
        with database.db() as conn:
            conn.execute("INSERT INTO users(prenom) VALUES('Second')")
            conn.execute("INSERT INTO user_jokers(prenom,count) VALUES('Second',1)")
            conn.execute("INSERT INTO events(prenom,type,created_at) VALUES('Second','login',?)", (self.now.isoformat(),))
        for offset in range(4):
            self.add_review_day((self.now.date() - timedelta(days=offset)).isoformat())
        result = streak.close_all_missed_days(config.read_params())
        self.assertEqual(result['Eleve']['events'], ['joker_awarded'])
        self.assertEqual(result['Second']['events'], ['joker_spent'])
        self.assertEqual(result['Second']['jokers'], 0)
        self.assertEqual(streak.close_all_missed_days(config.read_params())['Second']['events'], [])

    def test_account_metadata_does_not_start_a_students_streak_obligations(self):
        self.assertEqual(len(self.rows("SELECT id FROM events WHERE type='account_identity'")), 1)
        with database.db() as conn:
            conn.execute("INSERT INTO events(prenom,type,created_at) VALUES('Eleve','account_deleted',?)",
                         ((self.now - timedelta(days=2)).isoformat(),))
            conn.execute("INSERT INTO user_jokers(prenom,count) VALUES('Eleve',1)")
        conn = database.db()
        try:
            self.assertEqual(streak.close_missed_days(conn, 'Eleve', config.read_params(), include_today=True), {})
            self.assertEqual(streak.activity_days(conn, 'Eleve'), set())
            self.assertEqual(streak.compute_streak(conn, 'Eleve'), 0)
        finally:
            conn.close()
        result = streak.close_all_missed_days()['Eleve']
        self.assertEqual((result['streak'], result['jokers'], result['events'], result['closed']), (0, 1, [], {}))
        self.assertEqual(self.rows('SELECT * FROM sr_daily_streak'), [])
        self.assertEqual(self.rows('SELECT * FROM joker_ledger'), [])

    def test_pause_preserves_series_without_free_days_or_rewards(self):
        self.add_review_day('2026-10-11')
        self.add_review_day('2026-10-12')
        config.write_params({'module_forgecards': 0})
        before = self.rows('SELECT * FROM sr_daily_streak')
        result = streak.close_all_missed_days(config.read_params())
        # Le 13 est actif mais manque : la pause du 14 ne doit pas le reparer.
        self.assertEqual(result['Eleve']['streak'], 0)
        self.assertEqual(result['Eleve']['events'], [])
        self.assertEqual(self.rows('SELECT * FROM sr_daily_streak'), before)
        self.assertEqual(self.rows('SELECT * FROM joker_ledger'), [])
        self.assertEqual(self.client().get('/api/sr/today').status_code, 404)

    def test_pause_dates_bridge_series_record_and_milestones_on_resume(self):
        self.add_review_day('2026-10-11')
        self.add_review_day('2026-10-12')
        self.add_review_day('2026-10-13')
        config.write_params({'module_forgecards': 0})
        self.now = datetime(2026, 10, 17, 12)
        params = config.read_params()
        conn = database.db()
        try:
            self.assertEqual(streak.compute_streak(conn, 'Eleve'), 3)
            self.assertEqual(streak.reconcile_streak(conn, 'Eleve'), 3)
            self.assertEqual(streak.compute_record(streak.activity_days(conn, 'Eleve')), 3)
            self.assertEqual(self.bot_namespace()['compute_streak'](conn, 'Eleve', params), 3)
        finally:
            conn.close()
        self.assertEqual(self.rows('SELECT * FROM joker_ledger'), [])
        config.write_params({'module_forgecards': 1})
        self.add_review_day('2026-10-17')
        conn = database.db()
        try:
            self.assertEqual(streak.reconcile_streak(conn, 'Eleve'), 4)
            self.assertEqual(streak.compute_record(streak.activity_days(conn, 'Eleve')), 4)
            self.assertEqual(self.bot_namespace()['compute_streak'](conn, 'Eleve'), 4)
        finally:
            conn.close()
        self.assertEqual(self.rows('SELECT count FROM user_jokers')[0]['count'], 1)
        self.assertEqual(len(self.rows('SELECT id FROM joker_ledger')), 1)
        closed = streak.close_all_missed_days(config.read_params())['Eleve']['closed']
        self.assertFalse({'2026-10-14', '2026-10-15', '2026-10-16'} & set(closed))

    def test_disabling_after_todays_review_keeps_the_earned_day(self):
        for day in ('2026-10-12', '2026-10-13', '2026-10-14'):
            self.add_review_day(day)
        config.write_params({'module_forgecards': 0})
        for day in (14, 15):
            self.now = datetime(2026, 10, day, 23, 56)
            with self.subTest(day=day):
                conn = database.db()
                try:
                    self.assertEqual(streak.compute_streak(conn, 'Eleve'), 3)
                    self.assertEqual(streak.compute_record(streak.activity_days(conn, 'Eleve')), 3)
                    self.assertEqual(self.bot_namespace()['compute_streak'](conn, 'Eleve'), 3)
                finally:
                    conn.close()
                self.assertEqual(streak.close_all_missed_days()['Eleve']['streak'], 3)
                self.assertEqual(self.rows('SELECT * FROM sr_daily_streak'), [])
                self.assertEqual(self.rows('SELECT * FROM joker_ledger'), [])

    def test_legacy_disabled_today_does_not_pause_the_past(self):
        params = {**config.DEFAULT_PARAMS, 'module_forgecards': 0}
        self.assertTrue(config.forgecards_paused('2026-10-14', params))
        self.assertFalse(config.forgecards_paused('2026-10-13', params))

    def test_bot_total_includes_done_and_remaining(self):
        with database.db() as conn:
            for numero, day in ((1, '2026-10-15'), (2, '2026-10-14'), (3, '2026-10-14')):
                conn.execute('INSERT INTO sr_state_user(prenom,numero,repetitions,next_review) VALUES(?,?,1,?)', ('Eleve', numero, day))
        self.add_review_day('2026-10-14')
        conn = database.db()
        try:
            daily = self.bot_namespace()['compute_daily'](conn, 'Eleve', config.read_params())
            self.assertEqual((daily['done'], daily['total'], daily['remaining']), (1, 3, 2))
            daily = self.bot_namespace()['compute_daily'](conn, 'Eleve', {**config.read_params(), 'module_forgecards': 0})
            self.assertEqual(daily['remaining'], 0)
        finally:
            conn.close()

    def test_finished_duel_cleans_players_when_public_send_fails(self):
        namespace = self.bot_namespace()
        class Player:
            def __init__(self, identifier):
                self.id = identifier
                self.display_name = str(identifier)
            async def send(self, *args, **kwargs):
                pass
        class BrokenChannel:
            async def send(self, *args, **kwargs):
                raise RuntimeError('Missing Permissions')
        namespace.update({'_next_duel_id': [1], 'DUELS': {}, 'DUEL_BY_USER': {},
                          'client': SimpleNamespace(get_channel=lambda _: BrokenChannel()),
                          'fmt_grade': str})
        duel = namespace['Duel'](Player(1), Player(2), 123)
        duel.locked_grade = {1: 12, 2: 14}
        duel.state = 'grading'
        namespace['DUELS'][duel.id] = duel
        namespace['DUEL_BY_USER'].update({1: duel.id, 2: duel.id})
        asyncio.run(duel._resolve_grades())
        self.assertEqual(namespace['DUELS'], {})
        self.assertEqual(namespace['DUEL_BY_USER'], {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
