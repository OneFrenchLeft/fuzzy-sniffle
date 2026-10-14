// Run: node checks/module_admin_browser.cjs. Uses only isolated temporary data.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { chromium } = require('playwright');
const repo = path.resolve(__dirname, '..');

async function run() {
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = 'http://127.0.0.1:' + port;
  const python = existsSync(path.join(repo, 'venv/bin/python')) ? path.join(repo, 'venv/bin/python') : 'python3';
  const child = spawn(python, ['checks/content_admin_checks.py', '--serve'], {
    cwd: repo, env: { ...process.env, CONTENT_CHECK_PORT: String(port) }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, output = '';
  child.stdout.on('data', data => { output = (output + data).slice(-5000); });
  child.stderr.on('data', data => { output = (output + data).slice(-5000); });
  try {
    let ready = false;
    for (let i = 0; i < 60; i++) {
      try {
        const marker = await (await fetch(base + '/__content_checks__')).json();
        assert.equal(marker.isolated_content_fixture, true); ready = true; break;
      } catch (_) { await new Promise(resolve => setTimeout(resolve, 200)); }
    }
    assert.ok(ready, output);
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    const page = await context.newPage(); page.setDefaultTimeout(10000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let confirmDelete = true, confirms = 0;
    page.on('dialog', async dialog => { confirms++; await (confirmDelete ? dialog.accept() : dialog.dismiss()); });
    let releaseParams;
    const paramsReady = new Promise(resolve => { releaseParams = resolve; });
    await page.route('**/api/params', async route => { await paramsReady; await route.continue(); }, { times: 1 });
    const incomplete = [];
    page.on('request', request => { if (/\/api\/admin\/qcm\/\?/.test(request.url())) incomplete.push(request.url()); });
    await page.goto(base + '/admin');
    await page.locator('#pw-input').fill('content-checks-only');
    await page.locator('#pw-gate .btn-save').click();
    await page.locator('#admin-form').waitFor({ state: 'visible' });
    await page.locator('[data-atelier="qcm"]').click();
    assert.equal(await page.locator('#qcm-subject-select').inputValue(), '');
    releaseParams();
    await page.waitForFunction(() => qcmLoadedSubject === 'physique');
    assert.deepEqual(incomplete, []);
    const params = async data => assert.ok((await context.request.post(base + '/api/params', { data })).ok());

    // A non-JSON error must stop the loading placeholder and allow a reload.
    await page.route('**/api/admin/qcm/physique?*', route => route.fulfill({ status: 502, contentType: 'text/html', body: '<h1>Bad gateway</h1>' }), { times: 1 });
    await page.locator('#qcm-visual').getByRole('button', { name: '⟳ Recharger', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#qcm-editor-status').textContent.includes('502'));
    assert.doesNotMatch(await page.locator('#qcm-question-list').innerText(), /Chargement de la banque/);
    assert.equal(await page.locator('#qcm-add-question').isDisabled(), true);
    await page.locator('#qcm-visual').getByRole('button', { name: '⟳ Recharger', exact: true }).click();
    await page.waitForFunction(() => qcmLoadedSubject === 'physique');

    // Disabled public module still permits the administrator's diagnostic tool.
    await params({ module_forgecards: 0 });
    await page.locator('[data-atelier="proba"]').click();
    await page.waitForFunction(() => probaLoaded && !probaLoading);
    assert.match(await page.locator('#proba-status').innerText(), /1 fiches éligibles/);
    assert.ok(await page.locator('#proba-chart > div').count());
    await page.route('**/api/probabilites/data?*', route => route.fulfill({ status: 503, json: { ok: false, error: 'Simulation temporairement indisponible.' } }), { times: 1 });
    await page.locator('#proba-retry').click();
    await page.waitForFunction(() => !probaLoading && !probaLoaded);
    assert.equal(await page.locator('#proba-status').innerText(), 'Simulation temporairement indisponible.');
    await page.locator('[data-atelier="settings"]').click();
    await page.locator('[data-atelier="proba"]').click();
    await page.waitForFunction(() => probaLoaded && !probaLoading);
    await page.locator('#proba-hs-switch').click();
    await page.waitForFunction(() => probaHsOn && probaLoaded && !probaLoading);

    // The editor exposes deletion only for a saved question.
    await page.locator('[data-atelier="qcm"]').click();
    await page.waitForFunction(() => qcmLoadedSubject === 'physique');
    await page.locator('#qcm-add-question').click();
    assert.equal(await page.locator('#content-question-delete').isVisible(), false);
    await page.locator('#content-question-cancel').click();
    let card = page.locator('#qcm-question-list .content-question').first();
    const qid = await card.getAttribute('data-qid');
    await card.getByRole('button', { name: 'Dupliquer', exact: true }).click();
    assert.equal(await page.locator('#content-question-delete').isVisible(), false);
    await page.locator('#content-question-cancel').click();
    await card.getByRole('button', { name: 'Modifier', exact: true }).click();
    await page.locator('#content-question-text').fill(''); // Invalid form must still allow deletion.
    let deleteRequests = 0;
    page.on('request', request => { if (request.method() === 'DELETE') deleteRequests++; });
    confirmDelete = false;
    await page.locator('#content-question-delete').click();
    assert.equal(deleteRequests, 0);
    assert.equal(await page.locator('#content-question-dialog').isVisible(), true);
    confirmDelete = true;
    await page.route('**/api/admin/qcm/physique/questions/' + qid, route => route.fulfill({ status: 409, json: { ok: false, error: 'La banque a changé. Recharge-la avant de supprimer.' } }), { times: 1 });
    await page.locator('#content-question-delete').click();
    await page.waitForFunction(() => document.querySelector('#content-question-error').textContent.includes('La banque a changé'));
    assert.equal(await page.locator('#content-question-dialog').isVisible(), true);
    assert.equal(await page.locator('#content-question-text').inputValue(), '');
    await page.waitForFunction(() => !document.querySelector('#content-question-delete').disabled);
    const deletion = page.waitForResponse(response => response.request().method() === 'DELETE' && response.url().endsWith(qid));
    await page.locator('#content-question-delete').click();
    assert.ok((await deletion).ok());
    await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('[data-qid="' + qid + '"]').count(), 0);
    assert.equal(await page.locator('#qcm-add-question').evaluate(el => el === document.activeElement), true);
    assert.ok(confirms >= 3);

    // A visual edit survives a proxy error and saves successfully on retry.
    card = page.locator('#qcm-question-list .content-question').first();
    const remainingQid = await card.getAttribute('data-qid');
    await card.getByRole('button', { name: 'Modifier', exact: true }).click();
    await page.locator('#content-question-text').fill('Modification à conserver');
    await page.route('**/api/admin/qcm/physique/questions/' + remainingQid, route => route.fulfill({ status: 502, contentType: 'text/html', body: '<h1>Bad gateway</h1>' }), { times: 1 });
    await page.locator('#content-question-save').click();
    await page.waitForFunction(() => document.querySelector('#content-question-error').textContent.includes('502'));
    assert.equal(await page.locator('#content-question-text').inputValue(), 'Modification à conserver');
    await page.waitForFunction(() => !document.querySelector('#content-question-save').disabled);
    await page.locator('#content-question-save').click();
    await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    assert.match(await page.locator('#qcm-question-list').innerText(), /Modification à conserver/);

    // The action stays visible and usable in both themes and on narrow screens.
    for (const width of [320, 390, 1440]) for (const theme of ['light', 'dark']) {
      await page.setViewportSize({ width, height: 850 });
      await page.evaluate(theme => applyTheme(theme), theme);
      card = page.locator('#qcm-question-list .content-question').first();
      await card.getByRole('button', { name: 'Modifier', exact: true }).click();
      const metrics = await page.locator('#content-question-delete').evaluate(el => {
        const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, height: r.height };
      });
      assert.ok(metrics.x >= 0 && metrics.right <= width && metrics.y >= 0 && metrics.bottom <= 850 && metrics.height >= 44);
      assert.equal(await page.locator('#content-question-dialog').evaluate(el => el.scrollWidth > el.clientWidth), false);
      await page.locator('#content-question-cancel').click();
    }
    // Preserve the existing list deletion as well.
    const listDelete = page.waitForResponse(response => response.request().method() === 'DELETE');
    await card.getByRole('button', { name: 'Supprimer', exact: true }).click();
    assert.ok((await listDelete).ok());

    // Account renders and fetches only the enabled modules, for all 8 combinations.
    assert.ok((await context.request.post(base + '/api/sr/login', { data: { prenom: 'Élève', password: ['1', '2', '3', '4'] } })).ok());
    for (let mask = 0; mask < 8; mask++) {
      const forgecards = !!(mask & 1), qcm = !!(mask & 2), ankimie = !!(mask & 4);
      await params({ module_forgecards: +forgecards, module_qcm: +qcm, module_ankimie: +ankimie });
      await page.setViewportSize({ width: mask % 2 ? 1440 : 320, height: 850 });
      const requests = [];
      const record = request => requests.push(new URL(request.url()).pathname);
      page.on('request', record);
      await page.goto(base + '/');
      if (forgecards) { assert.equal(new URL(page.url()).pathname, '/'); await page.goto(base + '/compte'); }
      else assert.equal(new URL(page.url()).pathname, '/compte');
      await page.waitForFunction(() => currentSrUser === 'Élève');
      await page.waitForLoadState('networkidle');
      assert.equal(await page.locator('#compte-logged-in').isVisible(), true);
      assert.equal(await page.locator('#compte-dash').count(), +forgecards);
      assert.equal(await page.locator('#sr-hs-enabled').count(), +forgecards);
      assert.equal(await page.locator('#compte-qcm-errors').count(), +qcm);
      assert.equal(await page.locator('nav a[href="/ankimie"]').count(), +ankimie);
      assert.equal(requests.includes('/api/sr/dashboard'), forgecards);
      assert.equal(requests.includes('/api/sr/preferences'), forgecards);
      assert.equal(requests.includes('/api/sr/qcm/errors'), qcm);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      page.off('request', record);
    }
    await params({ module_forgecards: 0 });
    await context.request.post(base + '/api/sr/logout');
    await page.goto(base + '/');
    await page.locator('#compte-logged-out').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: 'Se connecter', exact: true }).click();
    await page.locator('#name-gate-overlay.open').waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: delayed subject loading, HTML error recovery and draft retention, simulation retry, editor/list deletion, 6 modal layouts, 8 module combinations and login access.');
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
