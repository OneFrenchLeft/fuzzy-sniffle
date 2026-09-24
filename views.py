# -*- coding: utf-8 -*-
"""Pages publiques, erreurs, entetes de securite, fichiers servis."""
from flask import Blueprint, render_template, request, jsonify, session, send_from_directory, make_response, abort
from config import TEMPLATES, STATIC, DATA, UPLOADS, read_params, write_params, CHAPITRES
import os
from auth import require_admin

bp = Blueprint('views', __name__)

@bp.route('/')
def home():
    return render_template('index.html', active='tirage')

@bp.route('/sr')
def page_sr():
    return render_template('sr.html', active='sr')

@bp.route('/liste')
def page_liste():
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
        return render_template('index.html')
    return render_template('qcm.html', prenom=session['sr_user'])

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
    return jsonify(read_params())

@bp.route('/api/params', methods=['POST'])
@require_admin
def post_params():
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'params': write_params(data)})

@bp.route('/api/chapitres', methods=['GET'])
def get_chapitres():
    return jsonify(CHAPITRES)

@bp.route('/papayou')
def page_papayou():
    return render_template('papayou.html', active='papayou')

@bp.route('/probabilites')
def page_probabilites():
    # Page « cachee » : accessible par URL sans login, aucun lien public n'y
    # mene (le seul lien est dans la page admin). Ne pas l'ajouter a la nav.
    return render_template('probabilites.html', active='probabilites')

