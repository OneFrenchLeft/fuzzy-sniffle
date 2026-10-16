// Exécution : node --test checks/frontend_regression.cjs
// Aucune application Flask ni donnée réelle : DOM et réponses HTTP simulés.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { before, after, test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/js/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function sandbox(elements = {}, page = '') {
  const messages = [];
  const context = vm.createContext({
    document: {
      body: { dataset: { page } },
      getElementById: id => elements[id] || null,
      querySelectorAll: () => [], addEventListener() {},
    },
    MutationObserver: class { observe() {} },
    setTimeout, clearTimeout,
    console: { error() {}, warn() {} },
    location: { href: '/current' },
  });
  vm.runInContext(source, context);
  context.showToast = message => messages.push(message);
  return { context, elements, messages };
}

test('le stockage refusé reste facultatif pour le thème et le dernier utilisateur', () => {
  const { context } = sandbox({ 'theme-toggle': { setAttribute() {} } });
  context.localStorage = {
    getItem() { throw new Error('SecurityError'); },
    setItem() { throw new Error('SecurityError'); },
  };
  context.document.documentElement = { setAttribute() {} };
  assert.equal(context.storedValue('mdc-theme', 'light'), 'light');
  assert.equal(context.storedValue('mdc-last-user', null), null);
  assert.doesNotThrow(() => context.applyTheme('dark'));
  assert.doesNotThrow(() => context.storeValue('mdc-last-user', 'Alice'));
});

test('la déconnexion attend sa réponse et conserve la session affichée en cas d’échec', async () => {
  const { context, messages } = sandbox({}, 'sr');
  context.currentSrUser = 'Alice';
  context.setupExportButton = context.refreshAuthUI = () => {};
  const first = deferred();
  let calls = 0;
  context.fetchJson = () => { calls++; return first.promise; };
  const result = context.logoutSr();
  context.logoutSr();
  assert.equal(calls, 1);
  assert.equal(context.currentSrUser, 'Alice');
  assert.equal(context.location.href, '/current');
  first.reject(new Error('Hors ligne'));
  await result;
  assert.equal(context.currentSrUser, 'Alice');
  assert.equal(context.location.href, '/current');
  assert.match(messages.at(-1), /toujours connecté/);
  const second = deferred();
  context.fetchJson = () => second.promise;
  const success = context.logoutSr();
  assert.equal(context.currentSrUser, 'Alice');
  second.resolve({ ok: true });
  await success;
  assert.equal(context.currentSrUser, null);
  assert.equal(context.location.href, '/');
});

test('une session Ankimie expirée rejoint la connexion sans chercher de modale absente', async () => {
  const { context } = sandbox({ 'ank-list': { innerHTML: '' } }, 'ankimie');
  context.fetchJson = () => Promise.reject({ status: 401 });
  context.loadAnkimieToday();
  await flush();
  assert.equal(context.location.href, '/compte');
});

test('une réponse ou erreur ancienne ne remplace pas les statistiques du filtre courant', async () => {
  const { context, elements } = sandbox({
    'qcm-weak-table': { innerHTML: '' },
    'qcm-weak-subject': { value: 'physique' },
    'qcm-weak-chapter': { value: '', innerHTML: '' },
  });
  const pending = [];
  context.fetchJson = () => { const d = deferred(); pending.push(d); return d.promise; };
  context.renderWeakQcm = () => { elements['qcm-weak-table'].innerHTML = context.weakQcmRows[0].question; };
  const old = context.loadWeakQcm();
  elements['qcm-weak-subject'].value = 'chimie';
  const current = context.loadWeakQcm();
  pending[1].resolve({ rows: [{ question: 'Chimie' }], chapitres: [] });
  await current;
  pending[0].resolve({ rows: [{ question: 'Physique' }], chapitres: ['Ancien chapitre'] });
  await old;
  assert.equal(elements['qcm-weak-table'].innerHTML, 'Chimie');
  assert.doesNotMatch(elements['qcm-weak-chapter'].innerHTML, /Ancien chapitre/);
  const staleFailure = context.loadWeakQcm();
  const newest = context.loadWeakQcm();
  pending[3].resolve({ rows: [{ question: 'Résultat récent' }], chapitres: [] });
  await newest;
  pending[2].reject(new Error('Ancienne panne'));
  await staleFailure;
  assert.equal(elements['qcm-weak-table'].innerHTML, 'Résultat récent');
});

