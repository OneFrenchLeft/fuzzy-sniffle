"""QCM JSON save/load regressions. Run with python test_qcm_json.py."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config

with patch.object(config, 'ADMIN_PASSWORD', 'test-only'):
    import db
    import qcm_admin
import qcm_engine
from flask import Flask


class QcmJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path = root / 'qcm_chimie.json'
        for target, name, value in (
            (db, 'DB_PATH', root / 'test.db'),
            (db, '_db_ready', False),
            (qcm_admin, 'QCM_FILES', {'chimie': self.path}),
            (qcm_engine, '_QUESTIONS_CACHE', {}),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY='test-only')
        app.register_blueprint(qcm_admin.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['admin'] = True
        self.question = {'question': 'Question ?', 'choix': ['A', 'B', 'C'],
                         'reponse': 2, 'temps': 30}

    def save(self, question):
        return self.client.put('/api/admin/qcm/chimie', json={'raw': [question]})

    def load(self, question):
        self.path.write_text(json.dumps([question]), encoding='utf-8')
        qcm_engine._QUESTIONS_CACHE.clear()
        return qcm_engine.read_qcm_questions(self.path, theme='chimie')

    def test_legacy_image_is_migrated_without_losing_it(self):
        question = dict(self.question, image_complete='https://example.org/old.png')
        self.assertEqual(self.load(question)[0]['image'], question['image_complete'])
        self.assertEqual(self.save(question).status_code, 200)
        saved = json.loads(self.path.read_text())[0]
        self.assertEqual(saved['image'], question['image_complete'])
        self.assertNotIn('image_complete', saved)
        self.assertEqual(qcm_engine.read_qcm_questions(self.path, 'chimie')[0]['image'], saved['image'])

    def test_canonical_image_wins_even_when_cleared(self):
        for image in ('https://example.org/new.png', '', None):
            with self.subTest(image=image):
                question = dict(self.question, image=image, image_complete='old.png')
                self.assertEqual(self.load(question)[0]['image'], image or '')
                self.assertEqual(self.save(question).status_code, 200)
                saved = json.loads(self.path.read_text())[0]
                self.assertEqual(saved.get('image', ''), image or '')
                self.assertNotIn('image_complete', saved)

    def test_invalid_indices_and_timers_do_not_overwrite_file(self):
        self.assertEqual(self.save(self.question).status_code, 200)
        original = self.path.read_bytes()
        for key, value in (('reponse', 1.9), ('reponse', True), ('temps', 30.9),
                           ('temps', True), ('reponse', float('inf'))):
            with self.subTest(key=key, value=value):
                response = self.save(dict(self.question, **{key: value}))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_request_shape_returns_400(self):
        for payload in ([self.question], 'invalid', 1, True):
            with self.subTest(payload=payload):
                response = self.client.put('/api/admin/qcm/chimie', json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.path.exists())

    def test_empty_choice_must_not_shift_the_correct_answer(self):
        question = dict(self.question, choix=['', 'A', 'B'], reponse=2)
        self.assertEqual(self.load(question), [])

    def test_loader_rejects_fractional_and_boolean_answer_indices(self):
        for answer in (1.9, True):
            with self.subTest(answer=answer):
                self.assertEqual(self.load(dict(self.question, reponse=answer)), [])

    def test_rich_choices_and_question_identity_survive_migration(self):
        question = dict(self.question, image_complete='question.png',
                        choix=[{'image': 'answer.png'}, {'texte': 'B'}, 'C'])
        before = self.load(question)[0]
        self.assertEqual(self.save(question).status_code, 200)
        after = qcm_engine.read_qcm_questions(self.path, 'chimie')[0]
        self.assertEqual(after, before)
        self.assertEqual(after['choices'][0]['image'], 'answer.png')
        self.assertEqual(after['choices'][after['answer']]['text'], 'B')

    def test_integer_strings_remain_supported(self):
        question = dict(self.question, reponse='2', temps='30')
        self.assertEqual(self.save(question).status_code, 200)
        saved = json.loads(self.path.read_text())[0]
        self.assertEqual((saved['reponse'], saved['temps']), (2, 30))


if __name__ == '__main__':
    unittest.main()
