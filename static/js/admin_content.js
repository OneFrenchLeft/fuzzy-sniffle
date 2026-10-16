/* Visual content tools share the canonical bank with the existing JSON editor. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var names = { physique: 'Physique', chimie: 'Chimie', maths: 'Mathématiques' };
  // Labels du registre matieres (window.qcmSubjects, rempli par loadParams) ;
  // repli sur `names` puis sur la cle brute.
  function labelFor(theme) {
    var list = window.qcmSubjects || [];
    for (var i = 0; i < list.length; i++) { if (list[i].key === theme) return list[i].label; }
    return names[theme] || theme;
  }
  var pageSize = 20, qPage = 0, lastBuffer = '', importDraft = null, formState = null;
  var importVersion = 0, jsonFileVersion = 0;
  var activationPending = false;
  var deleteState = null;
  var qSearchScope = '';

  function chapter(q) { return String(q.chapitre || 'Autre').trim() || 'Autre'; }
  function currentSubject() { return $('qcm-subject-select').value; }
  function path(theme, ch, emptyLabel) { return labelFor(theme) + ' > ' + (ch || emptyLabel || 'Tous les chapitres'); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function ready() { return qcmLoadedSubject === currentSubject(); }
  function dirty() { return $('qcm-json-editor').value !== lastBuffer; }
  function friendly(message) { return String(message || 'Impossible de réaliser cette opération.').replace(/reponse/g, 'bonne réponse').replace(/tableau choix requis/g, 'ajoute les réponses possibles'); }
  async function api(url, options) {
    var response;
    try { response = await fetchWithStatus(url, options || { cache: 'no-store' }); }
    catch (error) { throw new Error(qcmRequestError(error)); }
    var data = response.data || {};
    if (!data.ok) {
      var error = new Error(friendly(data.error || 'Impossible de réaliser cette opération.'));
      error.status = response.status; error.fields = data.fields || {}; throw error;
    }
    return data;
  }
  function send(url, method, data) {
    return api(url, { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), cache: 'no-store' });
  }
  function button(text, action, primary) {
    var b = document.createElement('button'); b.type = 'button';
    b.className = (primary ? 'btn-save' : 'btn-ghost') + ' btn-small'; b.textContent = text; b.onclick = action; return b;
  }
  function empty(target, text) { target.innerHTML = ''; var p = document.createElement('p'); p.className = 'content-empty'; p.textContent = text; target.appendChild(p); }
  function matches(q, text) { return normalizeName([q.question, chapter(q), (q.choix || []).map(function (c) { return normalizeQcmPreviewChoice(c).text; }).join(' ')].join(' ')).indexOf(normalizeName(text)) !== -1; }
  function paginate(target, total, page, callback) {
    target.innerHTML = ''; if (total <= pageSize) return;
    var back = button('← Précédent', function () { callback(page - 1); }); back.disabled = page === 0;
    var next = button('Suivant →', function () { callback(page + 1); }); next.disabled = (page + 1) * pageSize >= total;
    var label = document.createElement('span'); label.textContent = 'Page ' + (page + 1) + ' / ' + Math.ceil(total / pageSize);
    target.append(back, label, next);
  }
  function questionCard(q) {
    var el = document.createElement('article'); el.className = 'content-question'; el.dataset.qid = q.qid || '';
    var title = document.createElement('h4'); title.textContent = q.question + (q.latex ? '\n$$' + q.latex + '$$' : ''); el.appendChild(title);
    var meta = document.createElement('p'); meta.className = 'hint';
    var rich = [q].concat((q.choix || []).filter(function (c) { return c && typeof c === 'object'; }));
    meta.textContent = chapter(q) + ' · ' + (q.temps || 30) + ' s' + (rich.some(function (c) { return c.image; }) ? ' · Illustration' : '') + (rich.some(function (c) { return c.latex; }) || /\$|\\\(/.test(q.question) ? ' · Formule' : ''); el.appendChild(meta);
    if (qcmQuestionImage(q)) appendPreviewImg(el, qcmQuestionImage(q), 'Illustration de la question');
    var list = document.createElement('ol'); list.className = 'content-choices'; list.type = 'A';
    (q.choix || []).forEach(function (choice, i) {
      var c = normalizeQcmPreviewChoice(choice), li = document.createElement('li'); li.textContent = c.text;
      if (i + 1 === Number(q.reponse)) { li.className = 'content-correct'; li.appendChild(document.createTextNode(' — Bonne réponse')); }
      if (c.image) appendPreviewImg(li, c.image, 'Illustration de la réponse'); list.appendChild(li);
    }); el.appendChild(list);
    if (q.explication) { var explanation = document.createElement('p'); explanation.className = 'hint'; explanation.textContent = q.explication; el.appendChild(explanation); }
    return el;
  }
  function chaptersOverview(target, rows, theme, selected, choose) {
    target.innerHTML = '';
    if (!rows.length) { empty(target, 'Aucun chapitre disponible. Crée le premier chapitre ci-dessous.'); return; }
    var total = rows.reduce(function (sum, row) { return sum + row.qcm_count; }, 0);
    var allRow = document.createElement('div'); allRow.className = 'chapter-row' + (selected === '' ? ' selected' : '');
    var allPick = document.createElement('button'); allPick.type = 'button'; allPick.className = 'chapter-pick';
    allPick.disabled = selected === '';
    allPick.onclick = function () { choose(''); };
    var allName = document.createElement('span'); allName.className = 'chapter-name'; allName.textContent = 'Tous les chapitres';
    var allCount = document.createElement('span'); allCount.className = 'chapter-count'; allCount.textContent = plural(total, 'question', 'questions');
    allPick.append(allName, allCount); allRow.appendChild(allPick); target.appendChild(allRow);
    rows.forEach(function (row) {
      var line = document.createElement('div'); line.className = 'chapter-row' + (row.chapitre === selected ? ' selected' : '');
      var pick = document.createElement('button'); pick.type = 'button'; pick.className = 'chapter-pick';
      pick.disabled = row.chapitre === selected;
      pick.onclick = function () { choose(row.chapitre); };
      var name = document.createElement('span'); name.className = 'chapter-name'; name.textContent = row.chapitre;
      var count = document.createElement('span'); count.className = 'chapter-count'; count.textContent = row.qcm_count;
      pick.append(name, count); line.appendChild(pick);
      var label = document.createElement('label'); label.className = 'chapter-toggle'; label.title = 'Actif pour les élèves (' + (rows.some(function (r) { return r.ankimie_theme; }) ? 'QCM et Ankimie' : 'QCM') + ')';
      var toggle = document.createElement('input'); toggle.type = 'checkbox';
      toggle.checked = row.qcm_active;
      toggle.disabled = activationPending;
      label.appendChild(toggle); line.appendChild(label);
      toggle.onchange = async function () {
        if (activationPending) return;
        var value = toggle.checked, version = qcmLoadVersion;
        activationPending = true;
        target.querySelectorAll('input[type=checkbox]').forEach(function (input) { input.disabled = true; });
        try {
          var data = await send('/api/admin/content/' + theme + '/chapters', 'PUT', { chapitre: row.chapitre, qcm_active: value });
          if (version === qcmLoadVersion && theme === currentSubject()) {
            qcmChapters = data.chapters; chaptersOverview(target, qcmChapters, theme, qcmTheme, choose);
          }
          showToast(value ? (data.chapters.some(function (r) { return r.ankimie_theme; }) ? 'Chapitre activé pour les élèves (QCM et Ankimie).' : 'Chapitre activé pour les élèves.') : 'Chapitre désactivé partout. Contenu et historique conservés.');
        } catch (error) { toggle.checked = !value; showToast(error.message); }
        finally { activationPending = false; target.querySelectorAll('input[type=checkbox]').forEach(function (input) { input.disabled = false; }); }
      };
      var del = document.createElement('button'); del.type = 'button'; del.className = 'chapter-del content-danger'; del.textContent = '×'; del.title = 'Supprimer le chapitre';
      del.onclick = function () { openDeleteChapter(row); };
      del.disabled = qcmSaving || dirty();
      line.appendChild(del); target.appendChild(line);
    });
  }
  function setQcmControls() {
    var disabled = !ready() || qcmSaving;
    ['qcm-theme-select', 'qcm-add-question', 'qcm-open-import', 'qcm-question-search', 'qcm-new-theme-input'].forEach(function (id) { $(id).disabled = disabled; });
    $('qcm-add-question').disabled = disabled || dirty(); $('qcm-json-editor').readOnly = disabled;
    $('qcm-chapter-overview').querySelectorAll('.content-danger').forEach(function (b) { b.disabled = disabled || dirty(); });
  }
  function renderQuestions() {
    var target = $('qcm-question-list'); target.innerHTML = '';
    updateQcmContext(); $('qcm-search-summary').textContent = '';
    if (!ready()) { empty(target, 'La banque n’a pas été chargée. Clique sur « Recharger la banque » pour réessayer.'); return; }
    var questions = qcmFullList.filter(function (q) { return !qcmTheme || chapter(q) === qcmTheme; });
    if (dirty()) {
      try {
        questions = JSON.parse($('qcm-json-editor').value); if (!Array.isArray(questions)) throw new Error();
        questions.forEach(function (q, i) { validateQcmPreviewQuestion(q, i + 1); });
      }
      catch (_) { empty(target, 'Les modifications avancées ne peuvent pas être prévisualisées. Corrige-les ou recharge la banque.'); return; }
    }
    questions = questions.filter(function (q) { return q && typeof q === 'object' && matches(q, $('qcm-question-search').value); });
    var search = $('qcm-question-search').value.trim();
    if (search) $('qcm-search-summary').textContent = plural(questions.length, 'résultat', 'résultats') + ' pour « ' + search + ' »';
    qPage = Math.min(qPage, Math.max(0, Math.ceil(questions.length / pageSize) - 1));
    if (!questions.length) empty(target, search ? 'Aucune question ne correspond à cette recherche. Efface la recherche pour retrouver les questions du chapitre.' : 'Aucune question dans ce périmètre. Ajoute une question ou importe un fichier.');
    questions.slice(qPage * pageSize, (qPage + 1) * pageSize).forEach(function (q) {
      var card = questionCard(q), actions = document.createElement('div'); actions.className = 'content-toolbar';
      var edit = button('Modifier', function () { openQuestion(q, false); });
      var copy = button('Dupliquer', function () { openQuestion(q, true); });
      var remove = button('Supprimer', function () { deleteQuestion(q); });
      [edit, copy, remove].forEach(function (b) { b.disabled = qcmSaving || dirty(); actions.appendChild(b); }); card.appendChild(actions); target.appendChild(card);
    });
    paginate($('qcm-question-pages'), questions.length, qPage, function (p) { qPage = p; renderQuestions(); });
    typesetMathKatex(target);
  }
  function render() {
    lastBuffer = $('qcm-json-editor').value; qPage = 0; clearImport();
    var searchScope = path(currentSubject(), qcmTheme);
    if (searchScope !== qSearchScope) { $('qcm-question-search').value = ''; qSearchScope = searchScope; }
    setQcmControls();
    chaptersOverview($('qcm-chapter-overview'), qcmChapters, currentSubject(), qcmTheme, function (ch) {
      if (!canSwitch()) return; qcmTheme = ch; qcmScopeVersion++; $('qcm-theme-select').value = ch; renderQcmEditorBuffer();
    }); renderQuestions();
  }
  function updateQcmContext() {
    $('qcm-current-path').textContent = path(currentSubject(), qcmTheme);
  }
  function canSwitch() { return !dirty() || confirm('Des modifications avancées ne sont pas enregistrées. Les abandonner pour changer de matière ou de chapitre ?'); }
  function acceptBank(data, subject, version, scope) {
    if (subject !== currentSubject() || version !== qcmLoadVersion || scope !== qcmScopeVersion) return false;
    qcmFullList = data.questions; qcmRevision = data.revision; qcmChapters = data.chapters;
    renderQcmThemeSelect(); renderQcmEditorBuffer(); return true;
  }
  async function createChapter() {
    var input = $('qcm-new-theme-input');
    if (input.disabled || !ready() || qcmSaving || !canSwitch()) return;
    var ch = input.value.trim(); if (!ch) { input.focus(); showToast('Donne un nom au chapitre.'); return; }
    var theme = currentSubject();
    var version = qcmLoadVersion; input.disabled = true;
    var scope = qcmScopeVersion;
    var startingBuffer = $('qcm-json-editor').value;
    try {
      var data = await send('/api/admin/content/' + theme + '/chapters', 'POST', { chapitre: ch });
      if (theme === currentSubject() && version === qcmLoadVersion && scope === qcmScopeVersion) {
        if (dirty() && $('qcm-json-editor').value !== startingBuffer && !canSwitch()) return;
        qcmChapters = data.chapters; qcmTheme = ch; qcmScopeVersion++; renderQcmThemeSelect(); renderQcmEditorBuffer();
        input.value = '';
      }
      showToast('Chapitre créé, inactif pour les élèves.');
    } catch (error) { showToast(error.message); }
    finally { setQcmControls(); }
  }
  function choiceValues(c) {
    return typeof c === 'string' ? { text: c, image: '', latex: '' } : { text: c.texte !== undefined ? c.texte : (c.text || ''), image: c.image || '', latex: c.latex || '' };
  }
  function questionTitle() {
    if (!formState) return;
    var action = formState.duplicate ? 'Dupliquer une question dans ' : (formState.qid ? 'Modifier une question dans ' : 'Ajouter une question à ');
    var ch = formState.chapter || $('content-question-chapter').value;
    $('content-question-title').textContent = action + (ch ? path(formState.subject, ch) : labelFor(formState.subject));
  }
  function captureChoices() {
    if (!formState) return;
    formState.choices = Array.from($('content-question-choices').querySelectorAll('.content-choice')).map(function (el) {
      return { text: el.querySelector('[data-choice-text]').value, image: el.querySelector('[data-choice-image]').value, latex: el.querySelector('[data-choice-latex]').value };
    });
  }
  function renderChoices() {
    var target = $('content-question-choices'); target.innerHTML = '';
    formState.choices.forEach(function (c, i) {
      var el = document.createElement('fieldset'); el.className = 'content-choice';
      var letter = 'ABCD'[i]; el.innerHTML = '<legend>Réponse ' + letter + '</legend>' +
        '<label class="content-choice-correct"><input type="radio" name="content-correct-answer" value="' + i + '" required' + (formState.answer === i ? ' checked' : '') + '> Bonne réponse</label>' +
        '<textarea data-choice-text aria-label="Texte de la réponse ' + letter + '">' + esc(c.text) + '</textarea>' +
        '<details><summary>Image ou formule pour cette réponse</summary>' +
        '<label>Image<input type="text" data-choice-image value="' + esc(c.image) + '" placeholder="https://…"></label>' +
        '<label>Formule mathématique (LaTeX)<textarea data-choice-latex spellcheck="false">' + esc(c.latex) + '</textarea></label></details>';
      el.querySelector('input[type=radio]').onchange = function () { formState.answer = i; formState.dirty = true; };
      var remove = button('Retirer cette réponse', function () {
        captureChoices(); formState.choices.splice(i, 1);
        if (formState.answer === i) formState.answer = null; else if (formState.answer > i) formState.answer--;
        formState.dirty = true; renderChoices();
      }); remove.disabled = formState.choices.length <= 2; el.appendChild(remove); target.appendChild(el);
    }); $('content-choice-add').disabled = formState.choices.length >= 4;
  }
  function clearFormErrors() {
    ['question', 'choix', 'reponse', 'temps'].forEach(function (field) { $('content-error-' + field).textContent = ''; });
    $('content-question-error').textContent = '';
    $('content-question-form').querySelectorAll('[aria-invalid]').forEach(function (el) { el.removeAttribute('aria-invalid'); });
  }
  function openQuestion(q, duplicate) {
    if (!ready() || qcmSaving || dirty()) return;
    q = q || { question: '', choix: ['', ''], temps: 30, chapitre: qcmTheme, reponse: null };
    formState = { qid: duplicate ? null : q.qid, subject: currentSubject(), version: qcmLoadVersion, scope: qcmScopeVersion,
      revision: qcmRevision, chapter: qcmTheme, duplicate: duplicate, choices: q.choix.map(choiceValues), answer: q.reponse ? Number(q.reponse) - 1 : null, dirty: false, saving: false };
    $('content-question-delete').hidden = !formState.qid;
    $('content-question-text').value = q.question + (duplicate ? ' (copie)' : '');
    $('content-question-chapter').innerHTML = '<option value="">Choisir un chapitre</option>' + qcmThemeList().map(function (ch) { return '<option value="' + esc(ch) + '">' + esc(ch) + '</option>'; }).join('');
    $('content-question-chapter').value = qcmTheme || q.chapitre || (q.qid ? 'Autre' : '');
    $('content-question-chapter-field').hidden = !!qcmTheme;
    $('content-question-dialog').querySelectorAll('details').forEach(function (el) { el.open = false; });
    questionTitle();
    $('content-question-time').value = q.temps || 30; $('content-question-explanation').value = q.explication || '';
    $('content-question-image').value = qcmQuestionImage(q); $('content-question-latex').value = q.latex || '';
    clearFormErrors(); renderChoices(); $('content-question-dialog').showModal(); $('content-question-text').focus();
  }
  function closeQuestion() {
    if (formState && formState.saving) return;
    if (formState && formState.dirty && !confirm('Abandonner les modifications de cette question ?')) return;
    $('content-question-dialog').close(); formState = null;
  }
  async function saveQuestion(event) {
    event.preventDefault(); if (!formState || formState.saving || qcmSaving) return;
    captureChoices(); clearFormErrors(); var snapshot = formState;
    var question = { question: $('content-question-text').value.trim(), chapitre: snapshot.chapter || $('content-question-chapter').value,
      temps: Number($('content-question-time').value), reponse: snapshot.answer === null ? null : snapshot.answer + 1,
      explication: $('content-question-explanation').value.trim(), image: $('content-question-image').value.trim(), latex: $('content-question-latex').value.trim(),
      choix: snapshot.choices.map(function (c) {
        if (!c.image.trim() && !c.latex.trim()) return c.text.trim();
        return { texte: c.text.trim(), image: c.image.trim(), latex: c.latex.trim() };
      }) };
    var firstInvalid = null;
    var controls = Array.from($('content-question-form').querySelectorAll('input,textarea,select,button')).map(function (el) { return { el: el, disabled: el.disabled }; });
    snapshot.saving = true; qcmSaving = true; controls.forEach(function (c) { c.el.disabled = true; });
    try {
      var suffix = snapshot.qid ? '/' + encodeURIComponent(snapshot.qid) : '';
      var data = await send('/api/admin/qcm/' + snapshot.subject + '/questions' + suffix, snapshot.qid ? 'PUT' : 'POST', { question: question, revision: snapshot.revision });
      qcmSaving = false; acceptBank(data, snapshot.subject, snapshot.version, snapshot.scope);
      if (formState === snapshot) { $('content-question-dialog').close(); formState = null; }
      var editedCard = Array.from($('qcm-question-list').querySelectorAll('[data-qid]')).find(function (el) { return el.dataset.qid === snapshot.qid; });
      (editedCard ? editedCard.querySelector('button') : $('qcm-add-question')).focus();
      showToast('Question enregistrée.');
    } catch (error) {
      $('content-question-error').textContent = error.message;
      Object.keys(error.fields || {}).forEach(function (key) {
        var field = $('content-error-' + key); if (field) field.textContent = friendly(error.fields[key]);
        var control = key === 'question' ? $('content-question-text') : (key === 'temps' ? $('content-question-time') : $('content-question-form').querySelector('.content-answer-group'));
        if (control) {
          control.setAttribute('aria-invalid', 'true');
          if (!firstInvalid) firstInvalid = control.matches('input,textarea,select') ? control : control.querySelector('input,textarea,select');
        }
      });
    } finally { snapshot.saving = false; qcmSaving = false; controls.forEach(function (c) { c.el.disabled = c.disabled; }); setQcmControls();
      if (firstInvalid) { firstInvalid.focus(); firstInvalid.scrollIntoView({ block: 'nearest' }); }
    }
  }
  async function deleteQuestion(q, snapshot) {
    if (!q || !q.qid || !ready() || qcmSaving || dirty() || (snapshot && snapshot.saving)) return;
    var warning = 'Supprimer définitivement cette question de la banque QCM ? Elle ne sera plus disponible ni en QCM ni dans Ankimie. Les révisions historiques sont conservées.';
    if (snapshot && snapshot.dirty) warning += ' Les modifications non enregistrées seront abandonnées.';
    if (!confirm(warning)) return;
    var theme = snapshot ? snapshot.subject : currentSubject(), version = snapshot ? snapshot.version : qcmLoadVersion,
      scope = snapshot ? snapshot.scope : qcmScopeVersion, revision = snapshot ? snapshot.revision : qcmRevision;
    var controls = snapshot ? Array.from($('content-question-form').querySelectorAll('input,textarea,select,button')).map(function (el) { return { el: el, disabled: el.disabled }; }) : [];
    if (snapshot) { snapshot.saving = true; clearFormErrors(); }
    controls.forEach(function (c) { c.el.disabled = true; });
    qcmSaving = true; setQcmControls();
    try {
      var data = await send('/api/admin/qcm/' + theme + '/questions/' + encodeURIComponent(q.qid), 'DELETE', { revision: revision });
      qcmSaving = false; acceptBank(data, theme, version, scope);
      if (snapshot && formState === snapshot) { $('content-question-dialog').close(); formState = null; }
      $('qcm-add-question').focus(); showToast('Question supprimée de la banque.');
    } catch (error) {
      if (snapshot) $('content-question-error').textContent = error.message;
      else qcmEditorStatus(error.message, true);
    } finally {
      if (snapshot) snapshot.saving = false;
      controls.forEach(function (c) { c.el.disabled = c.disabled; });
      qcmSaving = false; setQcmControls(); renderQuestions();
    }
  }
  function closeDeleteChapter() {
    if (deleteState && deleteState.saving) return;
    $('content-delete-dialog').close(); deleteState = null;
  }
  function destinationStatus() {
    if (!deleteState) return;
    var ch = $('content-delete-destination').value;
    var row = deleteState.chapters.find(function (r) { return r.chapitre === ch; });
    $('content-delete-destination-status').textContent = 'Destination : ' + path(deleteState.theme, ch) + '. ' +
      (row ? 'Les questions suivront l’activation de ce chapitre (QCM et Ankimie) : ' + (row.qcm_active ? 'actif' : 'inactif') + '.' : 'Ce nouveau chapitre sera inactif pour les élèves.');
  }
  function openDeleteChapter(row) {
    if (!ready() || qcmSaving || dirty()) return;
    var theme = currentSubject();
    deleteState = { theme: theme, chapter: row.chapitre, chapters: qcmChapters,
      revision: qcmRevision, version: qcmLoadVersion, scope: qcmScopeVersion, saving: false };
    $('content-delete-title').textContent = 'Supprimer ' + path(theme, row.chapitre);
    $('content-delete-effects').textContent = row.qcm_count ? plural(row.qcm_count, 'question sera conservée et déplacée', 'questions seront conservées et déplacées') + ' dans le chapitre choisi ci-dessous. Aucune question ne sera supprimée de la banque.' : 'Ce chapitre ne contient aucune question QCM. Il sera retiré de la liste des chapitres.';
    $('content-delete-ankimie').textContent = 'Ankimie suit automatiquement : les questions déplacées seront révisables selon l’activation du chapitre de destination.';
    var fallback = row.chapitre === 'Autre' ? 'À classer' : 'Autre';
    var destinations = qcmChapters.filter(function (r) { return r.chapitre !== row.chapitre; }).map(function (r) { return r.chapitre; });
    if (destinations.indexOf(fallback) === -1) destinations.push(fallback);
    $('content-delete-destination').innerHTML = destinations.map(function (ch) { return '<option value="' + esc(ch) + '">' + esc(ch) + '</option>'; }).join('');
    $('content-delete-destination').value = fallback;
    $('content-delete-destination-field').hidden = !row.qcm_count;
    $('content-delete-error').textContent = ''; destinationStatus();
    $('content-delete-dialog').showModal(); $('content-delete-cancel').focus();
  }
  async function deleteChapter(event) {
    event.preventDefault(); if (!deleteState || deleteState.saving) return;
    var snapshot = deleteState;
    var controls = Array.from($('content-delete-form').querySelectorAll('button,select'));
    var destination = $('content-delete-destination').value;
    snapshot.saving = true; controls.forEach(function (el) { el.disabled = true; });
    qcmSaving = true; setQcmControls();
    try {
      var data = await send('/api/admin/content/' + snapshot.theme + '/chapters', 'DELETE', { chapitre: snapshot.chapter, destination: destination, revision: snapshot.revision });
      qcmSaving = false;
      if (snapshot.theme === currentSubject() && snapshot.version === qcmLoadVersion && snapshot.scope === qcmScopeVersion) {
        if (qcmTheme === snapshot.chapter) { qcmTheme = data.chapters.some(function (r) { return r.chapitre === destination; }) ? destination : ''; qcmScopeVersion++; }
        acceptBank(data, snapshot.theme, snapshot.version, qcmScopeVersion);
      }
      $('content-delete-dialog').close(); deleteState = null;
      showToast('Chapitre supprimé. Questions et historiques conservés.');
    } catch (error) { $('content-delete-error').textContent = error.message; }
    finally {
      snapshot.saving = false; controls.forEach(function (el) { el.disabled = false; });
      qcmSaving = false; setQcmControls(); renderQuestions();
    }
  }
  function clearImport() { importVersion++; importDraft = null; $('qcm-import-result').textContent = ''; $('qcm-import-confirm').disabled = true; }
  async function previewImport() {
    clearImport(); var file = $('qcm-import-file').files[0];
    if (!qcmTheme) { showToast('Choisis le chapitre destinataire avant de vérifier l’import.'); return; }
    if (!file) { showToast('Choisis un fichier CSV.'); return; }
    var subject = currentSubject(), version = qcmLoadVersion, scope = qcmScopeVersion;
    var previewVersion = importVersion;
    var form = new FormData(); form.append('file', file); form.append('chapitre', qcmTheme); form.append('format', 'csv');
    $('qcm-import-preview').disabled = true;
    try {
      var data = await api('/api/admin/qcm/' + subject + '/import/preview', { method: 'POST', body: form });
      if (version !== qcmLoadVersion || scope !== qcmScopeVersion || subject !== currentSubject() || previewVersion !== importVersion) return;
      importDraft = { data: data, subject: subject, version: version, scope: scope, chapter: qcmTheme };
      var result = $('qcm-import-result'); result.textContent = data.count + ' questions détectées · Matière : ' + labelFor(subject) + ' · Chapitre : ' + data.chapitre + ' · ' + data.valid_count + ' valides · ' + data.errors.length + ' à corriger';
      data.errors.forEach(function (item) { var p = document.createElement('p'); p.className = 'content-field-error'; p.textContent = 'Question ' + item.position + ' : ' + item.question + ' — ' + friendly(item.error); result.appendChild(p); });
      data.questions.slice(0, pageSize).forEach(function (q) { result.appendChild(questionCard(q)); });
      if (data.questions.length > pageSize) { var more = document.createElement('p'); more.className = 'hint'; more.textContent = 'Aperçu des 20 premières questions ; les ' + data.count + ' questions ont toutes été vérifiées.'; result.appendChild(more); }
      typesetMathKatex(result); $('qcm-import-confirm').disabled = !data.can_import;
    } catch (error) {
      if (version === qcmLoadVersion && scope === qcmScopeVersion && subject === currentSubject() && previewVersion === importVersion) $('qcm-import-result').textContent = error.message;
    }
    finally { $('qcm-import-preview').disabled = false; }
  }
  async function commitImport() {
    if (!importDraft || !importDraft.data.can_import || qcmSaving || dirty()) return;
    var snapshot = importDraft;
    if (snapshot.subject !== currentSubject() || snapshot.scope !== qcmScopeVersion || snapshot.version !== qcmLoadVersion) { clearImport(); return; }
    if (!confirm('Ajouter les ' + snapshot.data.valid_count + ' questions validées au chapitre « ' + snapshot.chapter + ' » ? Les questions existantes sont conservées.')) return;
    qcmSaving = true; $('qcm-import-confirm').disabled = true;
    try {
      var data = await send('/api/admin/qcm/' + snapshot.subject + '/import', 'POST', { chapitre: snapshot.chapter, questions: snapshot.data.questions, revision: snapshot.data.revision, preview_token: snapshot.data.preview_token });
      qcmSaving = false;
      if (acceptBank(data, snapshot.subject, snapshot.version, snapshot.scope)) { clearImport(); $('qcm-import-panel').hidden = true; }
      showToast('Questions importées. Vérifie l’activation du chapitre pour les élèves.');
    } catch (error) {
      if (snapshot === importDraft && snapshot.subject === currentSubject() && snapshot.scope === qcmScopeVersion && snapshot.version === qcmLoadVersion) $('qcm-import-result').textContent = error.message;
    }
    finally { qcmSaving = false; setQcmControls(); }
  }
  window.contentAdmin = { render: render, canSwitch: canSwitch, hasDraft: dirty, createChapter: createChapter,
    busy: function () { setQcmControls(); renderQuestions(); },
    loadFailed: function () { setQcmControls(); updateQcmContext(); empty($('qcm-chapter-overview'), 'Les chapitres sont indisponibles. Recharge la banque pour réessayer.'); renderQuestions(); },
    loading: function () { clearImport(); setQcmControls(); updateQcmContext(); empty($('qcm-chapter-overview'), 'Chargement des chapitres…'); empty($('qcm-question-list'), 'Chargement de la banque…'); } };
  document.addEventListener('DOMContentLoaded', function () {
    if (!$('qcm-visual')) return;
    $('qcm-add-question').onclick = function () { openQuestion(null, false); };
    $('content-question-delete').onclick = function () { deleteQuestion(formState, formState); };
    $('qcm-question-search').oninput = function () { qPage = 0; renderQuestions(); };
    $('qcm-json-editor').addEventListener('input', function () { setQcmControls(); renderQuestions(); });
    $('content-question-chapter').onchange = questionTitle;
    $('content-delete-form').onsubmit = deleteChapter;
    $('content-delete-destination').onchange = destinationStatus;
    $('content-delete-cancel').onclick = closeDeleteChapter;
    $('content-delete-dialog').addEventListener('cancel', function (event) { event.preventDefault(); closeDeleteChapter(); });
    $('qcm-json-file').onchange = async function () {
      var fileVersion = ++jsonFileVersion;
      var file = $('qcm-json-file').files[0]; if (!file || !ready() || qcmSaving) return;
      if (dirty() && !confirm('Remplacer les modifications avancées non enregistrées par ce fichier ?')) return;
      var subject = currentSubject(), version = qcmLoadVersion, scope = qcmScopeVersion;
      try {
        var text = await file.text(); JSON.parse(text);
        if (subject !== currentSubject() || version !== qcmLoadVersion || scope !== qcmScopeVersion || fileVersion !== jsonFileVersion) return;
        $('qcm-json-editor').value = text; setQcmControls(); renderQuestions(); qcmEditorStatus('Fichier chargé. Valide-le puis enregistre pour appliquer les modifications.', false);
      } catch (error) { qcmEditorStatus('Fichier JSON invalide : ' + error.message, true); }
    };
    $('qcm-open-import').onclick = function () { $('qcm-import-panel').hidden = false; $('qcm-import-file').focus(); };
    $('qcm-import-close').onclick = function () { $('qcm-import-panel').hidden = true; clearImport(); };
    $('qcm-import-file').onchange = clearImport; $('qcm-import-preview').onclick = previewImport; $('qcm-import-confirm').onclick = commitImport;
    $('content-choice-add').onclick = function () { if (!formState || formState.choices.length >= 4) return; captureChoices(); formState.choices.push({ text: '', image: '', latex: '' }); formState.dirty = true; renderChoices(); };
    $('content-question-form').onsubmit = saveQuestion;
    $('content-question-form').oninput = function () { if (formState) formState.dirty = true; };
    ['content-question-close', 'content-question-cancel'].forEach(function (id) { $(id).onclick = closeQuestion; });
    $('content-question-dialog').addEventListener('cancel', function (event) { event.preventDefault(); closeQuestion(); });
  });
})();
