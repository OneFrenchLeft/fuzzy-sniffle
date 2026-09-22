# -*- coding: utf-8 -*-
"""Fiches (forgecards) cote admin : upload, edition, stats, notes."""
import io, csv
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from config import CHAPITRES, UPLOADS, now_paris, read_params
from db import db, ensure_db, log_event
from helpers import (allowed_file, is_real_pdf, parse_teacher_difficulty, hs_sort_key,
                     card_label, filenames_for, remove_pdf_if_exists, card_exists,
                     interleave_by_chapitre)

from auth import require_admin
from db import csv_safe, csv_response
from replay import replay_reviews
from streak import activity_days, compute_record, compute_streak

bp = Blueprint('cards', __name__)

@bp.route('/api/forgecards', methods=['GET'])
@require_admin
def list_forgecards():
    ensure_db()
    conn = db()
    rows = conn.execute('SELECT numero, code, fiche_file, correction_file, bareme_file, titre, indices, chapitre, teacher_difficulty, hors_serie, created_at FROM forgecards ORDER BY numero ASC').fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    for c in out:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    return jsonify(out)

@bp.route('/api/forgecards/public')
def list_forgecards_public():
    ensure_db()
    q = (request.args.get('q') or '').strip().lower()
    params = read_params()
    max_active = int(params.get('max_active_num', 36))
    max_hs = int(params.get('max_hors_serie_num', 0))
    conn = db()
    rows = conn.execute(
        "SELECT numero, code, titre, chapitre, teacher_difficulty, fiche_file, correction_file, bareme_file, indices, hors_serie FROM forgecards WHERE hors_serie = 0 AND numero <= ? ORDER BY numero ASC",
        (max_active,)
    ).fetchall()
    if max_hs > 0:
        rows += conn.execute(
            "SELECT numero, code, titre, chapitre, teacher_difficulty, fiche_file, correction_file, bareme_file, indices, hors_serie FROM forgecards WHERE hors_serie = 1 ORDER BY numero ASC LIMIT ?",
            (max_hs,)
        ).fetchall()
    conn.close()
    cards = [dict(r) for r in rows]
    for c in cards:
        c['label'] = card_label(c.get('code'), c['hors_serie'])
    if q:
        def match(c):
            label = (c.get('label') or '').lower()
            return (q in (c.get('titre') or '').lower()
                    or q == str(c.get('numero'))
                    or q == label
                    or (c.get('hors_serie') and q == label.lstrip('h'))
                    or q in (c.get('chapitre') or '').lower())
        cards = [c for c in cards if match(c)]
    return jsonify(cards)

