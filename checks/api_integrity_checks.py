"""Vérifications isolées : venv/bin/python checks/api_integrity_checks.py.

Les sources sont copiées sous /tmp avant leur import : aucun fichier de données,
PDF ou .env du dépôt n'est lu ou modifié, et app.py n'est jamais importé.
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SOURCES = tempfile.TemporaryDirectory(prefix='forgecards-api-sources-')
for source in REPO.glob('*.py'):
    shutil.copy2(source, Path(SOURCES.name) / source.name)
sys.path.insert(0, SOURCES.name)
os.environ['MADEC_ADMIN_PASSWORD'] = 'api-checks-only'
os.environ['FLASK_SECRET_KEY'] = 'api-checks-only'
os.environ['MADEC_COOKIE_SECURE'] = '0'

import config
import db as database
import auth
import cards
import helpers
import sr
from flask import Flask
from werkzeug.datastructures import FileStorage


class ApiIntegrity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='forgecards-api-case-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        config.DATA = self.root
        config.DB_PATH = database.DB_PATH = self.root / 'checks.db'
        config.PARAMS_PATH = self.root / 'params.json'
        config.UPLOADS = database.UPLOADS = cards.UPLOADS = helpers.UPLOADS = self.uploads
        config.QCM_FILES.clear()
        for subject in config.SUBJECTS:
            path = self.root / ('qcm_' + subject + '.json')
            path.write_text('[]', encoding='utf-8')
            config.QCM_FILES[subject] = path
        database._db_ready = False
        database.ensure_db()
        auth._login_attempts.clear()
        self.app = Flask('api_integrity')
        self.app.secret_key = 'api-checks-only'
        self.app.config['TESTING'] = True
        self.app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=48)
        for module in (auth, cards, sr):
            self.app.register_blueprint(module.bp)
        self.admin = self.client(admin=True)
        self.add_user('Élève')
        self.add_card(1)

    def client(self, admin=False, legacy=False):
        client = self.app.test_client()
        if admin or legacy:
            with client.session_transaction() as session:
                if admin:
                    session['admin'] = True
                if legacy:
                    session['sr_user'] = 'Élève'
        return client

    def add_user(self, name):
        conn = database.db()
        conn.execute('INSERT INTO users(prenom,emoji_password) VALUES(?,?)', (name, '1|2|3|4'))
        conn.commit()
        conn.close()

    def add_card(self, numero, hors_serie=False, code=None):
        code = code or str(numero)
        conn = database.db()
        conn.execute(
            'INSERT INTO forgecards(numero,code,fiche_file,correction_file,bareme_file,hors_serie,titre) '
            'VALUES(?,?,?,?,?,?,?)',
            (numero, code, code + '.pdf', code + '-c.pdf', code + '-b.pdf', int(hors_serie), 'Ancien titre'))
        conn.commit()
        conn.close()
        for filename in (code + '.pdf', code + '-c.pdf', code + '-b.pdf'):
            (self.uploads / filename).write_bytes(('%PDF-original:' + filename).encode())

    def test_legacy_migration_reserves_codes_and_preserves_absent_correction(self):
        with database.db() as conn:
            conn.execute("INSERT INTO forgecards(numero,hors_serie,code,fiche_file,correction_file) VALUES(9001,1,'','legacy.pdf','')")
            conn.execute("INSERT INTO forgecards(numero,hors_serie,code,fiche_file,correction_file) VALUES(9002,1,'h1','h1.pdf','')")
        (self.uploads / 'legacy.pdf').write_bytes(b'%PDF-legacy')
        (self.uploads / 'h1.pdf').write_bytes(b'%PDF-preserved')
        database.init_db()
        with database.db() as conn:
            migrated = conn.execute('SELECT code,fiche_file,correction_file FROM forgecards WHERE numero=9001').fetchone()
        self.assertEqual(dict(migrated), {'code': 'h2', 'fiche_file': 'h2.pdf', 'correction_file': ''})
        self.assertEqual((self.uploads / 'h1.pdf').read_bytes(), b'%PDF-preserved')
        self.assertEqual((self.uploads / 'h2.pdf').read_bytes(), b'%PDF-legacy')

    def test_legacy_migration_collision_keeps_database_and_original_files(self):
        with database.db() as conn:
            conn.execute("INSERT INTO forgecards(numero,code,fiche_file,correction_file) VALUES(7,'','old.pdf','')")
        (self.uploads / 'old.pdf').write_bytes(b'%PDF-old')
        (self.uploads / '7.pdf').write_bytes(b'%PDF-other')
        with self.assertRaises(FileExistsError):
            database.init_db()
        self.assertEqual((self.uploads / 'old.pdf').read_bytes(), b'%PDF-old')
        self.assertEqual((self.uploads / '7.pdf').read_bytes(), b'%PDF-other')
        with database.db() as conn:
            self.assertEqual(conn.execute('SELECT code FROM forgecards WHERE numero=7').fetchone()['code'], '')

    def login(self, client):
        response = client.post('/api/sr/login', json={'prenom': 'Élève', 'password': ['1', '2', '3', '4']})
        self.assertEqual(response.status_code, 200)

    def upload_payload(self, **values):
        return {
            'numero': '1', 'titre': 'Nouveau titre',
            'fiche_pdf': (io.BytesIO(b'%PDF-new-fiche'), 'fiche.pdf'),
            'correction_pdf': (io.BytesIO(b'%PDF-new-correction'), 'correction.pdf'),
            **values,
        }

    def snapshot(self):
        conn = database.db()
        row = dict(conn.execute('SELECT * FROM forgecards WHERE numero=1').fetchone())
        conn.close()
        files = {path.name: path.read_bytes() for path in self.uploads.iterdir() if path.is_file()}
        return row, files

    def seed_review(self, numero=1, was_new=0):
        conn = database.db()
        conn.execute(
            "INSERT INTO reviews(numero,prenom,result,was_new,created_at) VALUES(?,'Élève','good',?,?)",
            (numero, was_new, config.now_paris().isoformat()))
        conn.commit()
        conn.close()

    def state(self, numero, repetitions=2, when=None):
        conn = database.db()
        conn.execute(
            'INSERT OR REPLACE INTO sr_state_user(prenom,numero,next_review,repetitions,stability,difficulty,state) '
            "VALUES('Élève',?,?,?,2,5,'review')",
            (numero, when or config.now_paris().date().isoformat(), repetitions))
        conn.commit()
        conn.close()

    def stats(self):
        response = self.admin.get('/api/users/stats')
        self.assertEqual(response.status_code, 200)
        return next(row for row in response.get_json() if row['prenom'] == 'Élève')

    def gated_connections(self, original, owner, contender, select_sql):
        # La seconde requête atteint soit le verrou (correctif), soit la
        # lecture encore non protégée (ancienne implémentation).
        class Connection:
            def __init__(self):
                self.conn = original()
            def __getattr__(self, key):
                return getattr(self.conn, key)
            def execute(self, sql, *args):
                competing = owner and threading.get_ident() != owner[0]
                if competing and sql == 'BEGIN IMMEDIATE':
                    contender.set()
                result = self.conn.execute(sql, *args)
                if competing and sql.startswith(select_sql):
                    contender.set()
                return result
        return Connection

    def test_auth_rejects_invalid_json_types_and_non_keypad_passwords(self):
        invalid = [('/api/login', ['x']), ('/api/login', {'password': {}}),
                   ('/api/sr/login', ['x']), ('/api/sr/login', {'prenom': 3}),
                   ('/api/users', ['x']), ('/api/users', {'prenom': 3}),
                   ('/api/sr/login', {'prenom': 'Élève', 'password': ['😀', '2', '3', '4']})]
        for route, payload in invalid:
            with self.subTest(route=route, payload=payload):
                response = self.admin.post(route, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()['ok'])

    def test_legacy_session_is_upgraded_without_logout(self):
        client = self.client(legacy=True)
        response = client.get('/api/sr/whoami')
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            self.assertTrue(session['sr_account_identity'])

    def test_deleted_account_refuses_legacy_and_current_cookies_even_after_recreation(self):
        current = self.client()
        self.login(current)
        with current.session_transaction() as session:
            old_cookie = dict(session)
        self.assertEqual(self.admin.delete('/api/users/Élève').status_code, 200)
        self.assertEqual(current.get('/api/sr/whoami').status_code, 401)
        response = self.admin.post('/api/users', json={'prenom': 'Élève'})
        self.assertEqual(response.status_code, 200)
        replayed = self.client()
        with replayed.session_transaction() as session:
            session.update(old_cookie)
        for client in (replayed, self.client(legacy=True)):
            self.assertEqual(client.get('/api/sr/whoami').status_code, 401)
            self.assertEqual(client.post('/api/sr/1/review', json={'result': 'good'}).status_code, 401)
        fresh = self.client()
        self.assertEqual(fresh.post('/api/sr/login', json={
            'prenom': 'Élève', 'password': response.get_json()['password']}).status_code, 200)
        self.assertEqual(fresh.get('/api/sr/whoami').status_code, 200)

    def test_repeated_deletion_keeps_old_sessions_invalid_after_recreation(self):
        legacy = self.client(legacy=True)
        current = self.client()
        self.login(current)
        for _ in range(2):
            self.assertEqual(self.admin.delete('/api/users/Élève').status_code, 200)
        self.assertEqual(self.admin.post('/api/users', json={'prenom': 'Élève'}).status_code, 200)
        for client in (legacy, current):
            self.assertEqual(client.get('/api/sr/whoami').status_code, 401)
            self.assertEqual(client.post('/api/sr/1/review', json={'result': 'good'}).status_code, 401)

    def test_regenerating_password_keeps_existing_session_identity(self):
        client = self.client()
        self.login(client)
        with client.session_transaction() as session:
            identity = session['sr_account_identity']
        self.assertEqual(self.admin.post('/api/users/Élève/regen-password').status_code, 200)
        self.assertEqual(client.get('/api/sr/whoami').status_code, 200)
        with client.session_transaction() as session:
            self.assertEqual(session['sr_account_identity'], identity)

    def test_account_identity_survives_event_maintenance(self):
        client = self.client()
        self.login(client)
        conn = database.db()
        conn.execute("UPDATE events SET created_at='2000-01-01' WHERE type='account_identity'")
        conn.execute("INSERT INTO events(prenom,type,created_at) VALUES('Élève','login','2000-01-01')")
        conn.commit()
        conn.close()

        database._db_ready = False
        database.ensure_db()
        self.assertEqual(client.get('/api/sr/whoami').status_code, 200)
        conn = database.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type='account_identity'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type='login' AND created_at='2000-01-01'").fetchone()[0], 0)
        conn.close()

    def test_account_identity_is_not_reported_as_student_activity(self):
        self.assertEqual(self.admin.post('/api/users', json={'prenom': 'Nouveau'}).status_code, 200)
        response = self.admin.get('/api/admin/suivi/Nouveau')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['assiduite']['last_activity'])
        overview = self.admin.get('/api/admin/stats/overview').get_json()
        row = next(row for row in overview['assiduite'] if row['prenom'] == 'Nouveau')
        self.assertIsNone(row['last'])
        export = self.admin.get('/api/admin/stats/export/activite-quotidienne')
        self.assertEqual(export.status_code, 200)
        self.assertNotIn('Nouveau', export.get_data(as_text=True))
    def test_first_legacy_requests_share_one_identity(self):
        clients = [self.client(legacy=True), self.client(legacy=True)]
        start = threading.Barrier(2)
        def check(client):
            start.wait(timeout=5)
            response = client.get('/api/sr/whoami')
            with client.session_transaction() as session:
                return response.status_code, session.get('sr_account_identity')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(check, clients))
        self.assertEqual([code for code, _ in results], [200, 200])
        self.assertTrue(results[0][1])
        self.assertEqual(results[0][1], results[1][1])

    def test_user_deletion_cleans_ankimie_and_keeps_archives(self):
        conn = database.db()
        conn.execute("INSERT INTO qcm_sr_state(prenom,qid,repetitions) VALUES('Élève','q',5)")
        conn.execute("INSERT INTO qcm_sr_reviews(prenom,qid,result,grade) VALUES('Élève','q','good',3)")
        conn.execute("INSERT INTO ankimie_undo(prenom,review_id,qid,created_at) VALUES('Élève',1,'q','2026-10-14')")
        conn.execute("INSERT INTO reviews_archive(numero,prenom,result) VALUES(1,'Élève','good')")
        conn.execute("INSERT INTO joker_ledger(prenom,day,delta,reason,balance_after) VALUES('Élève','2026-10-14',1,'admin_grant',1)")
        conn.commit()
        conn.close()

        self.assertEqual(self.admin.delete('/api/users/Élève').status_code, 200)
        self.assertEqual(self.admin.post('/api/users', json={'prenom': 'Élève'}).status_code, 200)
        conn = database.db()
        for table in ('qcm_sr_state', 'qcm_sr_reviews', 'ankimie_undo'):
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        for table in ('reviews_archive', 'joker_ledger'):
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 1)
        conn.close()

    def test_failed_deletion_marker_rolls_back_the_account_deletion(self):
        conn = database.db()
        conn.execute(
            "CREATE TRIGGER refuse_deletion_marker BEFORE INSERT ON events "
            "WHEN NEW.type='account_deleted' BEGIN SELECT RAISE(ABORT,'échec simulé'); END")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.admin.delete('/api/users/Élève')
        conn = database.db()
        self.assertIsNotNone(conn.execute("SELECT 1 FROM users WHERE prenom='Élève'").fetchone())
        conn.close()

    def test_socket_cleanup_failure_does_not_hide_successful_database_deletion(self):
        import qcm_engine
        with patch.object(qcm_engine, 'revoke_player', side_effect=RuntimeError('échec simulé'), create=True):
            with self.assertLogs(self.app.logger, level='ERROR'):
                response = self.admin.delete('/api/users/Élève')
        self.assertEqual(response.status_code, 200)
        conn = database.db()
        self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE prenom='Élève'").fetchone())
        conn.close()

    def test_concurrent_user_creation_only_returns_stored_password(self):
        owner, ready, contender = [], threading.Event(), threading.Event()
        def password():
            if not owner:
                owner.append(threading.get_ident())
                ready.set()
                self.assertTrue(contender.wait(timeout=5))
            return ['1', '1', '1', '1']
        def create():
            return self.client(admin=True).post('/api/users', json={'prenom': 'Concurrent'})
        connection = self.gated_connections(database.db, owner, contender, 'SELECT emoji_password FROM users')
        with patch.object(auth, 'db', connection), patch.object(auth, 'gen_emoji_password', password):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(create)
                self.assertTrue(ready.wait(timeout=5))
                second = pool.submit(create)
                responses = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual([response.status_code for response in responses], [200, 200])
        passwords = [response.get_json()['password'] for response in responses]
        self.assertEqual(sum(password is not None for password in passwords), 1)
        conn = database.db()
        stored = conn.execute("SELECT emoji_password FROM users WHERE prenom='Concurrent'").fetchone()[0]
        conn.close()
        self.assertEqual(stored, '|'.join(next(password for password in passwords if password)))

    def test_failed_upload_save_preserves_existing_pdfs_and_metadata(self):
        before = self.snapshot()
        original = FileStorage.save
        def fail(storage, destination, *args, **kwargs):
            if storage.filename == 'correction.pdf':
                raise OSError('échec de sauvegarde simulé')
            return original(storage, destination, *args, **kwargs)
        with patch.object(FileStorage, 'save', fail):
            with self.assertRaises(OSError):
                self.admin.post('/api/forgecards/upload', data=self.upload_payload())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(path.is_dir() for path in self.uploads.iterdir()))

    def test_failed_second_replacement_restores_all_original_pdfs(self):
        before = self.snapshot()
        original = os.replace
        failed = []
        def fail(source, destination):
            if str(destination).endswith('1-c.pdf') and not failed:
                failed.append(True)
                raise OSError('échec du remplacement simulé')
            return original(source, destination)
        with patch.object(cards.os, 'replace', fail):
            with self.assertRaises(OSError):
                self.admin.post('/api/forgecards/upload', data=self.upload_payload())
        self.assertEqual(self.snapshot(), before)

    def test_failed_sql_commit_restores_pdfs_after_upload_edit_and_delete(self):
        before = self.snapshot()
        original = database.db
        class FailedCommit:
            def __init__(self):
                self.conn = original()
            def __getattr__(self, key):
                return getattr(self.conn, key)
            def commit(self):
                raise sqlite3.OperationalError('échec commit simulé')
        operations = [lambda: self.admin.post('/api/forgecards/upload', data=self.upload_payload()),
                      lambda: self.admin.patch('/api/forgecards/1', data={'hors_serie': '1', 'titre': 'Renommée'}),
                      lambda: self.admin.delete('/api/forgecards/1')]
        for operation in operations:
            with self.subTest(operation=operation):
                with patch.object(cards, 'db', FailedCommit):
                    with self.assertRaises(sqlite3.OperationalError):
                        operation()
                self.assertEqual(self.snapshot(), before)

    def test_concurrent_hs_uploads_allocate_distinct_codes_and_keep_both_pdfs(self):
        owner, ready, contender = [], threading.Event(), threading.Event()
        original_names = cards.filenames_for
        def names(code):
            if not owner:
                owner.append(threading.get_ident())
                ready.set()
                self.assertTrue(contender.wait(timeout=5))
            return original_names(code)
        def upload(number):
            return self.client(admin=True).post('/api/forgecards/upload', data=self.upload_payload(
                numero='', hors_serie='1', titre=str(number),
                fiche_pdf=(io.BytesIO(('%PDF-' + str(number)).encode()), 'fiche.pdf')))
        connection = self.gated_connections(database.db, owner, contender, 'SELECT MAX(numero)')
        with patch.object(cards, 'db', connection), patch.object(cards, 'filenames_for', names):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(upload, 1)
                self.assertTrue(ready.wait(timeout=5))
                second = pool.submit(upload, 2)
                responses = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual({response.get_json()['code'] for response in responses}, {'h1', 'h2'})
        self.assertEqual({(self.uploads / (code + '.pdf')).read_bytes() for code in ('h1', 'h2')}, {b'%PDF-1', b'%PDF-2'})

    def test_upload_keeps_existing_bareme_without_new_file(self):
        before = (self.uploads / '1-b.pdf').read_bytes()
        self.assertEqual(self.admin.post('/api/forgecards/upload', data=self.upload_payload()).status_code, 200)
        self.assertEqual((self.uploads / '1-b.pdf').read_bytes(), before)
        self.assertEqual(self.snapshot()[0]['bareme_file'], '1-b.pdf')

    def test_successful_conversion_moves_all_pdfs_and_preserves_absent_correction(self):
        self.assertEqual(self.admin.patch('/api/forgecards/1', data={'hors_serie': '1', 'remove_correction': '1'}).status_code, 200)
        row = self.snapshot()[0]
        self.assertEqual((row['code'], row['correction_file']), ('h1', ''))
        self.assertTrue((self.uploads / 'h1.pdf').exists())
        self.assertTrue((self.uploads / 'h1-b.pdf').exists())
        self.assertFalse((self.uploads / '1.pdf').exists())
        self.assertFalse((self.uploads / 'h1-c.pdf').exists())

    def test_hs_deletion_and_reset_never_validate_streak(self):
        today = config.now_paris().date().isoformat()
        for operation in ('reset', 'delete'):
            with self.subTest(operation=operation):
                numero = 9001 if operation == 'reset' else 9002
                self.add_card(numero, hors_serie=True, code='h' + str(numero))
                self.seed_review(numero)
                response = (self.admin.post('/api/forgecards/' + str(numero) + '/reset-stats') if operation == 'reset'
                            else self.admin.delete('/api/forgecards/' + str(numero)))
                self.assertEqual(response.status_code, 200)
                conn = database.db()
                self.assertIsNone(conn.execute('SELECT validated FROM sr_daily_streak WHERE prenom=? AND day=?', ('Élève', today)).fetchone())
                conn.close()

    def test_normal_deletion_and_reset_preserve_legitimate_day(self):
        today = config.now_paris().date().isoformat()
        for operation in ('reset', 'delete'):
            with self.subTest(operation=operation):
                self.seed_review()
                conn = database.db()
                conn.execute('INSERT OR REPLACE INTO sr_daily_streak(prenom,day,validated) VALUES(?,?,0)', ('Élève', today))
                conn.commit()
                conn.close()
                response = (self.admin.post('/api/forgecards/1/reset-stats') if operation == 'reset'
                            else self.admin.delete('/api/forgecards/1'))
                self.assertEqual(response.status_code, 200)
                conn = database.db()
                self.assertEqual(conn.execute('SELECT validated FROM sr_daily_streak WHERE prenom=? AND day=?', ('Élève', today)).fetchone()[0], 1)
                conn.close()

    def test_admin_remaining_count_matches_sr_without_subtracting_done_twice(self):
        for number in (2, 3):
            self.add_card(number)
        tomorrow = (config.now_paris().date() + timedelta(days=1)).isoformat()
        self.state(1, when=tomorrow)
        self.state(2)
        self.state(3)
        self.seed_review(1)
        stats = self.stats()
        self.assertEqual((stats['due_today'], stats['done_today'], stats['todo_today']), (2, 1, 2))
        response = self.client(legacy=True).get('/api/sr/today')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(stats['todo_today'], len(response.get_json()['cards']))

    def test_admin_counts_new_cards_without_states_and_separates_quotas(self):
        config.write_params({'daily_new_limit': 1, 'daily_review_limit': 2})
        for number in (2, 3, 4):
            self.add_card(number)
        self.state(1)
        self.state(2)
        self.seed_review(1, was_new=0)
        stats = self.stats()
        self.assertEqual((stats['due_today'], stats['done_today'], stats['todo_today']), (4, 1, 2))

    def test_admin_counts_exclude_bonus_inactive_drop_and_disabled_module(self):
        self.add_card(9001, hors_serie=True, code='h1')
        self.state(9001)
        self.seed_review(9001)
        self.add_card(40)
        self.state(40)
        self.assertEqual((self.stats()['due_today'], self.stats()['done_today']), (1, 0))
        config.write_params({'max_active_num': 37})
        self.add_card(37)
        self.assertEqual(self.stats()['due_today'], 1)
        config.write_params({'module_forgecards': 0})
        stats = self.stats()
        self.assertEqual((stats['due_today'], stats['todo_today']), (0, 0))


    def test_public_list_shows_only_latest_grade_for_current_student(self):
        self.add_card(2)
        self.add_card(9001, hors_serie=True, code='h1')
        student = self.client()
        self.login(student)
        for numero, result in ((1, 'good'), (1, 'easy'), (9001, 'hard')):
            self.assertEqual(student.post('/api/sr/' + str(numero) + '/review', json={'result': result}).status_code, 200)
        self.add_user('Autre')
        other = self.client()
        with other.session_transaction() as session:
            session['sr_user'] = 'Autre'
        self.assertEqual(other.post('/api/sr/1/review', json={'result': 'again', 'note': 'Je dois reprendre la méthode.'}).status_code, 200)
        response = student.get('/api/forgecards/public?prenom=Autre')
        grades = {card['numero']: card['last_result'] for card in response.get_json()}
        self.assertEqual(grades, {1: 'easy', 2: None, 9001: 'hard'})
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('Cookie', response.vary)
        self.assertEqual(other.get('/api/forgecards/public').get_json()[0]['last_result'], 'again')
        for client in (self.client(), self.admin):
            self.assertTrue(all(card['last_result'] is None for card in client.get('/api/forgecards/public').get_json()))

    def test_reset_clears_last_grade_and_logout_never_exposes_it(self):
        student = self.client()
        self.login(student)
        self.assertEqual(student.post('/api/sr/1/review', json={'result': 'easy'}).status_code, 200)
        self.assertEqual(student.get('/api/forgecards/public').get_json()[0]['last_result'], 'easy')
        self.assertEqual(self.admin.post('/api/forgecards/1/reset-stats').status_code, 200)
        self.assertIsNone(student.get('/api/forgecards/public').get_json()[0]['last_result'])
        self.assertEqual(student.post('/api/sr/1/review', json={'result': 'hard'}).status_code, 200)
        self.assertEqual(student.post('/api/sr/logout').status_code, 200)
        self.assertIsNone(student.get('/api/forgecards/public').get_json()[0]['last_result'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
