'use strict';

var PAGE = document.body.dataset.page || '';
var params = { max_active_num: 36, fsrs_retention: 0.80 };
var horsSerieMode = false;
var pendingPrenom = null;
var currentSrUser = null;
var allPublicCards = [];
var chapitresList = [];
var cardStartTimes = {};
var pendingAgainCard = null;
var currentEditNumero = null;
var srTotalToday = 0;
var srDoneToday = 0;
var EMOJI_KEYPAD = [];
var EMOJI_PW_LENGTH = 4;
var currentEmojiPw = [];
var selectedDrawCount = 1;
var qcmFullList = [];
var qcmTheme = '';

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

var GATE_ERRORS = [
  'Expelliarmus', 'V=S', 'Malheur', 'Pas homogène',
  'T\'as oublié le vecteur au dessus du grad',
  'Pas de vecteur au dessus du div',
  'Le lion ne s\'associe pas avec le cafard',
  'NaN',
  'Ça aurait pu mais ce n\'est pas le bon code',
  'Objectif quadrillionnaire raté',
  'L\'écoulement n\'est pas irrotationnel',
  'Le système est-il fermé ???',
  'Il fallait faire un bilan mésoscopique, pas macroscopique'
];
function randomGateError() { return GATE_ERRORS[Math.floor(Math.random() * GATE_ERRORS.length)]; }

function fetchJson(url, opts) {
  opts = opts || {};
  var controller = new AbortController();
  var timer = setTimeout(function () { controller.abort(); }, 10000);
  opts.signal = controller.signal;
  return fetch(url, opts).then(function (res) {
    if (!res.ok) {
      return res.json().catch(function () { return null; }).then(function (body) {
        var err = new Error('HTTP ' + res.status);
        err.status = res.status;
        err.data = body;
        throw err;
      });
    }
    return res.json();
  }).finally(function () { clearTimeout(timer); });
}

function fetchWithStatus(url, opts) {
  return fetch(url, opts).then(function (r) {
    return r.json().then(function (d) { return { status: r.status, data: d }; });
  });
}

function adminAction(url, method, onOk, failMsg) {
  fetchJson(url, { method: method })
    .then(function (res) {
      if (res && res.ok === false) { showToast('Erreur : ' + (res.error || failMsg)); return; }
      onOk(res);
    })
    .catch(function (err) { showToast(err && err.data && err.data.error ? err.data.error : failMsg); });
}

function srPost(numero, action, body, okMsg, failVerb, reenable) {
  fetchJson('/api/sr/' + numero + '/' + action, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (res) {
      if (res) { removeCardFromList(numero); updateDailyStatusOnly(); showToast(okMsg); }
    })
    .catch(function (err) {
      if (err && err.status === 401) { openNameGate(); return; }
      if (reenable) reenable();
      showToast('Erreur reseau, la carte n\'a pas ete ' + failVerb + '.');
    });
}

function toggleSpinner(id, show) {
  var el = document.getElementById(id);
  if (el) el.style.display = show ? 'inline-block' : 'none';
}

var toastTimer = null;
function showToast(msg) {
  var t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.classList.remove('show'); }, 3000);
}

var flashTimers = {};
function flashStatus(elId, message, isError, ms) {
  var el = document.getElementById(elId);
  if (!el) return;
  el.textContent = message;
  el.style.color = isError ? '#c0392b' : 'var(--primary)';
  el.classList.add('show');
  clearTimeout(flashTimers[elId]);
  flashTimers[elId] = setTimeout(function () { el.classList.remove('show'); }, ms);
}

function om(id) { document.getElementById(id).classList.add('open'); }
function cm(id) { document.getElementById(id).classList.remove('open'); }
function openSrInfoModal() { om('sr-info-overlay'); }
function closeSrInfoModal() { cm('sr-info-overlay'); }
function openTirageInfoModal() { om('tirage-info-overlay'); }
function closeTirageInfoModal() { cm('tirage-info-overlay'); }
function closeConfirm() { cm('confirm-overlay'); }
function closeEditModal() { cm('edit-overlay'); currentEditNumero = null; }

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  var toggleBtn = document.getElementById('theme-toggle');
  if (!toggleBtn) return;
  toggleBtn.textContent = theme === 'dark' ? 'Jour' : 'Nuit';
  toggleBtn.setAttribute('aria-label', theme === 'dark' ? 'Passer en theme clair' : 'Passer en theme sombre');
  localStorage.setItem('mdc-theme', theme);
}
function toggleTheme() {
  var current = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

function toggleMobileNav() {
  var nav = document.getElementById('main-nav');
  var button = document.getElementById('hamburger-btn');
  var overlay = document.getElementById('nav-overlay');
  var isOpen = nav.classList.toggle('open');
  button.classList.toggle('open', isOpen);
  button.setAttribute('aria-expanded', String(isOpen));
  button.setAttribute('aria-label', isOpen ? 'Fermer le menu' : 'Ouvrir le menu');
  if (overlay) overlay.classList.toggle('open', isOpen);
}
function closeMobileNav() {
  var nav = document.getElementById('main-nav');
  var button = document.getElementById('hamburger-btn');
  var overlay = document.getElementById('nav-overlay');
  nav.classList.remove('open');
  button.classList.remove('open');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-label', 'Ouvrir le menu');
  if (overlay) overlay.classList.remove('open');
}

function loadCompteDashboard() {
  loadSrQcmErrors();
  if (PAGE !== 'compte' || !currentSrUser) return;
  fetchJson('/api/sr/dashboard').then(function (d) {
    if (!d || !d.ok) return;
    var set = function (id, v) { var el = document.getElementById(id); if (el) el.textContent = v; };
    set('dash-streak', d.streak + ' j');
    set('dash-record', d.record + ' j');
    set('dash-jokers', d.jokers + ' / 2');
    set('dash-semaine', d.semaine);
    set('dash-total', d.total);
  }).catch(function (e) { console.error(e); });
  fetchJson('/api/sr/qcm/weak').then(renderQcmWeak).catch(function (e) { console.error('qcm weak', e); });
}

function renderQcmWeak(d) {
  var box = document.getElementById('compte-qcm-weak');
  if (!box || !d || !d.ok) return;
  if (!d.rows.length) { box.innerHTML = ''; return; }
  var html = '<div class="section-title" style="margin:.5rem 0">Questions QCM à retravailler</div>' +
    '<p class="home-subtitle" style="text-align:left;font-size:.8rem;margin-bottom:.5rem">Tes questions les plus ratées, toutes parties confondues.</p>';
  d.rows.forEach(function (r) {
    var label = r.question.length > 110 ? r.question.slice(0, 110) + '…' : r.question;
    var color = r.taux_echec >= 40 ? '#c0392b' : (r.taux_echec >= 20 ? '#e67e22' : '#27ae60');
    html += '<div class="admin-row"><div>' +
      '<div style="font-size:.88rem">' + esc(label) + '</div>' +
      '<div class="meta">' + esc(r.theme) + (r.chapitre ? ' · ' + esc(r.chapitre) : '') + '</div></div>' +
      '<div class="meta" style="white-space:nowrap"><span class="weak-badge" style="background:' + color + '">' +
      r.taux_echec + ' %</span><br>' + r.echecs + '/' + r.sorties + ' ratées</div></div>';
  });
  box.innerHTML = html;
}

function refreshAuthUI() {
  var user = currentSrUser || null;
  if (user) loadCompteDashboard();
  document.querySelectorAll('.auth-only').forEach(function (el) { el.hidden = !user; });
  var compteBtn = document.querySelector('[data-view="compte"]');
  if (compteBtn) compteBtn.textContent = user ? '👤 ' + user : 'Espace compte';
  var off = document.getElementById('compte-logged-out');
  var on = document.getElementById('compte-logged-in');
  if (off) off.style.display = user ? 'none' : '';
  if (on) on.style.display = user ? '' : 'none';
  var lb = document.getElementById('compte-logout-btn');
  if (lb) lb.style.display = user ? '' : 'none';
  var nameEl = document.getElementById('compte-prenom');
  if (nameEl && user) nameEl.textContent = user;
}
function openLoginGate() { openNameGate(); }

function logoutSr() {
  fetch('/api/sr/logout', { method: 'POST' }).catch(function (e) { console.error(e); });
  currentSrUser = null;
  setupExportButton();
  refreshAuthUI();
  showToast('Déconnecté. À bientôt !');
  if (PAGE === 'sr') location.href = '/';
}

function chapitreOptionsHtml(list) {
  return list.map(function (c) { return '<option value="' + esc(c) + '">' + esc(c) + '</option>'; }).join('');
}
function loadChapitres() {
  return fetchJson('/api/chapitres').then(function (list) {
    chapitresList = list;
    var upSelect = document.getElementById('up-chapitre');
    if (upSelect) upSelect.innerHTML = chapitreOptionsHtml(list);
  }).catch(function () { showToast('Impossible de charger les chapitres.'); });
}

function fireConfetti() {
  var canvas = document.getElementById('confetti-canvas');
  if (!canvas) return;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(window.innerWidth * dpr);
  canvas.height = Math.floor(window.innerHeight * dpr);
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  var w = window.innerWidth, h = window.innerHeight;
  var colors = ['#01696f', '#ffb703', '#fb8500', '#e63946', '#2a9d8f'];
  var particles = [];
  for (var i = 0; i < 140; i++) {
    particles.push({
      x: Math.random() * w, y: -20 - Math.random() * h * 0.3,
      r: 4 + Math.random() * 5,
      color: colors[Math.floor(Math.random() * colors.length)],
      vx: -2 + Math.random() * 4, vy: 2 + Math.random() * 3,
      rot: Math.random() * 360, vrot: -6 + Math.random() * 12
    });
  }
  var start = Date.now();
  function frame() {
    var elapsed = Date.now() - start;
    ctx.clearRect(0, 0, w, h);
    particles.forEach(function (p) {
      p.x += p.vx; p.y += p.vy; p.rot += p.vrot;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot * Math.PI / 180);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.r / 2, -p.r / 2, p.r, p.r * 0.6);
      ctx.restore();
    });
    if (elapsed < 2200) requestAnimationFrame(frame);
    else ctx.clearRect(0, 0, w, h);
  }
  frame();
}

var gateNamesCache = [];

function normalizeName(s) { return (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, ''); }

function makeNameChip(label, prenom) {
  var chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'name-chip';
  chip.textContent = label;
  chip.onclick = function () { goToPwStep(prenom); };
  return chip;
}

function openNameGate() {
  document.getElementById('name-step').style.display = 'flex';
  document.getElementById('pw-step').style.display = 'none';
  document.getElementById('gate-pw-error').style.display = 'none';
  resetEmojiPw();
  om('name-gate-overlay');
  var list = document.getElementById('name-chip-list');
  var input = document.getElementById('gate-search');
  list.innerHTML = '<p style="grid-column:1/-1;font-size:.85rem;color:var(--muted)">Chargement des prenoms…</p>';
  fetchJson('/api/users/public').then(function (names) {
    gateNamesCache = names;
    renderGateChips('');
    renderLastUserChip();
    if (input) { input.value = ''; setTimeout(function () { input.focus(); }, 60); }
  }).catch(function () {
    list.innerHTML = '<p style="grid-column:1/-1;font-size:.85rem;color:var(--muted)">Impossible de charger les prenoms.</p><button class="btn-ghost btn-small" onclick="openNameGate()">Réessayer</button>';
  });
}

function renderLastUserChip() {
  var box = document.getElementById('gate-last-user');
  if (!box) return;
  box.innerHTML = '';
  var last = localStorage.getItem('mdc-last-user');
  if (!last || gateNamesCache.indexOf(last) === -1) return;
  var chip = makeNameChip('↩ ' + last, last);
  chip.setAttribute('aria-label', 'Continuer en tant que ' + last);
  box.appendChild(chip);
}

function renderGateChips(query) {
  var list = document.getElementById('name-chip-list');
  var q = normalizeName(query.trim());
  list.innerHTML = '';
  if (!q) {
    list.innerHTML = '<p style="grid-column:1/-1;font-size:.85rem;color:var(--muted)"> Commence à taper ton prenom…</p>';
    return;
  }
  var matches = gateNamesCache.filter(function (n) { return normalizeName(n).indexOf(q) !== -1; });
  if (!matches.length) {
    list.innerHTML = '<p style="grid-column:1/-1;font-size:.85rem;color:var(--muted)">Aucun match pour « ' + esc(query) + ' ».</p>';
    return;
  }
  matches.slice(0, 8).forEach(function (n) { list.appendChild(makeNameChip(n, n)); });
}