@bp.route('/api/forgecards/upload', methods=['POST'])
@require_admin
def upload_forgecard():
    ensure_db()
    numero = (request.form.get('numero') or '').strip()
    titre = (request.form.get('titre') or '').strip()
    indices = (request.form.get('indices') or '').strip()
    chapitre = (request.form.get('chapitre') or 'Autre').strip()
    fiche = request.files.get('fiche_pdf')
    correction = request.files.get('correction_pdf')
    bareme = request.files.get('bareme_pdf')
    if not fiche or not correction:
        return jsonify({'ok': False, 'error': 'missing data'}), 400
    if not allowed_file(fiche.filename) or not allowed_file(correction.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if bareme and not allowed_file(bareme.filename):
        return jsonify({'ok': False, 'error': 'bareme pdf only'}), 400
    # Impulse: Magic bytes, not vibes. A renamed .exe is not a fiche.
    if not is_real_pdf(fiche) or not is_real_pdf(correction) or (bareme and not is_real_pdf(bareme)):
        return jsonify({'ok': False, 'error': 'fichier invalide : ce n\'est pas un vrai PDF'}), 400
    if chapitre not in CHAPITRES:
        chapitre = 'Autre'
    teacher_difficulty = parse_teacher_difficulty(request.form.get('difficulty'))
    hors_serie = 1 if (request.form.get('hors_serie') or '') == '1' else 0
    raw_num = (numero or '').strip().lower()
    hs_target = raw_num if (raw_num.startswith('h') and raw_num[1:].isdigit()) else None
    if hs_target:
        hors_serie = 1
    if not numero.isdigit():
        if not hors_serie:
            return jsonify({'ok': False, 'error': 'numero requis'}), 400
        numero = None
    else:
        numero = int(numero)
    conn = db()
    try:
        if hs_target:
            row_t = conn.execute("SELECT numero FROM forgecards WHERE code = ? AND hors_serie = 1", (hs_target,)).fetchone()
            if not row_t:
                return jsonify({'ok': False, 'error': 'fiche ' + hs_target.upper() + ' introuvable'}), 404
            numero = row_t['numero']
            code = hs_target
        elif hors_serie:
            existing_codes = [r[0] for r in conn.execute("SELECT code FROM forgecards WHERE hors_serie = 1").fetchall()]
            next_idx = max([hs_sort_key(c) for c in existing_codes] + [0]) + 1
            code = f'h{next_idx}'
            # Un hors-serie ne squatte JAMAIS le numero d'une fiche normale :
            # slot force >= 9001 pour eviter tout ecrasement ON CONFLICT.
            row_max = conn.execute("SELECT MAX(numero) AS m FROM forgecards").fetchone()
            numero = max(9001, (row_max['m'] or 0) + 1)
        else:
            code = str(numero)
        fiche_name, corr_name, bareme_name = filenames_for(code)
        existing = conn.execute('SELECT fiche_file, correction_file, bareme_file FROM forgecards WHERE numero=?', (numero,)).fetchone()
        if existing:
            # L'ancien barème n'est supprime QUE si un nouveau est fourni :
            # sinon on conserve le fichier ET la reference en base.
            for name in [existing['fiche_file'], existing['correction_file']]:
                remove_pdf_if_exists(name)
            if bareme:
                remove_pdf_if_exists(existing['bareme_file'])
        fiche.save(UPLOADS / fiche_name)
        correction.save(UPLOADS / corr_name)
        final_bareme_name = ''
        if bareme:
            bareme.save(UPLOADS / bareme_name)
            final_bareme_name = bareme_name
        elif existing and existing['bareme_file']:
            final_bareme_name = existing['bareme_file']
        conn.execute(
            'INSERT INTO forgecards(numero, code, fiche_file, correction_file, bareme_file, titre, indices, chapitre, teacher_difficulty, hors_serie) '
            'VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(numero) DO UPDATE SET '
            'code=excluded.code, fiche_file=excluded.fiche_file, correction_file=excluded.correction_file, '
            'bareme_file=excluded.bareme_file, titre=excluded.titre, indices=excluded.indices, chapitre=excluded.chapitre, teacher_difficulty=excluded.teacher_difficulty, hors_serie=excluded.hors_serie',
            (numero, code, fiche_name, corr_name, final_bareme_name, titre, indices, chapitre, teacher_difficulty, hors_serie)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'numero': numero, 'code': code, 'label': card_label(code, hors_serie)})

@bp.route('/api/forgecards/<int:numero>', methods=['DELETE'])
@require_admin
def delete_forgecard(numero):
    ensure_db()
    conn = db()
    row = conn.execute('SELECT fiche_file, correction_file, bareme_file FROM forgecards WHERE numero=?', (numero,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'not found'}), 404
    conn.execute('DELETE FROM forgecards WHERE numero=?', (numero,))
    conn.execute('DELETE FROM sr_state_user WHERE numero=?', (numero,))

    # Preserver les jours d'activite de tous les eleves (sinon leurs streaks
    # cassent retroactivement) puis archiver les reviews pour la tracabilite.
    conn.execute(
        "INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) "
        "SELECT DISTINCT prenom, substr(created_at,1,10), 1 FROM reviews WHERE numero=?",
        (numero,),
    )
    conn.execute(
        "INSERT INTO reviews_archive(numero, prenom, result, note, duration_seconds, was_new, created_at, note_masquee) "
        "SELECT numero, prenom, result, note, duration_seconds, was_new, created_at, note_masquee FROM reviews WHERE numero=?",
        (numero,),
    )
    conn.execute('DELETE FROM reviews WHERE numero=?', (numero,))
    conn.execute('DELETE FROM draw_history WHERE numero=?', (numero,))
    conn.commit()
    conn.close()
    for name in [row['fiche_file'], row['correction_file'], row['bareme_file']]:
        remove_pdf_if_exists(name)
    return jsonify({'ok': True})

