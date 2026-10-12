// Run: node checks/admin_content_browser.cjs. Requires Playwright + Chromium.
// Starts its own isolated temporary-data server unless CONTENT_CHECK_URL is set.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { existsSync, mkdirSync } = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { chromium } = require('playwright');
const repo = path.resolve(__dirname, '..');

async function freePort() {
  const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port; await new Promise(resolve => server.close(resolve)); return port;
}
async function run() {
  let child, browser, output = '';
  let base = process.env.CONTENT_CHECK_URL;
  try {
    if (!base) {
      const port = await freePort(); base = 'http://127.0.0.1:' + port;
      const python = existsSync(path.join(repo, 'venv/bin/python')) ? path.join(repo, 'venv/bin/python') : 'python3';
      child = spawn(python, ['checks/content_admin_checks.py', '--serve'], { cwd: repo, env: { ...process.env, CONTENT_CHECK_PORT: String(port) }, stdio: ['ignore', 'pipe', 'pipe'] });
      child.stdout.on('data', data => { output = (output + data).slice(-6000); }); child.stderr.on('data', data => { output = (output + data).slice(-6000); });
      let ready = false;
      for (let i = 0; i < 60; i++) {
        try { await fetch(base + '/admin'); ready = true; break; } catch (_) { await new Promise(resolve => setTimeout(resolve, 200)); }
      }
      assert.ok(ready, 'Fixture server failed to start: ' + output);
    }
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const probe = await context.request.get(base + '/__content_checks__');
    assert.ok(probe.ok() && (await probe.json()).isolated_content_fixture, 'Browser checks require an isolated fixture server.');
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message)); page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/admin'); await page.locator('#pw-input').fill('content-checks-only'); await page.locator('#pw-gate .btn-save').click();
    await page.locator('#admin-form').waitFor({ state: 'visible' });
    await page.locator('[data-atelier="qcm"]').click();
    await page.waitForFunction(() => qcmLoadedSubject === 'physique');
    assert.equal(await page.locator('#qcm-json-editor').isVisible(), false, 'JSON must be hidden in normal mode');
    await page.locator('#qcm-theme-select').selectOption('Optique');
    assert.equal(await page.locator('#qcm-question-list .content-question').count(), 1);
    assert.match(await page.locator('#qcm-question-list').innerText(), /physique.*Optique/);
    await page.locator('#qcm-add-question').click(); await page.locator('#content-question-dialog').waitFor({ state: 'visible' });
    await page.locator('#content-question-text').fill('Créée visuellement');
    await page.locator('[data-choice-text]').nth(0).fill('Première réponse'); await page.locator('[data-choice-text]').nth(1).fill('Bonne réponse');
    await page.locator('input[name="content-correct-answer"]').nth(1).check();
    await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    let card = page.locator('#qcm-question-list .content-question').filter({ hasText: 'Créée visuellement' }); await card.waitFor();
    const qid = await card.getAttribute('data-qid');
    assert.ok(JSON.parse(await page.locator('#qcm-json-editor').inputValue()).some(q => q.question === 'Créée visuellement'));
    assert.match(await page.locator('#content-subject-overview [data-subject="physique"]').innerText(), /3 questions/);
    await card.getByRole('button', { name: 'Modifier', exact: true }).click(); await page.locator('#content-question-text').fill('Modifiée visuellement');
    await page.locator('#content-choice-add').click(); await page.locator('[data-choice-text]').nth(2).fill('Troisième réponse'); await page.locator('input[name="content-correct-answer"]').nth(2).check();
    await page.locator('.content-choice').nth(1).getByRole('button', { name: 'Retirer cette réponse' }).click();
    await page.locator('#content-question-time').fill('45'); await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    card = page.locator('#qcm-question-list .content-question').filter({ hasText: 'Modifiée visuellement' }); await card.waitFor();
    assert.equal(await card.getAttribute('data-qid'), qid);
    let saved = JSON.parse(await page.locator('#qcm-json-editor').inputValue()).find(q => q.qid === qid);
    assert.equal(saved.reponse, 2); assert.equal(saved.choix.length, 2); assert.equal(saved.temps, 45);
    await card.getByRole('button', { name: 'Dupliquer' }).click(); await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    const copy = page.locator('#qcm-question-list .content-question').filter({ hasText: '(copie)' }); await copy.waitFor(); assert.notEqual(await copy.getAttribute('data-qid'), qid);
    await copy.getByRole('button', { name: 'Supprimer de la banque' }).click(); await copy.waitFor({ state: 'detached' });
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).chapters.find(ch => ch.chapitre === 'Mécanique').qcm_count, 1);

    await page.locator('#qcm-new-theme-input').fill('Thermodynamique'); await page.getByRole('button', { name: 'Créer le chapitre', exact: true }).first().click();
    await page.waitForFunction(() => qcmTheme === 'Thermodynamique');
    const chapterCard = page.locator('#qcm-chapter-overview .content-chapter').filter({ hasText: 'Thermodynamique' });
    assert.equal(await chapterCard.locator('input[type=checkbox]').isChecked(), false);
    await page.locator('#qcm-open-import').click();
    const csv = 'Question;Réponse A;Réponse B;Bonne réponse;Temps\nImportée A;A;B;B;30\nImportée B;A;B;A;20\n';
    await page.locator('#qcm-import-file').setInputFiles({ name: 'invalide.csv', mimeType: 'text/csv', buffer: Buffer.from(csv + 'Invalide;A;;A;30\n') });
    await page.locator('#qcm-import-preview').click(); await page.waitForFunction(() => document.querySelector('#qcm-import-result').textContent.includes('à corriger'));
    assert.equal(await page.locator('#qcm-import-confirm').isDisabled(), true);
    assert.match(await page.locator('#qcm-import-result').innerText(), /3 questions détectées.*2 valides.*1 à corriger/);
    await page.locator('#qcm-import-file').setInputFiles({ name: 'valide.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
    await page.locator('#qcm-import-preview').click(); await page.waitForFunction(() => !document.querySelector('#qcm-import-confirm').disabled);
    await page.locator('#qcm-import-confirm').click(); await page.waitForFunction(() => qcmFullList.filter(q => q.chapitre === 'Thermodynamique').length === 2);
    assert.equal(await chapterCard.locator('input[type=checkbox]').isChecked(), false);
    await chapterCard.locator('input[type=checkbox]').check(); await page.waitForFunction(() => qcmChapters.find(ch => ch.chapitre === 'Thermodynamique').qcm_active);
    await chapterCard.locator('input[type=checkbox]').uncheck(); await page.waitForFunction(() => !qcmChapters.find(ch => ch.chapitre === 'Thermodynamique').qcm_active);
    assert.equal(await page.locator('#qcm-question-list .content-question').count(), 2);

    await page.locator('#qcm-theme-select').selectOption('Optique'); await page.locator('#qcm-advanced > summary').click();
    let edited = JSON.parse(await page.locator('#qcm-json-editor').inputValue()); edited.find(q => q.qid === qid).question = 'Modifiée en mode avancé';
    await page.locator('#qcm-json-editor').fill(JSON.stringify(edited)); assert.match(await page.locator('#qcm-question-list').innerText(), /Modifiée en mode avancé/);
    await page.locator('#qcm-json-validate').click(); await page.getByRole('button', { name: /Enregistrer/, exact: false }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    await page.waitForFunction(() => qcmFullList.some(q => q.question === 'Modifiée en mode avancé') && !qcmSaving);
    await page.locator('#qcm-json-editor').fill('{ incorrect'); const before = await (await context.request.get(base + '/api/admin/qcm/physique')).json();
    await page.getByRole('button', { name: /Enregistrer/ }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).revision, before.revision);
    await page.locator('#qcm-json-editor').fill(JSON.stringify([{ question: 'Invalide', choix: {}, reponse: 1 }]));
    await page.getByRole('button', { name: /Enregistrer/ }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).revision, before.revision);
    await page.locator('#qcm-visual').getByRole('button', { name: 'Recharger la banque', exact: true }).click(); await page.waitForFunction(() => qcmLoadedSubject === 'physique');
    let held = false;
    await page.route('**/api/admin/qcm/physique?*', async route => { if (!held) { held = true; await new Promise(resolve => setTimeout(resolve, 500)); } await route.continue(); });
    await page.locator('#qcm-subject-select').selectOption('chimie'); await page.waitForFunction(() => qcmLoadedSubject === 'chimie');
    await page.locator('#qcm-subject-select').selectOption('physique'); await page.locator('#qcm-subject-select').selectOption('maths'); await page.waitForFunction(() => qcmLoadedSubject === 'maths'); await page.waitForTimeout(600);
    assert.equal(await page.locator('#qcm-subject-select').inputValue(), 'maths'); assert.match(await page.locator('#qcm-question-list').innerText(), /maths/); assert.doesNotMatch(await page.locator('#qcm-question-list').innerText(), /physique/);
    await page.unroute('**/api/admin/qcm/physique?*');

    await page.locator('[data-atelier="ankimie"]').click(); await page.waitForFunction(() => !document.querySelector('#ank-open-picker').disabled);
    await page.locator('#ank-admin-chapter').selectOption('Optique');
    assert.equal(await page.locator('#ank-collection-list .content-question').count(), 1);
    let releaseMembership;
    const membershipGate = new Promise(resolve => { releaseMembership = resolve; });
    await page.route('**/api/admin/ankimie/chimie/collections', async route => {
      if (route.request().method() !== 'PUT') { await route.continue(); return; }
      const response = await route.fetch(); await membershipGate; await route.fulfill({ response });
    });
    await page.locator('#ank-collection-list').getByRole('button', { name: 'Retirer de ce chapitre' }).click();
    assert.equal(await page.locator('#ank-admin-chapter').isDisabled(), true);
    for (const view of await page.locator('#ank-chapter-overview').getByRole('button', { name: 'Voir le chapitre' }).all()) assert.equal(await view.isDisabled(), true, 'Collection switching must not leave a pending membership save stuck');
    releaseMembership();
    await page.waitForFunction(() => document.querySelectorAll('#ank-collection-list .content-question').length === 0 && !document.querySelector('#ank-open-picker').disabled);
    await page.unroute('**/api/admin/ankimie/chimie/collections');
    assert.equal(JSON.parse((await (await context.request.get(base + '/api/admin/qcm/chimie')).json()).raw).length, 2);
    await page.locator('#ank-open-picker').click(); await page.locator('#ank-picker-search').fill('Optique'); await page.locator('#ank-picker-list').getByRole('button', { name: 'Ajouter au chapitre' }).click();
    await page.waitForFunction(() => document.querySelectorAll('#ank-collection-list .content-question').length === 1);
    await page.locator('#ank-new-chapter').fill('Révisions personnelles'); await page.locator('#ank-create-chapter').click(); await page.waitForFunction(() => document.querySelector('#ank-admin-chapter').value === 'Révisions personnelles');
    const ankChapter = page.locator('#ank-chapter-overview .content-chapter').filter({ hasText: 'Révisions personnelles' }); assert.equal(await ankChapter.locator('input[type=checkbox]').isChecked(), false);
    await page.locator('#ank-picker-search').fill('Mécanique'); await page.locator('#ank-picker-list').getByRole('button', { name: 'Ajouter au chapitre' }).click(); await page.waitForFunction(() => document.querySelectorAll('#ank-collection-list .content-question').length === 1);
    await ankChapter.locator('input[type=checkbox]').check();
    await page.waitForFunction(() => document.querySelector('#ank-chapter-overview .content-chapter.selected input').checked);

    await page.locator('[data-atelier="qcm"]').click();
    if (await page.locator('#qcm-advanced').evaluate(el => el.open)) await page.locator('#qcm-advanced > summary').click();
    const screenshots = process.env.CONTENT_CHECK_SCREENSHOTS;
    if (screenshots) mkdirSync(screenshots, { recursive: true });
    for (const theme of ['light', 'dark']) {
      await page.evaluate(t => applyTheme(t), theme);
      for (const width of [320,390,768,1280]) {
        await page.setViewportSize({ width, height: 844 });
        for (const key of ['qcm','ankimie']) {
          await page.locator('[data-atelier="' + key + '"]').click();
          if (key === 'ankimie') await page.waitForFunction(() => !document.querySelector('#ank-admin-chapter').disabled);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, key + ' overflow ' + width);
          const mainWidth = await page.locator('.atelier-main').evaluate(el => el.getBoundingClientRect().width);
          if (width <= 1000) assert.ok(mainWidth >= width - (width <= 600 ? 80 : 112), 'Content panel compressed at ' + width + 'px: ' + mainWidth);
          if (key === 'qcm') {
            await page.locator('#qcm-add-question').click(); await page.locator('#content-question-dialog').waitFor({ state: 'visible' });
            assert.equal(await page.locator('#content-question-text').evaluate(el => el === document.activeElement), true, 'Question form should focus its first field');
            assert.equal(await page.locator('#content-question-dialog').evaluate(el => el.scrollWidth > el.clientWidth), false, 'dialog overflow ' + width);
            if (width === 320) await page.keyboard.press('Escape'); else await page.locator('#content-question-cancel').click();
            await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
          }
          if (screenshots) await page.screenshot({ path: path.join(screenshots, theme + '-' + width + '-' + key + '.png'), fullPage: true });
        }
      }
    }
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: visual CRUD/duplication, choices, chapters, import, advanced JSON sync, stale responses, Ankimie collections, and 16 viewport/theme cases.');
  } finally {
    if (browser) await browser.close();
    if (child) child.kill('SIGTERM');
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
