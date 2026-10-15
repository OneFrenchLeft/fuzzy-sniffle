# -*- coding: utf-8 -*-
"""Pages publiques, erreurs, entetes de securite, fichiers servis."""
from flask import Blueprint, render_template, request, jsonify, session, send_from_directory, make_response, abort, redirect, url_for
from config import TEMPLATES, STATIC, DATA, UPLOADS, read_params, write_params, module_enabled
from config import get_chapitres as current_chapitres, subject_list
import os
from auth import require_admin

bp = Blueprint('views', __name__)

@bp.app_context_processor
def inject_modules():
    """Interrupteurs de modules et textes configurables, dans tous les templates."""
    p = read_params()
    return {
        'modules': {
            'forgecards': bool(p.get('module_forgecards', 1)),
            'ankimie': bool(p.get('module_ankimie', 1)),
            'qcm': bool(p.get('module_qcm', 1)),
        },
        'wording': {
            'site_name': p.get('site_name') or 'Forgecards',
            'tirage_label': p.get('tirage_label') or 'Simulation de khôlle',
        },
    }

def _module_off(module, label):
    if module_enabled(module):
        return None
    return render_template('module_off.html', active='', module_label=label)

@bp.route('/')
def home():
    if not module_enabled('forgecards'):
        return redirect(url_for('views.page_compte'))
    return render_template('index.html', active='tirage')

@bp.route('/sr')
def page_sr():
    off = _module_off('forgecards', 'Les Forgecards')
    if off: return off
    return render_template('sr.html', active='sr')

@bp.route('/liste')
def page_liste():
    off = _module_off('forgecards', 'Les Forgecards')
    if off: return off
    return render_template('liste.html', active='liste')

@bp.route('/compte')
def page_compte():
    return render_template('compte.html', active='compte')

@bp.route('/admin')
def page_admin():
    return render_template('admin.html', active='admin')

@bp.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@bp.route('/qcm')
def qcm_page():
    if not session.get('sr_user'):
        return redirect(url_for('views.page_compte'))
    off = _module_off('qcm', 'Les QCM')
    if off: return off
    themes = [[s['key'], s['label'], s['color']] for s in subject_list()]
    return render_template('qcm.html', prenom=session['sr_user'], active='qcm', subjects=themes)


@bp.route('/ankimie')
def page_ankimie():
    if not session.get('sr_user'):
        return redirect(url_for('views.page_compte'))
    off = _module_off('ankimie', 'Ankimie')
    if off: return off
    return render_template('ankimie.html', active='ankimie')

@bp.route('/qcm-images/<path:filename>')
def qcm_image(filename):
    root = (DATA / 'qcm_images').resolve()
    safe = (root / filename).resolve()
    if not str(safe).startswith(str(root) + os.sep) or not safe.is_file():
        abort(404)
    return send_from_directory(safe.parent, safe.name)

@bp.route('/uploads/fiche/<filename>')
def fiche_file(filename):

    return send_from_directory(UPLOADS, filename, max_age=0)

@bp.app_errorhandler(413)
def too_large(e):
    return jsonify({'ok': False, 'error': 'fichier trop volumineux (max 15 Mo)'}), 413

@bp.app_errorhandler(404)
def page_not_found(e):
    try:
        return render_template('404.html'), 404
    except Exception:

        return make_response('Page introuvable', 404)

@bp.app_errorhandler(500)
def internal_error(e):
    try:
        resp = send_from_directory(STATIC, '500.html')
        resp.status_code = 500
        return resp
    except Exception:
        return make_response('Erreur interne du serveur', 500)

def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.is_secure:
        resp.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'

    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-src https://www.youtube.com; "
        "frame-ancestors 'none'"
    )
    return resp

@bp.route('/api/params', methods=['GET'])
def get_params():
    out = read_params()
    # Registre des matieres (config.py) expose en lecture pour les selects
    # admin et le lobby QCM ; cle '_' : jamais ecrite dans params.json.
    out['_subjects'] = subject_list()
    return jsonify(out)

@bp.route('/api/params', methods=['POST'])
@require_admin
def post_params():
    try:
        params = write_params(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({'ok': True, 'params': params})

@bp.route('/api/chapitres', methods=['GET'])
def get_chapitres():
    return jsonify(current_chapitres())

@bp.route('/papayou')
def page_papayou():
    return render_template('papayou.html', active='papayou')