@bp.route('/api/forgecards/<int:numero>', methods=['PATCH'])
@require_admin
def edit_forgecard(numero):
    ensure_db()
    titre = (request.form.get('titre') or '').strip()
    indices = (request.form.get('indices') or '').strip()
    chapitre = (request.form.get('chapitre') or 'Autre').strip()
    remove_bareme = (request.form.get('remove_bareme') or '') == '1'
    remove_correction = (request.form.get('remove_correction') or '') == '1'
    fiche = request.files.get('fiche_pdf')
    correction = request.files.get('correction_pdf')
    bareme = request.files.get('bareme_pdf')
    if chapitre not in CHAPITRES:
        chapitre = 'Autre'
    teacher_difficulty = parse_teacher_difficulty(request.form.get('difficulty'))
    if fiche and not allowed_file(fiche.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if correction and not allowed_file(correction.filename):
        return jsonify({'ok': False, 'error': 'pdf only'}), 400
    if bareme and not allowed_file(bareme.filename):
        return jsonify({'ok': False, 'error': 'bareme pdf only'}), 400
    if ((fiche and not is_real_pdf(fiche)) or (correction and not is_real_pdf(correction))
            or (bareme and not is_real_pdf(bareme))):
        return jsonify({'ok': False, 'error': 'fichier invalide : ce n\'est pas un vrai PDF'}), 400
    if correction and remove_correction:
        return jsonify({'ok': False, 'error': 'choisis soit remplacer soit retirer la correction, pas les deux'}), 400
    hors_serie_edit = 1 if (request.form.get('hors_serie') or '') == '1' else 0
    conn = db()
    try:
        row = conn.execute('SELECT fiche_file, correction_file, bareme_file, code, hors_serie FROM forgecards WHERE numero=?', (numero,)).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        code = row['code'] or str(numero)
        if hors_serie_edit and not row['hors_serie']:
            existing_codes = [r[0] for r in conn.execute("SELECT code FROM forgecards WHERE hors_serie = 1").fetchall()]
            code = f"h{max([hs_sort_key(c) for c in existing_codes] + [0]) + 1}"
        elif not hors_serie_edit and row['hors_serie']:
            code = str(numero)
        if code != (row['code'] or str(numero)):
            for old_name, new_name in zip(
                    [row['fiche_file'], row['correction_file'], row['bareme_file']],
                    filenames_for(code)):
                old_path = UPLOADS / old_name if old_name else None
                if old_name and old_name != new_name and old_path.exists():
                    old_path.replace(UPLOADS / new_name)
            conn.execute(
                'UPDATE forgecards SET fiche_file=?, correction_file=?, bareme_file=? WHERE numero=?',
                (filenames_for(code)[0],
                 filenames_for(code)[1] if row['correction_file'] else '',
                 filenames_for(code)[2] if row['bareme_file'] else '',
                 numero))
            row = conn.execute('SELECT fiche_file, correction_file, bareme_file, code, hors_serie FROM forgecards WHERE numero=?', (numero,)).fetchone()
        fiche_name, corr_name, bareme_name = filenames_for(code)
        final_fiche_name = row['fiche_file']
        final_corr_name = row['correction_file']
        final_bareme_name = row['bareme_file']
        if fiche:
            fiche.save(UPLOADS / fiche_name)
            final_fiche_name = fiche_name
        if correction:
            correction.save(UPLOADS / corr_name)
            final_corr_name = corr_name
        elif remove_correction:
            remove_pdf_if_exists(row['correction_file'])
            final_corr_name = ''
        if bareme:
            bareme.save(UPLOADS / bareme_name)
            final_bareme_name = bareme_name
        elif remove_bareme:
            remove_pdf_if_exists(row['bareme_file'])
            final_bareme_name = ''
        conn.execute(
            'UPDATE forgecards SET titre=?, indices=?, chapitre=?, fiche_file=?, correction_file=?, bareme_file=?, teacher_difficulty=?, hors_serie=?, code=? WHERE numero=?',
            (titre, indices, chapitre, final_fiche_name, final_corr_name, final_bareme_name, teacher_difficulty,
             hors_serie_edit, code, numero)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True, 'numero': numero, 'code': code, 'label': card_label(code, hors_serie_edit)})

