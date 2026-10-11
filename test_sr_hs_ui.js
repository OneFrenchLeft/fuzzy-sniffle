// Run with: node --test test_sr_hs_ui.js
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const source = readFileSync(__dirname + '/static/js/app.js', 'utf8');

function setup(data) {
  function element() {
    return { style: {}, children: [], innerHTML: '', appendChild(child) { this.children.push(child); } };
  }
  const elements = Object.fromEntries(['sr-list', 'sr-hs-wrap', 'sr-hs-list', 'sr-extra-slot', 'sr-hs-enabled']
    .map(id => [id, element()]));
  const context = vm.createContext({
    document: {
      body: { dataset: {} }, getElementById: id => elements[id] || null,
      addEventListener() {}, querySelectorAll: () => [],
    },
    MutationObserver: class { observe() {} },
    console,
  });
  vm.runInContext(source, context);
  context.fetchJson = () => Promise.resolve(data);
  context.currentSrUser = 'Alice';
  context.showToast = () => {};
  for (const name of ['renderDailyStatus', 'updateSrProgress', 'updateExtraSlot']) context[name] = () => {};
  context.buildSrCard = card => ({ card, setAttribute(key, value) { this[key] = value; } });
  return { context, elements };
}

for (const normalCards of [[], [{ numero: 1 }]]) {
  test(`due bonus cards render with ${normalCards.length} normal cards and stay outside progress`, async () => {
    const { context, elements } = setup({ cards: normalCards, hs_cards: [{ numero: 9001 }, { numero: 9002 }], hors_serie_enabled: true });
    context.loadSrToday();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements['sr-hs-wrap'].style.display, 'block');
    assert.equal(elements['sr-hs-list'].children.length, 2);
    assert.equal(elements['sr-hs-list'].children[0]['data-hs'], '1');
    assert.equal(context.srTotalToday, normalCards.length);
  });
}

test('opting out hides the bonus section and reflects the saved preference', async () => {
  const { context, elements } = setup({ cards: [], hs_cards: [], hors_serie_enabled: false });
  context.loadSrToday();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['sr-hs-wrap'].style.display, 'none');
});

for (const enabled of [false, true]) {
  test(`account loads the saved hors-serie preference (${enabled}) without loading a deck`, async () => {
    const { context, elements } = setup({ ok: true, hors_serie_enabled: enabled });
    let reloaded = false;
    context.loadSrToday = () => { reloaded = true; };
    context.loadSrHsPreference();
    assert.equal(elements['sr-hs-enabled'].disabled, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements['sr-hs-enabled'].checked, enabled);
    assert.equal(elements['sr-hs-enabled'].disabled, false);
    assert.equal(reloaded, false);
  });
}

test('toggle saves the account preference without reloading the SR deck', async () => {
  const { context, elements } = setup({});
  let request;
  context.fetchJson = (url, options) => { request = { url, options }; return Promise.resolve({ ok: true }); };
  let reloaded = false;
  context.loadSrToday = () => { reloaded = true; };
  elements['sr-hs-enabled'].checked = true;
  context.saveSrHsPreference(elements['sr-hs-enabled']);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(request.url, '/api/sr/preferences');
  assert.equal(request.options.method, 'PUT');
  assert.deepEqual(JSON.parse(request.options.body), { hors_serie_enabled: true });
  assert.equal(reloaded, false);
  assert.equal(elements['sr-hs-enabled'].disabled, false);
});

test('a rejected preference save restores the checkbox and allows retrying', async () => {
  const { context, elements } = setup({});
  context.fetchJson = () => Promise.reject(new Error('Network error'));
  elements['sr-hs-enabled'].checked = false;
  context.saveSrHsPreference(elements['sr-hs-enabled']);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['sr-hs-enabled'].checked, true);
  assert.equal(elements['sr-hs-enabled'].disabled, false);
});
