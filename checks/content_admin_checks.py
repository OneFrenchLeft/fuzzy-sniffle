"""Run: venv/bin/python checks/content_admin_checks.py (isolated temporary data)."""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ['MADEC_ADMIN_PASSWORD'] = 'content-checks-only'
os.environ['FLASK_SECRET_KEY'] = 'content-checks-only'
os.environ['MADEC_COOKIE_SECURE'] = '0'
import config

initial = tempfile.TemporaryDirectory(prefix='forgecards-content-checks-')
config.DATA = Path(initial.name)
config.DB_PATH = config.DATA / 'checks.db'
config.PARAMS_PATH = config.DATA / 'params.json'
config.UPLOADS = config.DATA / 'uploads'

import db as database
import views, auth, cards, sr, qcm_admin, qcm_engine, ankimie, content_admin
from flask import Flask


def fixture(root):
    config.DATA = root
    database.DB_PATH = root / 'checks.db'
    database._db_ready = False
    config.PARAMS_PATH = root / 'params.json'
    config.QCM_FILES.clear()
    config.QCM_FILES.update({theme: root / ('qcm_' + theme + '.json')
                            for theme in ('physique', 'chimie', 'maths')})
    qcm_engine.QCM_FILES = config.QCM_FILES
    qcm_engine._QUESTIONS_CACHE.clear()
    for theme, path in config.QCM_FILES.items():
        questions = [{'question': theme + ' — Question ' + ch, 'chapitre': ch,
                      'choix': ['Réponse A', 'Réponse B'], 'reponse': 1, 'temps': 30,
                      'explication': 'Une explication.'} for ch in ('Optique', 'Mécanique')]
        path.write_text(json.dumps(questions), encoding='utf-8')
    database.init_db()
    conn = database.db()
    conn.execute("INSERT INTO users(prenom,emoji_password) VALUES ('Élève','1|2|3|4')")
    conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file, titre) VALUES (1,'1.pdf','1-c.pdf','Fiche à conserver')")
    conn.commit()
    conn.close()


def application():
    app = Flask(__name__, template_folder=str(config.TEMPLATES), static_folder=str(config.STATIC))
    app.secret_key = 'content-checks-only'
    app.config['TESTING'] = True
    app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
    for module in (views, auth, cards, sr, qcm_admin, ankimie, content_admin):
        app.register_blueprint(module.bp)
    @app.route('/__content_checks__')
    def isolated_fixture():
        return {'isolated_content_fixture': True}
    return app