@bp.route("/api/forgecards/<int:numero>/reset-stats", methods=["POST"])
@require_admin
def reset_forgecard_stats(numero):
    ensure_db()
    conn = db()

    exists = conn.execute(
        "SELECT 1 FROM forgecards WHERE numero = ?",
        (numero,),
    ).fetchone()

    if not exists:
        conn.close()
        return jsonify(ok=False, error="fiche introuvable"), 404

    # Preserver les jours d'activite de tous les eleves (sinon leurs streaks
    # cassent retroactivement) puis archiver les reviews pour la tracabilite.
    conn.execute(
        "INSERT OR IGNORE INTO sr_daily_streak(prenom, day, validated) "
        "SELECT DISTINCT prenom, substr(created_at,1,10), 1 FROM reviews WHERE numero=?",
        (numero,),
    )
    conn.execute(
        "INSERT INTO reviews_archive(numero, prenom, result, note, duration_seconds, was_new, created_at, note_masquee) "
        "SELECT numero, prenom, result, note, duration_seconds, was_new, created_at, note_masquee FROM reviews WHERE numero=?",
        (numero,),
    )
    conn.execute(
        "DELETE FROM reviews WHERE numero = ?",
        (numero,),
    )

    conn.execute(
        "DELETE FROM sr_state_user WHERE numero = ?",
        (numero,),
    )

    conn.commit()
    conn.close()

    return jsonify(ok=True, numero=numero)

