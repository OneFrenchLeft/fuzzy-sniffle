# -*- coding: utf-8 -*-
"""Pages publiques, erreurs, entetes de securite, fichiers servis."""
from flask import Blueprint, render_template, request, jsonify, session, send_from_directory, make_response, abort
from config import (TEMPLATES, STATIC, DATA, UPLOADS, read_params, write_params,
                    chapitres_for, subjects_payload, all_subjects_payload,
                    ENABLED_SUBJECTS)
import os
from auth import require_admin
from helpers import current_subject, admin_subject

bp = Blueprint('views', __name__)

def _requested_subject():
    """Matiere demandee pour params/chapitres : l'admin (session ouverte) peut
    viser n'importe quelle matiere connue ; l'eleve seulement les matieres
    activees par les flags."""
    if session.get('admin'):
        return admin_subject()
    s = request.values.get('subject')
    return s if s in ENABLED_SUBJECTS else current_subject()

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
    # L'admin voit TOUTES les matieres connues : il prepare la chimie pendant
    # qu'elle est encore cachee aux eleves. Les QCM restent communs aux deux
    # pages (selecteur de matiere dans l'editeur).
    return render_template('admin.html', active='admin',
                           subjects=all_subjects_payload(), subject_key='physique')

@bp.route('/cadmin')
def page_cadmin():
    # Meme template, meme mot de passe, matiere cible = chimie. Le JS lit
    # data-subject et passe ?subject=chimie a tous ses appels admin.
    return render_template('admin.html', active='admin',
                           subjects=all_subjects_payload(), subject_key='chimie')

@bp.route('/favicon.ico')
def favicon():
    return send_from_directory(STATIC, 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@bp.route('/qcm')
def qcm_page():
    if not session.get('sr_user'):
        return render_template('index.html')
    return render_template('qcm.html', prenom=session['sr_user'], subjects=subjects_payload())

@bp.route('/qcm-images/<path:filename>')
def qcm_image(filename):
    root = (DATA / 'qcm_images').resolve()
    safe = (root / filename).resolve()
    if not str(safe).startswith(str(root) + os.sep) or not safe.is_file():
        abort(404)
    return send_from_directory(safe.parent, safe.name)

@bp.route('/uploads/fiche/<path:filename>')
def fiche_file(filename):
    # `path:` + send_from_directory : les PDF des matieres non-physiques
    # vivent dans un sous-dossier (`uploads/fiche/chimie/...`) et les tentatives
    # de traversal sortent en 404, comme avant.
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
        "frame-src https://www.youtube.com; "
        "frame-ancestors 'none'"
    )
    return resp

@bp.route('/api/params', methods=['GET'])
def get_params():
    return jsonify(read_params(_requested_subject()))

@bp.route('/api/params', methods=['POST'])
@require_admin
def post_params():
    data = request.get_json(silent=True) or {}
    return jsonify({'ok': True, 'params': write_params(data, admin_subject())})

@bp.route('/api/chapitres', methods=['GET'])
def get_chapitres():
    return jsonify(chapitres_for(_requested_subject()))

@bp.route('/api/subjects', methods=['GET'])
def get_subjects():
    # L'UI ne montre que les matieres activees par les flags du .env, plus la
    # matiere courante de la session (le switch l'affiche immediatement).
    return jsonify({'ok': True, 'subjects': subjects_payload(),
                    'current': current_subject()})

@bp.route('/api/subject', methods=['POST'])
def set_subject():
    """Switch matiere : contexte de navigation, pas d'authentification. Le
    login eleve reste unique et commun aux deux matieres."""
    data = request.get_json(silent=True) or {}
    s = data.get('subject')
    if s not in ENABLED_SUBJECTS:
        return jsonify({'ok': False, 'error': 'matiere inconnue ou non activee'}), 400
    session['subject'] = s
    session.modified = True
    return jsonify({'ok': True, 'subject': s})

@bp.route('/papayou')
def page_papayou():
    return render_template('papayou.html', active='papayou')
