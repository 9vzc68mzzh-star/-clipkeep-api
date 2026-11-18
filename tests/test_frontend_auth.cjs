const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync('dist/app.js', 'utf8');
const backend = 'https://backend.example';
const video = 'https://www.facebook.com/share/r/example/';

function storage(values = {}) {
  const data = new Map(Object.entries(values));
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), removeItem: key => data.delete(key) };
}

function launch(local = storage(), session = storage(), acceptedKey = 'valid-fixture-key', publicDownloads = false, navigatorInfo = {}) {
  const elements = new Map();
  const requests = [];
  const listeners = {};
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: id === '#quality' ? '480' : '', hidden: true, checked: false, open: false,
      classList: { toggle() {} }, addEventListener() {}, append() {}, focus() {},
      attributes: {}, setAttribute(name,value) {this.attributes[name]=value;}, getAttribute(name) {return this.attributes[name];},
      showModal() { this.open = true; }, close() { this.open = false; },
    });
    return elements.get(id);
  }
  const deviceButtons = ['iphone','android','ipad','laptop','desktop'].map(device => {
    const button = element('#device-' + device);
    button.dataset = { device };
    return button;
  });
  const context = vm.createContext({
    localStorage: local, sessionStorage: session, URL,
    navigator: navigatorInfo,
    location: { origin: 'https://frontend.example', protocol: 'https:' },
    document: { querySelector: element, querySelectorAll: selector => selector === '[data-device]' ? deviceButtons : [], createElement: element, addEventListener: (name,handler) => {listeners[name]=handler;} },
    setTimeout: (callback, delay) => { if (delay === 2000) queueMicrotask(callback); return 1; }, clearTimeout() {}, AbortSignal: { timeout: () => undefined },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const health = url.endsWith('/api/health');
      const authorized = options.headers.Authorization === `Bearer ${acceptedKey}`;
      const publicJob = publicDownloads && url.includes('/api/public/jobs');
      return {
        ok: health || authorized || publicJob, status: health || authorized || publicJob ? 200 : 401,
        json: async () => health ? { service: 'clipkeep', version: 3, public_downloads: publicDownloads } : publicJob ? options.method === 'POST' ? {id:'public-fixture'} : {status:'ready',title:'Public fixture',height:360,duration:5,download_url:'/api/files/public-fixture'} : authorized ? { ok: true } : { detail: 'API Key ไม่ถูกต้อง กรุณาตรวจสอบการตั้งค่า' },
      };
    },
  });
  vm.runInContext(source, context);
  return { context, element, requests, local, session, listeners };
}

async function connect(app, remember, key = 'valid-fixture-key') {
  app.element('#api-url').value = backend;
  app.element('#api-key').value = key;
  app.element('#remember-key').checked = remember;
  await app.element('#settings-form').onsubmit({ preventDefault() {} });
}

test('opt-in key survives a new tab and authenticates against its saved backend', async () => {
  const app = launch();
  await connect(app, true);
  assert.equal(JSON.parse(app.local.getItem('clipkeep-connection')).key, 'valid-fixture-key');
  const reopened = launch(app.local, storage());
  await vm.runInContext("api('/api/auth-check')", reopened.context);
  assert.equal(reopened.requests[0].url, backend + '/api/auth-check');
  assert.equal(reopened.requests[0].options.headers.Authorization, 'Bearer valid-fixture-key');
});

test('default key survives refresh but a new tab asks for a key before creating any job', async () => {
  const app = launch();
  await connect(app, false);
  assert.equal(app.local.getItem('clipkeep-connection'), null);
  const refreshed = launch(app.local, app.session);
  assert.equal(vm.runInContext('apiKey', refreshed.context), 'valid-fixture-key');
  const reopened = launch(app.local, storage());
  reopened.element('#video-url').value = video;
  await vm.runInContext('download()', reopened.context);
  assert.deepEqual(reopened.requests.map(r => r.url), [backend + '/api/health']);
  assert.equal(reopened.element('#settings-dialog').open, true);
  assert.match(reopened.element('#settings-message').textContent, /กรุณาใส่ API Key/);
  assert.equal(reopened.element('#video-url').value, video);
});