@bp.route('/api/forgecards/stats/export', methods=['GET'])
@require_admin
def forgecards_stats_export():
    ensure_db()
    conn = db()
    cards = conn.execute('SELECT numero, titre, chapitre FROM forgecards ORDER BY numero ASC').fetchall()

    stats_rows = conn.execute(
        "SELECT numero, "
        "COUNT(*) as total_revisions, "
        "COUNT(DISTINCT prenom) as nb_eleves_distincts, "
        "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) as nb_again, "
        "SUM(CASE WHEN result='hard' THEN 1 ELSE 0 END) as nb_hard, "
        "SUM(CASE WHEN result='good' THEN 1 ELSE 0 END) as nb_good, "
        "SUM(CASE WHEN result='easy' THEN 1 ELSE 0 END) as nb_easy, "
        "AVG(duration_seconds) as avg_duration, "
        "MIN(created_at) as premiere_revision, "
        "MAX(created_at) as derniere_revision "
        "FROM reviews GROUP BY numero"
    ).fetchall()
    stats_by_numero = {r['numero']: r for r in stats_rows}

    avg_rows = conn.execute(
        "SELECT numero, AVG(difficulty) as avg_difficulty, AVG(stability) as avg_stability, "
        "SUM(lapses) as total_lapses, COUNT(*) as nb_decks "
        "FROM sr_state_user WHERE difficulty IS NOT NULL GROUP BY numero"
    ).fetchall()
    avg_by_numero = {r['numero']: r for r in avg_rows}
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Numéro de la fiche', 'Titre', 'Chapitre', 'Nb élèves différents ayant révisé', 'Nb de révisions',
        'Nb pas réussi', 'Nb difficile', 'Nb j ai dû réfléchir', 'Nb ultra facile',
        'Taux d échec (%)', 'Durée moyenne (secondes)', 'Difficulté moyenne', 'Stabilité moyenne', 'Total d oublie cumulés (implique recommencer FSRS)',
        'Première révision en date', 'Dernière révision en date'
    ])
    for c in cards:
        numero = c['numero']
        s = stats_by_numero.get(numero)
        a = avg_by_numero.get(numero)
        total = s['total_revisions'] if s else 0
        nb_again = s['nb_again'] if s else 0
        taux_echec = round(100 * nb_again / total, 1) if total else ''
        writer.writerow([
            numero, csv_safe(c['titre'] or ''), csv_safe(c['chapitre'] or ''),
            s['nb_eleves_distincts'] if s else 0,
            total, nb_again,
            s['nb_hard'] if s else 0,
            s['nb_good'] if s else 0,
            s['nb_easy'] if s else 0,
            taux_echec,
            round(s['avg_duration'], 0) if s and s['avg_duration'] is not None else '',
            round(a['avg_difficulty'], 2) if a and a['avg_difficulty'] is not None else '',
            round(a['avg_stability'], 2) if a and a['avg_stability'] is not None else '',
            a['total_lapses'] if a else 0,
            s['premiere_revision'] if s else '',
            s['derniere_revision'] if s else '',
        ])

    return csv_response(output, 'Statistiques_moyennes_par_fiche.csv')

@bp.route('/api/dashboard/weak-cards', methods=['GET'])
@require_admin
def dashboard_weak_cards():

    ensure_db()
    chapitre_filter = request.args.get('chapitre')
    conn = db()
    query = (
        "SELECT f.numero, f.titre, f.chapitre, "
        "COALESCE(rv.total_revisions, 0) as total_revisions, "
        "COALESCE(rv.nb_eleves, 0) as nb_eleves, "
        "COALESCE(rv.nb_again, 0) as nb_again, "
        "rv.avg_duration as avg_duration, "
        "s.avg_difficulty as avg_difficulty "
        "FROM forgecards f "
        "LEFT JOIN ("
        "SELECT numero, COUNT(*) as total_revisions, COUNT(DISTINCT prenom) as nb_eleves, "
        "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) as nb_again, "
        "AVG(duration_seconds) as avg_duration "
        "FROM reviews WHERE prenom != 'admin' GROUP BY numero"
        ") rv ON rv.numero = f.numero "
        "LEFT JOIN ("
        "SELECT numero, AVG(difficulty) as avg_difficulty "
        "FROM sr_state_user WHERE difficulty IS NOT NULL AND prenom != 'admin' GROUP BY numero"
        ") s ON s.numero = f.numero "
    )
    params_sql = []
    if chapitre_filter:
        query += "WHERE f.chapitre = ? "
        params_sql.append(chapitre_filter)
    query += "ORDER BY f.numero ASC"
    rows = conn.execute(query, params_sql).fetchall()
    conn.close()

    out = []
    for r in rows:
        total = r['total_revisions'] or 0
        taux_echec = round(100 * (r['nb_again'] or 0) / total, 1) if total else 0
        out.append({
            'numero': r['numero'], 'titre': r['titre'], 'chapitre': r['chapitre'],
            'total_revisions': total, 'nb_eleves': r['nb_eleves'] or 0,
            'taux_echec_pct': taux_echec,
            'avg_duration_seconds': round(r['avg_duration'], 0) if r['avg_duration'] is not None else None,
            'avg_difficulty': round(r['avg_difficulty'], 2) if r['avg_difficulty'] is not None else None,
        })
    out.sort(key=lambda c: (-c['taux_echec_pct'], -c['total_revisions']))
    return jsonify(out)