test('les liens PDF conservent les documents existants sans inventer de correction', () => {
  const { context } = sandbox();
  const card = { numero: 1, fiche_file: '1.pdf', correction_file: '', bareme_file: 'bareme.pdf' };
  const html = context.pdfLinksHtml(card, 'Sujet', 'Barème');
  assert.match(html, /1\.pdf/);
  assert.match(html, /bareme\.pdf/);
  assert.doesNotMatch(html, /Voir la correction|href="\/uploads\/fiche\/"/);
  assert.match(context.pdfLinksHtml({ ...card, correction_file: 'corr.pdf' }, 'Sujet', 'Barème'), /corr\.pdf/);
});

test('la sauvegarde montre la rétention canonique sans écraser une nouvelle saisie', async () => {
  const { context, elements } = sandbox({
    'f-max-active': { value: '36' },
    'f-fsrs-retention': { value: '79' },
    'f-ankimie-retention': { value: '85' },
  });
  const request = deferred();
  context.fetchJson = () => request.promise;
  const saved = context.saveParams();
  elements['f-ankimie-retention'].value = '92';
  request.resolve({ ok: true, params: { fsrs_retention: 0.80, ankimie_retention: 0.86 } });
  await saved;
  assert.equal(elements['f-fsrs-retention'].value, 80);
  assert.equal(elements['f-ankimie-retention'].value, '92');
  const template = fs.readFileSync(path.join(root, 'templates/admin.html'), 'utf8');
  assert.match(template, /id="f-fsrs-retention"[^>]*min="80"/);
});

test('un échec du suivi des élèves affiche une relance qui permet le rétablissement', async () => {
  const { context, elements } = sandbox({ 'stats-list': { innerHTML: '' } });
  context.fetchJson = () => Promise.reject(new Error('Indisponible'));
  await context.loadStats();
  assert.match(elements['stats-list'].innerHTML, /Impossible de charger le suivi/);
  assert.match(elements['stats-list'].innerHTML, /onclick="loadStats\(\)"/);
  context.fetchJson = () => Promise.resolve([]);
  await context.loadStats();
  assert.match(elements['stats-list'].innerHTML, /Aucun prenom/);
  assert.doesNotMatch(elements['stats-list'].innerHTML, /Réessayer/);
});

test('les réponses personnelles anciennes ne remplacent pas le compte courant', async () => {
  const elements = { 'compte-dash': {}, 'compte-qcm-errors': { innerHTML: '' }, 'dash-total': {} };
  const { context } = sandbox(elements, 'compte');
  const requests = [];
  context.typesetMath = () => {};
  context.fetchJson = url => { const request = deferred(); requests.push({ url, ...request }); return request.promise; };
  context.currentSrUser = 'Alice'; context.loadCompteDashboard();
  context.currentSrUser = 'Bob'; context.loadCompteDashboard();
  requests[2].resolve({ ok: true, rows: [{ question: 'Erreur de Bob' }] });
  requests[3].resolve({ ok: true, total: 2 });
  await flush();
  requests[0].resolve({ ok: true, rows: [{ question: 'Erreur privée Alice' }] });
  requests[1].resolve({ ok: true, total: 100 });
  await flush();
  assert.equal(elements['dash-total'].textContent, 2);
  assert.match(elements['compte-qcm-errors'].innerHTML, /Bob/);
  assert.doesNotMatch(elements['compte-qcm-errors'].innerHTML, /Alice/);
  context.document.querySelector = () => null;
  context.currentSrUser = null;
  context.refreshAuthUI();
  assert.equal(elements['compte-qcm-errors'].innerHTML, '');
  assert.equal(elements['dash-total'].textContent, '—');
});

test('la recherche retrouve le code public des hors-séries', () => {
  const { context } = sandbox();
  context.allPublicCards = [{ numero: 9001, code: 'h1', label: 'H1', titre: 'Diffusion', hors_serie: true }];
  assert.equal(context.filterPublicCards('H1').length, 1);
});