function goToPwStep(prenom) {
  pendingPrenom = prenom;
  document.getElementById('name-step').style.display = 'none';
  document.getElementById('pw-step').style.display = 'flex';
  document.getElementById('pw-step-title').textContent = 'Bonjour ' + prenom;
  resetEmojiPw();
  loadEmojiKeypad();
}

function cancelGate() {
  cm('name-gate-overlay');
  if (PAGE === 'sr' && !currentSrUser) location.href = '/';
}

function loadEmojiKeypad() {
  var pad = document.getElementById('psswrd-keypad');
  if (EMOJI_KEYPAD.length) { renderEmojiKeypad(); return; }
  fetchJson('/api/psswrd-keypad').then(function (keys) {
    EMOJI_KEYPAD = keys;
    renderEmojiKeypad();
  }).catch(function () {
    pad.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Impossible de charger le pave.</p>';
  });
}
function renderEmojiKeypad() {
  var pad = document.getElementById('psswrd-keypad');
  pad.innerHTML = '';
  EMOJI_KEYPAD.forEach(function (e) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'psswrd-key';
    btn.textContent = e;
    btn.setAttribute('aria-label', 'Emoji ' + e);
    btn.onclick = function () { pressEmoji(e); };
    pad.appendChild(btn);
  });
}

function pressEmoji(e) {
  if (currentEmojiPw.length >= EMOJI_PW_LENGTH) return;
  if (currentEmojiPw.length === 0) {
    var err = document.getElementById('gate-pw-error');
    if (err) err.style.display = 'none';
  }
  currentEmojiPw.push(e);
  renderEmojiDots();
  if (currentEmojiPw.length === EMOJI_PW_LENGTH) validateGatePw();
}
function renderEmojiDots() {
  document.querySelectorAll('#psswrd-pw-dots .psswrd-dot').forEach(function (dot, i) {
    dot.textContent = currentEmojiPw[i] || '';
    dot.classList.toggle('filled', !!currentEmojiPw[i]);
  });
}
function resetEmojiPw() {
  currentEmojiPw = [];
  renderEmojiDots();
  var err = document.getElementById('gate-pw-error');
  if (err) err.style.display = 'none';
}

function validateGatePw() {
  if (currentEmojiPw.length !== EMOJI_PW_LENGTH) return;
  submitPasswordLogin('/api/sr/login', { prenom: pendingPrenom, password: currentEmojiPw }, 'gate-spinner', function (res) {
    currentSrUser = res.prenom;
    localStorage.setItem('mdc-last-user', res.prenom);
    cm('name-gate-overlay');
    setupExportButton();
    refreshAuthUI();
    if (PAGE === 'sr') loadSrToday();
  }, function () {
    resetEmojiPw();
    var errEl = document.getElementById('gate-pw-error');
    errEl.textContent = randomGateError();
    errEl.style.display = 'block';
  });
}

function setupExportButton() {
  var wrap = document.getElementById('sr-export-wrap');
  var btn = document.getElementById('sr-export-btn');
  if (!wrap || !btn) return;
  if (!currentSrUser) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  btn.onclick = function () {
    window.location.href = '/api/users/' + encodeURIComponent(currentSrUser) + '/export';
  };
}

function askConfirm(text, onOk) {
  document.getElementById('confirm-text').textContent = text;
  om('confirm-overlay');
  setTimeout(function () {
    var cancel = document.querySelector('#confirm-overlay .btn-ghost');
    if (cancel) cancel.focus();
  }, 60);
  document.getElementById('confirm-ok-btn').onclick = function () {
    closeConfirm();
    onOk();
  };
}

function submitPasswordLogin(url, body, spinnerId, onOk, onFail) {
  toggleSpinner(spinnerId, true);
  fetchJson(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (res) {
      toggleSpinner(spinnerId, false);
      if (res.ok) { onOk(res); } else { onFail(res); }
    })
    .catch(function (err) {
      toggleSpinner(spinnerId, false);
      if (err && err.data) { onFail(err.data); return; }
      onFail({ error: err && err.status ? 'Erreur serveur (' + err.status + ')' : 'Erreur reseau, reessaie.' });
    });
}

function checkPw() {
  submitPasswordLogin(
    '/api/login',
    { password: document.getElementById('pw-input').value },
    'login-spinner',
    function () {
      document.getElementById('pw-gate').style.display = 'none';
      document.getElementById('admin-form').style.display = 'block';

      loadChapitres().then(function () {
        loadParams();
        loadAdminList();
        loadStats();
        loadWeakCards();
        loadFailureNotes();

        var subjSelect = document.getElementById('qcm-subject-select');
        if (subjSelect && !subjSelect.dataset.bound) {
          subjSelect.dataset.bound = '1';
          subjSelect.addEventListener('change', function () { qcmTheme = ''; loadQcmEditor(); });
        }
        var themeSelect = document.getElementById('qcm-theme-select');
        if (themeSelect && !themeSelect.dataset.bound) {
          themeSelect.dataset.bound = '1';
          themeSelect.addEventListener('change', function () { qcmTheme = themeSelect.value; renderQcmEditorBuffer(); });
        }
        loadQcmEditor();
      });

      loadWeakQcm();
      loadQcmGames();
      var soBox = document.getElementById('stats-overview-box');
      if (soBox && !soBox.dataset.bound) {
        soBox.dataset.bound = '1';
        soBox.addEventListener('toggle', function () { if (soBox.open) loadStatsOverview(); });
      }
      var weakSubj = document.getElementById('qcm-weak-subject');
      if (weakSubj && !weakSubj.dataset.bound) {
        weakSubj.dataset.bound = '1';
        weakSubj.addEventListener('change', function () {
          var chSel = document.getElementById('qcm-weak-chapter');
          if (chSel) chSel.value = '';
          loadWeakQcm();
        });
      }
      var weakChap = document.getElementById('qcm-weak-chapter');
      if (weakChap && !weakChap.dataset.bound) {
        weakChap.dataset.bound = '1';
        weakChap.addEventListener('change', function () { loadWeakQcm(); });
      }
    },
    function (res) {
      document.getElementById('pw-error').textContent =
        res.error === "J'ai vraiment dit de pas brute forcer.... tu as fini dans un trou noir c'est malin"
          ? res.error
          : "Aie, c'est un conducteur parfait... l'onde est réfléchie";
      document.getElementById('pw-error').style.display = 'block';
    }
  );
}

var qcmPreviewQuestions = [];
var qcmPreviewIndex = 0;

function qcmPreviewStatus(message, isError) { flashStatus('qcm-preview-status', message, isError, 5000); }

