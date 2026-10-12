/* Visual content tools share the canonical bank with the existing JSON editor. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var names = { physique: 'Physique', chimie: 'Chimie', maths: 'Mathématiques' };
  var pageSize = 20, qPage = 0, lastBuffer = '', importDraft = null, formState = null;
  var overviewLoaded = false;
  var importVersion = 0, jsonFileVersion = 0;
  var activationPending = { qcm: false, ank: false };
  var ank = { version: 0, theme: 'chimie', chapter: '', questions: [], chapters: [], included: new Set(), revision: null, loaded: false, saving: false, page: 0, pickerPage: 0 };

  function chapter(q) { return String(q.chapitre || 'Autre').trim() || 'Autre'; }
  function currentSubject() { return $('qcm-subject-select').value; }
  function ready() { return qcmLoadedSubject === currentSubject(); }
  function dirty() { return $('qcm-json-editor').value !== lastBuffer; }
  function friendly(message) { return String(message || 'Impossible de réaliser cette opération.').replace(/reponse/g, 'bonne réponse').replace(/tableau choix requis/g, 'ajoute les réponses possibles'); }
  async function api(url, options) {
    var response = await fetchWithStatus(url, options || { cache: 'no-store' });
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
    if (qcmQuestionImage(q)) appendPreviewImg(el, qcmPreviewImageUrl(qcmQuestionImage(q)), 'Illustration de la question');
    var list = document.createElement('ol'); list.className = 'content-choices'; list.type = 'A';
    (q.choix || []).forEach(function (choice, i) {
      var c = normalizeQcmPreviewChoice(choice), li = document.createElement('li'); li.textContent = c.text;
      if (i + 1 === Number(q.reponse)) { li.className = 'content-correct'; li.appendChild(document.createTextNode(' — Bonne réponse')); }
      if (c.image) appendPreviewImg(li, qcmPreviewImageUrl(c.image), 'Illustration de la réponse'); list.appendChild(li);
    }); el.appendChild(list);
    if (q.explication) { var explanation = document.createElement('p'); explanation.className = 'hint'; explanation.textContent = q.explication; el.appendChild(explanation); }
    return el;
  }
  function chaptersOverview(target, rows, theme, selected, mode, choose) {
    target.innerHTML = '';
    if (!rows.length) { empty(target, 'Aucun chapitre disponible. Crée le premier chapitre ci-dessous.'); return; }
    rows.forEach(function (row) {
      var card = document.createElement('article'); card.className = 'content-chapter' + (row.chapitre === selected ? ' selected' : '');
      var h = document.createElement('h4'); h.textContent = row.chapitre;
      var counts = document.createElement('p'); counts.className = 'hint'; counts.textContent = row.qcm_count + ' questions disponibles · ' + row.ankimie_count + ' cartes Ankimie'; card.append(h, counts);
      var label = document.createElement('label'), toggle = document.createElement('input'); toggle.type = 'checkbox';
      var flag = mode === 'qcm' ? 'qcm_active' : 'ankimie_active'; toggle.checked = row[flag];
      toggle.disabled = activationPending[mode];
      label.append(toggle, document.createTextNode('Actif pour les élèves (' + (mode === 'qcm' ? 'QCM' : 'Ankimie') + ')')); card.appendChild(label);
      toggle.onchange = async function () {
        if (activationPending[mode]) return;
        var value = toggle.checked, version = mode === 'qcm' ? qcmLoadVersion : ank.version;
        activationPending[mode] = true;
        target.querySelectorAll('input[type=checkbox]').forEach(function (input) { input.disabled = true; });
        try {
          var payload = { chapitre: row.chapitre }; payload[flag] = value;
          var data = await send('/api/admin/content/' + theme + '/chapters', 'PUT', payload);
          if (mode === 'qcm' && version === qcmLoadVersion && theme === currentSubject()) {
            qcmChapters = data.chapters; chaptersOverview(target, qcmChapters, theme, qcmTheme, mode, choose);
          } else if (mode === 'ank' && version === ank.version && theme === ank.theme) {
            ank.chapters = data.chapters; renderAnkimie();
          }
          showToast(value ? 'Chapitre activé pour les élèves.' : 'Chapitre désactivé. Contenu et historique conservés.');
        } catch (error) { toggle.checked = !value; showToast(error.message); }
        finally { activationPending[mode] = false; target.querySelectorAll('input[type=checkbox]').forEach(function (input) { input.disabled = false; }); }
      };
      var view = button('Voir le chapitre', function () { if (mode !== 'ank' || !ank.saving) choose(row.chapitre); });
      view.disabled = mode === 'ank' && ank.saving;
      card.appendChild(view); target.appendChild(card);
    });
  }
  function loadSubjectOverview() {
    if (overviewLoaded) return; overviewLoaded = true;
    api('/api/admin/content/subjects').then(function (data) {
      var target = $('content-subject-overview'); target.innerHTML = '';
      data.subjects.forEach(function (subject) {
        var card = document.createElement('div'); card.className = 'content-chapter'; card.dataset.subject = subject.theme;
        var h = document.createElement('h4'); h.textContent = names[subject.theme];
        var p = document.createElement('p'); p.className = 'hint'; p.textContent = subject.error ? 'Banque à vérifier : ' + subject.error : subject.count + ' questions · ' + subject.chapters.length + ' chapitres'; card.append(h, p); target.appendChild(card);
      });
      if (ready()) updateSubjectCount(currentSubject(), qcmFullList.length, qcmChapters.length);
      if (ank.loaded && ank.theme !== currentSubject()) updateSubjectCount(ank.theme, ank.questions.length, ank.chapters.length);
    }).catch(function () { overviewLoaded = false; });
  }
  function updateSubjectCount(theme, count, chapters) {
    var card = Array.from($('content-subject-overview').children).find(function (el) { return el.dataset.subject === theme; });
    if (card) card.querySelector('p').textContent = count + ' questions · ' + chapters + ' chapitres';
  }
  function setQcmControls() {
    var disabled = !ready() || qcmSaving;
    ['qcm-theme-select', 'qcm-add-question', 'qcm-open-import', 'qcm-question-search', 'qcm-new-theme-input'].forEach(function (id) { $(id).disabled = disabled; });
    $('qcm-add-question').disabled = disabled || dirty(); $('qcm-json-editor').readOnly = disabled;
  }
  function renderQuestions() {
    var target = $('qcm-question-list'); target.innerHTML = '';
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
    qPage = Math.min(qPage, Math.max(0, Math.ceil(questions.length / pageSize) - 1));
    if (!questions.length) empty(target, 'Aucune question dans ce périmètre. Ajoute une question ou importe un fichier.');
    questions.slice(qPage * pageSize, (qPage + 1) * pageSize).forEach(function (q) {
      var card = questionCard(q), actions = document.createElement('div'); actions.className = 'content-toolbar';
      var edit = button('Modifier', function () { openQuestion(q, false); });
      var copy = button('Dupliquer', function () { openQuestion(q, true); });
      var remove = button('Supprimer de la banque', function () { deleteQuestion(q); });
      [edit, copy, remove].forEach(function (b) { b.disabled = qcmSaving || dirty(); actions.appendChild(b); }); card.appendChild(actions); target.appendChild(card);
    });
    paginate($('qcm-question-pages'), questions.length, qPage, function (p) { qPage = p; renderQuestions(); });
    typesetMathKatex(target);
  }
  function render() {
    lastBuffer = $('qcm-json-editor').value; qPage = 0; clearImport();
    setQcmControls(); loadSubjectOverview(); updateSubjectCount(currentSubject(), qcmFullList.length, qcmChapters.length);
    chaptersOverview($('qcm-chapter-overview'), qcmChapters, currentSubject(), qcmTheme, 'qcm', function (ch) {
      if (!canSwitch()) return; qcmTheme = ch; qcmScopeVersion++; $('qcm-theme-select').value = ch; renderQcmEditorBuffer();
    }); renderQuestions();
  }
  function canSwitch() { return !dirty() || confirm('Des modifications avancées ne sont pas enregistrées. Les abandonner pour changer de matière ou de chapitre ?'); }
  function acceptBank(data, subject, version, scope) {
    if (subject !== currentSubject() || version !== qcmLoadVersion || scope !== qcmScopeVersion) return false;
    qcmFullList = data.questions; qcmRevision = data.revision; qcmChapters = data.chapters;
    renderQcmThemeSelect(); renderQcmEditorBuffer(); return true;
  }
  async function createChapter(mode) {
    var input = $(mode === 'qcm' ? 'qcm-new-theme-input' : 'ank-new-chapter');
    if (input.disabled || (mode === 'qcm' && (!ready() || qcmSaving || !canSwitch())) || (mode === 'ank' && (!ank.loaded || ank.saving))) return;
    var ch = input.value.trim(); if (!ch) { input.focus(); showToast('Donne un nom au chapitre.'); return; }
    var theme = mode === 'qcm' ? currentSubject() : $('ank-admin-subject').value;
    var version = mode === 'qcm' ? qcmLoadVersion : ank.version; input.disabled = true;
    var scope = mode === 'qcm' ? qcmScopeVersion : ank.chapter;
    var startingBuffer = mode === 'qcm' ? $('qcm-json-editor').value : null;
    try {
      var data = await send('/api/admin/content/' + theme + '/chapters', 'POST', { chapitre: ch });
      if (mode === 'qcm' && theme === currentSubject() && version === qcmLoadVersion && scope === qcmScopeVersion) {
        if (dirty() && $('qcm-json-editor').value !== startingBuffer && !canSwitch()) return;
        qcmChapters = data.chapters; qcmTheme = ch; qcmScopeVersion++; renderQcmThemeSelect(); renderQcmEditorBuffer();
        input.value = '';
      } else if (mode === 'ank' && theme === $('ank-admin-subject').value && version === ank.version && scope === ank.chapter) { ank.chapter = ch; await loadAnkimie(); input.value = ''; }
      showToast('Chapitre créé, inactif pour les élèves.');
    } catch (error) { showToast(error.message); }
    finally { if (mode === 'qcm') setQcmControls(); else input.disabled = !ank.loaded; }
  }
  function choiceValues(c) {
    return typeof c === 'string' ? { text: c, image: '', latex: '' } : { text: c.texte !== undefined ? c.texte : (c.text || ''), image: c.image || '', latex: c.latex || '' };
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
      revision: qcmRevision, choices: q.choix.map(choiceValues), answer: q.reponse ? Number(q.reponse) - 1 : null, dirty: false, saving: false };
    $('content-question-title').textContent = duplicate ? 'Dupliquer la question' : (q.qid ? 'Modifier la question' : 'Ajouter une question');
    $('content-question-text').value = q.question + (duplicate ? ' (copie)' : '');
    $('content-question-chapter').innerHTML = '<option value="">Choisir un chapitre</option>' + qcmThemeList().map(function (ch) { return '<option value="' + esc(ch) + '">' + esc(ch) + '</option>'; }).join('');
    $('content-question-chapter').value = q.chapitre || qcmTheme || (q.qid ? 'Autre' : '');
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
    var question = { question: $('content-question-text').value.trim(), chapitre: $('content-question-chapter').value,
      temps: Number($('content-question-time').value), reponse: snapshot.answer === null ? null : snapshot.answer + 1,
      explication: $('content-question-explanation').value.trim(), image: $('content-question-image').value.trim(), latex: $('content-question-latex').value.trim(),
      choix: snapshot.choices.map(function (c) {
        if (!c.image.trim() && !c.latex.trim()) return c.text.trim();
        return { texte: c.text.trim(), image: c.image.trim(), latex: c.latex.trim() };
      }) };
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
        if (control) control.setAttribute('aria-invalid', 'true');
      });
    } finally { snapshot.saving = false; qcmSaving = false; controls.forEach(function (c) { c.el.disabled = c.disabled; }); setQcmControls(); }
  }
  async function deleteQuestion(q) {
    if (!ready() || qcmSaving || dirty()) return;
    if (!confirm('Supprimer définitivement cette question de la banque QCM ? Elle ne sera plus disponible dans ses collections Ankimie. Les révisions historiques sont conservées.')) return;
    var theme = currentSubject(), version = qcmLoadVersion, scope = qcmScopeVersion; qcmSaving = true; setQcmControls();
    try {
      var data = await send('/api/admin/qcm/' + theme + '/questions/' + encodeURIComponent(q.qid), 'DELETE', { revision: qcmRevision });
      qcmSaving = false; acceptBank(data, theme, version, scope); showToast('Question supprimée de la banque.');
    } catch (error) { qcmEditorStatus(error.message, true); }
    finally { qcmSaving = false; setQcmControls(); renderQuestions(); }
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
      var result = $('qcm-import-result'); result.textContent = data.count + ' questions détectées · Matière : ' + names[subject] + ' · Chapitre : ' + data.chapitre + ' · ' + data.valid_count + ' valides · ' + data.errors.length + ' à corriger';
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
  async function validateJson() {
    if (!ready() || qcmSaving) return;
    try {
      var text = $('qcm-json-editor').value, questions = JSON.parse(text);
      var subject = currentSubject(), version = qcmLoadVersion, scope = qcmScopeVersion;
      var data = await send('/api/admin/qcm/' + subject + '/validate', 'POST', { questions: questions });
      if (version === qcmLoadVersion && scope === qcmScopeVersion && subject === currentSubject() && text === $('qcm-json-editor').value) qcmEditorStatus(data.questions.length + ' questions valides. Tu peux enregistrer.', false);
    } catch (error) { qcmEditorStatus(error.message, true); }
  }
  async function loadAnkimie() {
    var theme = $('ank-admin-subject').value, version = ++ank.version;
    ank.theme = theme; ank.saving = true; ank.loaded = false;
    $('ank-new-chapter').disabled = true;
    empty($('ank-chapter-overview'), 'Chargement des chapitres…');
    $('ank-admin-chapter').disabled = true; $('ank-open-picker').disabled = true; empty($('ank-collection-list'), 'Chargement des cartes…');
    try {
      var data = await api('/api/admin/ankimie/' + theme + '/collections?chapitre=' + encodeURIComponent(ank.chapter));
      if (version !== ank.version || theme !== $('ank-admin-subject').value) return;
      ank.questions = data.questions; ank.chapters = data.chapters; ank.revision = data.revision; ank.byChapter = data.included_by_chapter;
      if (!ank.chapters.some(function (ch) { return ch.chapitre === ank.chapter; })) ank.chapter = ank.chapters.length ? ank.chapters[0].chapitre : '';
      $('ank-admin-chapter').innerHTML = ank.chapters.map(function (ch) { return '<option value="' + esc(ch.chapitre) + '">' + esc(ch.chapitre) + ' (' + ch.ankimie_count + ' cartes)</option>'; }).join('');
      $('ank-admin-chapter').value = ank.chapter; ank.included = new Set(ank.byChapter[ank.chapter] || []); ank.page = 0; ank.pickerPage = 0; ank.saving = false; ank.loaded = true; renderAnkimie();
    } catch (error) { if (version === ank.version) { empty($('ank-collection-list'), error.message + ' Clique sur « Recharger les cartes » pour réessayer.'); $('ank-content-status').textContent = error.message; } }
    finally { if (version === ank.version) { ank.saving = false; $('ank-new-chapter').disabled = !ank.loaded; $('ank-admin-chapter').disabled = !ank.loaded; $('ank-open-picker').disabled = !ank.loaded || !ank.chapter; } }
  }
  async function membership(q, included) {
    if (ank.saving || !ank.loaded || !ank.chapter) return;
    var theme = ank.theme, ch = ank.chapter, version = ank.version;
    ank.saving = true; renderAnkimie();
    try {
      await send('/api/admin/ankimie/' + theme + '/collections', 'PUT', { chapitre: ch, qid: q.qid, included: included, revision: ank.revision });
      if (version === ank.version && theme === $('ank-admin-subject').value) { await loadAnkimie(); $('ank-content-status').textContent = included ? 'Carte ajoutée au chapitre.' : 'Carte retirée de ce chapitre. La banque et l’historique sont conservés.'; }
    } catch (error) { if (version === ank.version) $('ank-content-status').textContent = error.message; }
    finally { if (version === ank.version) { ank.saving = false; renderAnkimie(); } }
  }
  function renderAnkimie() {
    updateSubjectCount(ank.theme, ank.questions.length, ank.chapters.length);
    chaptersOverview($('ank-chapter-overview'), ank.chapters, ank.theme, ank.chapter, 'ank', function (ch) {
      ank.version++; ank.chapter = ch; $('ank-admin-chapter').value = ch; ank.included = new Set(ank.byChapter[ch] || []); ank.page = 0; ank.pickerPage = 0; renderAnkimie();
    });
    $('ank-collection-title').textContent = ank.chapter ? ank.chapter + ' — ' + ank.included.size + ' cartes' : 'Cartes du chapitre';
    var selected = ank.questions.filter(function (q) { return ank.included.has(q.qid); });
    var available = ank.questions.filter(function (q) { return !ank.included.has(q.qid) && matches(q, $('ank-picker-search').value); });
    [['ank-collection-list', 'ank-collection-pages', selected, 'page', false], ['ank-picker-list', 'ank-picker-pages', available, 'pickerPage', true]].forEach(function (entry) {
      var target = $(entry[0]); target.innerHTML = ''; var page = Math.min(ank[entry[3]], Math.max(0, Math.ceil(entry[2].length / pageSize) - 1)); ank[entry[3]] = page;
      if (!entry[2].length) empty(target, entry[4] ? 'Aucune carte supplémentaire à ajouter. Tu peux créer des questions dans la rubrique QCM.' : 'Ce chapitre ne contient pas encore de carte. Utilise « Ajouter des cartes existantes ».');
      entry[2].slice(page * pageSize, (page + 1) * pageSize).forEach(function (q) {
        var card = questionCard(q), actions = document.createElement('div'); actions.className = 'content-toolbar';
        var b = button(entry[4] ? 'Ajouter au chapitre' : 'Retirer de ce chapitre', function () { membership(q, entry[4]); }); b.disabled = ank.saving || !ank.chapter; actions.appendChild(b); card.appendChild(actions); target.appendChild(card);
      });
      paginate($(entry[1]), entry[2].length, page, function (p) { ank[entry[3]] = p; renderAnkimie(); }); typesetMathKatex(target);
    });
    $('ank-open-picker').disabled = !ank.loaded || !ank.chapter || ank.saving;
    $('ank-admin-chapter').disabled = ank.saving || !ank.loaded;
  }
  window.contentAdmin = { render: render, canSwitch: canSwitch, hasDraft: dirty, createChapter: createChapter, loadAnkimie: loadAnkimie,
    busy: function () { setQcmControls(); renderQuestions(); },
    loading: function () { clearImport(); setQcmControls(); empty($('qcm-chapter-overview'), 'Chargement des chapitres…'); empty($('qcm-question-list'), 'Chargement de la banque…'); } };
  document.addEventListener('DOMContentLoaded', function () {
    if (!$('qcm-visual')) return;
    $('qcm-add-question').onclick = function () { openQuestion(null, false); };
    $('qcm-question-search').oninput = function () { qPage = 0; renderQuestions(); };
    $('qcm-json-editor').addEventListener('input', function () { setQcmControls(); renderQuestions(); });
    $('qcm-json-validate').onclick = validateJson;
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
    $('ank-admin-subject').onchange = function () { ank.chapter = ''; $('ank-card-picker').hidden = true; loadAnkimie(); };
    $('ank-admin-chapter').onchange = function () { ank.version++; ank.chapter = $('ank-admin-chapter').value; ank.included = new Set(ank.byChapter[ank.chapter] || []); ank.page = 0; ank.pickerPage = 0; renderAnkimie(); };
    $('ank-create-chapter').onclick = function () { createChapter('ank'); }; $('ank-reload').onclick = loadAnkimie;
    $('ank-open-picker').onclick = function () { $('ank-card-picker').hidden = false; renderAnkimie(); $('ank-picker-search').focus(); };
    $('ank-close-picker').onclick = function () { $('ank-card-picker').hidden = true; };
    $('ank-picker-search').oninput = function () { ank.pickerPage = 0; renderAnkimie(); };
  });
})();
