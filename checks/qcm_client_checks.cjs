// Lancer : node --test checks/qcm_client_checks.cjs
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');

const template = readFileSync(__dirname + '/../templates/qcm.html', 'utf8');

function browser() {
  class Element {
    constructor() {
      this.children = [];
      this.style = {};
      this.dataset = {};
      this.attributes = {};
      const values = new Set();
      this.classList = {
        add: (...names) => names.forEach(name => values.add(name)),
        remove: (...names) => names.forEach(name => values.delete(name)),
        contains: name => values.has(name),
        toggle(name, force) {
          const enabled = force === undefined ? !values.has(name) : !!force;
          if (enabled) values.add(name); else values.delete(name);
          return enabled;
        },
      };
    }
    set innerHTML(value) { this.children = []; this._text = value; }
    get innerHTML() { return this._text || ''; }
    set textContent(value) { this.children = []; this._text = String(value); }
    get textContent() { return (this._text || '') + this.children.map(child => child.textContent || '').join(''); }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren(...children) { this._text = ''; this.children = children; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] || null; }
    addEventListener() {}
    focus() {}
  }
  const elements = {};
  const get = id => elements[id] || (elements[id] = new Element());
  get('qcm-meta').dataset.prenom = 'Élève';
  const handlers = {}, emissions = [];
  const socket = {
    on(name, callback) { handlers[name] = callback; },
    emit(name, data, callback) { emissions.push({ name, data, callback }); },
  };
  const root = new Element();
  root.setAttribute('data-theme', 'light');
  const context = vm.createContext({
    document: {
      documentElement: root,
      body: new Element(),
      getElementById: get,
      createElement: () => new Element(),
      createTextNode: text => ({ textContent: String(text) }),
      addEventListener() {},
      querySelectorAll: selector => selector === '#choices .choice' ? get('choices').children : [],
    },
    window: {}, io: () => socket,
    localStorage: { getItem() { throw new Error('stockage bloqué'); }, setItem() { throw new Error('stockage bloqué'); } },
    Date: { now: () => 1000 * 1000 },
    Image: class {},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    console,
  });
  const firstScript = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
  vm.runInContext(firstScript, context);
  const start = template.indexOf('(function(){', template.indexOf('id="toast"'));
  const end = template.indexOf('</script>', start);
  let script = template.slice(start, end).replace('{{ subjects | tojson }}', '[ ["physique", "Physique", "#01696f"] ]');
  script = script.replace('})();', 'window.inspectQcm = function(){ return {questions:questions,currentQ:currentQ,phase:phase,answered:answered,endTs:endTs}; };})();');
  vm.runInContext(script, context);
  return { handlers, emissions, elements, get, context, state: () => context.window.inspectQcm() };
}

function begin(state, extra = {}) {
  state.handlers.qcm_debut({ gid: 'game', scores: {}, media: [], ...extra });
}

function question(state, extra = {}) {
  state.handlers.qcm_question({ gid: 'game', q: 1, total: 2, question: 'Question',
    choix: [{ text: 'Oui' }, { text: 'Non' }], phase: 'ouverture', temps: 30,
    ends_at: 1020, now: 1000, deja_repondu: false, ...extra });
}

test('le stockage local bloqué ne casse ni le thème ni les événements QCM', () => {
  const state = browser();
  assert.equal(typeof state.handlers.qcm_question, 'function');
  assert.equal(state.context.document.documentElement.getAttribute('data-theme'), 'light');
  state.get('theme-toggle').onclick();
  assert.equal(state.context.document.documentElement.getAttribute('data-theme'), 'dark');
});