function qcmPreviewImageUrl(ref) {
  ref = String(ref || '').trim();
  if (!ref) return '';
  if (/^https:\/\//i.test(ref)) return ref;
  return '/qcm-images/' + ref;
}

function normalizeQcmPreviewChoice(choice) {
  if (choice && typeof choice === 'object' && !Array.isArray(choice)) {
    return {
      text: String(choice.texte !== undefined ? choice.texte : (choice.text || '')).trim(),
      image: String(choice.image || '').trim()
    };
  }
  return { text: String(choice == null ? '' : choice).trim(), image: '' };
}

function validateQcmPreviewQuestion(question, position) {
  var prefix = 'Question ' + position + ' : ';
  if (!question || typeof question !== 'object' || Array.isArray(question)) {
    throw new Error(prefix + 'objet JSON attendu.');
  }
  var text = String(question.question || '').trim();
  if (!text) throw new Error(prefix + 'champ "question" manquant.');
  if (!Array.isArray(question.choix)) throw new Error(prefix + 'tableau "choix" manquant.');
  if (question.choix.length < 2 || question.choix.length > 4) {
    throw new Error(prefix + 'il faut entre 2 et 4 choix.');
  }
  var choices = question.choix.map(normalizeQcmPreviewChoice);
  choices.forEach(function (choice, i) {
    if (!choice.text && !choice.image) {
      throw new Error(prefix + 'choix ' + (i + 1) + ' : texte ou image requis.');
    }
  });
  var answer = Number(question.reponse);
  if (!Number.isInteger(answer) || answer < 1 || answer > choices.length) {
    throw new Error(prefix + '"reponse" doit être un numéro entre 1 et ' + choices.length + '.');
  }
  var timer = question.temps === undefined ? 30 : Number(question.temps);
  if (!Number.isInteger(timer) || timer < 10 || timer > 300) {
    throw new Error(prefix + '"temps" doit être un entier entre 10 et 300.');
  }
  return {
    question: text,
    image: String(question.image || '').trim(),
    choices: choices,
    answer: answer - 1,
    time: timer
  };
}

function appendPreviewImg(parent, src, alt) {
  var image = document.createElement('img');
  image.src = qcmPreviewImageUrl(src);
  image.alt = alt;
  image.loading = 'eager';
  parent.appendChild(image);
}

function previewQcmJson() {
  var editor = document.getElementById('qcm-preview-json');
  if (!editor) return;
  var raw;
  try { raw = JSON.parse(editor.value); }
  catch (err) { qcmPreviewStatus('JSON invalide : ' + err.message, true); return; }
  var list = Array.isArray(raw) ? raw : [raw];
  if (!list.length) { qcmPreviewStatus('Ajoute au moins une question.', true); return; }
  try {
    qcmPreviewQuestions = list.map(function (question, index) {
      return validateQcmPreviewQuestion(question, index + 1);
    });
  } catch (err) { qcmPreviewStatus(err.message, true); return; }
  qcmPreviewIndex = 0;
  renderQcmPreview();
  qcmPreviewStatus('✅ ' + qcmPreviewQuestions.length + ' question(s) prête(s) à tester.', false);
}

function renderQcmPreview() {
  if (!qcmPreviewQuestions.length) return;
  var q = qcmPreviewQuestions[qcmPreviewIndex];
  var render = document.getElementById('qcm-preview-render');
  var imageBox = document.getElementById('qcm-preview-image');
  var choicesBox = document.getElementById('qcm-preview-choices');

  render.style.display = 'block';
  document.getElementById('qcm-preview-number').textContent =
    'Question ' + (qcmPreviewIndex + 1) + ' / ' + qcmPreviewQuestions.length + ' · ' + q.time + ' s';
  document.getElementById('qcm-preview-question').textContent = q.question;

  imageBox.innerHTML = '';
  if (q.image) appendPreviewImg(imageBox, q.image, 'Illustration de l’énoncé');

  choicesBox.innerHTML = '';
  var shapes = ['▲', '◆', '●', '■'];
  q.choices.forEach(function (choice, index) {
    var card = document.createElement('div');
    card.className = 'qcm-preview-choice c' + index;
    if (index === q.answer) card.classList.add('correct');

    var shape = document.createElement('span');
    shape.className = 'qcm-shape';
    shape.textContent = shapes[index];

    var content = document.createElement('span');
    content.className = 'qcm-choice-content';
    if (choice.text) {
      var text = document.createElement('span');
      text.textContent = choice.text;
      content.appendChild(text);
    }
    if (choice.image) appendPreviewImg(content, choice.image, 'Illustration de la réponse');

    card.appendChild(shape);
    card.appendChild(content);
    choicesBox.appendChild(card);
  });

  document.getElementById('qcm-preview-prev').disabled = qcmPreviewIndex === 0;
  document.getElementById('qcm-preview-next').disabled = qcmPreviewIndex === qcmPreviewQuestions.length - 1;

  if (window.MathJax && MathJax.typesetPromise) {
    MathJax.typesetPromise([render]).catch(function (error) {
      console.warn('MathJax aperçu QCM :', error);
    });
  }
}

function moveQcmPreview(d) {
  var next = qcmPreviewIndex + d;
  if (next < 0 || next >= qcmPreviewQuestions.length) return;
  qcmPreviewIndex = next;
  renderQcmPreview();
}
function previousQcmPreview() { moveQcmPreview(-1); }
function nextQcmPreview() { moveQcmPreview(1); }

function clearQcmPreview() {
  var editor = document.getElementById('qcm-preview-json');
  var render = document.getElementById('qcm-preview-render');
  if (editor) editor.value = '';
  if (render) render.style.display = 'none';
  qcmPreviewQuestions = [];
  qcmPreviewIndex = 0;
  qcmPreviewStatus('Aperçu effacé.', false);
}

function loadQcmPreviewExample() {
  var editor = document.getElementById('qcm-preview-json');
  if (!editor) return;
  editor.value = JSON.stringify([
    {
      question: 'Calculer : $$\\int_0^1 x^2\\,\\mathrm{d}x$$',
      choix: ['$\\frac{1}{3}$', '$\\frac{1}{2}$', '$1$', '$2$'],
      reponse: 1, temps: 30
    },
    {
      question: 'Commentez cette photo.',
      image: 'https://i.postimg.cc/L4BSBFFk/Optique-scalaire-ride.jpg',
      choix: ['Elle est cool', 'Elle est SUPER cool'],
      reponse: 2, temps: 10
    }
  ], null, 2);
  previewQcmJson();
}

function loadParams() {
  fetchJson('/api/params').then(function (p) {
    params = p;
    document.getElementById('f-max-active').value = p.max_active_num;
    var hsMaxEl = document.getElementById('f-max-hs');
    if (hsMaxEl) hsMaxEl.value = p.max_hors_serie_num != null ? p.max_hors_serie_num : 0;
    var fEl = document.getElementById('f-fsrs-retention');
    if (fEl) fEl.value = Math.round((p.fsrs_retention || 0.80) * 100);
    var newEl = document.getElementById('f-daily-new');
    if (newEl) newEl.value = p.daily_new_limit != null ? p.daily_new_limit : 3;
    var revEl = document.getElementById('f-daily-review');
    if (revEl) revEl.value = p.daily_review_limit != null ? p.daily_review_limit : 3;
  }).catch(function (e) { console.error(e); });
}
function saveParams() {
  var retentionEl = document.getElementById('f-fsrs-retention');
  var newEl = document.getElementById('f-daily-new');
  var reviewEl = document.getElementById('f-daily-review');
  var body = {
    max_active_num: document.getElementById('f-max-active').value,
    max_hors_serie_num: (document.getElementById('f-max-hs') || {}).value || 0,
    fsrs_retention: retentionEl ? parseFloat(retentionEl.value) / 100 : 0.80,
    daily_new_limit: newEl ? newEl.value : 3,
    daily_review_limit: reviewEl ? reviewEl.value : 3,
  };
  fetchJson('/api/params', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function () {
      var msg = document.getElementById('saved-msg');
      msg.classList.add('show');
      setTimeout(function () { msg.classList.remove('show'); }, 2000);
    })
    .catch(function (err) {
      showToast(err && err.status === 401 ? 'Session admin expiree, reconnecte-toi.' : 'Erreur lors de l\'enregistrement.');
    });
}

function qcmEditorStatus(message, isError) { flashStatus('qcm-editor-status', message, isError, 6000); }

function qcmEditorFail(status, d) {
  qcmEditorStatus(status === 401
    ? 'Session admin expirée : reconnecte-toi.'
    : ((d && d.error) || ('Erreur ' + status)), true);
}

function qcmApiUrl(subject) {
  return '/api/admin/qcm/' + encodeURIComponent(subject) + '?_=' + Date.now();
}

function qcmThemeList() {
  var seen = [];
  qcmFullList.forEach(function (q) {
    var t = String(q.chapitre || '').trim();
    if (t && seen.indexOf(t) === -1) seen.push(t);
  });
  return seen;
}

function renderQcmThemeSelect() {
  var sel = document.getElementById('qcm-theme-select');
  if (!sel) return;
  var themes = qcmThemeList();
  var html = '<option value="">Tous les thèmes (' + qcmFullList.length + ')</option>';
  themes.forEach(function (t) {
    var n = qcmFullList.filter(function (q) { return String(q.chapitre || '').trim() === t; }).length;
    html += '<option value="' + esc(t) + '"' + (t === qcmTheme ? ' selected' : '') + '>' + esc(t) + ' (' + n + ')</option>';
  });
  sel.innerHTML = html;
  var row = document.getElementById('qcm-new-theme-row');
  if (row) row.style.display = 'flex';
}

function renderQcmEditorBuffer() {
  var editor = document.getElementById('qcm-json-editor');
  if (!editor) return;
  var subset = qcmTheme
    ? qcmFullList.filter(function (q) { return String(q.chapitre || '').trim() === qcmTheme; })
    : qcmFullList;
  editor.value = JSON.stringify(subset, null, 2) + '\n';
}

function createQcmTheme() {
  var input = document.getElementById('qcm-new-theme-input');
  var name = (input.value || '').trim();
  if (!name) return;
  //no duplicates: typing an existing name just selects that theme (case-insensitive)
  var dup = null;
  qcmThemeList().forEach(function (t) {
    if (t.toLowerCase() === name.toLowerCase()) dup = t;
  });
  if (dup) {
    qcmTheme = dup;
    input.value = '';
    renderQcmThemeSelect();
    var sel0 = document.getElementById('qcm-theme-select');
    if (sel0) sel0.value = dup;
    renderQcmEditorBuffer();
    qcmEditorStatus('« ' + dup + ' » existe deja, tu es dessus.', false);
    return;
  }
  qcmTheme = name;
  input.value = '';
  renderQcmThemeSelect();
  var sel = document.getElementById('qcm-theme-select');
  if (sel) {
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name + ' (0)';
    opt.selected = true;
    sel.appendChild(opt);
  }
  renderQcmEditorBuffer();
  qcmEditorStatus('Thème « ' + name + ' » prêt : colle ses questions ci-dessous, chaque question doit porter "chapitre": "' + name + '" (ou laisse, il sera ajouté au save).', false);
}

function loadQcmEditor() {
  var subjEl = document.getElementById('qcm-subject-select');
  var editor = document.getElementById('qcm-json-editor');
  if (!subjEl || !editor) return;
  qcmEditorStatus('Chargement de ' + subjEl.value + '…', false);
  fetchWithStatus(qcmApiUrl(subjEl.value), { cache: 'no-store' })
    .then(function (res) {
      var d = res.data || {};
      if (!d.ok) { qcmEditorFail(res.status, d); return; }
      try { qcmFullList = JSON.parse(d.raw || '[]'); }
      catch (e) { qcmFullList = []; }
      renderQcmThemeSelect();
      renderQcmEditorBuffer();
      qcmEditorStatus('Chargé depuis le disque' + (d.mtime
        ? ' - modifié le ' + new Date(d.mtime * 1000).toLocaleString('fr-FR')
        : ' - fichier absent (il sera créé à l’enregistrement)'), false);
    })
    .catch(function (e) { qcmEditorStatus('Erreur réseau : ' + e, true); });
}

function loadQcmPhysiqueEditor() { loadQcmEditor(); }

function formatQcmEditor() {
  var editor = document.getElementById('qcm-json-editor');
  if (!editor) return;
  try {
    editor.value = JSON.stringify(JSON.parse(editor.value), null, 2) + '\n';
    qcmEditorStatus('JSON formaté ✔', false);
  } catch (err) {
    qcmEditorStatus('JSON invalide : ' + err.message, true);
  }
}

function saveQcmEditor() {
  var subjEl = document.getElementById('qcm-subject-select');
  var editor = document.getElementById('qcm-json-editor');
  var spinner = document.getElementById('qcm-save-spinner');
  if (!subjEl || !editor) return;
  var edited;
  try { edited = JSON.parse(editor.value); }
  catch (err) { qcmEditorStatus('JSON invalide : ' + err.message, true); return; }
  if (!Array.isArray(edited)) { qcmEditorStatus('Le JSON doit être une liste de questions.', true); return; }

  if (qcmTheme) {
    edited = edited.map(function (q) {
      if (q && typeof q === 'object' && !Array.isArray(q)) {
        q.chapitre = qcmTheme;
      }
      return q;
    });
    var others = qcmFullList.filter(function (q) { return String(q.chapitre || '').trim() !== qcmTheme; });
    qcmFullList = others.concat(edited);
  } else {
    qcmFullList = edited;
  }

  var raw = JSON.stringify(qcmFullList, null, 2) + '\n';
  if (spinner) spinner.style.display = 'inline-block';
  qcmEditorStatus('Enregistrement…', false);
  fetchWithStatus(qcmApiUrl(subjEl.value), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ raw: raw }),
    cache: 'no-store'
  })
    .then(function (res) {
      if (spinner) spinner.style.display = 'none';
      var d = res.data || {};
      if (!d.ok) { qcmEditorFail(res.status, d); return; }
      qcmEditorStatus('Enregistré ✔ - ' + d.count + ' questions valides pour ' + d.theme + '.', false);
      showToast('QCM ' + d.theme + ' enregistré.');
      loadQcmEditor();
    })
    .catch(function (e) {
      if (spinner) spinner.style.display = 'none';
      qcmEditorStatus('Erreur réseau : ' + e, true);
    });
}


var weakQcmRows = [];

function fmtDateShort(iso) {
  if (!iso) return '—';
  var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[3] + '/' + m[2] + '/' + m[1] : '—';
}

function fmtDateTimeFR(iso) {
  if (!iso) return '—';
  var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? m[3] + '/' + m[2] + '/' + m[1] + ' ' + m[4] + 'h' + m[5] : '—';
}

function loadQcmGames() {
  var box = document.getElementById('qcm-games-table');
  if (!box) return;
  fetchJson('/api/admin/qcm/games', { cache: 'no-store' }).then(function (d) {
    if (!d || d.ok === false || !d.rows.length) {
      box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Aucune partie enregistrée pour l\'instant.</p>';
      return;
    }
    var medals = ['🥇', '🥈', '🥉'];
    var html = '';
    d.rows.forEach(function (g) {
      var podium = (g.podium || []).map(function (p, i) {
        return (medals[i] || '') + ' ' + esc(p.prenom) + ' (' + p.score + ')';
      }).join(' ');
      html += '<div class="admin-row"><div>' +
        '<div class="name">' + fmtDateTimeFR(g.created_at) + ' — ' + esc(g.themes || 'tous thèmes') + '</div>' +
        '<div class="meta">' + g.nb_players + ' joueurs · ' + g.nb_questions + ' questions</div></div>' +
        '<div class="meta" style="text-align:right">' + podium + '</div></div>';
    });
    box.innerHTML = html;
  }).catch(function () {
    box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Erreur réseau.</p>';
  });
}

function loadWeakQcm() {
  var box = document.getElementById('qcm-weak-table');
  if (!box) return;
  var subjectSel = document.getElementById('qcm-weak-subject');
  var chapterSel = document.getElementById('qcm-weak-chapter');
  var theme = subjectSel ? subjectSel.value : '';
  var chapitre = chapterSel ? chapterSel.value : '';
  box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Chargement…</p>';
  fetchJson('/api/admin/qcm/weak?theme=' + encodeURIComponent(theme) + '&chapitre=' + encodeURIComponent(chapitre), { cache: 'no-store' })
    .then(function (d) {
      if (!d || d.ok === false) {
        box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Chargement impossible.</p>';
        return;
      }
      weakQcmRows = d.rows || [];
      if (chapterSel) {
        var keep = chapitre;
        var opts = '<option value="">Tous les chapitres</option>';
        (d.chapitres || []).forEach(function (c) {
          opts += '<option value="' + esc(c) + '"' + (c === keep ? ' selected' : '') + '>' + esc(c) + '</option>';
        });
        chapterSel.innerHTML = opts;
        if (keep && (d.chapitres || []).indexOf(keep) === -1) chapterSel.value = '';
      }
      renderWeakQcm();
    })
    .catch(function (err) {
      if (err && err.status === 401) { showToast('Session admin expirée, reconnecte-toi.'); return; }
      box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Erreur réseau.</p>';
    });
}

