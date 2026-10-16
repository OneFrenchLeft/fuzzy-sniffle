"""Contrôles isolés : venv/bin/python checks/config_runtime_checks.py."""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SOURCES = tempfile.TemporaryDirectory(prefix='forgecards-config-sources-')
for source in REPO.glob('*.py'):
    shutil.copyfile(source, Path(SOURCES.name) / source.name)
sys.path.insert(0, SOURCES.name)
os.environ['MADEC_ADMIN_PASSWORD'] = 'config-checks-only'
os.environ['MADEC_INTERNAL_API_KEY'] = 'config-checks-only'
import config
import db as database
import views, auth, internal
from flask import Flask


class Configuration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='forgecards-config-case-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config.PARAMS_PATH = self.root / 'params.json'
        database.DB_PATH = self.root / 'checks.db'
        database.UPLOADS = self.root / 'uploads'
        database.UPLOADS.mkdir()
        database._db_ready = False
        self.app = Flask('config-checks', template_folder=str(REPO / 'templates'))
        self.app.secret_key = 'config-checks-only'
        self.app.config['TESTING'] = True
        for module in (views, auth, internal):
            self.app.register_blueprint(module.bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['admin'] = True
        self.today = datetime(2026, 10, 14, 12)
        self.clock = patch.object(config, 'now_paris', lambda: self.today)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        config.read_params()

    def test_invalid_numbers_are_rejected_without_changing_file(self):
        original = config.PARAMS_PATH.read_bytes()
        for key, value in [('max_active_num', 10 ** 100), ('daily_new_limit', -1),
                           ('max_kholle_num', float('inf')), ('fsrs_retention', 'NaN'),
                           ('last_chapter_bonus', float('nan'))]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    config.write_params({key: value})
                self.assertEqual(config.PARAMS_PATH.read_bytes(), original)

    def test_corrupt_roots_and_numbers_have_safe_finite_defaults(self):
        for raw in ('[]', 'null', 'broken', '{"max_active_num":Infinity,"fsrs_retention":"NaN"}'):
            config.PARAMS_PATH.write_text(raw)
            with self.assertLogs('config', level='WARNING') if raw in ('[]', 'null', 'broken') else self.subTest(raw=raw):
                result = config.read_params()
            self.assertEqual(result['max_active_num'], config.DEFAULT_PARAMS['max_active_num'])
            json.dumps(result, allow_nan=False)

    def test_admin_api_rejects_non_objects_and_out_of_range_limits(self):
        original = config.PARAMS_PATH.read_bytes()
        for value in (['x'], None, {'max_active_num': 1e100}):
            response = self.client.post('/api/params', json=value)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()['ok'])
            self.assertEqual(config.PARAMS_PATH.read_bytes(), original)

    def test_invalid_private_metadata_cannot_break_sql_or_pause_reader(self):
        config.PARAMS_PATH.write_text(json.dumps({
            '_max_active_increment': {'date': '2026-10-14', 'from': 10 ** 100},
            '_forgecards_pauses': [None, 'x', {'start': None}, {'start': '2026-10-14', 'end': 3}],
        }))
        result = config.read_params()
        self.assertNotIn('_max_active_increment', result)
        self.assertFalse(config.forgecards_paused('2026-10-14', result))
        for raw in (None, {}, [None, 'x', {'start': None}, {'start': '2026-10-14', 'end': 3}]):
            self.assertFalse(config.forgecards_paused('2026-10-14', {'_forgecards_pauses': raw}))

    def test_increments_keep_first_threshold_and_partial_reduction(self):
        config.write_params({'max_active_num': 10})
        for number in (20, 30, 25):
            result = config.write_params({'max_active_num': number})
            self.assertEqual(result['_max_active_increment'], {'date': '2026-10-14', 'from': 10})
        self.assertNotIn('_max_active_increment', config.write_params({'max_active_num': 10}))

    def test_pause_history_survives_restarts_and_resume(self):
        config.write_params({'module_forgecards': 0})
        self.today += timedelta(days=2)
        paused = config.read_params()
        self.assertTrue(config.forgecards_paused('2026-10-14', paused))
        self.assertTrue(config.forgecards_paused('2026-10-15', paused))
        resumed = config.write_params({'module_forgecards': 1})
        self.assertTrue(config.forgecards_paused('2026-10-15', resumed))
        self.assertFalse(config.forgecards_paused('2026-10-16', resumed))
        self.assertEqual(config.read_params()['_forgecards_pauses'], resumed['_forgecards_pauses'])

    def test_legacy_disabled_module_records_first_observed_day_only(self):
        config.PARAMS_PATH.write_text('{"module_forgecards":0}')
        result = config.read_params()
        self.assertEqual(result['_forgecards_pauses'], [{'start': '2026-10-14', 'end': None}])
        self.today += timedelta(days=1)
        result = config.read_params()
        self.assertFalse(config.forgecards_paused('2026-10-13', result))
        self.assertTrue(config.forgecards_paused('2026-10-14', result))

    def test_atomic_write_failure_preserves_original_and_cleans_temporary(self):
        original = config.PARAMS_PATH.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('disque indisponible')):
            with self.assertRaises(OSError):
                config.write_params({'site_name': 'Nouveau nom'})
        self.assertEqual(config.PARAMS_PATH.read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_failed_read_cannot_overwrite_existing_settings(self):
        config.write_params({'module_qcm': 0, 'max_active_num': 17})
        original = config.PARAMS_PATH.read_bytes()
        with patch.object(Path, 'read_text', side_effect=OSError('lecture indisponible')):
            with self.assertRaises(ValueError):
                config.write_params({'site_name': 'Nouveau nom'})
        self.assertEqual(config.PARAMS_PATH.read_bytes(), original)

    def test_parallel_changes_do_not_lose_unrelated_settings(self):
        barrier = threading.Barrier(2)
        def change(data):
            barrier.wait(timeout=5)
            config.write_params(data)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, data) for data in ({'site_name': 'Classe'}, {'reminder_hour': 18})]
            for future in futures:
                future.result(timeout=10)
        result = config.read_params()
        self.assertEqual(result['site_name'], 'Classe')
        self.assertEqual(result['reminder_hour'], 18)

    def test_internal_payload_errors_are_json_400(self):
        headers = {'X-Madec-Internal-Key': 'config-checks-only'}
        for endpoint, body in [('/api/internal/log-event', ['x']),
                               ('/api/internal/log-event', {'prenom': 3, 'type': 'login'}),
                               ('/api/internal/qcm-invites', {'prenom': 'Eleve', 'ttl_minutes': None})]:
            response = self.client.post(endpoint, json=body, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()['ok'])

    def test_guard_retries_after_transient_failure(self):
        tree = ast.parse((REPO / 'app.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_streak_guard_loop')
        calls = []
        def close(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('verrou temporaire')
            return {}
        class Stop(BaseException):
            pass
        ticks = []
        def sleep(_seconds):
            ticks.append(1)
            if len(ticks) == 3:
                raise Stop()
        namespace = {'now_paris': lambda: datetime(2026, 10, 14, 23, 56),
                     'read_params': lambda: {}, 'socketio': SimpleNamespace(sleep=sleep)}
        with patch.dict(sys.modules, {'streak': SimpleNamespace(close_all_missed_days=close)}):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'app.py', 'exec'), namespace)
            with self.assertRaises(Stop):
                namespace['_streak_guard_loop']()
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