test('failed authentication never saves an invalid key', async () => {
  const app = launch();
  await connect(app, true, 'invalid-fixture-key');
  assert.equal(app.local.getItem('clipkeep-connection'), null);
  assert.equal(app.session.getItem('clipkeep-connection'), null);
  assert.equal(vm.runInContext('apiKey', app.context), '');
  assert.equal(app.element('#settings-message').hidden, false);
});

test('a 401 during download opens the key dialog while preserving the video link', async () => {
  const app = launch(storage({ 'clipkeep-api': backend }), storage({ 'clipkeep-connection': JSON.stringify({ base: backend, key: 'expired-fixture-key' }) }));
  app.element('#video-url').value = video;
  await vm.runInContext('download()', app.context);
  assert.equal(app.element('#settings-dialog').open, true);
  assert.match(app.element('#settings-message').textContent, /CLIPKEEP_API_KEY/);
  assert.equal(app.element('#video-url').value, video);
  assert.equal(app.element('#download-btn').disabled, false);
});

test('forget removes both persistent and tab keys immediately', async () => {
  const app = launch();
  await connect(app, true);
  app.element('#forget-key').onclick();
  assert.equal(app.local.getItem('clipkeep-connection'), null);
  assert.equal(app.session.getItem('clipkeep-connection'), null);
  assert.equal(vm.runInContext('apiKey', app.context), '');
  assert.equal(app.element('#remember-key').checked, false);
});

test('a saved key is never restored for a different backend', async () => {
  const app = launch(storage({
    'clipkeep-api': 'https://another-backend.example',
    'clipkeep-connection': JSON.stringify({ base: backend, key: 'valid-fixture-key' }),
  }));
  app.element('#video-url').value = video;
  await vm.runInContext('download()', app.context);
  assert.deepEqual(app.requests.map(r => r.url), ['https://another-backend.example/api/health']);
  assert.equal(vm.runInContext('apiKey', app.context), '');
});

test('denied persistent storage retains a working in-memory key and explains the limitation', async () => {
  const denied = storage();
  denied.setItem = () => { throw Error('storage unavailable'); };
  const app = launch(denied);
  await connect(app, true);
  assert.equal(vm.runInContext('apiKey', app.context), 'valid-fixture-key');
  assert.equal(vm.runInContext('rememberKey', app.context), false);
  assert.match(app.element('#toast').textContent, /ไม่อนุญาตให้จำคีย์/);
});

test('a fresh public visitor downloads using the default backend without an API key', async () => {
  const app = launch(storage(), storage(), 'valid-fixture-key', true);
  app.element('#video-url').value = video;
  await vm.runInContext('download()', app.context);
  assert.equal(app.element('#settings-dialog').open, false);
  assert.equal(app.element('#result').hidden, false);
  assert.ok(app.requests.some(r => r.url === 'https://clipkeep-api.onrender.com/api/public/jobs' && r.options.method === 'POST'));
  assert.ok(app.requests.some(r => r.url.endsWith('/api/public/jobs/public-fixture')));
  assert.ok(app.requests.every(r => !r.options.headers.Authorization));
});

test('public download requests do not send a remembered owner key', async () => {
  const app = launch(storage({'clipkeep-api':backend,'clipkeep-connection':JSON.stringify({base:backend,key:'owner-fixture-key'})}),storage(),'valid-fixture-key',true);
  app.element('#video-url').value = video;
  await vm.runInContext('download()', app.context);
  const publicRequests = app.requests.filter(r => r.url.includes('/api/public/jobs'));
  assert.equal(publicRequests.length, 2);
  assert.ok(publicRequests.every(r => !r.options.headers.Authorization));
});