function renderWeakQcm() {
  var box = document.getElementById('qcm-weak-table');
  if (!box) return;
  if (!weakQcmRows.length) {
    box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Aucune question pour ce filtre.</p>';
    return;
  }
  var html = '<table class="weak-table">' +
    '<thead><tr><th>Question</th><th title="Nombre de fois où la carte a été posée (tous joueurs confondus)">Sorties</th>' +
    '<th>Taux d\'échec</th><th title="Dernière fois où la carte est tombée">Dernière</th></tr></thead><tbody>';
  weakQcmRows.forEach(function (r) {
    var color = r.taux_echec >= 40 ? '#c0392b' : (r.taux_echec >= 20 ? '#e67e22' : '#27ae60');
    var label = r.question.length > 90 ? r.question.slice(0, 90) + '…' : r.question;
    html += '<tr' + (r.absente ? ' style="opacity:.55" title="Question retirée du fichier : stats conservées"' : '') + '>' +
      '<td><div style="font-size:.85rem">' + esc(label) + (r.absente ? ' <em>(retirée)</em>' : '') + '</div>' +
      '<div class="meta">' + esc(r.theme) + (r.chapitre ? ' · ' + esc(r.chapitre) : '') + '</div></td>' +
      '<td>' + r.sorties + '</td>' +
      '<td><span class="weak-badge" style="background:' + color + '">' + r.taux_echec + ' %</span></td>' +
      '<td>' + fmtDateShort(r.derniere) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table>';
  if (weakQcmRows.every(function (r) { return r.sorties === 0; })) {
    html += '<p class="hint" style="margin-top:.4rem">Aucune partie enregistrée pour l\'instant : les compteurs démarrent à la prochaine partie.</p>';
  }
  var retiredCount = weakQcmRows.filter(function (r) { return r.absente; }).length;
  if (retiredCount > 0) {
    html += '<p style="margin-top:.4rem"><button class="btn-ghost btn-small" onclick="purgeQcmRetired()">Purger les ' + retiredCount + ' question' + (retiredCount > 1 ? 's' : '') + ' retiree' + (retiredCount > 1 ? 's' : '') + '</button></p>';
  }
  box.innerHTML = html;
}

var MAX_UPLOAD_BYTES = 15 * 1024 * 1024;

function checkPdfFile(file, label) {
  if (!file) return true;
  var nameOk = /\.pdf$/i.test(file.name || '');
  var typeOk = !file.type || file.type === 'application/pdf';
  if (!nameOk || !typeOk) { showToast(label + ' : seuls les PDF sont acceptes.'); return false; }
  if (file.size > MAX_UPLOAD_BYTES) { showToast(label + ' : fichier trop volumineux (max 15 Mo).'); return false; }
  return true;
}
function checkPdfs(fiche, correction, bareme) {
  return checkPdfFile(fiche, 'Fiche') && checkPdfFile(correction, 'Correction') && checkPdfFile(bareme, 'Bareme');
}
function appendIf(fd, key, value) { if (value) fd.append(key, value); }

function uploadFiche() {
  var numero = document.getElementById('up-numero').value;
  var fiche = document.getElementById('up-fiche').files[0];
  var correction = document.getElementById('up-correction').files[0];
  var bareme = document.getElementById('up-bareme').files[0];
  var hsChecked = document.getElementById('up-hors-serie') && document.getElementById('up-hors-serie').checked;
  if ((!numero && !hsChecked) || !fiche || !correction) { showToast('Remplis le numero et les deux PDF (fiche + correction).'); return; }
  if (!checkPdfs(fiche, correction, bareme)) return;
  var fd = new FormData();
  if (numero) fd.append('numero', numero);
  fd.append('titre', document.getElementById('up-titre').value);
  fd.append('indices', document.getElementById('up-indices').value);
  fd.append('chapitre', document.getElementById('up-chapitre').value);
  appendIf(fd, 'difficulty', document.getElementById('up-difficulty').value);
  var hsUp = document.getElementById('up-hors-serie');
  fd.append('hors_serie', hsUp && hsUp.checked ? '1' : '0');
  fd.append('fiche_pdf', fiche);
  fd.append('correction_pdf', correction);
  appendIf(fd, 'bareme_pdf', bareme);
  toggleSpinner('upload-spinner', true);
  fetchJson('/api/forgecards/upload', { method: 'POST', body: fd })
    .then(function (res) {
      toggleSpinner('upload-spinner', false);
      var out = document.getElementById('upload-status');
      out.textContent = res.ok ? ('Fiche ' + (res.label || res.numero) + ' importee.') : ('Erreur : ' + res.error);
      if (res.ok) loadAdminList();
    })
    .catch(function (err) {
      toggleSpinner('upload-spinner', false);
      document.getElementById('upload-status').textContent = err && err.status === 401 ? 'Session admin expiree, reconnecte-toi.' : 'Erreur reseau pendant l\'import.';
    });
}

function adminRow(leftHtml, actionsHtml) {
  var row = document.createElement('div');
  row.className = 'admin-row';
  row.innerHTML = '<div>' + leftHtml + '</div><div class="admin-row-actions">' + actionsHtml + '</div>';
  return row;
}
function cardTitleHtml(c) {
  return '<div class="name">Fiche ' + esc(c.label || c.numero) + (c.titre ? (' - ' + esc(c.titre)) : '') +
    '<span class="chip-chapitre">' + esc(c.chapitre || 'Autre') + '</span>' +
    (c.hors_serie ? '<span class="chip-chapitre" style="background:rgba(230,126,34,.12);color:#e67e22">Hors-série</span>' : '') + '</div>';
}
var allAdminCards = [];
var currentFicheNumero = null;
function loadAdminList() {
  fetchJson('/api/forgecards').then(function (cards) {
    allAdminCards = cards.slice().sort(function (a, b) { return ((a.hors_serie ? 1 : 0) - (b.hors_serie ? 1 : 0)) || (a.numero - b.numero); });
    var lastNormal = null;
    allAdminCards.forEach(function (c) { if (!c.hors_serie) lastNormal = c.numero; });
    currentFicheNumero = lastNormal != null ? lastNormal : (allAdminCards.length ? allAdminCards[allAdminCards.length - 1].numero : null);
    var sel = document.getElementById('fiche-picker');
    if (sel && !sel.dataset.bound) {
      sel.dataset.bound = '1';
      sel.addEventListener('change', function () {
        currentFicheNumero = parseInt(sel.value, 10);
        showMaskedFicheNotes = false;
        renderAdminDetail();
      });
      document.getElementById('fiche-prev').onclick = function () { stepFiche(-1); };
      document.getElementById('fiche-next').onclick = function () { stepFiche(1); };
      document.addEventListener('keydown', function (e) {
        if (e.target.matches('input,textarea,select')) return;
        if (e.key === 'ArrowLeft') stepFiche(-1);
        if (e.key === 'ArrowRight') stepFiche(1);
      });
    }
    renderAdminDetail();
  }).catch(function (err) {
    if (err && err.status === 401) showToast('Session admin expiree, reconnecte-toi.');
  });
}

function stepFiche(d) {
  if (!allAdminCards.length) return;
  var idx = allAdminCards.findIndex(function (c) { return c.numero === currentFicheNumero; });
  idx = Math.max(0, Math.min(allAdminCards.length - 1, idx + d));
  currentFicheNumero = allAdminCards[idx].numero;
  showMaskedFicheNotes = false;
  renderAdminDetail();
}

function renderAdminDetail() {
  var list = document.getElementById('admin-list');
  if (!list) return;
  list.innerHTML = '';
  var sel = document.getElementById('fiche-picker');
  if (!allAdminCards.length) {
    if (sel) sel.innerHTML = '';
    list.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Aucune fiche importee.</p>';
    return;
  }
  if (sel) {
    sel.innerHTML = allAdminCards.map(function (c) {
      return '<option value="' + c.numero + '"' + (c.numero === currentFicheNumero ? ' selected' : '') + '>' +
        esc('Fiche ' + (c.label || c.numero) + (c.titre ? ' : ' + c.titre : '')) + '</option>';
    }).join('');
  }
  var c = allAdminCards.find(function (x) { return x.numero === currentFicheNumero; });
  if (!c) return;
  var w = allWeakCards.find(function (x) { return x.numero === currentFicheNumero; });
  var wstats = '';
  if (w) {
    var wcol = w.taux_echec_pct >= 40 ? '#c0392b' : (w.taux_echec_pct >= 20 ? '#e67e22' : '#27ae60');
    wstats = '<div class="meta" style="margin-top:.4rem"><span class="weak-badge" style="background:' + wcol + '">' + esc(w.taux_echec_pct) + ' % echec</span>' +
      ' ' + esc(w.nb_eleves + ' eleves - ' + w.total_revisions + ' revisions - duree moy. ' + (w.avg_duration_seconds != null ? fmtDuree(w.avg_duration_seconds) : '-')) + '</div>';
  }
  var row = adminRow(
    cardTitleHtml(c) +
    '<div class="meta">' + esc(c.fiche_file) + (c.bareme_file ? (' | bareme: ' + esc(c.bareme_file)) : ' | pas de bareme') + ' | difficulté : ' + esc(c.teacher_difficulty != null ? c.teacher_difficulty : (c.difficulty != null ? c.difficulty : '-')) + '</div>' + wstats,
    '<button class="btn-ghost btn-small" data-action="edit">Modifier</button>' +
    '<button class="btn-ghost btn-small" data-action="reset-stats">Reinitialiser stats</button>' +
    '<button class="btn-danger btn-small" data-action="delete">Supprimer</button>'
  );
  row.querySelector('[data-action="edit"]').onclick = function () { openEditModal(c); };
  row.querySelector('[data-action="delete"]').onclick = function () {
    askConfirm('Supprimer definitivement la fiche ' + c.numero + ' ?', function () { deleteFiche(c.numero); });
  };
  row.querySelector('[data-action="reset-stats"]').onclick = function () {
    askConfirm('Reinitialiser les statistiques de la fiche ' + c.numero + ' pour tous les eleves ? Cette action est irreversible.', function () { resetCardStats(c.numero); });
  };
  list.appendChild(row);

  var notesBox = document.createElement('div');
  notesBox.id = 'fiche-notes';
  notesBox.style.marginTop = '.75rem';
  var title = document.createElement('div');
  title.className = 'sr-block-title';
  title.style.marginBottom = '.4rem';
  title.textContent = 'Notes de blocage sur cette fiche';
  notesBox.appendChild(title);
  list.appendChild(notesBox);
  renderFicheNotes();
}

function openEditModal(c) {
  currentEditNumero = c.numero;
  document.getElementById('edit-numero').textContent = c.numero;
  document.getElementById('edit-titre').value = c.titre || '';
  document.getElementById('edit-indices').value = c.indices || '';
  document.getElementById('edit-difficulty').value = c.teacher_difficulty != null ? c.teacher_difficulty : (c.difficulty != null ? c.difficulty : '');
  var hsEdit = document.getElementById('edit-hors-serie');
  if (hsEdit) hsEdit.checked = !!c.hors_serie;
  var sel = document.getElementById('edit-chapitre');
  sel.innerHTML = chapitresList.map(function (ch) {
    return '<option value="' + esc(ch) + '"' + (ch === c.chapitre ? ' selected' : '') + '>' + esc(ch) + '</option>';
  }).join('');
  document.getElementById('edit-fiche').value = '';
  document.getElementById('edit-correction').value = '';
  document.getElementById('edit-bareme').value = '';
  document.getElementById('edit-remove-bareme').checked = false;
  document.getElementById('edit-remove-correction').checked = false;
  document.getElementById('edit-correction-hint').textContent = c.correction_file
    ? ('Correction actuelle : ' + c.correction_file + '. Laisse vide pour la garder.')
    : 'Aucune correction actuellement pour cette fiche.';
  document.getElementById('edit-bareme-hint').textContent = c.bareme_file
    ? ('Bareme actuel : ' + c.bareme_file + '. Laisse vide pour le garder.')
    : 'Aucun bareme actuellement pour cette fiche.';
  om('edit-overlay');
}

function saveEditFiche() {
  if (!currentEditNumero) return;
  var fiche = document.getElementById('edit-fiche').files[0];
  var correction = document.getElementById('edit-correction').files[0];
  var bareme = document.getElementById('edit-bareme').files[0];
  var removeBareme = document.getElementById('edit-remove-bareme').checked;
  var removeCorrection = document.getElementById('edit-remove-correction').checked;
  if (bareme && removeBareme) { showToast('Choisis soit un nouveau bareme, soit "Retirer le bareme", pas les deux.'); return; }
  if (correction && removeCorrection) { showToast('Choisis soit une nouvelle correction, soit "Retirer la correction", pas les deux.'); return; }
  if (!checkPdfs(fiche, correction, bareme)) return;
  var fd = new FormData();
  fd.append('titre', document.getElementById('edit-titre').value);
  fd.append('indices', document.getElementById('edit-indices').value);
  fd.append('chapitre', document.getElementById('edit-chapitre').value);
  appendIf(fd, 'difficulty', document.getElementById('edit-difficulty').value);
  var hsEdit2 = document.getElementById('edit-hors-serie');
  fd.append('hors_serie', hsEdit2 && hsEdit2.checked ? '1' : '0');
  appendIf(fd, 'fiche_pdf', fiche);
  appendIf(fd, 'correction_pdf', correction);
  appendIf(fd, 'bareme_pdf', bareme);
  if (removeBareme) fd.append('remove_bareme', '1');
  if (removeCorrection) fd.append('remove_correction', '1');
  toggleSpinner('edit-spinner', true);
  fetchJson('/api/forgecards/' + currentEditNumero, { method: 'PATCH', body: fd })
    .then(function (res) {
      toggleSpinner('edit-spinner', false);
      if (res.ok) { closeEditModal(); loadAdminList(); }
      else showToast('Erreur : ' + (res.error || 'modification impossible'));
    })
    .catch(function (err) {
      toggleSpinner('edit-spinner', false);
      showToast(err && err.status === 401 ? 'Session expiree, reconnecte-toi (mot de passe admin)' : 'Erreur reseau.');
    });
}

function resetCardStats(numero) {
  adminAction('/api/forgecards/' + numero + '/reset-stats', 'POST', function () {
    loadAdminList();
    loadWeakCards();
    loadStats();
    loadFailureNotes();
  }, 'reinitialisation impossible');
}
function deleteFiche(numero) {
  adminAction('/api/forgecards/' + numero, 'DELETE', function () { loadAdminList(); }, 'Erreur reseau lors de la suppression.');
}

function loadPrenomList() { loadStats(); }
function copyToClipboard(text, okMsg) {
  var done = function () { showToast(okMsg || 'Mot de passe copie dans le presse-papier.'); };
  var failed = function () { showToast('Copie impossible, selectionne-le a la main.'); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(function () { legacyCopy(text, done, failed); });
  } else {
    legacyCopy(text, done, failed);
  }
}
function legacyCopy(text, done, failed) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {
    if (document.execCommand('copy')) done(); else failed();
  } catch (e) { failed(); }
  document.body.removeChild(ta);
}

function deliverPassword(prenom, pw, label) {
  var joined = pw.join(' ');
  copyToClipboard(joined, label + prenom + ' copie : ' + joined);
}

function showUserPassword(prenom, btn) {
  fetchJson('/api/users/' + encodeURIComponent(prenom) + '/password')
    .then(function (res) {
      if (res.ok && res.password) {
        var pw = res.password.join(' ');
        btn.textContent = pw;
        btn.style.fontSize = '1.1rem';
        btn.title = 'Cliquer pour copier';
        btn.setAttribute('aria-label', 'Mot de passe de ' + prenom + ' - cliquer pour copier');
        btn.onclick = function () { copyToClipboard(pw); };
      } else {
        btn.textContent = 'Aucun mdp';
      }
    })
    .catch(function () { btn.textContent = 'Erreur'; });
}
function regenUserPassword(prenom) {
  fetchJson('/api/users/' + encodeURIComponent(prenom) + '/regen-password', { method: 'POST' })
    .then(function (res) {
      if (res.ok && res.password) deliverPassword(prenom, res.password, 'Nouveau mdp de ');
      else showToast('Erreur : ' + (res.error || 'regeneration impossible'));
    })
    .catch(function () { showToast('Erreur reseau.'); });
}
function addPrenom() {
  var input = document.getElementById('new-prenom');
  var prenom = input.value.trim();
  if (!prenom) return;
  fetchJson('/api/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prenom: prenom }) })
    .then(function (res) {
      input.value = '';
      loadStats();
      if (res && res.ok && res.password) deliverPassword(prenom, res.password, 'Mot de passe de ');
    })
    .catch(function () { showToast('Erreur reseau.'); });
}
function grantJoker(prenom) {
  adminAction('/api/users/' + encodeURIComponent(prenom) + '/joker', 'POST', function (res) {
    if (res && res.full) { showToast(prenom + ' a deja 2 jokers, plafond atteint.'); return; }
    showToast('🃏 Joker donne a ' + prenom + ' (total: ' + (res && res.jokers != null ? res.jokers : '?') + ').');
  }, 'Erreur reseau.');
}

function deletePrenom(prenom) {
  adminAction('/api/users/' + encodeURIComponent(prenom), 'DELETE', function () { loadStats(); loadWeakCards(); }, 'Erreur reseau.');
}

function joursDepuis(iso) {
  if (!iso) return 9999;
  var t = Date.parse(iso.replace(' ', 'T'));
  if (isNaN(t)) return 9999;
  return Math.floor((Date.now() - t) / 86400000);
}
function fmtDuree(sec) {
  if (sec == null || isNaN(sec)) return '-';
  var m = Math.floor(sec / 60);
  var s2 = Math.round(sec % 60);
  return m ? (m + ' min ' + String(s2).padStart(2, '0')) : (s2 + ' s');
}

function renderInactiveAlert(stats) {
  var box = document.getElementById('inactive-alert');
  if (!box) return;
  var inactifs = stats.filter(function (s) { return joursDepuis(s.last_review) >= 2 && joursDepuis(s.last_review) < 9999 && (s.due_today || 0) > 0; });
  var jamais = stats.filter(function (s) { return joursDepuis(s.last_review) >= 9999; });
  if (!inactifs.length && !jamais.length) { box.innerHTML = ''; return; }
  var parts = inactifs.map(function (s) { return esc(s.prenom) + ' (' + joursDepuis(s.last_review) + ' j)'; });
  if (jamais.length) parts.push(jamais.map(function (s) { return esc(s.prenom); }).join(', ') + ' (jamais)');
  box.innerHTML = '<div class="inactive-banner">🔴 Inactifs depuis 2 jours ou plus : ' + parts.join(', ') + '</div>';
}




function loadStats() {
  fetchJson('/api/users/stats').then(function (stats) {
    var list = document.getElementById('stats-list');
    if (!list) return;
    list.innerHTML = '';
    renderInactiveAlert(stats);
    if (!stats.length) {
      list.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Aucun prenom enregistre.</p>';
      return;
    }
    stats.forEach(function (s) {
      var total = s.total_reviews || 0;
      var taux = total ? Math.round(100 * (total - s.again) / total) : null;
      var jours = joursDepuis(s.last_review);
      var det = document.createElement('details');
      det.className = 'eleve-row';
      det.innerHTML =
        '<summary><span class="name">' + esc(s.prenom) + '</span>' +
        '<span class="meta">' + esc(total + ' rev. - ' + (taux != null ? taux + ' % reussite' : 'pas de revision') +
        ' - moy. ' + fmtDuree(s.avg_duration) + ' - ' + (jours >= 9999 ? 'jamais actif' : 'il y a ' + jours + ' j')) + '</span></summary>' +
        '<div class="eleve-detail">' +
        '<div class="meta">' + esc(s.easy + ' automatique - ' + s.good + ' reussi - ' + s.hard + ' difficile - ' + s.again + ' echec - ' + (s.todo_today != null ? s.todo_today + ' a faire aujourd hui (' + s.due_today + ' dues au total)' : s.due_today + ' du aujourd hui')) + '</div>' +
        '<div class="admin-row-actions" style="margin-top:.5rem">' +
        '<button class="btn-ghost btn-small" data-action="show-pw">Voir mdp</button>' +
        '<button class="btn-ghost btn-small" data-action="regen-pw">New mdp</button>' +
        '<button class="btn-ghost btn-small" data-action="joker" title="Donner un joker">🃏</button>' +
        '<button class="btn-ghost btn-small" data-action="deck">Deck</button>' +
        '<a class="btn-ghost btn-small" style="text-decoration:none;text-align:center" href="/api/users/' + encodeURIComponent(s.prenom) + '/export">Export CSV</a>' +
        '<button class="btn-danger btn-small" data-action="delete">Retirer</button></div>' +
        '<div class="deck-slot"></div></div>';
      det.querySelector('[data-action="show-pw"]').onclick = function () { showUserPassword(s.prenom, this); };
      det.querySelector('[data-action="regen-pw"]').onclick = function () { regenUserPassword(s.prenom); };
      det.querySelector('[data-action="joker"]').onclick = function () { grantJoker(s.prenom); };
      det.querySelector('[data-action="deck"]').onclick = function () {
        var slot = det.querySelector('.deck-slot');
        if (slot.dataset.loaded) { slot.innerHTML = ''; delete slot.dataset.loaded; this.textContent = 'Deck'; return; }
        this.textContent = 'Chargement…';
        loadDeck(s.prenom, slot, this);
      };
      det.querySelector('[data-action="delete"]').onclick = function () {
        askConfirm('Retirer ' + s.prenom + ' de la liste ? Cela supprimera aussi definitivement tout son historique de revisions.', function () { deletePrenom(s.prenom); });
      };
      list.appendChild(det);
    });
  }).catch(function (e) { console.error(e); });
}
function loadDeck(prenom, slot, btn) {
  var box = slot || document.getElementById('deck-view');
  if (!box) return;
  fetchJson('/api/users/' + encodeURIComponent(prenom) + '/deck').then(function (cards) {
    var rowsHtml = cards.map(function (c) {
      return '<div class="admin-row"><div>' + cardTitleHtml(c) +
        '<div class="meta">Difficulte ' + esc(c.difficulty ? c.difficulty.toFixed(1) : '-') + ' - Stabilite ' + esc(c.stability ? c.stability.toFixed(1) + 'j' : '-') + ' - ' + esc(c.repetitions || 0) + ' revisions - ' + esc(c.lapses || 0) + ' echecs</div></div>' +
        '<div class="meta">Prochaine: ' + esc(c.next_review ? fmtDateShort(c.next_review) : 'aujourd hui') + '</div></div>';
    }).join('');
    box.innerHTML = '<div class="admin-list" style="margin-top:.5rem">' + rowsHtml + '</div>';
    box.dataset.loaded = '1';
    if (btn) btn.textContent = 'Masquer';
  }).catch(function () {
    if (btn) btn.textContent = 'Deck';
    showToast('Impossible de charger le deck.');
  });
}
var allWeakCards = [];
function loadWeakCards() {
  fetchJson('/api/dashboard/weak-cards').then(function (rows) {
    allWeakCards = rows;
    renderAdminDetail();
  }).catch(function (e) { console.error(e); });
}

var allFailureNotes = [];
var showMaskedNotes = false;
var showMaskedFicheNotes = false;

function buildNoteItem(r, allowDelete) {
  var item = document.createElement('div');
  item.className = 'note-item' + (r.note_masquee ? ' note-masquee' : '');
  var meta = fmtDateTimeFR(r.created_at) + ' - ' + r.prenom + ' - Fiche ' + r.numero + (r.titre ? (' - ' + r.titre) : '');
  var canDelete = allowDelete === true;
  var actions = '<button type="button" class="note-mask" aria-label="' + (r.note_masquee ? 'Restaurer cette note' : 'Masquer cette note') + '">' + (r.note_masquee ? '👁' : '🙈') + '</button>';
  if (canDelete) actions += '<button type="button" class="note-delete" aria-label="Supprimer cette note">✕</button>';
  item.innerHTML = '<div class="note-actions">' + actions + '</div>' +
    '<div class="note-meta">' + esc(meta) + '</div><div>' + esc(r.note) + '</div>';
  item.querySelector('.note-mask').onclick = function () { toggleNoteMask(r.id, !r.note_masquee); };
  var del = item.querySelector('.note-delete');
  if (del) del.onclick = function () {
    askConfirm('Supprimer definitivement la note de ' + r.prenom + ' sur la fiche ' + r.numero + ' ? La revision est conservee.', function () { deleteFailureNote(r.id); });
  };
  return item;
}

function toggleNoteMask(id, masquer) {
  adminAction('/api/reviews/' + id + '/note/' + (masquer ? 'masquer' : 'restaurer'), 'POST', function () {
    loadFailureNotes();
    showToast(masquer ? 'Note masquee (retrouvable via la fiche).' : 'Note restauree.');
  }, 'action impossible');
}

function loadFailureNotes() {
  fetchJson('/api/dashboard/failure-notes').then(function (rows) {
    allFailureNotes = rows;
    renderFailureNotes();
    renderFicheNotes();
  }).catch(function (e) { console.error(e); });
}

function renderFailureNotes() {
  var box = document.getElementById('failure-notes-list');
  if (!box) return;
  var visibles = allFailureNotes.filter(function (r) { return !r.note_masquee; });
  var masquees = allFailureNotes.filter(function (r) { return r.note_masquee; });
  box.innerHTML = '';
  if (!visibles.length) {
    box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Aucune note de blocage visible.</p>';
  }
  visibles.forEach(function (r) { box.appendChild(buildNoteItem(r, false)); });
  var toggle = document.getElementById('notes-masked-toggle');
  var maskedBox = document.getElementById('failure-notes-masked');
  if (toggle) toggle.style.display = 'none';
  if (maskedBox) { maskedBox.style.display = 'none'; maskedBox.innerHTML = ''; }
}

function renderFicheNotes() {
  var box = document.getElementById('fiche-notes');
  if (!box) return;
  var notes = allFailureNotes.filter(function (r) { return r.numero === currentFicheNumero; });
  box.innerHTML = '';
  if (!notes.length) box.innerHTML = '<p class="hint">Aucune note sur cette fiche.</p>';
  notes.forEach(function (r) {
    var it = buildNoteItem(r, true);
    var m = it.querySelector('.note-mask');
    if (m) m.remove();
    box.appendChild(it);
  });
}
function deleteFailureNote(id) {
  adminAction('/api/reviews/' + id + '/note', 'DELETE', function () {
    loadFailureNotes();
    showToast('Note supprimee.');
  }, 'suppression impossible');
}

function purgeQcmRetired() {
  var subjectSel = document.getElementById('qcm-weak-subject');
  var chapterSel = document.getElementById('qcm-weak-chapter');
  var theme = subjectSel ? subjectSel.value : '';
  var chapitre = chapterSel ? chapterSel.value : '';
  var url = '/api/admin/qcm/purge-retired?theme=' + encodeURIComponent(theme) +
            '&chapitre=' + encodeURIComponent(chapitre);
  askConfirm('Supprimer definitivement les stats des questions retirees du fichier QCM' +
             (chapitre ? ' (chapitre ' + chapitre + ')' : '') +
             ' ? Cela efface leur historique de reponses pour tous les eleves.', function () {
    adminAction(url, 'POST', function (res) {
      loadWeakQcm();
      showToast('Purge : ' + (res.deleted || 0) + ' reponses supprimees (' + (res.questions || 0) + ' questions).');
    }, 'purge impossible');
  });
}

function exportCSV() { window.location.href = '/api/sr/export'; }

function exportCsv(kind) {
  window.location.href = '/api/admin/stats/export/' + encodeURIComponent(kind);
}

function loadStatsOverview() {
  var box = document.getElementById('stats-overview');
  if (!box) return;
  box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Calcul en cours…</p>';
  fetchJson('/api/admin/stats/overview', { cache: 'no-store' }).then(function (d) {
    if (!d || d.ok === false) {
      box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Calcul impossible.</p>';
      return;
    }
    var html = '';
    if (d.retention_reelle != null) {
      html += '<p style="font-size:.9rem;margin:.5rem 0"><strong>Rétention réelle</strong> (révisions hors nouvelles cartes) : <strong>' +
        d.retention_reelle + ' %</strong> de réussite — cible 90 %.</p>';
    }
    if (d.retention_jours && d.retention_jours.length) {
      html += '<div class="section-title" style="margin:.75rem 0 .4rem">Rétention quotidienne (60 derniers jours, hors nouvelles cartes)</div>';
      html += retentionChartSvg(d.retention_jours);
    }
    html += '<div class="section-title" style="margin:.75rem 0 .4rem">Jours actifs par semaine (8 dernières semaines)</div>';
    html += '<table class="weak-table"><thead><tr><th>Élève</th>' +
      d.semaines.map(function (w) { return '<th title="Semaine finissant le ' + w + '">' + w.slice(5).split('-').reverse().join('/') + '</th>'; }).join('') +
      '<th>Total</th></tr></thead><tbody>';
    d.assiduite.forEach(function (r) {
      html += '<tr><td>' + esc(r.prenom) + '</td>' +
        d.semaines.map(function (w) {
          var v = r[w] || 0;
          var bg = v >= 5 ? 'rgba(39,174,96,.25)' : (v >= 3 ? 'rgba(230,126,34,.18)' : (v >= 1 ? 'rgba(230,126,34,.07)' : ''));
          return '<td style="text-align:center;background:' + bg + '">' + v + '</td>';
        }).join('') +
        '<td style="text-align:center"><strong>' + r.total + '</strong></td></tr>';
    });
    html += '</tbody></table>';
    var maxH = Math.max.apply(null, d.heures.concat([1]));
    html += '<div class="section-title" style="margin:.75rem 0 .4rem">Heures de révision (toutes cartes)</div>';
    html += '<div style="display:flex;align-items:flex-end;gap:2px;height:60px">';
    d.heures.forEach(function (n, h) {
      html += '<div title="' + h + 'h : ' + n + '" style="flex:1;background:var(--primary,#01696f);opacity:.8;height:' +
        Math.max(2, Math.round(100 * n / maxH)) + '%"></div>';
    });
    html += '</div><div class="meta" style="text-align:center">0h — 23h</div>';
    if (d.qcm_mois.length) {
      html += '<div class="section-title" style="margin:.75rem 0 .4rem">QCM : taux d\'échec par chapitre et par mois</div>';
      html += '<table class="weak-table"><thead><tr><th>Mois</th><th>Chapitre</th><th>Sorties</th><th>Échec</th></tr></thead><tbody>';
      d.qcm_mois.forEach(function (r) {
        var taux = r.n ? Math.round(1000 * (r.n - (r.bonnes || 0)) / r.n) / 10 : 0;
        var color = taux >= 40 ? '#c0392b' : (taux >= 20 ? '#e67e22' : '#27ae60');
        html += '<tr><td>' + esc(r.mois) + '</td><td>' + esc(r.chapitre || 'Autre') + '</td>' +
          '<td>' + r.n + '</td><td><span class="weak-badge" style="background:' + color + '">' + taux + ' %</span></td></tr>';
      });
      html += '</tbody></table>';
    }
    box.innerHTML = html;
  }).catch(function () {
    box.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Erreur réseau.</p>';
  });
}
function exportStatsByCard() { window.location.href = '/api/forgecards/stats/export'; }

function selectDrawCount(n) {
  selectedDrawCount = n;
  document.querySelectorAll('.draw-count-btn').forEach(function (b) {
    var isActive = parseInt(b.dataset.count, 10) === n;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-pressed', String(isActive));
  });
}

function pdfLinksHtml(c, subjectLabel, baremeLabel, primary) {
  var html = '<a class="fiche-link' + (primary ? ' primary' : '') + '" href="/uploads/fiche/' + encodeURIComponent(c.fiche_file || (c.numero + '.pdf')) + '" target="_blank">' + subjectLabel + '</a>';
  html += '<a class="fiche-link" href="/uploads/fiche/' + encodeURIComponent(c.correction_file) + '" target="_blank">Voir la correction</a>';
  if (c.bareme_file) {
    html += '<a class="fiche-link" href="/uploads/fiche/' + encodeURIComponent(c.bareme_file) + '" target="_blank">' + baremeLabel + '</a>';
  }
  return html;
}

function wireIndicesToggle(scope, blockId, showLabel, hideLabel) {
  var toggleBtn = scope.querySelector('.toggle-btn');
  if (!toggleBtn) return;
  toggleBtn.onclick = function () {
    var block = document.getElementById(blockId);
    var isHidden = block.style.display !== 'block';
    block.style.display = isHidden ? 'block' : 'none';
    toggleBtn.textContent = isHidden ? hideLabel : showLabel;
    toggleBtn.setAttribute('aria-expanded', String(isHidden));
  };
}

function lancerTirage() {
  var button = document.getElementById('draw-btn');
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  toggleSpinner('draw-spinner', true);
  fetchJson('/api/draw?count=' + selectedDrawCount + (horsSerieMode ? '&hors_serie=1' : '')).then(function (cards) {
    toggleSpinner('draw-spinner', false);
    button.disabled = false;
    button.removeAttribute('aria-busy');
    var grid = document.getElementById('cards-grid');
    grid.innerHTML = '';
    cards.forEach(function (c) {
      var div = document.createElement('div');
      div.className = 'fiche-card' + (c.hors_serie ? ' fiche-hs' : '');
      var links = '<a class="card-stretch-link" href="/uploads/fiche/' + encodeURIComponent(c.fiche_file || (c.numero + '.pdf')) + '" target="_blank" aria-label="Ouvrir le sujet de la fiche ' + esc(c.numero) + '"></a>';
      links += '<div class="fiche-links">' + pdfLinksHtml(c, 'Voir le Sujet', 'Voir le Bareme');
      if (c.indices) {
        links += '<button class="toggle-btn" aria-expanded="false">Voir indices</button>';
      }
      links += '</div>';
      div.innerHTML = '<div class="fiche-number">' + esc(c.label || c.numero) + '</div>' +
        (c.chapitre ? ('<div class="fiche-chapitre">' + esc(c.chapitre) + '</div>') : '') +
        (c.titre ? ('<div class="fiche-titre">' + esc(c.titre) + '</div>') : '') +
        links +
        (c.indices ? ('<div class="hidden-block" id="indices-' + esc(c.numero) + '">' + esc(c.indices) + '</div>') : '');
      grid.appendChild(div);
      wireIndicesToggle(div, 'indices-' + c.numero, 'Voir indices', 'Cacher indices');
    });
    document.getElementById('results-section').style.display = 'block';
    document.getElementById('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (cards.length) {
      fireConfetti();
      showToast(cards.length + ' fiche' + (cards.length > 1 ? 's' : '') + ' tirée' + (cards.length > 1 ? 's' : '') + '.');
    }
  }).catch(function () {
    toggleSpinner('draw-spinner', false);
    button.disabled = false;
    button.removeAttribute('aria-busy');
    showToast('Impossible de tirer les fiches.');
  });
}

function renderHorsSerie(cards) {
  var box = document.getElementById('hors-serie-box');
  if (!box) return;
  var hs = (cards || []).filter(function (c) { return c.hors_serie; });
  box.style.display = hs.length ? '' : 'none';
  var list = document.getElementById('hors-serie-list');
  if (!list) return;
  list.innerHTML = '';
  hs.forEach(function (c) {
    var card = document.createElement('details');
    card.className = 'public-card';
    var html = '<summary><span class="name">Fiche ' + esc(c.label || c.numero) +
      (c.titre ? (' - ' + esc(c.titre)) : '') +
      '<span class="chip-chapitre">' + esc(c.chapitre || 'Autre') + '</span>' +
      '<span class="chip-chapitre" style="background:rgba(230,126,34,.12);color:#e67e22">Hors-série</span>' +
      '</span><span class="card-chevron" aria-hidden="true"></span></summary>';
    html += '<div class="public-card-actions">' + pdfLinksHtml(c, 'Voir le sujet', 'Voir le barème', true);
    if (c.indices) html += '<button class="toggle-btn" aria-expanded="false">Voir les indices</button>';
    html += '</div>';
    card.innerHTML = html +
      (c.indices ? '<div class="hidden-block" id="indices-hs-' + esc(c.numero) + '">' + esc(c.indices) + '</div>' : '');
    wireIndicesToggle(card, 'indices-hs-' + c.numero, 'Voir les indices', 'Cacher les indices');
    list.appendChild(card);
  });
}

function renderPublicList(cards) {
  var list = document.getElementById('public-list');
  list.innerHTML = '';
  renderHorsSerie(cards);
  cards = (cards || []).filter(function (c) { return !c.hors_serie; });
  var status = document.getElementById('liste-search-status');
  var search = document.getElementById('liste-search');
  var q = search ? search.value.trim() : '';
  if (status) {
    status.textContent = cards.length + ' fiche' + (cards.length > 1 ? 's' : '') + ' trouvée' + (cards.length > 1 ? 's' : '');
  }
  if (!cards.length) {
    list.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">' + (q ? ('Aucune fiche ne correspond a « ' + esc(q) + ' ».') : 'Aucune fiche trouvée.') + '</p>';
    return;
  }
  cards.forEach(function (c) {
    var card = document.createElement('details');
    card.className = 'public-card';
    var html = '<summary><span class="name">Fiche ' + esc(c.label || c.numero) + (c.titre ? (' - ' + esc(c.titre)) : '') + '<span class="chip-chapitre">' + esc(c.chapitre || 'Autre') + '</span></span><span class="card-chevron" aria-hidden="true">▾</span></summary>';
    html += '<div class="public-card-actions">' + pdfLinksHtml(c, 'Voir le sujet', 'Voir le bareme', true);
    if (c.indices) {
      html += '<button class="toggle-btn" aria-expanded="false">Voir les indices</button>';
    }
    html += '</div>';
    card.innerHTML = html + (c.indices ? ('<div class="hidden-block" id="indices-liste-' + esc(c.numero) + '">' + esc(c.indices) + '</div>') : '');
    wireIndicesToggle(card, 'indices-liste-' + c.numero, 'Voir les indices', 'Cacher les indices');
    list.appendChild(card);
  });
}
function loadPublicList() {
  var list = document.getElementById('public-list');
  list.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Chargement des fiches…</p>';
  fetchJson('/api/forgecards/public').then(function (cards) {
    allPublicCards = cards;
    renderPublicList(cards);
  }).catch(function () {
    list.innerHTML = '<p style="font-size:.85rem;color:var(--muted)">Impossible de charger les fiches.</p><button class="btn-ghost btn-small" onclick="loadPublicList()">Réessayer</button>';
  });
}
function clearListeSearch() {
  var search = document.getElementById('liste-search');
  if (!search) return;
  search.value = '';
  search.dispatchEvent(new Event('input'));
  search.focus();
}

function renderDailyStatus(data) {
  var box = document.getElementById('sr-daily-status');
  if (!box) return;
  box.style.display = 'block';
  var streak = data.streak || 0;
  var streakEl = document.getElementById('sr-streak-badge');
  if (streakEl) {
    streakEl.textContent = streak > 0 ? ('🔥 ' + streak + ' jour' + (streak > 1 ? 's' : '') + ' de suite') : 'Commence ta serie aujourd hui !';
    if (data.jokers > 0) {
      streakEl.textContent += ' et tu as ' + data.jokers + ' Joker' + (data.jokers > 1 ? 's' : '') + ' 🃏';
    }
  }
  var quotaEl = document.getElementById('sr-quota-badge');
  if (quotaEl) {
    quotaEl.textContent = 'Nouvelles: ' + data.new_done_today + '/' + data.new_limit + ' - Revisions: ' + data.review_done_today + '/' + data.review_limit;
  }
  var forecastEl = document.getElementById('sr-forecast-badge');
  if (forecastEl) {
    var f = data.tomorrow_forecast;
    forecastEl.textContent = f && f.total > 0
      ? 'Demain: ' + f.total + ' carte' + (f.total > 1 ? 's' : '') + ' t\'attendent (' + f.nb_new + ' nouvelle' + (f.nb_new > 1 ? 's' : '') + ' + ' + f.nb_review + ' revision' + (f.nb_review > 1 ? 's' : '') + ').'
      : 'Rien de prevu pour demain pour l\'instant.';
  }
}
function updateDailyStatusOnly() {
  fetchJson('/api/sr/today').then(function (data) {
    if (data) renderDailyStatus(data);
  }).catch(function (e) { console.error(e); });
}
function checkSrListEmpty() {
  var list = document.getElementById('sr-list');
  if (list && list.children.length === 0) {
    list.innerHTML = '<p>Bravo, tu as termine toutes tes cartes du jour ! Reviens demain.</p>';
    fireConfetti();
  }
}
function removeCardFromList(numero) {
  var div = document.querySelector('.sr-item[data-numero="' + numero + '"]');
  if (div) {
    div.style.transition = 'opacity .3s, transform .3s';
    div.style.opacity = '0';
    div.style.transform = 'scale(0.95)';
    setTimeout(function () { div.remove(); delete cardStartTimes[numero]; srDoneToday++; updateSrProgress(); checkSrListEmpty(); }, 300);
  } else {
    checkSrListEmpty();
  }
}

function setRateButtonsEnabled(numero, enabled) {
  var div = document.querySelector('.sr-item[data-numero="' + numero + '"]');
  if (div) div.querySelectorAll('.btn-rate').forEach(function (rb) { rb.disabled = !enabled; });
}

function setLinkEnabled(link, enabled) {
  if (!link) return;
  if (enabled) {
    if (link.dataset.href) link.href = link.dataset.href;
    link.classList.remove('disabled-link');
    link.removeAttribute('aria-disabled');
    link.removeAttribute('tabindex');
  } else {
    link.dataset.href = link.getAttribute('href') || link.dataset.href || '';
    link.removeAttribute('href');
    link.classList.add('disabled-link');
    link.setAttribute('aria-disabled', 'true');
    link.setAttribute('tabindex', '-1');
  }
}

function loadSrToday() {
  var list = document.getElementById('sr-list');
  list.innerHTML = '<p>Chargement de tes cartes…</p>';
  fetchJson('/api/sr/today').then(function (data) {
    renderDailyStatus(data);
    var cards = data.cards || [];
    list.innerHTML = '';
    srTotalToday = cards.length;
    srDoneToday = 0;
    updateSrProgress();
    if (!cards.length) {
      list.innerHTML = '<p>Aucune carte a reviser aujourd\'hui.</p>';
      if (data.auto_validated_streak) {
        var forecastEl = document.getElementById('sr-forecast-badge');
        if (forecastEl) forecastEl.textContent = 'Jour validé (Bravo tu as fait toutes les cartes du jour).';
      }
      return;
    }
    cards.forEach(function (c) {
      var div = document.createElement('div');
      div.className = 'sr-item';
      div.setAttribute('data-numero', c.numero);
      var html = '<div class="sr-top"><div class="sr-name">Fiche ' + esc(c.numero) + (c.titre ? (' - ' + esc(c.titre)) : '') + '<span class="chip-chapitre">' + esc(c.chapitre || 'Autre') + '</span>' + (c.was_new ? '<span class="chip-chapitre" style="background:rgba(230,126,34,.12);color:#e67e22">Nouvelle</span>' : '');
      html += '<div class="sr-block sr-toolbar">';
      html += '<a class="sr-chip" data-action="open-enonce" data-numero="' + esc(c.numero) + '" href="/uploads/fiche/' + encodeURIComponent(c.numero) + '.pdf" target="_blank">📄 Énoncé</a>';
      if (c.indices) {
        html += '<button class="sr-chip" data-action="toggle-indices" data-numero="' + esc(c.numero) + '" aria-expanded="false">💡 Indices</button>';
      }
      if (c.bareme_file) {
        html += '<a class="sr-chip disabled-link" aria-disabled="true" tabindex="-1" id="bareme-link-' + esc(c.numero) + '" data-href="/uploads/fiche/' + encodeURIComponent(c.bareme_file) + '" target="_blank">📊 Barème</a>';
      }
      html += '<a class="sr-chip disabled-link" aria-disabled="true" tabindex="-1" id="corr-link-' + esc(c.numero) + '" data-href="/uploads/fiche/' + encodeURIComponent(c.correction_file) + '" target="_blank">✅ Corrigé</a>';
      html += '</div>';
      if (c.indices) {
        html += '<div class="hidden-block" id="sr-indices-' + esc(c.numero) + '">' + esc(c.indices) + '</div>';
      }
      html += '<label class="sr-finish-label"><input type="checkbox" id="finished-' + esc(c.numero) + '" data-action="finished" data-numero="' + esc(c.numero) + '"> J\'ai fini, je veux voir le corrigé</label>';
      html += '<div class="sr-actions">' +
        '<div class="sr-block-title" style="margin-bottom:.4rem">Autoévaluation</div>' +
        '<div class="sr-grade-row">' +
        '<button class="btn-rate btn-again" disabled data-result="again" data-numero="' + esc(c.numero) + '">Pas réussi<small id="prev-' + esc(c.numero) + '-again">…</small></button>' +
        '<button class="btn-rate btn-hard" disabled data-result="hard" data-numero="' + esc(c.numero) + '">Difficile<small id="prev-' + esc(c.numero) + '-hard">…</small></button>' +
        '<button class="btn-rate btn-good" disabled data-result="good" data-numero="' + esc(c.numero) + '">Réussi<small id="prev-' + esc(c.numero) + '-good">…</small></button>' +
        '<button class="btn-rate btn-easy" disabled data-result="easy" data-numero="' + esc(c.numero) + '">Automatique<small id="prev-' + esc(c.numero) + '-easy">…</small></button>' +
        '</div>' +
        '<div class="advance-row"><button class="btn-ghost btn-small" data-action="advance" data-numero="' + esc(c.numero) + '">Repousser a demain</button></div>' +
        '</div>';

      div.innerHTML = html;

      var enonceLink = div.querySelector('[data-action="open-enonce"]');
      if (enonceLink) {
        enonceLink.addEventListener('click', function () {
          var numero = enonceLink.getAttribute('data-numero');
          if (!cardStartTimes[numero]) cardStartTimes[numero] = Date.now();
        });
      }

      div.querySelectorAll('.btn-rate').forEach(function (b) {
        b.onclick = function () {
          if (b.disabled) return;
          var numero = parseInt(b.getAttribute('data-numero'), 10);
          var result = b.getAttribute('data-result');
          if (result === 'again') {
            openAgainModal(numero);
            return;
          }
          setRateButtonsEnabled(numero, false);
          reviewCard(numero, result, '');
        };
      });
      var finishedCheckbox = div.querySelector('[data-action="finished"]');
      if (finishedCheckbox) {
        finishedCheckbox.onchange = function () {
          var numero = finishedCheckbox.getAttribute('data-numero');
          var corrLink = document.getElementById('corr-link-' + numero);
          var baremeLink = document.getElementById('bareme-link-' + numero);
          setLinkEnabled(corrLink, finishedCheckbox.checked);
          setLinkEnabled(baremeLink, finishedCheckbox.checked);
          div.querySelectorAll('.btn-rate').forEach(function (rb) { rb.disabled = !finishedCheckbox.checked; });
        };
      }
      var advanceBtn = div.querySelector('[data-action="advance"]');
      if (advanceBtn) {
        advanceBtn.onclick = function () {
          advanceBtn.disabled = true;
          advanceCard(parseInt(advanceBtn.getAttribute('data-numero'), 10));
        };
      }
      var indicesToggle = div.querySelector('[data-action="toggle-indices"]');
      if (indicesToggle) {
        indicesToggle.onclick = function () {
          var block = document.getElementById('sr-indices-' + indicesToggle.getAttribute('data-numero'));
          var isHidden = block.style.display !== 'block';
          block.style.display = isHidden ? 'block' : 'none';
          indicesToggle.textContent = isHidden ? 'Cacher les indices' : 'Voir les indices';
          indicesToggle.setAttribute('aria-expanded', String(isHidden));
        };
      }
      list.appendChild(div);
      queuePreview(c.numero, div);
    });
  }).catch(function (err) {
    if (err && err.status === 401) { openNameGate(); return; }
    list.innerHTML = '<p>Impossible de charger tes cartes.</p><button class="btn-ghost btn-small" onclick="loadSrToday()">Réessayer</button>';
  });
}

function loadPreview(numero) {
  fetchJson('/api/sr/' + numero + '/preview').then(function (data) {
    var map = { PAS_REUSSI: 'again', DIFFICILE: 'hard', J_AI_DU_REFLECHIR: 'good', ULTRA_FACILE: 'easy' };
    Object.keys(data.preview).forEach(function (key) {
      var el = document.getElementById('prev-' + numero + '-' + map[key]);
      if (el) el.textContent = data.preview[key].label;
    });
  }).catch(function (e) { console.error(e); });
}

function openAgainModal(numero) {
  pendingAgainCard = numero;
  document.getElementById('again-note-input').value = '';
  om('again-overlay');
  againNoteWire();
  setTimeout(function () { document.getElementById('again-note-input').focus(); }, 50);
}
function againNoteWire() {
  var overlay = document.getElementById('again-overlay');
  var input = document.getElementById('again-note-input');
  if (!overlay || !input) return;
  var btn = overlay.querySelector('[onclick="submitAgainNote()"]');
  var skip = overlay.querySelector('[onclick="skipAgainNote()"]');
  if (skip) skip.style.display = 'none';
  var sub = overlay.querySelector('.home-subtitle');
  if (sub) sub.textContent = "Obligatoire : indique les points du barème que tu as oublié ou pas réussis, ou l'endroit ou tu as bloqué (10 caracteres minimum).";
  var counter = document.getElementById('again-note-count');
  if (!counter) {
    counter = document.createElement('div');
    counter.id = 'again-note-count';
    counter.className = 'hint';
    counter.style.marginTop = '.35rem';
    input.parentNode.appendChild(counter);
  }
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = '1';
    input.addEventListener('input', againNoteUpdate);
  }
  againNoteUpdate();
}
function againNoteUpdate() {
  var overlay = document.getElementById('again-overlay');
  var input = document.getElementById('again-note-input');
  if (!overlay || !input) return;
  var btn = overlay.querySelector('[onclick="submitAgainNote()"]');
  var counter = document.getElementById('again-note-count');
  var len = input.value.trim().length;
  if (btn) btn.disabled = len < 10;
  if (counter) {
    counter.textContent = len + '/10 caracteres minimum';
    counter.style.color = len >= 10 ? 'var(--primary)' : '#c0392b';
  }
}
function closeAgainModal() {
  cm('again-overlay');
  pendingAgainCard = null;
}
function finishAgainReview(note) {
  var numero = pendingAgainCard;
  note = (note || '').trim();
  if (note.length < 10) { againNoteUpdate(); return; }
  closeAgainModal();
  if (numero == null) return;
  setRateButtonsEnabled(numero, false);
  reviewCard(numero, 'again', note);
}
function submitAgainNote() { finishAgainReview(document.getElementById('again-note-input').value); }

function reviewCard(numero, result, note) {
  var duration = null;
  if (cardStartTimes[numero]) {
    duration = Math.round((Date.now() - cardStartTimes[numero]) / 1000);
  }
  srPost(numero, 'review', { result: result, note: note || '', duration_seconds: duration },
    'Carte enregistree.', 'enregistree',
    function () { setRateButtonsEnabled(numero, true); });
}
function advanceCard(numero) {
  srPost(numero, 'advance', { days: 1 },
    'Carte reportee a demain.', 'reportee',
    function () {
      var btn = document.querySelector('.sr-item[data-numero="' + numero + '"] [data-action="advance"]');
      if (btn) btn.disabled = false;
    });
}
function updateSrProgress() {
  var el = document.getElementById('sr-progress');
  if (!el) return;
  if (srTotalToday > 0) {
    el.style.display = 'block';
    el.textContent = 'Progression : ' + srDoneToday + ' / ' + srTotalToday;
  } else {
    el.style.display = 'none';
    el.textContent = '';
  }
}

var previewObserver = null;
function queuePreview(numero, el) {
  if ('IntersectionObserver' in window) {
    if (!previewObserver) {
      previewObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            previewObserver.unobserve(entry.target);
            loadPreview(parseInt(entry.target.getAttribute('data-numero'), 10));
          }
        });
      }, { rootMargin: '100px' });
    }
    previewObserver.observe(el);
  } else {
    loadPreview(numero);
  }
}

