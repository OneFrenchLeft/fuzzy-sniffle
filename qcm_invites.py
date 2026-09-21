# Xiao: One-time QCM invitations. Keep token handling boring and predictable.
from flask import Blueprint, render_template, request, session, redirect
import secrets
from datetime import datetime, timedelta
from config import QCM_INVITE_TTL_MINUTES, now_paris
from db import db, ensure_db, log_event

bp = Blueprint('qcm_invites', __name__)

def create_qcm_invite(prenom, ttl_minutes=QCM_INVITE_TTL_MINUTES):
    # Small: Validate the database before creating anything.
    ensure_db()

    prenom = (prenom or '').strip()
    if not prenom:
        raise ValueError('prenom requis')

    conn = db()

    exists = conn.execute(
        'SELECT 1 FROM users WHERE prenom=?',
        (prenom,)
    ).fetchone()

    if not exists:
        # Ethan: Don't leave an open connection on validation failure.
        conn.close()
        raise ValueError('prenom inconnu')

    try:
        ttl_minutes = int(ttl_minutes)
    except (TypeError, ValueError):
        # Steph: Bad TTL input gets the configured default. Revolutionary.
        ttl_minutes = QCM_INVITE_TTL_MINUTES

    ttl_minutes = max(1, min(120, ttl_minutes))
    now = now_paris()
    expires = now + timedelta(minutes=ttl_minutes)
    token = secrets.token_urlsafe(32)

    conn.execute(
        '''
        INSERT INTO qcm_invites(token, prenom, created_at, expires_at, used_at)
        VALUES(?,?,?,?,NULL)
        ''',
        (token, prenom, now.isoformat(), expires.isoformat())
    )

    # Flying: Clean old invitations after creating the new one.
    conn.execute(
        '''
        DELETE FROM qcm_invites
        WHERE expires_at < ?
           OR (used_at IS NOT NULL AND used_at < ?)
        ''',
        (
            now.isoformat(),
            (now - timedelta(days=1)).isoformat(),
        )
    )

    conn.commit()
    conn.close()

    return token, expires

def get_valid_qcm_invite(token):
    # Eliot: Read-only validation; consuming the token happens on POST.
    ensure_db()

    conn = db()
    row = conn.execute(
        """
        SELECT token, prenom, expires_at, used_at
        FROM qcm_invites
        WHERE token=?
        """,
        (token,)
    ).fetchone()
    conn.close()

    if not row:
        return None, "Ce lien QCM est introuvable.", 404

    if row['used_at'] is not None:
        return None, "Ce lien QCM a déjà été utilisé.", 410

    try:
        expired = datetime.fromisoformat(row['expires_at']) <= now_paris()
    except (TypeError, ValueError):
        # Xiao: Invalid expiry means invalid invite. Don't get creative here.
        expired = True

    if expired:
        return None, "Ce lien QCM a expiré. Demande un nouveau lien.", 410

    return row, None, 200

@bp.route('/qcm/join/<token>', methods=['GET'])
def qcm_join_with_invite(token):
    # Small: GET only checks the token; it must not consume it.
    row, error, status = get_valid_qcm_invite(token)

    if error:
        return render_template(
            'qcm_invite_error.html',
            message=error
        ), status

    return render_template(
        'qcm_invite_confirm.html',
        token=token,
        prenom=row['prenom']
    )

@bp.route('/qcm/join/<token>', methods=['POST'])
def consume_qcm_invite(token):
    # Ethan: Actual consumption belongs here, not in the confirmation page.
    ensure_db()

    now = now_paris()
    conn = db()
    row = conn.execute(
        """
        SELECT prenom, expires_at, used_at
        FROM qcm_invites
        WHERE token=?
        """,
        (token,)
    ).fetchone()

    if not row:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM est introuvable."
        ), 404

    if row['used_at'] is not None:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM a déjà été utilisé."
        ), 410

    try:
        expired = datetime.fromisoformat(row['expires_at']) <= now
    except (TypeError, ValueError):
        # Steph: Same rule as GET. Invalid dates are expired.
        expired = True

    if expired:
        conn.close()
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM a expiré. Demande un nouveau lien."
        ), 410

    # Flying: Atomic update prevents two requests from consuming the same token.
    cur = conn.execute(
        """
        UPDATE qcm_invites
        SET used_at=?
        WHERE token=? AND used_at IS NULL
        """,
        (now.isoformat(), token)
    )
    conn.commit()
    conn.close()

    if cur.rowcount != 1:
        # Eliot: Someone got there first. Good thing we checked rowcount.
        return render_template(
            'qcm_invite_error.html',
            message="Ce lien QCM vient déjà d'être utilisé."
        ), 410

    # Xiao: Fresh session after accepting the invite.
    session.clear()
    session['sr_user'] = row['prenom']
    session.permanent = True
    try:
        conn = db()
        log_event(conn, row['prenom'], 'invite_used')
        conn.commit()
        conn.close()
    except Exception:
        # Small: Logging failure shouldn't prevent a valid invite from working.
        pass

    return redirect('/qcm')