@bp.route('/api/dashboard/failure-notes', methods=['GET'])
@require_admin
def dashboard_failure_notes():

    ensure_db()
    conn = db()
    rows = conn.execute(
        "SELECT rv.id, rv.created_at, rv.numero, f.titre, rv.prenom, rv.note, rv.note_masquee "
        "FROM reviews rv LEFT JOIN forgecards f ON f.numero = rv.numero "
        "WHERE rv.result = 'again' AND rv.note != '' "
        "ORDER BY rv.created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@bp.route('/api/reviews/<int:review_id>/note', methods=['DELETE'])
@require_admin
def delete_failure_note(review_id):
    ensure_db()
    conn = db()
    cur = conn.execute("UPDATE reviews SET note='' WHERE id=?", (review_id,))
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        return jsonify({'ok': False, 'error': 'note introuvable'}), 404
    return jsonify({'ok': True})

def _set_note_masquee(review_id, valeur):
    ensure_db()
    conn = db()
    cur = conn.execute("UPDATE reviews SET note_masquee=? WHERE id=?", (valeur, review_id))
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        return jsonify({'ok': False, 'error': 'note introuvable'}), 404
    return jsonify({'ok': True})

@bp.route('/api/reviews/<int:review_id>/note/masquer', methods=['POST'])
@require_admin
def masquer_failure_note(review_id):
    return _set_note_masquee(review_id, 1)

@bp.route('/api/reviews/<int:review_id>/note/restaurer', methods=['POST'])
@require_admin
def restaurer_failure_note(review_id):
    return _set_note_masquee(review_id, 0)

@bp.route('/api/admin/stats/overview', methods=['GET'])
@require_admin
def admin_stats_overview():
    ensure_db()
    conn = db()
    today = now_paris().date()
    eight_weeks_ago = (today - timedelta(days=56)).isoformat()

    users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin' ORDER BY prenom").fetchall()]
    active_days = {}
    for r in conn.execute(
            "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n FROM events "
            "WHERE type IN ('login','sr_open','review') AND created_at >= ? GROUP BY prenom, d",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['d'])
    for r in conn.execute(
            "SELECT prenom, substr(created_at,1,10) AS d FROM reviews WHERE created_at >= ? GROUP BY prenom, d",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['d'])
    for r in conn.execute(
            "SELECT prenom, day FROM sr_daily_streak WHERE validated=1 AND day >= ?",
            (eight_weeks_ago,)).fetchall():
        active_days.setdefault(r['prenom'], set()).add(r['day'])
    weeks = [(today - timedelta(days=7 * k)).isoformat() for k in range(8)][::-1]
    assiduite = []
    for p in users:
        days = active_days.get(p, set())
        row = {'prenom': p, 'total': len(days)}
        for wk in weeks:
            w0 = datetime.strptime(wk, '%Y-%m-%d').date()
            row[wk] = sum(1 for d in days if w0 - timedelta(days=6) <= datetime.strptime(d, '%Y-%m-%d').date() <= w0)
        assiduite.append(row)

    heures = [0] * 24
    for r in conn.execute(
            "SELECT CAST(substr(created_at,12,2) AS INTEGER) AS h, COUNT(*) AS n "
            "FROM reviews GROUP BY h").fetchall():
        if r['h'] is not None:
            heures[r['h']] = r['n']

    qcm_mois = [dict(r) for r in conn.execute(
        "SELECT substr(created_at,1,7) AS mois, chapitre, COUNT(*) AS n, SUM(ok) AS bonnes "
        "FROM qcm_answers GROUP BY mois, chapitre ORDER BY mois, chapitre").fetchall()]

    ret = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok "
        "FROM reviews WHERE was_new = 0").fetchone()
    retention_reelle = round(100 * (ret['ok'] or 0) / ret['n'], 1) if ret['n'] else None

    two_months_ago = (today - timedelta(days=60)).isoformat()
    ret_rows = conn.execute(
        "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n, "
        "SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok "
        "FROM reviews WHERE was_new = 0 AND created_at >= ? "
        "GROUP BY d ORDER BY d",
        (two_months_ago,)
    ).fetchall()
    retention_jours = [
        {'d': r['d'], 'n': r['n'], 'taux': round(100 * (r['ok'] or 0) / r['n'], 1)}
        for r in ret_rows if r['n']
    ]

    conn.close()
    return jsonify({'ok': True, 'semaines': weeks, 'assiduite': assiduite,
                    'heures': heures, 'qcm_mois': qcm_mois,
                    'retention_reelle': retention_reelle,
                    'retention_jours': retention_jours})