var modalWasOpen = false;
var lastFocusedBeforeModal = null;

document.addEventListener('focusin', function (e) {
  if (!document.querySelector('.modal-overlay.open')) lastFocusedBeforeModal = e.target;
});

function syncModalState() {
  var openOverlay = document.querySelector('.modal-overlay.open');
  var isOpen = !!openOverlay;
  document.body.classList.toggle('modal-open', isOpen);
  if (isOpen && !modalWasOpen) {
    var focusables = openOverlay.querySelectorAll('button, input, textarea, select, a[href]');
    for (var i = 0; i < focusables.length; i++) {
      if (focusables[i].offsetParent !== null) { focusables[i].focus(); break; }
    }
  }
  if (!isOpen && modalWasOpen && lastFocusedBeforeModal && lastFocusedBeforeModal.focus) {
    lastFocusedBeforeModal.focus();
  }
  modalWasOpen = isOpen;
}

var modalClassObserver = new MutationObserver(syncModalState);
document.querySelectorAll('.modal-overlay').forEach(function (o) {
  modalClassObserver.observe(o, { attributes: true, attributeFilter: ['class'] });
  o.addEventListener('click', function (e) {
    if (e.target === o && !o.classList.contains('critical')) {
      if (o.id === 'name-gate-overlay') { cancelGate(); return; }
      o.classList.remove('open');
    }
  });
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    var gate = document.getElementById('name-gate-overlay');
    if (gate && gate.classList.contains('open')) { cancelGate(); closeMobileNav(); return; }
    document.querySelectorAll('.modal-overlay.open').forEach(function (o) { o.classList.remove('open'); });
    closeMobileNav();
  }
});

