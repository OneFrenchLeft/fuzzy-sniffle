#!/usr/bin/env python3
# fix_qcm_purge.py — bouton "Purger les questions retirees" dans les stats QCM admin.
# Usage : cd /var/www/tireur-dev && python3 fix_qcm_purge.py
import sys, shutil, py_compile
from pathlib import Path

ROOT = Path.cwd()
OK, SKIP, FAIL = [], [], []

# ---------------- app.py : endpoint purge ----------------
PY = ROOT / 'app.py'
text = PY.read_text(encoding='utf-8')

ROUTE = '''

@app.route('/api/admin/qcm/purge-retired', methods=['POST'])
@require_admin
def admin_qcm_purge_retired():
    # Supprime de qcm_answers les lignes dont la question n'existe plus
    # dans les fichiers (theme/chapitre filtres respectes).
    ensure_db()
    theme = request.args.get('theme', '')
    chapitre = request.args.get('chapitre', '')
    themes = [theme] if theme in QCM_FILES else list(QCM_FILES)
    active_qids = set()
    for t in themes:
        path = QCM_FILES[t]
        if not path.exists():
            continue
        for q in qcm_engine.read_qcm_questions(path, theme=t):
            if chapitre and (q.get('chapitre') or 'Autre') != chapitre:
                continue
            if q.get('qid'):
                active_qids.add(q['qid'])
    clauses, args = [], []
    if theme in QCM_FILES:
        clauses.append('theme = ?')
        args.append(theme)
    if chapitre:
        clauses.append('chapitre = ?')
        args.append(chapitre)
    where = (' AND '.join(clauses)) if clauses else '1=1'
    conn = db()
    rows = conn.execute(f'SELECT DISTINCT qid FROM qcm_answers WHERE {where}', args).fetchall()
    qids = [r['qid'] for r in rows if r['qid'] not in active_qids]
    deleted = 0
    if qids:
        marks = ','.join('?' * len(qids))
        cur = conn.execute(
            f'DELETE FROM qcm_answers WHERE qid IN ({marks}) AND {where}',
            qids + args)
        deleted = cur.rowcount
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted, 'questions': len(qids)})
'''

marker_py = 'purge-retired'
anchor_py = "@app.route('/api/admin/qcm/games', methods=['GET'])"
if marker_py in text:
    SKIP.append('app.py route purge')
elif text.count(anchor_py) != 1:
    FAIL.append('app.py ancre route (introuvable)')
else:
    text = text.replace(anchor_py, ROUTE.lstrip('\n') + '\n' + anchor_py, 1)
    shutil.copy(PY, str(PY) + '.bak-purge')
    PY.write_text(text, encoding='utf-8')
    OK.append('app.py route purge')
try:
    py_compile.compile(str(PY), doraise=True)
    print('app.py : syntaxe OK')
except py_compile.PyCompileError as e:
    FAIL.append('app.py SYNTAXE')
    print(e)

# ---------------- app.js : bouton + appel ----------------
JS = None
for cand in ['static/js/app.js', 'static/app.js']:
    p = ROOT / cand
    if p.exists():
        JS = p
        break
if JS is None:
    FAIL.append('app.js introuvable')
else:
    text = JS.read_text(encoding='utf-8')
    orig = text

    name = 'js: bouton purge dans renderWeakQcm'
    marker = 'purgeQcmRetired'
    old = "  box.innerHTML = html;\n}\n\nvar MAX_UPLOAD_"
    new = ("  var retiredCount = weakQcmRows.filter(function (r) { return r.absente; }).length;\n"
           "  if (retiredCount > 0) {\n"
           "    html += '<p style=\"margin-top:.4rem\"><button class=\"btn-ghost btn-small\" onclick=\"purgeQcmRetired()\">Purger les ' + retiredCount + ' question' + (retiredCount > 1 ? 's' : '') + ' retiree' + (retiredCount > 1 ? 's' : '') + '</button></p>';\n"
           "  }\n"
           "  box.innerHTML = html;\n}\n\nvar MAX_UPLOAD_")
    if marker in text:
        SKIP.append(name)
    elif text.count(old) != 1:
        FAIL.append(name + ' (ancre introuvable)')
    else:
        text = text.replace(old, new, 1)
        OK.append(name)

    name = 'js: fonction purgeQcmRetired'
    old = "function exportCSV() { window.location.href = '/api/sr/export'; }"
    fn_lines = [
        "function purgeQcmRetired() {",
        "  var subjectSel = document.getElementById('qcm-weak-subject');",
        "  var chapterSel = document.getElementById('qcm-weak-chapter');",
        "  var theme = subjectSel ? subjectSel.value : '';",
        "  var chapitre = chapterSel ? chapterSel.value : '';",
        "  var url = '/api/admin/qcm/purge-retired?theme=' + encodeURIComponent(theme) +",
        "            '&chapitre=' + encodeURIComponent(chapitre);",
        "  askConfirm('Supprimer definitivement les stats des questions retirees du fichier QCM' +",
        "             (chapitre ? ' (chapitre ' + chapitre + ')' : '') +",
        "             ' ? Cela efface leur historique de reponses pour tous les eleves.', function () {",
        "    adminAction(url, 'POST', function (res) {",
        "      loadWeakQcm();",
        "      showToast('Purge : ' + (res.deleted || 0) + ' reponses supprimees (' + (res.questions || 0) + ' questions).');",
        "    }, 'purge impossible');",
        "  });",
        "}",
        "",
        "",
    ]
    new = '\n'.join(fn_lines) + old
    if "askConfirm('Supprimer definitivement les stats" in text:
        SKIP.append(name)
    elif text.count(old) != 1:
        FAIL.append(name + ' (ancre introuvable)')
    else:
        text = text.replace(old, new, 1)
        OK.append(name)

    if text != orig:
        shutil.copy(JS, str(JS) + '.bak-purge')
        JS.write_text(text, encoding='utf-8')

    node = shutil.which('node')
    if node and text != orig:
        import subprocess
        r = subprocess.run([node, '--check', str(JS)], capture_output=True, text=True)
        if r.returncode != 0:
            FAIL.append('app.js SYNTAXE')
            print(r.stderr)
        else:
            print('app.js : syntaxe OK')

print()
print('APPLIQUE :', len(OK))
for a in OK: print('  +', a)
if SKIP:
    print('SAUTE :', len(SKIP))
    for s in SKIP: print('  =', s)
if FAIL:
    print('ECHECS :', len(FAIL))
    for f in FAIL: print('  !', f)
    sys.exit(1)
print()
print('Termine. Redemarre : sudo systemctl restart gunicorn  (et monte v= dans base.html si le bouton n apparait pas)')