@bp.route('/api/admin/stats/export/<kind>', methods=['GET'])
@require_admin
def admin_stats_export(kind):
    ensure_db()
    conn = db()
    output = io.StringIO()
    writer = csv.writer(output)

    if kind == 'activite-quotidienne':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        rev = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n, "
                "SUM(CASE WHEN result='again' THEN 1 ELSE 0 END) AS again, "
                "SUM(CASE WHEN result='hard' THEN 1 ELSE 0 END) AS hard, "
                "SUM(CASE WHEN result='good' THEN 1 ELSE 0 END) AS good, "
                "SUM(CASE WHEN result='easy' THEN 1 ELSE 0 END) AS easy, "
                "SUM(duration_seconds) AS duree "
                "FROM reviews GROUP BY prenom, d").fetchall():
            rev[(r['prenom'], r['d'])] = r
        ev = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, type, COUNT(*) AS n "
                "FROM events GROUP BY prenom, d, type").fetchall():
            ev.setdefault((r['prenom'], r['d']), {})[r['type']] = r['n']
        qcm = {}
        for r in conn.execute(
                "SELECT prenom, substr(created_at,1,10) AS d, COUNT(*) AS n, SUM(ok) AS bonnes "
                "FROM qcm_answers GROUP BY prenom, d").fetchall():
            qcm[(r['prenom'], r['d'])] = r
        writer.writerow(['Eleve', 'Date', 'Revisions', 'Echecs', 'Difficiles', 'Reussies', 'Automatiques',
                         'Duree totale (s)', 'Connexions', 'Ouvertures SR', 'Tirages', 'Reports',
                         'Jokers depenses', 'Relances recues', 'Reponses QCM', 'Bonnes QCM'])
        jours = sorted({d for (p, d) in rev} | {d for (p, d) in ev} | {d for (p, d) in qcm})
        for p in users:
            for d in jours:
                rv, e, q = rev.get((p, d)), ev.get((p, d), {}), qcm.get((p, d))
                if rv is None and not e and q is None:
                    continue
                writer.writerow([csv_safe(p), d,
                                 rv['n'] if rv else 0, rv['again'] if rv else 0,
                                 rv['hard'] if rv else 0, rv['good'] if rv else 0,
                                 rv['easy'] if rv else 0,
                                 rv['duree'] if rv and rv['duree'] else '',
                                 e.get('login', 0), e.get('sr_open', 0), e.get('draw', 0),
                                 e.get('advance', 0), e.get('joker_spent', 0), e.get('reminder', 0),
                                 q['n'] if q else 0, (q['bonnes'] or 0) if q else 0])
        conn.close()
        return csv_response(output, 'activite_quotidienne.csv')

    if kind == 'qcm-par-mois':
        writer.writerow(['Mois', 'Theme', 'Chapitre', 'Question', 'Sorties', 'Bonnes', 'Taux echec (%)'])
        for r in conn.execute(
                "SELECT substr(created_at,1,7) AS mois, theme, chapitre, question, "
                "COUNT(*) AS n, SUM(ok) AS bonnes FROM qcm_answers "
                "GROUP BY mois, qid ORDER BY mois, theme, chapitre").fetchall():
            n = r['n']
            writer.writerow([r['mois'], csv_safe(r['theme'] or ''), csv_safe(r['chapitre'] or ''),
                             csv_safe(r['question'] or '')[:120], n, r['bonnes'] or 0,
                             round(100 * (n - (r['bonnes'] or 0)) / n, 1) if n else ''])
        conn.close()
        return csv_response(output, 'qcm_par_mois.csv')

    if kind == 'synthese-eleves':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        writer.writerow(['Eleve', 'Jours actifs', 'Plus longue serie', 'Serie en cours',
                         'Revisions totales', 'Taux reussite (%)', 'Temps total (min)',
                         'Jokers gagnes', 'Jokers depenses', 'Relances recues',
                         'Parties QCM jouees', 'Reponses QCM', 'Taux reussite QCM (%)'])
        for p in users:
            days = activity_days(conn, p)
            record = compute_record(days)
            streak = compute_streak(conn, p)
            rv = conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN result IN ('good','easy') THEN 1 ELSE 0 END) AS ok, "
                "SUM(duration_seconds) AS duree FROM reviews WHERE prenom=?", (p,)).fetchone()
            ev = {r['type']: r['n'] for r in conn.execute(
                "SELECT type, COUNT(*) AS n FROM events WHERE prenom=? GROUP BY type", (p,)).fetchall()}
            q = conn.execute(
                "SELECT COUNT(*) AS n, SUM(ok) AS bonnes FROM qcm_answers WHERE prenom=?", (p,)).fetchone()
            writer.writerow([csv_safe(p), len(days), record, streak,
                             rv['n'], round(100 * (rv['ok'] or 0) / rv['n'], 1) if rv['n'] else '',
                             round((rv['duree'] or 0) / 60, 1) if rv['duree'] else '',
                             ev.get('joker_awarded', 0), ev.get('joker_spent', 0), ev.get('reminder', 0),
                             ev.get('qcm_played', 0), q['n'],
                             round(100 * (q['bonnes'] or 0) / q['n'], 1) if q['n'] else ''])
        conn.close()
        return csv_response(output, 'synthese_eleves.csv')

    if kind == 'etude-revisions':
        users = [r['prenom'] for r in conn.execute("SELECT prenom FROM users WHERE prenom != 'admin'").fetchall()]
        writer.writerow(['Eleve', 'Date', 'Numero fiche', 'Chapitre', 'Resultat', 'Duree (s)',
                         'Jours depuis la revision precedente', 'Retrievabilite FSRS avant (%)',
                         'Stabilite apres (j)', 'Difficulte apres', 'N-ieme revision de la fiche'])
        for p in users:
            rows = conn.execute(
                'SELECT rv.created_at, rv.numero, f.chapitre, rv.result, rv.duration_seconds '
                'FROM reviews rv LEFT JOIN forgecards f ON f.numero = rv.numero '
                'WHERE rv.prenom = ? ORDER BY rv.created_at ASC, rv.id ASC', (p,)).fetchall()
            for r, snap in zip(rows, replay_reviews(conn, p, rows)):
                writer.writerow([csv_safe(p), r['created_at'], r['numero'], csv_safe(r['chapitre'] or ''),
                                 r['result'], r['duration_seconds'] if r['duration_seconds'] is not None else '',
                                 snap['elapsed_days'] if snap['elapsed_days'] is not None else '',
                                 round(100 * snap['r_before'], 1) if snap['r_before'] is not None else '',
                                 round(snap['stability'], 2) if snap['stability'] is not None else '',
                                 round(snap['difficulty'], 2) if snap['difficulty'] is not None else '',
                                 snap['repetitions']])
        conn.close()
        return csv_response(output, 'etude_revisions.csv')

    conn.close()
    return jsonify({'ok': False, 'error': 'export inconnu'}), 404