document.addEventListener('DOMContentLoaded', function () {
  applyTheme(localStorage.getItem('mdc-theme') || 'light');

  ['up-difficulty', 'edit-difficulty'].forEach(function (id) {
    var input = document.getElementById(id);
    if (!input) return;
    input.addEventListener('input', function () {
      var val = parseInt(input.value, 10);
      if (!isNaN(val)) {
        if (val < 1) input.value = 1;
        if (val > 5) input.value = 5;
      }
    });
  });

  var gateSearch = document.getElementById('gate-search');
  if (gateSearch) {
    gateSearch.addEventListener('input', function () { renderGateChips(gateSearch.value); });
    gateSearch.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        var first = document.querySelector('#name-chip-list .name-chip');
        if (first) { e.preventDefault(); first.click(); }
      }
    });
  }

  var search = document.getElementById('liste-search');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      var clearBtn = document.getElementById('liste-search-clear');
      if (clearBtn) clearBtn.style.display = q ? 'inline-block' : 'none';
      if (!q) { renderPublicList(allPublicCards); return; }
      var filtered = allPublicCards.filter(function (c) {
        return (c.titre || '').toLowerCase().indexOf(q) !== -1 || String(c.numero).indexOf(q) !== -1 || (c.chapitre || '').toLowerCase().indexOf(q) !== -1;
      });
      renderPublicList(filtered);
    });
  }

  var compteExportBtn = document.getElementById('compte-export-btn');
  if (compteExportBtn) {
    compteExportBtn.onclick = function () {
      if (currentSrUser) {
        window.location.href = '/api/users/' + encodeURIComponent(currentSrUser) + '/export';
      }
    };
  }

  if (document.getElementById('public-list')) loadPublicList();
  
  fetch('/api/sr/whoami')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.ok && d.prenom) {
        currentSrUser = d.prenom;
        setupExportButton();
      }
      refreshAuthUI();
      if (PAGE === 'sr') {
        if (currentSrUser) loadSrToday();
        else openNameGate();
      }
    })
    .catch(function () {
      refreshAuthUI();
      if (PAGE === 'sr') openNameGate();
    });
});
(function () {
  var btn = document.getElementById('capsule-play');
  var vid = document.getElementById('capsule-video');
  if (!btn || !vid) return;  // page sans capsule : on ne fait rien
  btn.addEventListener('click', function () {
    btn.style.display = 'none';
    vid.play();
  });
})();

