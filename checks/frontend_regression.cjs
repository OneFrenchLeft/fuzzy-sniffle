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