test('la reconnexion en phase ouverte restaure le temps restant et la réponse propre', () => {
  const state = browser();
  begin(state, { resync: true, history: [{ q: 1, question: 'Précédente', choix: [], correct: 0, ma: 1 }] });
  question(state, { q: 2, deja_repondu: true, choice: 1 });
  assert.equal(state.get('qtimer').textContent, '20 s');
  assert.equal(state.state().endTs, 1020);
  assert.equal(state.state().questions[0].question, 'Précédente');
  assert.equal(state.state().questions[0].ma, 1);
  assert.equal(state.state().questions[1].ma, 1);
  assert.equal(state.get('choices').children[1].classList.contains('picked'), true);
  assert.equal(state.get('choices').children.every(button => button.disabled), true);
});

test('une resynchronisation conserve l’historique local en attendant le snapshot serveur', () => {
  const state = browser();
  begin(state);
  question(state);
  state.get('choices').children[0].onclick();
  begin(state, { resync: true });
  assert.equal(state.state().questions[0].ma, 0);
  question(state, { deja_repondu: true, choice: 0 });
  assert.equal(state.state().questions[0].ma, 0);
});

test('la phase de correction garde les choix fermés et réaffiche la bonne correction', () => {
  const state = browser();
  begin(state, { resync: true, history: [{ q: 1, question: 'Question', choix: [], correct: 1, ma: 0 }] });
  question(state, { phase: 'correction', deja_repondu: true, choice: 0 });
  assert.equal(state.get('qtimer').textContent, '');
  assert.equal(state.state().currentQ, 1);
  assert.equal(state.get('choices').children.every(button => button.disabled), true);
  state.handlers.qcm_reveal({ gid: 'game', q: 1, correct: 1, bonne: { text: 'Non' }, detail: [], scores: {} });
  assert.equal(state.get('choices').children[1].classList.contains('reveal-ok'), true);
  assert.equal(state.state().questions[0].correct, 1);
});

test('une réponse porte la partie et la question ; un ancien accusé ne touche pas la suivante', () => {
  const state = browser();
  begin(state);
  question(state);
  state.get('choices').children[0].onclick();
  const emission = state.emissions.find(item => item.name === 'qcm_answer');
  assert.equal(emission.data.gid, 'game');
  assert.equal(emission.data.q, 1);
  assert.equal(emission.data.choice, 0);
  question(state, { q: 2 });
  const status = state.get('qstatus').textContent;
  emission.callback({ ok: false, raison: 'phase de lecture' });
  assert.equal(state.get('qstatus').textContent, status);
  assert.equal(state.state().currentQ, 2);
  assert.equal(state.state().questions[1].ma, null);
});

test('les événements retardés d’une autre partie ne remplacent pas la partie courante', () => {
  const state = browser();
  begin(state);
  question(state);
  state.handlers.qcm_reveal({ gid: 'old', q: 1, correct: 1, bonne: { text: 'Non' }, detail: [], scores: {} });
  state.handlers.qcm_fin({ gid: 'old', podium: [], corrections: [] });
  assert.equal(state.state().phase, 'ouverture');
  assert.equal(state.state().questions[0].correct, null);
  assert.equal(state.get('choices').children.every(button => !button.disabled), true);
});

test('la lecture conserve sa durée et les corrections différées restent cachées', () => {
  const state = browser();
  begin(state, { resync: true, history: [{ q: 1, question: 'Précédente', choix: [], correct: null, ma: 1, explication: '' }] });
  question(state, { q: 2, phase: 'lecture', ends_at: 1034 });
  assert.equal(state.get('qtimer').textContent, 'ouverture dans 4 s');
  assert.equal(state.state().questions[0].correct, null);
  assert.equal(state.get('choices').children.every(button => button.disabled), true);
});

test('la première synchronisation corrige immédiatement une horloge décalée', () => {
  const state = browser();
  state.context.setTimeout = callback => { callback(); return 1; };
  state.handlers.connect();
  const pings = state.emissions.filter(event => event.name === 'qcm_ping');
  assert.equal(pings.length, 3);
  pings.forEach(event => event.callback({ now: 700 }));
  begin(state);
  question(state, { ends_at: 730, now: 700 });
  assert.equal(state.get('qtimer').textContent, '30 s');
});
