// Exécution : node checks/admin_content_browser.cjs (Playwright et Chromium).
// Le serveur utilise des données temporaires ; une URL fournie doit exposer le marqueur de test.
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
    const chapterRow = name => page.locator('#qcm-chapter-overview .chapter-row').filter({ has: page.locator('.chapter-name', { hasText: new RegExp('^' + name + '$') }) });
    async function selectChapter(name) {
      const button = chapterRow(name).locator('.chapter-pick');
      if (await button.isEnabled()) await button.click();
    }
    async function openView(key) {
      await page.locator('[data-atelier="' + key + '"]').click();
      await page.locator('#atelier-' + key).waitFor({ state: 'visible' });
    }
    async function bank(theme) {
      const response = await context.request.get(base + '/api/admin/qcm/' + theme);
      assert.equal(response.status(), 200);
      return response.json();
    }
    await page.goto(base + '/admin'); await page.locator('#pw-input').fill('content-checks-only'); await page.locator('#pw-gate .btn-save').click();
    await page.locator('#admin-form').waitFor({ state: 'visible' });
    await openView('qcm');
    await page.waitForFunction(() => qcmLoadedSubject === 'physique');
    assert.equal(await page.locator('#qcm-json-editor').isVisible(), false, 'JSON must be hidden in normal mode');
    assert.equal(await page.locator('#qcm-visual .content-help').evaluate(el => el.open), false);
    await selectChapter('Optique');
    assert.equal(await page.locator('#qcm-current-path').innerText(), 'Physique > Optique');
    assert.equal(await page.locator('#qcm-question-list .content-question').count(), 1);
    assert.match(await page.locator('#qcm-question-list').innerText(), /physique.*Optique/);
    await page.locator('#qcm-add-question').click(); await page.locator('#content-question-dialog').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#content-question-delete').isVisible(), false, 'A new question must not offer deletion');
    assert.match(await page.locator('#content-question-title').innerText(), /Ajouter une question à Physique > Optique/);
    assert.equal(await page.locator('#content-question-chapter').isVisible(), false, 'Chapter scope should be automatic');
    await page.locator('#content-question-text').fill('Créée visuellement');
    await page.locator('[data-choice-text]').nth(0).fill('Première réponse'); await page.locator('[data-choice-text]').nth(1).fill('Bonne réponse');
    await page.locator('input[name="content-correct-answer"]').nth(1).check();
    await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    let card = page.locator('#qcm-question-list .content-question').filter({ hasText: 'Créée visuellement' }); await card.waitFor();
    const qid = await card.getAttribute('data-qid');
    assert.ok(JSON.parse(await page.locator('#qcm-json-editor').inputValue()).some(q => q.question === 'Créée visuellement'));
    assert.match(await chapterRow('Tous les chapitres').innerText(), /3 questions/);
    await card.getByRole('button', { name: 'Modifier', exact: true }).click(); await page.locator('#content-question-text').fill('Modifiée visuellement');
    await page.locator('#content-choice-add').click(); await page.locator('[data-choice-text]').nth(2).fill('Troisième réponse'); await page.locator('input[name="content-correct-answer"]').nth(2).check();
    await page.locator('.content-choice').nth(1).getByRole('button', { name: 'Retirer cette réponse' }).click();
    await page.locator('#content-question-time').fill('45'); await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    card = page.locator('#qcm-question-list .content-question').filter({ hasText: 'Modifiée visuellement' }); await card.waitFor();
    assert.equal(await card.getAttribute('data-qid'), qid);
    let saved = JSON.parse(await page.locator('#qcm-json-editor').inputValue()).find(q => q.qid === qid);
    assert.equal(saved.reponse, 2); assert.equal(saved.choix.length, 2); assert.equal(saved.temps, 45);
    assert.equal(saved.chapitre, 'Optique');
    await card.getByRole('button', { name: 'Dupliquer' }).click(); await page.locator('#content-question-save').click(); await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    const copy = page.locator('#qcm-question-list .content-question').filter({ hasText: '(copie)' }); await copy.waitFor(); assert.notEqual(await copy.getAttribute('data-qid'), qid);
    await copy.getByRole('button', { name: 'Modifier', exact: true }).click();
    assert.equal(await page.locator('#content-question-delete').isVisible(), true, 'Existing questions can be deleted directly in their editor');
    await page.locator('#content-question-delete').click();
    await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    await copy.waitFor({ state: 'detached' });
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).chapters.find(ch => ch.chapitre === 'Mécanique').qcm_count, 1);

    await page.locator('#qcm-new-theme-input').fill('Thermodynamique'); await page.locator('#qcm-new-theme-row').getByRole('button', { name: 'Créer', exact: true }).click();
    await page.waitForFunction(() => qcmTheme === 'Thermodynamique');
    const chapterCard = chapterRow('Thermodynamique');
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

    await selectChapter('Optique'); await page.locator('[data-subtab="json"]').click();
    let edited = JSON.parse(await page.locator('#qcm-json-editor').inputValue()); edited.find(q => q.qid === qid).question = 'Modifiée en mode avancé';
    await page.locator('#qcm-json-editor').fill(JSON.stringify(edited)); assert.match(await page.locator('#qcm-question-list').innerText(), /Modifiée en mode avancé/);
    assert.equal(await page.locator('#qcm-chapter-overview .content-danger').first().isDisabled(), true, 'Unsaved JSON should block chapter deletion');
    assert.equal(await page.getByRole('button', { name: 'Valider avant d’enregistrer' }).count(), 0);
    await page.getByRole('button', { name: /Enregistrer/, exact: false }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    await page.waitForFunction(() => qcmFullList.some(q => q.question === 'Modifiée en mode avancé') && !qcmSaving);
    await page.locator('#qcm-json-editor').fill('{ incorrect'); const before = await (await context.request.get(base + '/api/admin/qcm/physique')).json();
    await page.getByRole('button', { name: /Enregistrer/ }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).revision, before.revision);
    await page.locator('#qcm-json-editor').fill(JSON.stringify([{ question: 'Invalide', choix: {}, reponse: 1 }]));
    await page.getByRole('button', { name: /Enregistrer/ }).filter({ has: page.locator('#qcm-save-spinner') }).click();
    await page.waitForFunction(() => !qcmSaving && document.querySelector('#qcm-editor-status').textContent.includes('choix'));
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/physique')).json()).revision, before.revision);
    await page.locator('#qcm-editor-box').getByRole('button', { name: /Recharger/, exact: false }).click(); await page.waitForFunction(() => qcmLoadedSubject === 'physique' && !qcmSaving);
    await page.locator('[data-subtab="bank"]').click();
    let held = false;
    await page.route('**/api/admin/qcm/physique?*', async route => { if (!held) { held = true; await new Promise(resolve => setTimeout(resolve, 500)); } await route.continue(); });
    await page.locator('#qcm-subject-select').selectOption('chimie'); await page.waitForFunction(() => qcmLoadedSubject === 'chimie');
    await page.locator('#qcm-subject-select').selectOption('physique'); await page.locator('#qcm-subject-select').selectOption('maths'); await page.waitForFunction(() => qcmLoadedSubject === 'maths'); await page.waitForTimeout(600);
    assert.equal(await page.locator('#qcm-subject-select').inputValue(), 'maths'); assert.match(await page.locator('#qcm-question-list').innerText(), /maths/); assert.doesNotMatch(await page.locator('#qcm-question-list').innerText(), /physique/);
    await page.unroute('**/api/admin/qcm/physique?*');

    // La recherche signale son résultat et se réinitialise au changement de chapitre.
    await page.locator('#qcm-question-search').fill('inexistant');
    assert.match(await page.locator('#qcm-question-list').innerText(), /correspond à cette recherche/);
    assert.match(await page.locator('#qcm-search-summary').innerText(), /0 résultat.*« inexistant »/);
    await selectChapter('Optique');
    assert.equal(await page.locator('#qcm-question-search').inputValue(), '');
    assert.equal(await page.locator('#qcm-question-list .content-question').count(), 1);

    // Annuler puis supprimer un chapitre rempli en conservant toutes ses questions.
    const optics = chapterRow('Optique');
    const mathsBefore = (await (await context.request.get(base + '/api/admin/qcm/maths')).json());
    const opticsId = JSON.parse(mathsBefore.raw).find(q => q.chapitre === 'Optique').qid;
    await optics.getByTitle('Supprimer le chapitre', { exact: true }).click();
    assert.match(await page.locator('#content-delete-title').innerText(), /Mathématiques > Optique/);
    assert.match(await page.locator('#content-delete-effects').innerText(), /conservée et déplacée/);
    assert.equal(await page.locator('#content-delete-cancel').evaluate(el => el === document.activeElement), true);
    await page.keyboard.press('Escape');
    assert.equal((await (await context.request.get(base + '/api/admin/qcm/maths')).json()).revision, mathsBefore.revision);
    await optics.getByTitle('Supprimer le chapitre', { exact: true }).click();
    await page.locator('#content-delete-destination').selectOption('Mécanique'); await page.locator('#content-delete-confirm').click();
    await page.locator('#content-delete-dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#qcm-current-path').innerText(), 'Mathématiques > Mécanique');
    assert.equal(await page.locator('#qcm-question-list .content-question').count(), 2);
    const mathsAfter = (await (await context.request.get(base + '/api/admin/qcm/maths')).json());
    assert.ok(JSON.parse(mathsAfter.raw).some(q => q.qid === opticsId && q.chapitre === 'Mécanique'));
    assert.ok(!mathsAfter.chapters.some(ch => ch.chapitre === 'Optique'));

    // Une seule activation de chapitre alimente les deux activités, selon les matières choisies.
    await openView('settings');
    await page.locator('[data-setting-section="ankimie"]').evaluate(el => { el.open = true; });
    assert.equal(await page.locator('#f-ank-subjects input[data-subject="chimie"]').isChecked(), true);
    assert.equal(await page.locator('#f-ank-subjects input[data-subject="physique"]').isChecked(), false);
    await page.locator('#f-ank-subjects input[data-subject="physique"]').check();
    await page.getByRole('button', { name: 'Enregistrer tous les réglages', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#saved-msg').classList.contains('show'));
    const configured = await context.request.get(base + '/api/params');
    assert.deepEqual((await configured.json()).ankimie_subjects.sort(), ['chimie', 'physique']);
    const statsResponse = page.waitForResponse(response => response.url() === base + '/api/ankimie/stats' && response.request().method() === 'GET');
    await openView('ankimie');
    const stats = await statsResponse;
    assert.equal(stats.status(), 200, 'Ankimie statistics must load successfully');
    assert.equal((await stats.json()).ok, true);
    await page.locator('#ankimie-stats table').first().waitFor({ state: 'visible' });
    assert.match(await page.locator('#ankimie-stats').innerText(), /Élèves/);
    await page.getByRole('button', { name: 'Modifier les réglages Ankimie', exact: true }).click();
    assert.equal(await page.locator('#atelier-settings').isVisible(), true);
    assert.equal(await page.locator('[data-setting-section="ankimie"]').evaluate(el => el.open), true);
    await page.getByRole('button', { name: 'Gérer les chapitres QCM', exact: true }).click();
    await page.waitForFunction(() => qcmLoadedSubject === document.querySelector('#qcm-subject-select').value);
    assert.equal(await page.locator('[data-pane="bank"]').isVisible(), true);

    // Un échec de sauvegarde garde le formulaire et le brouillon pour réessayer.
    await page.locator('#qcm-question-list').getByRole('button', { name: 'Modifier', exact: true }).first().click();
    await page.locator('#content-question-text').fill('Brouillon conservé après panne');
    await page.route('**/api/admin/qcm/maths/questions/*', route => {
      if (route.request().method() === 'PUT') return route.fulfill({ status: 503, contentType: 'text/html', body: '<html>Service indisponible</html>' });
      return route.continue();
    });
    await page.locator('#content-question-save').click();
    await page.waitForFunction(() => document.querySelector('#content-question-error').textContent.length > 0);
    assert.equal(await page.locator('#content-question-dialog').isVisible(), true);
    assert.equal(await page.locator('#content-question-text').inputValue(), 'Brouillon conservé après panne');
    assert.ok(!JSON.parse((await bank('maths')).raw).some(q => q.question === 'Brouillon conservé après panne'));
    await page.unroute('**/api/admin/qcm/maths/questions/*');
    await page.locator('#content-question-save').click();
    await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
    assert.ok(JSON.parse((await bank('maths')).raw).some(q => q.question === 'Brouillon conservé après panne'));

    await openView('proba');
    await page.waitForFunction(() => document.querySelector('#proba-chart').children.length > 0 || document.querySelector('#proba-status').textContent.includes('Aucune fiche'));
    await openView('qcm');
    await page.waitForFunction(() => qcmLoadedSubject === document.querySelector('#qcm-subject-select').value);
    await page.locator('[data-subtab="bank"]').click();
    const screenshots = process.env.CONTENT_CHECK_SCREENSHOTS;
    if (screenshots) mkdirSync(screenshots, { recursive: true });
    for (const theme of ['light', 'dark']) {
      await page.evaluate(t => applyTheme(t), theme);
      for (const width of [320,390,768,1280]) {
        await page.setViewportSize({ width, height: 844 });
        for (const key of ['qcm','ankimie']) {
          await openView(key);
          if (key === 'qcm') await page.waitForFunction(() => qcmLoadedSubject === document.querySelector('#qcm-subject-select').value);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, key + ' overflow ' + width);
          const mainWidth = await page.locator('.atelier-main').evaluate(el => el.getBoundingClientRect().width);
          if (width <= 1000) assert.ok(mainWidth >= width - (width <= 600 ? 80 : 112), 'Content panel compressed at ' + width + 'px: ' + mainWidth);
          if (key === 'qcm') {
            await page.locator('#qcm-add-question').click(); await page.locator('#content-question-dialog').waitFor({ state: 'visible' });
            assert.equal(await page.locator('#content-question-text').evaluate(el => el === document.activeElement), true, 'Question form should focus its first field');
            assert.equal(await page.locator('#content-question-dialog').evaluate(el => el.scrollWidth > el.clientWidth), false, 'dialog overflow ' + width);
            if (screenshots && width === 390) await page.screenshot({ path: path.join(screenshots, theme + '-390-question-form.png') });
            if (width === 320) await page.keyboard.press('Escape'); else await page.locator('#content-question-cancel').click();
            await page.locator('#content-question-dialog').waitFor({ state: 'hidden' });
            await page.locator('#qcm-chapter-overview .chapter-row').nth(1).hover();
            await page.locator('#qcm-chapter-overview').getByTitle('Supprimer le chapitre', { exact: true }).first().click();
            assert.equal(await page.locator('#content-delete-dialog').evaluate(el => el.scrollWidth > el.clientWidth), false, 'deletion dialog overflow ' + width);
            if (screenshots && width === 390) await page.screenshot({ path: path.join(screenshots, theme + '-390-chapter-delete.png') });
            await page.keyboard.press('Escape');
          }
          if (screenshots) await page.screenshot({ path: path.join(screenshots, theme + '-' + width + '-' + key + '.png'), fullPage: true });
        }
      }
    }
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: visual CRUD/duplication, choices, chapters, import, advanced JSON sync, stale responses, shared QCM/Ankimie activation and settings, save-error recovery, and 16 viewport/theme cases.');
  } finally {
    if (browser) await browser.close();
    if (child) child.kill('SIGTERM');
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