function toggleHorsSerie() {
  horsSerieMode = !horsSerieMode;
  var sw = document.querySelector('.hs-switch');
  if (sw) {
    sw.classList.toggle('on', horsSerieMode);
    sw.setAttribute('aria-checked', String(horsSerieMode));
  }
  showToast(horsSerieMode ? 'MODE HORS-SÉRIE' : 'MODE HORS-SÉRIE DÉSACTIVÉ');
}

function toggleHsNumero(on) {
  var n = document.getElementById('up-numero');
  if (!n) return;
  n.disabled = false;
  n.placeholder = on ? 'vide = auto, ou h1, h2\u2026 pour remplacer' : '';
  if (on) n.value = '';
}

function initHorsSerieToggle() {
  var t = document.getElementById('hs-toggle');
  if (!t) return;
  fetchJson('/api/forgecards/public').then(function (cards) {
    if ((cards || []).some(function (c) { return c.hors_serie; })) t.style.display = 'flex';
  }).catch(function (e) { console.error(e); });
}
document.addEventListener('DOMContentLoaded', initHorsSerieToggle);

function retentionChartSvg(points) {
  var W = 600, H = 140, padL = 34, padB = 20, padT = 8, padR = 8;
  var n = points.length;
  var x = function (i) { return padL + (W - padL - padR) * (n === 1 ? 0.5 : i / (n - 1)); };
  var y = function (v) { return padT + (H - padT - padB) * (1 - v / 100); };
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto" role="img" aria-label="Rétention quotidienne">';
  [0, 50, 100].forEach(function (g) {
    svg += '<line x1="' + padL + '" y1="' + y(g) + '" x2="' + (W - padR) + '" y2="' + y(g) + '" stroke="rgba(128,128,128,.25)"/>' +
      '<text x="4" y="' + (y(g) + 3) + '" font-size="9" fill="var(--muted)">' + g + '%</text>';
  });
  svg += '<line x1="' + padL + '" y1="' + y(90) + '" x2="' + (W - padR) + '" y2="' + y(90) + '" stroke="#e67e22" stroke-dasharray="4 3"/>';
  svg += '<polyline points="' + points.map(function (p, i) { return x(i).toFixed(1) + ',' + y(p.taux).toFixed(1); }).join(' ') +
    '" fill="none" stroke="var(--primary,#01696f)" stroke-width="2"/>';
  points.forEach(function (p, i) {
    svg += '<circle cx="' + x(i).toFixed(1) + '" cy="' + y(p.taux).toFixed(1) + '" r="2.4" fill="var(--primary,#01696f)">' +
      '<title>' + p.d + ' : ' + p.taux + '% (' + p.n + ' révisions)</title></circle>';
  });
  svg += '<text x="' + padL + '" y="' + (H - 5) + '" font-size="9" fill="var(--muted)">' + points[0].d + '</text>' +
    '<text x="' + (W - padR) + '" y="' + (H - 5) + '" font-size="9" fill="var(--muted)" text-anchor="end">' + points[n - 1].d + '</text>';
  return svg + '</svg>';
}