test('les reprises Ankimie comptent les cartes uniques et restent annulables', async () => {
  const elements = { 'ank-list': { innerHTML: '', querySelector() { return null; } },
    'ank-progress': {}, 'ank-progressbar': { style: {} }, 'ank-progress-fill': { style: {} } };
  const { context } = sandbox(elements, 'ankimie');
  const renderEnd = context.renderNextAnkCard;
  context.renderNextAnkCard = () => context.updateAnkProgress();
  context.fireConfetti = () => {};
  const card = { qid: 'q', was_new: true };
  context.ankQueue = [card]; context.ankTotal = 1; context.ankSessionReset();
  await context.finishAnkCard(card, 'again', 10, 1);
  assert.equal(elements['ank-progress'].textContent, '1 / 1');
  await context.finishAnkCard(card, 'good', 10, 2);
  renderEnd();
  assert.match(elements['ank-list'].innerHTML, /dont 1 nouvelle/);
  assert.equal(context.ankSession.nouvelles, 1);
  context.fetchJson = async () => ({ ok: true, qid: 'q', review_id: 2 });
  await context.undoAnkReview();
  assert.equal(context.ankSession.nouvelles, 1);
  assert.equal(context.ankDone, 1);
  assert.equal(context.ankQueue[0].qid, 'q');
});

let browser;
before(async () => {
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
});
after(async () => { if (browser) await browser.close(); });

function html(page) {
  const template = fs.readFileSync(path.join(root, 'templates', page + '.html'), 'utf8');
  const content = template.match(/\{% block content %\}([\s\S]*?)\{% endblock %\}/)[1];
  const modals = template.match(/\{% block modals %\}([\s\S]*?)\{% endblock %\}/)[1];
  return '<!doctype html><html><meta charset="utf-8"><style>.modal-overlay:not(.open){display:none}</style>' +
    '<body data-page="' + page + '"><header><button id="hamburger-btn"></button><nav id="main-nav"></nav>' +
    '<button id="theme-toggle"></button></header><main>' + content + '</main>' + modals +
    '<div id="toast"></div><div id="toast-chrono"></div><script>window.katexDelimiters=[];window.renderMathInElement=function(){};</script>' +
    '<script>' + source + '</script></body></html>';
}

const cardA = { qid: 'a', question: 'Carte A', choices: [{ text: 'A', image: 'choice.png' }, { text: 'B', image: '' }], chapitre: 'Chapitre', image: 'diagram.png', was_new: false };
const cardB = { ...cardA, qid: 'b', question: 'Carte B', image: '' };

async function fixture(pageName, overrides = {}, storageDenied = false) {
  const context = await browser.newContext({ reducedMotion: 'reduce' });
  if (storageDenied) await context.addInitScript(() => {
    Storage.prototype.getItem = Storage.prototype.setItem = function () { throw new DOMException('Stockage refusé', 'SecurityError'); };
  });
  const page = await context.newPage();
  const requests = [], errors = [];
  page.setDefaultTimeout(5000);
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://frontend.test/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    requests.push({ pathname, request: route.request() });
    if (pathname === '/' + pageName) return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html(pageName) });
    if (overrides[pathname]) return overrides[pathname](route);
    let data;
    if (pathname === '/api/sr/whoami') data = { ok: true, prenom: 'Alice' };
    else if (pathname === '/api/sr/today') data = { cards: [{ numero: 1, titre: 'Fiche', chapitre: 'Chapitre', fiche_file: '1.pdf', correction_file: '' }] };
    else if (pathname === '/api/ankimie/today') data = { ok: true, cards: [cardA, cardB], new_limit: 0 };
    else if (pathname.endsWith('/preview')) data = { preview: {} };
    else if (pathname.endsWith('/answer')) data = { ok: true, answer: 0, intervals: {} };
    else if (pathname.startsWith('/qcm-images/')) return route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/c+UAAAAASUVORK5CYII=', 'base64') });
    else return route.fulfill({ status: 404, json: { ok: false } });
    return route.fulfill({ json: data });
  });
  await page.goto('http://frontend.test/' + pageName);
  return { context, page, requests, errors };
}

test('le refus du stockage ne bloque pas l’authentification ni le chargement Ankimie', async () => {
  const f = await fixture('ankimie', {}, true);
  try {
    await f.page.waitForFunction(() => currentSrUser === 'Alice' && ankQueue.length === 2);
    assert.equal(await f.page.locator('.ank-card').getAttribute('data-qid'), 'a');
    assert.deepEqual(f.errors, []);
  } finally { await f.context.close(); }
});

