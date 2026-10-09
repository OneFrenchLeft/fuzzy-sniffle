// Run with: node --test test_qcm_editor.js
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(__dirname + '/static/js/app.js', 'utf8');
const question = { question: 'Question ?', choix: ['A', 'B'], reponse: 1 };
const flush = () => new Promise(resolve => setImmediate(resolve));

function editor() {
  const elements = {
    'qcm-subject-select': { value: 'physique' },
    'qcm-json-editor': { value: '[]' },
    'qcm-save-spinner': { style: {} },
  };
  const context = vm.createContext({
    document: {
      body: { dataset: {} },
      getElementById: id => elements[id] || null,
      addEventListener() {},
      querySelectorAll: () => [],
    },
    MutationObserver: class { observe() {} },
  });
  vm.runInContext(source, context);
  const requests = [];
  const messages = [];
  context.fetchWithStatus = (url, options) => new Promise((resolve, reject) => {
    requests.push({ url, options, resolve, reject });
  });
  context.qcmEditorStatus = (message, error) => messages.push({ message, error });
  context.showToast = () => {};
  return { context, elements, requests, messages };
}

async function load(state, questions = [question]) {
  state.context.loadQcmEditor();
  state.requests.at(-1).resolve({ status: 200, data: { ok: true, raw: JSON.stringify(questions) } });
  await flush();
}

test('legacy images are shown and saved using the canonical image field', async () => {
  const state = editor();
  const legacy = { ...question, image_complete: 'old.png' };
  await load(state, [legacy]);
  const shown = JSON.parse(state.elements['qcm-json-editor'].value)[0];
  assert.equal(shown.image, 'old.png');
  assert.equal('image_complete' in shown, false);
  assert.equal(state.context.validateQcmPreviewQuestion(legacy, 1).image, 'old.png');
  for (const image of ['new.png', '', null]) {
    assert.equal(state.context.validateQcmPreviewQuestion({ ...legacy, image }, 1).image, image || '');
  }
  state.context.saveQcmEditor();
  const saved = JSON.parse(JSON.parse(state.requests.at(-1).options.body).raw)[0];
  assert.equal(saved.image, 'old.png');
  assert.equal('image_complete' in saved, false);
});

test('late responses cannot replace another subject in the editor', async () => {
  const state = editor();
  state.context.loadQcmEditor();
  state.elements['qcm-subject-select'].value = 'chimie';
  state.context.loadQcmEditor();
  state.requests[1].resolve({ status: 200, data: { ok: true, raw: JSON.stringify([{ ...question, question: 'Chimie' }]) } });
  await flush();
  state.requests[0].resolve({ status: 200, data: { ok: true, raw: JSON.stringify([{ ...question, question: 'Physique' }]) } });
  await flush();
  assert.equal(JSON.parse(state.elements['qcm-json-editor'].value)[0].question, 'Chimie');
});

test('saving is blocked until the selected subject has loaded successfully', async () => {
  const state = editor();
  await load(state);
  state.elements['qcm-subject-select'].value = 'chimie';
  state.context.loadQcmEditor();
  state.context.saveQcmEditor();
  assert.equal(state.requests.length, 2);
  state.requests[1].reject(new Error('offline'));
  await flush();
  state.context.saveQcmEditor();
  assert.equal(state.requests.length, 2);
});

test('invalid server JSON cannot become a blank deck that overwrites the file', async () => {
  for (const raw of ['broken JSON', '{}', '[null]']) {
    const state = editor();
    state.context.loadQcmEditor();
    state.requests[0].resolve({ status: 200, data: { ok: true, raw } });
    await flush();
    state.context.saveQcmEditor();
    assert.equal(state.requests.length, 1, raw);
    assert.equal(state.messages.at(-1).error, true);
  }
});

test('a rejected save preserves the loaded deck and keeps edits for correction', async () => {
  const state = editor();
  await load(state, [{ ...question, chapitre: 'Optique' }, { ...question, chapitre: 'Meca' }]);
  const before = JSON.stringify(state.context.qcmFullList);
  state.context.qcmTheme = 'Optique';
  const edits = JSON.stringify([{ ...question, reponse: 9 }]);
  state.elements['qcm-json-editor'].value = edits;
  state.context.saveQcmEditor();
  const candidate = JSON.parse(JSON.parse(state.requests[1].options.body).raw);
  assert.equal(candidate.length, 2);
  assert.equal(candidate.find(q => q.chapitre === 'Meca').reponse, 1);
  state.requests[1].resolve({ status: 400, data: { ok: false, error: 'Invalid answer' } });
  await flush();
  assert.equal(JSON.stringify(state.context.qcmFullList), before);
  assert.equal(state.elements['qcm-json-editor'].value, edits);
});

test('successful saves reload canonical server data and prevent duplicate writes', async () => {
  const state = editor();
  await load(state);
  state.context.saveQcmEditor();
  state.context.saveQcmEditor();
  assert.equal(state.requests.length, 2);
  state.requests[1].resolve({ status: 200, data: { ok: true, count: 1, theme: 'physique' } });
  await flush();
  assert.equal(state.requests.length, 3);
  assert.match(state.requests[2].url, /\/physique\?/);
  state.requests[2].resolve({ status: 200, data: {
    ok: true, raw: JSON.stringify([{ ...question, qid: 'p-123456789abc-12345678' }]),
  } });
  await flush();
  assert.equal(JSON.parse(state.elements['qcm-json-editor'].value)[0].qid, 'p-123456789abc-12345678');
});

test('a save completing after switching subjects does not reload or erase the new editor', async () => {
  const state = editor();
  await load(state);
  state.context.saveQcmEditor();
  state.elements['qcm-subject-select'].value = 'chimie';
  await load(state, [{ ...question, question: 'Chimie' }]);
  const edits = JSON.stringify([{ ...question, question: 'Chimie edited' }]);
  state.elements['qcm-json-editor'].value = edits;
  state.requests[1].resolve({ status: 200, data: { ok: true, count: 1, theme: 'physique' } });
  await flush();
  assert.equal(state.requests.length, 3);
  assert.equal(state.elements['qcm-json-editor'].value, edits);
});