class ContentAdministration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='forgecards-content-case-')
        self.addCleanup(self.temporary.cleanup)
        fixture(Path(self.temporary.name))
        self.app = application()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['admin'] = True
            session['sr_user'] = 'Élève'

    def bank(self, theme='physique'):
        return self.client.get('/api/admin/qcm/' + theme).get_json()

    def questions(self, theme='physique'):
        return json.loads(self.bank(theme)['raw'])

    def question(self, text='Nouvelle question', chapter='Optique'):
        return {'question': text, 'chapitre': chapter, 'choix': ['A', 'B'], 'reponse': 1, 'temps': 30}

    def count(self, table):
        conn = database.db()
        try:
            return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        finally:
            conn.close()

    def test_visual_create_edit_delete_preserve_other_chapters_and_ids(self):
        before = self.questions()
        response = self.client.post('/api/admin/qcm/physique/questions', json={
            'question': self.question(), 'revision': self.bank()['revision']})
        self.assertEqual(response.status_code, 200)
        added = next(q for q in response.get_json()['questions'] if q['question'] == 'Nouvelle question')
        self.assertRegex(added['qid'], config.QID_RE)
        edited = {**added, 'question': 'Question modifiée', 'choix': ['A', 'B', 'C'], 'reponse': 3}
        response = self.client.put('/api/admin/qcm/physique/questions/' + added['qid'], json={
            'question': edited, 'revision': self.bank()['revision']})
        self.assertEqual(response.status_code, 200)
        changed = next(q for q in self.questions() if q['qid'] == added['qid'])
        self.assertEqual(changed['reponse'], 3)
        self.assertEqual(changed['question'], 'Question modifiée')
        for q in before:
            self.assertIn(q, self.questions())
        response = self.client.delete('/api/admin/qcm/physique/questions/' + added['qid'], json={'revision': self.bank()['revision']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.questions(), before)

    def test_legacy_content_active_and_new_chapter_inactive(self):
        self.assertTrue(all(ch['qcm_active'] for ch in self.bank()['chapters']))
        self.assertEqual(len(qcm_engine.pick_questions(['physique'], 20)), 2)
        response = self.client.post('/api/admin/content/physique/chapters', json={'chapitre': 'Électricité'})
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/api/admin/qcm/physique/questions', json={'question': self.question(chapter='Électricité')})
        self.assertEqual(response.status_code, 200)
        row = next(ch for ch in self.bank()['chapters'] if ch['chapitre'] == 'Électricité')
        self.assertFalse(row['qcm_active'])
        self.assertFalse(row['ankimie_active'])
        self.assertEqual(len(self.questions()), 3)
        self.assertEqual(len(qcm_engine.pick_questions(['physique'], 20)), 2)
        database.init_db()
        self.assertFalse(next(ch for ch in self.bank()['chapters'] if ch['chapitre'] == 'Électricité')['qcm_active'])

    def test_deactivation_filters_lobby_game_review_but_keeps_history(self):
        optics = self.questions()[0]
        conn = database.db()
        conn.execute('INSERT INTO qcm_answers(qid,theme,chapitre,question,prenom,ok) VALUES(?,?,?,?,?,0)',
                     (optics['qid'], 'physique', 'Optique', optics['question'], 'Élève'))
        conn.commit(); conn.close()
        qcm_engine._review_query = lambda _: [{'qid': optics['qid'], 'created_at': '2026-10-12'}]
        original = config.QCM_FILES['physique'].read_bytes()
        response = self.client.put('/api/admin/content/physique/chapters', json={'chapitre': 'Optique', 'qcm_active': False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        self.assertEqual(len(self.questions()), 2)
        self.assertNotIn(['physique', 'Optique'], qcm_engine.available_chapters(['physique']))
        self.assertNotIn(optics['qid'], [q['qid'] for q in qcm_engine.pick_questions(['physique'], 20)])
        self.assertEqual(qcm_engine.review_questions('Élève', ['physique'], 10), [])
        self.assertEqual(self.count('qcm_answers'), 1)
        self.assertEqual(self.client.get('/api/qcm/last-states').get_json()['wrongs'], {})

    def test_ankimie_deactivation_retains_review_state(self):
        q = self.questions('chimie')[0]
        self.assertEqual(self.client.post('/api/ankimie/' + q['qid'] + '/review', json={'result': 'good'}).status_code, 200)
        response = self.client.put('/api/admin/content/chimie/chapters', json={'chapitre': 'Optique', 'ankimie_active': False})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(q['qid'], [c['qid'] for c in self.client.get('/api/ankimie/today').get_json()['cards']])
        self.assertEqual(self.client.get('/api/ankimie/' + q['qid'] + '/answer').status_code, 404)
        self.assertEqual(self.client.post('/api/ankimie/' + q['qid'] + '/review', json={'result': 'good'}).status_code, 404)
        self.assertEqual(self.count('qcm_sr_reviews'), 1)
        self.client.put('/api/admin/content/chimie/chapters', json={'chapitre': 'Optique', 'ankimie_active': True})
        conn = database.db()
        self.assertEqual(conn.execute('SELECT repetitions FROM qcm_sr_state WHERE qid=?', (q['qid'],)).fetchone()[0], 1)
        conn.close()

    def test_collection_removal_never_deletes_bank_forgecard_or_history(self):
        q = self.questions('chimie')[0]
        self.client.post('/api/ankimie/' + q['qid'] + '/review', json={'result': 'good'})
        response = self.client.put('/api/admin/ankimie/chimie/collections', json={'chapitre': 'Optique', 'qid': q['qid'], 'included': False})
        self.assertEqual(response.status_code, 200)
        self.assertIn(q, self.questions('chimie'))
        self.assertEqual(self.count('forgecards'), 1)
        self.assertEqual(self.count('qcm_sr_reviews'), 1)
        self.assertNotIn(q['qid'], self.client.get('/api/admin/ankimie/chimie/collections?chapitre=Optique').get_json()['included'])
        self.client.put('/api/admin/ankimie/chimie/collections', json={'chapitre': 'Optique', 'qid': q['qid'], 'included': True})
        self.client.put('/api/admin/ankimie/chimie/collections', json={'chapitre': 'Mécanique', 'qid': q['qid'], 'included': True})
        self.assertEqual(sum(card['qid'] == q['qid'] for card in ankimie._deck_questions()), 1)

    def test_new_ankimie_subject_is_opt_in(self):
        q = self.questions()[0]
        self.client.put('/api/admin/ankimie/physique/collections', json={'chapitre': 'Optique', 'qid': q['qid'], 'included': True})
        self.assertNotIn(q['qid'], ankimie._deck_index())
        self.client.put('/api/admin/content/physique/chapters', json={'chapitre': 'Optique', 'ankimie_active': True})
        self.assertIn(q['qid'], ankimie._deck_index())

    def test_revision_conflict_and_duplicate_submission(self):
        revision = self.bank()['revision']
        data = {'question': self.question(), 'revision': revision}
        self.assertEqual(self.client.post('/api/admin/qcm/physique/questions', json=data).status_code, 200)
        current = config.QCM_FILES['physique'].read_bytes()
        self.assertEqual(self.client.post('/api/admin/qcm/physique/questions', json=data).status_code, 409)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), current)
        self.assertEqual(len(self.questions()), 3)

    def test_invalid_questions_and_json_never_replace_bank(self):
        original = config.QCM_FILES['physique'].read_bytes()
        candidates = [self.question(), self.question(), self.question(), self.question()]
        candidates[0]['choix'] = ['A']
        candidates[1]['temps'] = 301
        candidates[2]['reponse'] = True
        candidates[3]['choix'] = ['A', '']
        for q in candidates:
            self.assertEqual(self.client.post('/api/admin/qcm/physique/questions', json={'question': q}).status_code, 400)
            self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        self.assertEqual(self.client.put('/api/admin/qcm/physique', json={'raw': 'broken JSON'}).status_code, 400)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)

    def test_csv_preview_and_atomic_import(self):
        text = 'Question;Réponse A;Réponse B;Bonne réponse;Temps\nImportée;A;B;B;30\nInvalide;A;;A;30\n'
        original = config.QCM_FILES['physique'].read_bytes()
        preview = self.client.post('/api/admin/qcm/physique/import/preview', json={'text': text, 'chapitre': 'Optique'}).get_json()
        self.assertEqual(preview['count'], 2)
        self.assertEqual(preview['valid_count'], 1)
        self.assertFalse(preview['can_import'])
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        self.assertEqual(self.client.post('/api/admin/qcm/physique/import', json={'chapitre': 'Optique', 'questions': [self.question(), {'question': 'Invalide'}]}).status_code, 400)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        preview = self.client.post('/api/admin/qcm/physique/import/preview', json={'text': text.split('Invalide')[0], 'chapitre': 'Optique'}).get_json()
        payload = {'chapitre': 'Optique', 'questions': preview['questions'], 'revision': preview['revision'], 'preview_token': preview['preview_token']}
        self.assertEqual(self.client.post('/api/admin/qcm/physique/import', json=payload).status_code, 200)
        self.assertEqual(len(self.questions()), 3)
        self.assertEqual(self.client.post('/api/admin/qcm/physique/import', json=payload).status_code, 409)
        self.assertIn('Mécanique', [q['chapitre'] for q in self.questions()])

    def test_write_failure_preserves_original_and_rolls_back_chapter(self):
        original = config.QCM_FILES['physique'].read_bytes()
        def failed_write(*args):
            conn = database.db()
            row = conn.execute('SELECT qcm_active, ankimie_active FROM content_chapters WHERE chapitre=?', ('Nouveau',)).fetchone()
            self.assertEqual(tuple(row), (0, 0), 'New content must already be inactive when the bank file is exposed')
            conn.close()
            raise OSError('read only')
        with patch('qcm_admin._atomic_bank_write', side_effect=failed_write):
            response = self.client.post('/api/admin/qcm/physique/questions', json={'question': self.question(chapter='Nouveau')})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        self.assertNotIn('Nouveau', [ch['chapitre'] for ch in self.bank()['chapters']])

    def test_import_confirmation_requires_the_complete_validated_preview(self):
        text = 'Question;Réponse A;Réponse B;Bonne réponse\nImport 1;A;B;A\nInvalide;A;;A\n'
        original = config.QCM_FILES['physique'].read_bytes()
        preview = self.client.post('/api/admin/qcm/physique/import/preview', json={'text': text, 'chapitre': 'Optique'}).get_json()
        partial = {'chapitre': 'Optique', 'questions': preview['questions'], 'revision': preview['revision'], 'preview_token': preview['preview_token']}
        self.assertEqual(self.client.post('/api/admin/qcm/physique/import', json=partial).status_code, 400)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)
        valid = text.split('Invalide')[0] + 'Import 2;A;B;B\n'
        preview = self.client.post('/api/admin/qcm/physique/import/preview', json={'text': valid, 'chapitre': 'Optique'}).get_json()
        partial = {'chapitre': 'Optique', 'questions': preview['questions'][:1], 'revision': preview['revision'], 'preview_token': preview['preview_token']}
        self.assertEqual(self.client.post('/api/admin/qcm/physique/import', json=partial).status_code, 400)
        self.assertEqual(config.QCM_FILES['physique'].read_bytes(), original)

    def test_rich_latex_roundtrip_and_authentication(self):
        q = self.question(); q['latex'] = r'\frac{1}{2}'; q['choix'] = [{'latex': 'x^2'}, {'image': 'answer.png'}]
        self.assertEqual(self.client.post('/api/admin/qcm/physique/questions', json={'question': q}).status_code, 200)
        rendered = qcm_engine.read_qcm_questions(config.QCM_FILES['physique'], 'physique')
        self.assertIn('$x^2$', rendered[-1]['choices'][0]['text'])
        self.assertIn(r'$$\frac{1}{2}$$', rendered[-1]['question'])
        anon = self.app.test_client()
        self.assertEqual(anon.get('/api/admin/content/subjects').status_code, 401)
        self.assertEqual(anon.post('/api/admin/qcm/physique/questions', json={'question': self.question()}).status_code, 401)
        self.assertEqual(anon.put('/api/admin/ankimie/chimie/collections', json={}).status_code, 401)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        fixture(Path(initial.name))
        application().run(host='127.0.0.1', port=int(os.environ.get('CONTENT_CHECK_PORT', '5011')), debug=False, use_reloader=False)
    else:
        unittest.main(verbosity=2)