test('device buttons change the saving guide beside the file without changing download choices', () => {
  const app = launch(storage({'klipnivo-device':'ipad'}));
  app.element('#video-url').value = video;
  assert.match(app.element('#guide-title').textContent, /iPad/);
  assert.match(app.element('#guide-steps').innerHTML, /Safari/);
  app.element('#device-laptop').onclick();
  assert.match(app.element('#guide-title').textContent, /โน้ตบุ๊ก/);
  assert.match(app.element('#guide-steps').innerHTML, /Windows และ Mac/);
  assert.equal(app.element('#device-laptop').getAttribute('aria-pressed'), 'true');
  assert.equal(app.element('#device-ipad').getAttribute('aria-pressed'), 'false');
  assert.equal(app.element('#video-url').value, video);
  assert.equal(app.element('#quality').value, '480');
  assert.equal(vm.runInContext('format', app.context), 'mp4');
  assert.equal(app.local.getItem('klipnivo-device'), 'laptop');
});

test('MP3 guides avoid photo-saving instructions and remain correct when switching devices', () => {
  const app = launch(storage({'klipnivo-device':'ipad'}));
  vm.runInContext("resetFormat('mp3')", app.context);
  assert.match(app.element('#guide-title').textContent, /MP3/);
  assert.match(app.element('#guide-file-note').textContent, /ไฟล์เสียง/);
  assert.doesNotMatch(app.element('#guide-file-note').textContent, /บันทึกวิดีโอ/);
  app.element('#device-iphone').onclick();
  assert.match(app.element('#guide-title').textContent, /MP3.*iPhone/);
  assert.equal(vm.runInContext('format', app.context), 'mp3');
});

test('iPad desktop mode is detected and invalid saved preferences are ignored', () => {
  const app = launch(storage({'klipnivo-device':'constructor'}), storage(), 'valid-fixture-key', false, {platform:'MacIntel',maxTouchPoints:5,userAgent:'Mozilla/5.0 (Macintosh)'});
  assert.match(app.element('#guide-title').textContent, /iPad/);
  assert.equal(app.element('#device-ipad').getAttribute('aria-pressed'), 'true');
});

test('opening the static page starts one anonymous health request without blocking its form', async () => {
  const app = launch(storage({'clipkeep-api':backend,'clipkeep-connection':JSON.stringify({base:backend,key:'owner-fixture-key'})}));
  await app.listeners.DOMContentLoaded();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, backend + '/api/health');
  assert.equal(Object.keys(app.requests[0].options.headers).length, 0);
  assert.equal(app.element('#backend-status').textContent, 'พร้อมดาวน์โหลด');
  assert.notEqual(app.element('#download-btn').disabled, true);
});

test('download readiness reuses a pending wake request then allows another readiness check later', async () => {
  const app = launch();
  let resolveHealth;
  let requests = 0;
  app.context.fetch = () => {
    requests++;
    return new Promise(resolve => {resolveHealth = () => resolve({ok:true,status:200,json:async()=>({service:'clipkeep',public_downloads:true})});});
  };
  const warmup = app.listeners.DOMContentLoaded();
  const fromDownload = vm.runInContext('checkBackend()',app.context);
  assert.equal(requests, 1);
  assert.match(app.element('#backend-status').textContent, /กำลังเตรียม/);
  resolveHealth();
  await Promise.all([warmup,fromDownload]);
  const later = vm.runInContext('checkBackend()',app.context);
  assert.equal(requests, 2);
  resolveHealth();
  await later;
});

test('a failed wake request leaves the page usable and can be retried', async () => {
  const app = launch();
  app.context.fetch = async () => {throw Error('temporary network error');};
  await app.listeners.DOMContentLoaded();
  assert.match(app.element('#backend-status').textContent, /เชื่อมต่ออีกครั้ง/);
  assert.notEqual(app.element('#download-btn').disabled, true);
  app.context.fetch = async () => ({ok:true,status:200,json:async()=>({service:'clipkeep',public_downloads:true})});
  await vm.runInContext('checkBackend()',app.context);
  assert.equal(app.element('#backend-status').textContent, 'พร้อมดาวน์โหลด');
});