test('la liste affiche les quatre dernières évaluations et préserve recherche, mobile et déconnexion', async () => {
  const cards = [
    { numero: 1, titre: 'Conservation de l’énergie mécanique et hypothèses de travail', chapitre: 'Mécanique', last_result: 'easy' },
    { numero: 2, titre: 'Réussite', last_result: 'good' },
    { numero: 3, titre: 'Difficulté', last_result: 'hard' },
    { numero: 4, titre: 'À reprendre', last_result: 'again' },
    { numero: 5, titre: 'Jamais évaluée' },
    { numero: 9001, label: 'H1', titre: 'Bonus', hors_serie: true, last_result: 'easy' },
  ];
  let loggedIn = true;
  const f = await fixture('liste', {
    '/api/forgecards/public': route => route.fulfill({ json: cards.map(card => ({ ...card, last_result: loggedIn ? card.last_result : null })) }),
    '/api/sr/logout': route => { loggedIn = false; return route.fulfill({ json: { ok: true } }); },
  });
  try {
    await f.page.waitForFunction(() => currentSrUser === 'Alice' && document.querySelectorAll('.last-review-badge').length === 5);
    assert.deepEqual(await f.page.locator('#public-list .last-review-badge').allTextContents(), ['Automatique ✅', 'Réussi 👍', 'Difficile 🟠', 'Pas réussi ❌']);
    assert.equal(await f.page.locator('#public-list .public-card').last().locator('.last-review-badge').count(), 0);
    assert.equal(await f.page.locator('#hors-serie-list .last-review-badge').textContent(), 'Automatique ✅');
    await f.page.addStyleTag({ content: fs.readFileSync(path.join(root, 'static/css/main.css'), 'utf8') + fs.readFileSync(path.join(root, 'static/css/public.css'), 'utf8') });
    await f.page.locator('body').evaluate(el => el.classList.add('public-app'));
    for (const width of [320, 390, 1280]) for (const theme of ['light', 'dark']) {
      await f.page.setViewportSize({ width, height: 850 });
      await f.page.evaluate(theme => applyTheme(theme), theme);
      assert.equal(await f.page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await f.page.locator('#liste-search').fill('H1');
    await f.page.waitForFunction(() => document.querySelector('#liste-search-status').textContent === '1 fiche trouvée');
    assert.equal(await f.page.locator('#hors-serie-list .public-card').count(), 1);
    assert.equal(await f.page.locator('#liste-search-status').textContent(), '1 fiche trouvée');
    assert.doesNotMatch(await f.page.locator('#public-list').innerText(), /Aucune fiche/);
    await f.page.locator('#liste-search').fill('mécanique');
    await f.page.waitForFunction(() => document.querySelectorAll('#public-list .public-card').length === 1);
    await f.page.evaluate(() => loadPublicList());
    assert.equal(await f.page.locator('#public-list .public-card').count(), 1);
    assert.match(await f.page.locator('#public-list summary').innerText(), /Automatique ✅/);
    await f.page.evaluate(() => logoutSr());
    await f.page.waitForFunction(() => currentSrUser === null && document.querySelectorAll('#public-list .public-card').length === 1 && document.querySelectorAll('.last-review-badge').length === 0);
    assert.deepEqual(f.errors, []);
  } finally { await f.context.close(); }
});

test('une réponse de liste ancienne ne réaffiche pas les notes du compte précédent', async () => {
  const list = { innerHTML: '' };
  const { context } = sandbox({ 'public-list': list });
  const first = deferred(), second = deferred(), rendered = [];
  let count = 0;
  context.fetchJson = () => (++count === 1 ? first.promise : second.promise);
  context.renderPublicList = cards => rendered.push(cards);
  const oldLoad = context.loadPublicList();
  const newLoad = context.loadPublicList();
  second.resolve([{ numero: 1, last_result: null }]);
  await newLoad;
  first.resolve([{ numero: 1, last_result: 'easy' }]);
  await oldLoad;
  assert.equal(rendered.length, 1);
  assert.equal(context.allPublicCards[0].last_result, null);
  assert.equal(context.lastReviewBadgeHtml('<img onerror=alert(1)>'), '');
});

test('SR verrouille note, report et case pendant le POST puis permet de réessayer', async () => {
  const failure = deferred();
  const f = await fixture('sr', {
    '/api/sr/1/review': async route => { await failure.promise; await route.fulfill({ status: 503, json: { ok: false, error: 'Indisponible' } }); },
  });
  try {
    await f.page.locator('#finished-1').check();
    assert.equal(await f.page.locator('#corr-link-1').count(), 0);
    assert.match(await f.page.locator('.sr-finish-label').innerText(), /m’évaluer|m'évaluer/);
    await f.page.locator('.btn-good').click();
    await f.page.waitForFunction(() => !!srPending[1]);
    for (const selector of ['.btn-good', '[data-action="advance"]', '#finished-1']) assert.equal(await f.page.locator(selector).isDisabled(), true);
    await f.page.evaluate(() => {
      document.querySelector('[data-action="advance"]').onclick();
      document.querySelector('.btn-good').onclick();
      document.querySelector('#finished-1').onchange();
    });
    assert.equal(f.requests.filter(r => /\/(review|advance)$/.test(r.pathname)).length, 1);
    failure.resolve();
    await f.page.waitForFunction(() => !srPending[1]);
    assert.equal(await f.page.locator('.btn-good').isDisabled(), false);
    assert.equal(await f.page.locator('[data-action="advance"]').isDisabled(), false);
    await f.page.locator('#finished-1').uncheck();
    assert.equal(await f.page.locator('.btn-good').isDisabled(), true);
    assert.deepEqual(f.errors, []);
  } finally { failure.resolve(); await f.context.close(); }
});

test('Ankimie résout les images locales et leur zoom sans remplacer le quota zéro', async () => {
  const f = await fixture('ankimie');
  try {
    await f.page.locator('.ank-card').waitFor();
    assert.equal(await f.page.locator('#ank-quota-badge').innerText(), 'Nouvelles: 0/0');
    assert.equal(await f.page.locator('.ank-image img').getAttribute('src'), '/qcm-images/diagram.png');
    await f.page.locator('.ank-image img').click();
    assert.match(await f.page.locator('#img-zoom-img').getAttribute('src'), /\/qcm-images\/diagram\.png$/);
    await f.page.evaluate(() => cm('img-zoom-overlay'));
    await f.page.locator('.ank-show-answer').click();
    await f.page.locator('.ank-reveal').waitFor({ state: 'visible' });
    assert.equal(await f.page.locator('.ank-verdict-img').getAttribute('src'), '/qcm-images/choice.png');
    assert.equal(await f.page.evaluate(() => qcmPreviewImageUrl('https://example.test/photo.png')), 'https://example.test/photo.png');
    assert.equal(f.requests.some(r => r.pathname === '/diagram.png' || r.pathname === '/choice.png'), false);
    assert.deepEqual(f.errors, []);
  } finally { await f.context.close(); }
});

test('Ankimie sérialise les notes et annulations et envoie l’identifiant de révision attendu', async () => {
  const review = deferred(), undo = deferred();
  let undoBody;
  const f = await fixture('ankimie', {
    '/api/ankimie/a/review': async route => { await review.promise; await route.fulfill({ json: { ok: true, qid: 'a', review_id: 41 } }); },
    '/api/ankimie/undo': async route => { undoBody = route.request().postDataJSON(); await undo.promise; await route.fulfill({ json: { ok: true, qid: 'a', review_id: 41 } }); },
  });
  try {
    await f.page.locator('.ank-show-answer').click();
    await f.page.locator('.ank-grade').waitFor({ state: 'visible' });
    await f.page.locator('.btn-good').click();
    await f.page.waitForFunction(() => ankMutationPending);
    assert.equal(await f.page.locator('.btn-good').isDisabled(), true);
    await f.page.evaluate(() => { undoAnkReview(); document.querySelector('.btn-good').onclick(); });
    assert.equal(f.requests.filter(r => r.pathname.endsWith('/review')).length, 1);
    assert.equal(f.requests.filter(r => r.pathname === '/api/ankimie/undo').length, 0);
    review.resolve();
    await f.page.waitForFunction(() => !ankMutationPending && document.querySelector('.ank-card').dataset.qid === 'b');
    await f.page.locator('#ank-undo').click();
    await f.page.waitForFunction(() => ankMutationPending);
    assert.deepEqual(undoBody, { review_id: 41 });
    assert.equal(await f.page.locator('#ank-undo').isDisabled(), true);
    assert.equal(await f.page.locator('.ank-show-answer').isDisabled(), true);
    await f.page.evaluate(() => { undoAnkReview(); document.querySelector('.btn-good').onclick(); });
    assert.equal(f.requests.filter(r => r.pathname.endsWith('/review')).length, 1);
    assert.equal(f.requests.filter(r => r.pathname === '/api/ankimie/undo').length, 1);
    undo.resolve();
    await f.page.waitForFunction(() => !ankMutationPending && document.querySelector('.ank-card').dataset.qid === 'a');
    assert.equal(await f.page.evaluate(() => ankDone), 0);
    assert.equal(await f.page.locator('.ank-show-answer').isDisabled(), false);
    assert.deepEqual(f.errors, []);
  } finally { review.resolve(); undo.resolve(); await f.context.close(); }
});

for (const mode of ['réponse incohérente', 'révision périmée']) {
  test('Ankimie recharge le deck sans restaurer une carte lors d’une ' + mode, async () => {
    let reloads = 0;
    const f = await fixture('ankimie', {
      '/api/ankimie/today': route => { reloads++; return route.fulfill({ json: { ok: true, cards: [cardB], new_limit: 0 } }); },
      '/api/ankimie/undo': route => route.fulfill(mode === 'révision périmée'
        ? { status: 409, json: { ok: false, error: 'La dernière note a changé.' } }
        : { json: { ok: true, qid: 'b', review_id: 99 } }),
    });
    try {
      await f.page.locator('.ank-card').waitFor();
      await f.page.evaluate(() => {
        ankLastReview = { qid: 'a', reviewId: 41, card: { qid: 'a' }, counted: true };
        ankDone = 1; ankUpdateUndoBtn();
      });
      await f.page.locator('#ank-undo').click();
      await f.page.waitForFunction(() => !ankMutationPending && ankLastReview === null && document.querySelector('.ank-card')?.dataset.qid === 'b');
      assert.equal(reloads, 2);
      assert.equal(await f.page.evaluate(() => ankQueue.some(card => card.qid === 'a')), false);
      assert.deepEqual(f.errors, []);
    } finally { await f.context.close(); }
  });
}

test('Entrée active le bouton Aide Ankimie sans révéler la réponse', async () => {
  const f = await fixture('ankimie');
  try {
    await f.page.locator('.ank-card').waitFor();
    await f.page.locator('.info-btn').focus();
    await f.page.keyboard.press('Enter');
    await f.page.locator('#ank-info-overlay.open').waitFor();
    assert.equal(await f.page.locator('.ank-reveal').evaluate(el => el.style.display), 'none');
    assert.deepEqual(f.errors, []);
  } finally { await f.context.close(); }
});

test('une fiche sans chapitre connu se replie explicitement sur Autre', async () => {
  const page = await browser.newPage();
  try {
    const template = fs.readFileSync(path.join(root, 'templates/admin.html'), 'utf8');
    await page.setContent('<body data-page="admin">' + template + '</body>');
    await page.addScriptTag({ content: source });
    const chapter = await page.evaluate(() => {
      chapitresList = ['Diffusion thermique', 'Autre'];
      openEditModal({ numero: 1, chapitre: 'Ancien chapitre', titre: 'Existant' });
      return document.getElementById('edit-chapitre').value;
    });
    assert.equal(chapter, 'Autre');
  } finally { await page.close(); }
});


test('une correction devenue obsolète actualise la carte avant toute notation', async () => {
  let loads = 0;
  const f = await fixture('ankimie', {
    '/api/ankimie/today': route => route.fulfill({ json: { ok: true, cards: [++loads === 1 ? { ...cardA, revision: 'ancienne' } : { ...cardB, revision: 'actuelle' }] } }),
    '/api/ankimie/a/answer': route => {
      assert.equal(new URL(route.request().url()).searchParams.get('revision'), 'ancienne');
      return route.fulfill({ status: 409, json: { ok: false, error: 'La question a changé.' } });
    },
  });
  try {
    await f.page.locator('.ank-show-answer').click();
    await f.page.waitForFunction(() => document.querySelector('.ank-card')?.dataset.qid === 'b');
    assert.equal(loads, 2);
    assert.equal(f.requests.filter(r => r.pathname.endsWith('/review')).length, 0);
    assert.equal(await f.page.locator('.ank-reveal').evaluate(el => el.style.display), 'none');
    assert.deepEqual(f.errors, []);
  } finally { await f.context.close(); }
});