/* --- SR : dernieres erreurs QCM avec explication --- */
function loadSrQcmErrors() {
  var box = document.getElementById('compte-qcm-errors');
  if (!box) return;
  fetchJson('/api/sr/qcm/errors', { cache: 'no-store' }).then(function (d) {
    if (!d || d.ok === false || !d.rows || !d.rows.length) {
      box.innerHTML = '<p class="hint">Aucune erreur QCM enregistree pour l\'instant.</p>';
      return;
    }
    var html = '';
    d.rows.forEach(function (r) {
      var when = fmtDateTimeFR(r.created_at);
      var theme = r.theme ? esc(r.theme) + (r.chapitre ? ' · ' + esc(r.chapitre) : '') : '';
      html += '<div class="note-item">' +
        '<div class="note-meta">' + esc(when) + (theme ? ' · ' + theme : '') + '</div>' +
        '<div style="font-weight:600">' + esc(r.question || '') + '</div>' +
        (r.bonne_reponse ? '<div class="meta" style="margin-top:.3rem">Bonne reponse : <strong>' + esc(r.bonne_reponse) + '</strong></div>' : '') +
        (r.explication ? '<div class="meta" style="margin-top:.3rem;white-space:pre-wrap">' + esc(r.explication) + '</div>' : '') +
        '</div>';
    });
    box.innerHTML = html;
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([box]).catch(function (e) { console.error(e); });
  }).catch(function () {
    box.innerHTML = '<p class="hint">Impossible de charger les erreurs QCM.</p>';
  });
}
