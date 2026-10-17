"""Browser durable login and stale-tab recovery through the real inline scripts."""
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN_HTML = ROOT / "dashboard" / "static" / "login.html"
APP_HTML = ROOT / "dashboard" / "static" / "app.html"
PYTHON_TIMEOUT = 20
DEVICE_ID = "A" * 22
DEVICE_SECRET = "B" * 43
DURABLE_CREDENTIAL = f"{DEVICE_ID}.{DEVICE_SECRET}"


LOGIN_NODE = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const config = input.config || {};
let unhandled = 0;
process.on('unhandledRejection', () => { unhandled += 1; });
const settle = async (turns = 8) => {
  for (let i = 0; i < turns; i += 1) await new Promise(resolve => setImmediate(resolve));
};

(async () => {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const listeners = {};
      elements.set(id, {
        id, innerHTML: '', textContent: '', value: '', disabled: false, children: [],
        style: {}, offsetWidth: 1,
        classList: { add() {}, remove() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        appendChild(child) { this.children.push(child); },
        querySelector() { return element('child'); },
        focus() { this.focused = true; },
        _listeners: listeners,
      });
    }
    return elements.get(id);
  }

  const local = Object.assign({}, config.local || {});
  const session = Object.assign({}, config.session || {});
  const removedLocal = [];
  const removedSession = [];
  const events = [];
  const fetches = [];
  const replacements = [];
  const historyCalls = [];
  const logs = [];
  const timers = new Map();
  let nextTimer = 1;
  const responses = (config.responses || []).slice();
  const location = {
    protocol: 'https:', host: 'example.test', origin: 'https://example.test',
    pathname: '/login', hash: config.hash || '', href: '',
    replace(url) { replacements.push(url); events.push('location-replace'); },
  };
  const history = {
    replaceState(state, title, url) {
      historyCalls.push({ state, title, url });
      events.push('history-replace');
      location.hash = '';
      location.pathname = url;
    },
  };
  const fetch = async (url, opts = {}) => {
    fetches.push({ url, method: opts.method || 'GET', body: opts.body || null });
    events.push('fetch');
    const response = responses.shift() || { status: 500, body: {} };
    if (response.network) throw new Error('network unavailable');
    const status = response.status === undefined ? 200 : response.status;
    return {
      status,
      ok: response.ok === undefined ? status >= 200 && status < 300 : response.ok,
      json: async () => Object.assign({}, response.body || {}),
    };
  };
  const setTimeout = (fn, delay) => {
    const id = nextTimer++;
    timers.set(id, { fn, delay });
    return id;
  };
  const clearTimeout = id => timers.delete(id);
  const safeConsole = {
    log: (...args) => logs.push(args.map(String).join(' ')),
    warn: (...args) => logs.push(args.map(String).join(' ')),
    error: (...args) => logs.push(args.map(String).join(' ')),
  };
  const context = vm.createContext({
    console: safeConsole, fetch, location, history,
    localStorage: {
      getItem: key => local[key] ?? null,
      setItem: (key, value) => { local[key] = String(value); },
      removeItem: key => { removedLocal.push(key); delete local[key]; },
    },
    sessionStorage: {
      getItem: key => session[key] ?? null,
      setItem: (key, value) => { session[key] = String(value); },
      removeItem: key => { removedSession.push(key); delete session[key]; },
    },
    document: { getElementById: element, createElement: () => element(`new-${elements.size}`) },
    btoa: value => Buffer.from(value).toString('base64'),
    URL, Blob, Math, JSON, Promise, Date, setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval() {},
  });
  const scripts = [...input.html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).join('\n');
  vm.runInContext(scripts, context);
  await settle();
  if (config.setup) {
    const pending = vm.runInContext(config.setup, context);
    if (pending && typeof pending.then === 'function') await pending;
    await settle();
  }
  process.stdout.write(JSON.stringify({
    local, session, removedLocal, removedSession, events, fetches,
    replacements, historyCalls, logs, unhandled,
    reconnectDisplay: element('reconnecting').style.display || '',
    retryDisplay: element('retry-device').style.display || '',
    errorText: element('err').textContent || '',
    inputValue: element('key').value || '',
    pendingTimers: timers.size,
  }));
})().catch(() => {
  process.stderr.write('browser login harness failed\n');
  process.exitCode = 1;
});
"""


APP_NODE = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const config = input.config || {};
const settle = async (turns = 8) => {
  for (let i = 0; i < turns; i += 1) await new Promise(resolve => setImmediate(resolve));
};

(async () => {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const listeners = {};
      elements.set(id, {
        id, innerHTML: '', textContent: '', value: '', disabled: false,
        children: [], files: [], style: {}, scrollTop: 0, scrollHeight: 0,
        className: '', title: '',
        classList: { add() {}, remove() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        appendChild(child) { this.children.push(child); },
        querySelector(selector) { return element(`${id}:${selector}`); },
        insertAdjacentHTML() {},
        _listeners: listeners,
      });
    }
    return elements.get(id);
  }

  const local = Object.assign({ jarvis_device_credential: config.durable || 'fixture' }, config.local || {});
  const session = Object.assign({ jarvis_token: 'session-token', jarvis_key: 'session-key' }, config.session || {});
  const removedLocal = [];
  const removedSession = [];
  const replacements = [];
  const fetches = [];
  const timerDelays = [];
  const probeTimerDelays = [];
  const responses = (config.responses || []).slice();
  const timers = new Map();
  let nextTimer = 1;
  let reloads = 0;
  let mediaCalls = 0;
  let tracksStopped = 0;
  const pageListeners = {};
  const sockets = [];
  const xhrs = [];
  const pendingFetches = [];
  let fetchAborts = 0;
  let now = 1000000;

  const setTimeout = (fn, delay) => {
    const id = nextTimer++;
    timers.set(id, { fn, delay });
    if (String(fn).includes('_recoveryProbeTimer')) probeTimerDelays.push(delay);
    else timerDelays.push(delay);
    return id;
  };
  const clearTimeout = id => timers.delete(id);
  async function runNextTimer() {
    const entry = timers.entries().next();
    if (entry.done) return false;
    const [id, timer] = entry.value;
    timers.delete(id);
    now += timer.delay;
    timer.fn();
    await settle();
    return true;
  }
  const fetch = async (url, opts = {}) => {
    fetches.push(url);
    const response = responses.shift() || { status: 500 };
    if (response.network) throw new Error('network unavailable');
    if (response.defer) {
      return await new Promise((resolve, reject) => {
        pendingFetches.push(() => resolve({
          status: response.status,
          ok: response.status >= 200 && response.status < 300,
          json: async () => Object.assign({}, response.body || {}),
        }));
        if (opts.signal) {
          const aborted = () => { fetchAborts += 1; reject(new Error('aborted')); };
          if (opts.signal.aborted) aborted();
          else opts.signal.addEventListener('abort', aborted, { once: true });
        }
      });
    }
    const status = response.status === undefined ? 200 : response.status;
    return {
      status,
      ok: response.ok === undefined ? status >= 200 && status < 300 : response.ok,
      json: async () => Object.assign({}, response.body || {}),
    };
  };
  class WS {
    constructor(url) {
      this.path = new URL(url).pathname;
      this.readyState = 0;
      this.sent = [];
      this.closeCalls = 0;
      sockets.push(this);
    }
    send(value) { this.sent.push(value); }
    close() { this.closeCalls += 1; this.readyState = 3; }
  }
  class AC {
    constructor() { this.state = 'running'; this.sampleRate = 16000; this.destination = {}; }
    resume() { return Promise.resolve(); }
    close() { this.closed = true; return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { return { connect() {}, disconnect() {} }; }
  }
  class XHR {
    constructor() {
      this.upload = {};
      this.status = 0;
      this.responseText = '';
      this.headers = {};
      xhrs.push(this);
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send() { this.sent = true; }
  }
  class FD { append() {} }
  class FakeDate extends Date { static now() { return now; } }
  const location = {
    protocol: 'https:', host: 'example.test', origin: 'https://example.test',
    replace(url) { replacements.push(url); },
    reload() { reloads += 1; },
  };
  const stream = () => ({ getTracks: () => [{ stop() { tracksStopped += 1; } }] });
  const hooks = {
    sockets, xhrs,
    status(allowed) {
      sockets[0].onmessage({ data: JSON.stringify({ type: 'status', state: 'active', audio_allowed: allowed }) });
    },
    firePage(type) { if (pageListeners[type]) pageListeners[type](); },
    resolveFetch() { if (pendingFetches.length) pendingFetches.shift()(); },
  };
  const context = vm.createContext({
    console: { log() {}, warn() {}, error() {} },
    WebSocket: WS, AudioContext: AC, XMLHttpRequest: XHR, FormData: FD,
    Blob, URL, Float32Array, Int16Array, Uint8Array, Math, JSON, Promise,
    Date: FakeDate, AbortController,
    document: { getElementById: element, createElement: () => element(`new-${elements.size}`) },
    sessionStorage: {
      getItem: key => session[key] ?? null,
      setItem: (key, value) => { session[key] = String(value); },
      removeItem: key => { removedSession.push(key); delete session[key]; },
    },
    localStorage: {
      getItem: key => local[key] ?? null,
      setItem: (key, value) => { local[key] = String(value); },
      removeItem: key => { removedLocal.push(key); delete local[key]; },
    },
    location,
    navigator: { mediaDevices: { getUserMedia: async () => { mediaCalls += 1; return stream(); } } },
    fetch, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    addEventListener(type, handler) { pageListeners[type] = handler; },
    window: { addEventListener(type, handler) { pageListeners[type] = handler; } },
    btoa: value => Buffer.from(value).toString('base64'),
    __test: hooks,
  });
  const scripts = [...input.html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).join('\n');
  vm.runInContext(scripts, context);
  await settle();
  if (config.setup) {
    const pending = vm.runInContext(config.setup, context);
    if (pending && typeof pending.then === 'function') await pending;
    await settle();
  }
  if (config.afterSettle) {
    const pending = vm.runInContext(config.afterSettle, context);
    if (pending && typeof pending.then === 'function') await pending;
    await settle();
  }
  for (let i = 0; i < (config.runTimers || 0); i += 1) await runNextTimer();
  const internals = vm.runInContext(`({
    recoveryActive: _recoveryActive,
    captureAllowed: _captureAllowed,
    micAutoResume: _micAutoResume
  })`, context);
  process.stdout.write(JSON.stringify({
    local, session, removedLocal, removedSession, replacements, reloads,
    fetches, fetchAborts, timerDelays, probeTimerDelays, pendingTimers: timers.size,
    socketCount: sockets.length,
    socketPaths: sockets.map(socket => socket.path),
    socketCloseCalls: sockets.map(socket => socket.closeCalls),
    mediaCalls, tracksStopped, xhrCount: xhrs.length,
    micDisabled: element('mic-btn').disabled,
    internals,
  }));
})().catch(() => {
  process.stderr.write('browser app harness failed\n');
  process.exitCode = 1;
});
"""


def _run_node(script: str, html_path: Path, config: dict) -> dict:
    payload = json.dumps({"html": html_path.read_text(encoding="utf-8"), "config": config})
    proc = subprocess.run(
        ["node", "-e", script],
        input=payload,
        text=True,
        capture_output=True,
        timeout=PYTHON_TIMEOUT,
    )
    if proc.returncode != 0:
        raise AssertionError("browser VM harness failed without exposing fixture credentials")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError("browser VM harness returned no usable state") from exc


def _login(config: dict | None = None) -> dict:
    return _run_node(LOGIN_NODE, LOGIN_HTML, config or {})


def _app(config: dict | None = None) -> dict:
    config = dict(config or {})
    config.setdefault("durable", DURABLE_CREDENTIAL)
    return _run_node(APP_NODE, APP_HTML, config)


class LoginTests(unittest.TestCase):
    def test_fragment_is_removed_before_split_credential_post(self):
        result = _login({
            "hash": f"#device={DURABLE_CREDENTIAL}",
            "responses": [{"status": 200, "body": {"ok": True, "token": "fresh-token", "key": "fresh-key"}}],
        })
        self.assertTrue(result["events"][:2] == ["history-replace", "fetch"], "history must be scrubbed before network I/O")
        self.assertTrue(len(result["fetches"]) == 1, "a valid fragment must make one exchange")
        body = json.loads(result["fetches"][0]["body"])
        self.assertTrue(body.get("device_id") == DEVICE_ID and body.get("secret") == DEVICE_SECRET, "the exchange must split the opaque credential")
        self.assertTrue(result["fetches"][0]["url"] == "/api/device-login", "the credential must not enter the fetch URL")
        self.assertTrue(result["historyCalls"] == [{"state": None, "title": "", "url": "/login"}], "browser history must retain only the safe login path")

    def test_fragment_success_stores_durable_and_fresh_session_values(self):
        result = _login({
            "hash": f"#device={DURABLE_CREDENTIAL}",
            "session": {"unrelated": "keep"},
            "responses": [{"status": 200, "body": {"ok": True, "token": "fresh-token", "key": "fresh-key"}}],
        })
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "successful fragment login must persist the opaque credential")
        self.assertTrue(result["session"].get("jarvis_token") == "fresh-token", "successful login must store the returned bearer")
        self.assertTrue(result["session"].get("jarvis_key") == "fresh-key", "successful login must store the returned session key")
        self.assertTrue(result["session"].get("unrelated") == "keep", "successful login must not disturb unrelated session state")
        self.assertTrue(result["replacements"] == ["/"], "successful login must use location.replace")

    def test_stored_durable_credential_reauthenticates_after_restart(self):
        result = _login({
            "local": {"jarvis_device_credential": DURABLE_CREDENTIAL},
            "session": {"jarvis_token": "stale-token", "jarvis_key": "stale-key"},
            "responses": [{"status": 200, "body": {"ok": True, "token": "renewed-token", "key": "renewed-key"}}],
        })
        self.assertTrue(len(result["fetches"]) == 1, "restart recovery must exchange once")
        self.assertTrue(result["session"].get("jarvis_token") == "renewed-token", "restart recovery must replace the stale bearer")
        self.assertTrue(result["session"].get("jarvis_key") == "renewed-key", "restart recovery must replace the stale session key")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "restart recovery must preserve the durable credential")
        self.assertTrue(result["replacements"] == ["/"], "restart recovery must replace the login page")

    def test_new_fragment_replaces_an_older_stored_credential(self):
        result = _login({
            "hash": f"#device={DURABLE_CREDENTIAL}",
            "local": {"jarvis_device_credential": f"{'C' * 22}.{'D' * 43}"},
            "responses": [{"status": 200, "body": {"ok": True, "token": "fresh-token", "key": "fresh-key"}}],
        })
        body = json.loads(result["fetches"][0]["body"])
        self.assertTrue(body.get("device_id") == DEVICE_ID and body.get("secret") == DEVICE_SECRET, "a newly scanned fragment must take precedence")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "a newly scanned credential must replace older durable state")

    def test_durable_401_clears_only_durable_state_and_does_not_retry(self):
        result = _login({
            "local": {"jarvis_device_credential": DURABLE_CREDENTIAL, "unrelated": "keep"},
            "session": {"jarvis_token": "existing-token", "jarvis_key": "existing-key", "unrelated": "keep"},
            "responses": [{"status": 401, "body": {"ok": False}}],
        })
        self.assertTrue(len(result["fetches"]) == 1, "revocation must make exactly one exchange")
        self.assertTrue("jarvis_device_credential" not in result["local"], "revocation must clear the durable credential")
        self.assertTrue(result["local"].get("unrelated") == "keep", "revocation must preserve unrelated local state")
        self.assertTrue(result["session"].get("jarvis_token") == "existing-token" and result["session"].get("jarvis_key") == "existing-key", "durable rejection must not clear session state")
        self.assertTrue(result["reconnectDisplay"] == "none", "revocation must hide reconnect state")
        self.assertTrue(result["errorText"] == "Invalid or revoked device", "revocation must show the bounded generic error")
        self.assertTrue(result["pendingTimers"] == 0, "revocation must not schedule a retry")

    def test_rate_limit_preserves_durable_state_and_allows_one_user_retry(self):
        result = _login({
            "local": {"jarvis_device_credential": DURABLE_CREDENTIAL},
            "responses": [
                {"status": 429, "body": {"ok": False}},
                {"status": 429, "body": {"ok": False}},
            ],
            "setup": "_retryDurableLogin()",
        })
        self.assertTrue(len(result["fetches"]) == 2, "one user retry must produce one additional exchange")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "rate limiting must preserve durable state")
        self.assertTrue(result["retryDisplay"] != "none", "rate limiting must leave a bounded retry control visible")
        self.assertTrue(result["pendingTimers"] == 0, "rate limiting must not create an automatic retry loop")

    def test_network_failure_preserves_durable_state_and_allows_one_user_retry(self):
        result = _login({
            "local": {"jarvis_device_credential": DURABLE_CREDENTIAL},
            "responses": [
                {"network": True},
                {"status": 200, "body": {"ok": True, "token": "renewed-token", "key": "renewed-key"}},
            ],
            "setup": "_retryDurableLogin()",
        })
        self.assertTrue(len(result["fetches"]) == 2, "one user retry must follow one network failure")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "network failure must preserve durable state")
        self.assertTrue(result["session"].get("jarvis_token") == "renewed-token", "a successful user retry must establish a fresh session")
        self.assertTrue(result["unhandled"] == 0, "network failure must be handled inside the login script")

    def test_malformed_fragment_is_scrubbed_without_storage_or_post(self):
        result = _login({"hash": f"#device={DEVICE_ID}.{'B' * 42}", "responses": []})
        self.assertTrue(result["historyCalls"] == [{"state": None, "title": "", "url": "/login"}], "malformed fragments must still be scrubbed")
        self.assertTrue(result["fetches"] == [], "malformed fragments must never be posted")
        self.assertTrue("jarvis_device_credential" not in result["local"], "malformed fragments must never be stored")

    def test_manual_pin_uses_returned_session_values_without_durable_write(self):
        result = _login({
            "responses": [{"status": 200, "body": {"ok": True, "token": "manual-token", "key": "manual-key"}}],
            "setup": "document.getElementById('key').value = 'ABCDEF'; doLogin()",
        })
        self.assertTrue(len(result["fetches"]) == 1 and result["fetches"][0]["url"] == "/login", "manual PIN must retain its endpoint")
        self.assertTrue(json.loads(result["fetches"][0]["body"]) == {"pin": "ABCDEF"}, "manual PIN must retain its request shape")
        self.assertTrue(result["session"].get("jarvis_token") == "manual-token" and result["session"].get("jarvis_key") == "manual-key", "manual login must store returned fresh session values")
        self.assertTrue("jarvis_device_credential" not in result["local"], "manual PIN must never create durable state")
        self.assertTrue(result["replacements"] == ["/"], "manual login must use location.replace")

    def test_manual_pin_rejects_success_without_fresh_session_key(self):
        result = _login({
            "responses": [{"status": 200, "body": {"ok": True, "token": "manual-token"}}],
            "setup": "document.getElementById('key').value = 'ABCDEF'; doLogin()",
        })
        self.assertTrue(result["replacements"] == [], "manual login without a session key must stay on the login page")
        self.assertTrue("jarvis_token" not in result["session"] and "jarvis_key" not in result["session"],
                        "malformed login success must not store partial session state")
        self.assertTrue(result["errorText"] == "Invalid or expired key", "malformed login success must fail generically")

    def test_login_does_not_render_or_log_credential_material(self):
        result = _login({
            "hash": f"#device={DURABLE_CREDENTIAL}",
            "responses": [{"status": 401, "body": {"ok": False}}],
        })
        rendered = "\n".join([result["errorText"], *result["logs"]])
        self.assertTrue(DEVICE_ID not in rendered and DEVICE_SECRET not in rendered and DURABLE_CREDENTIAL not in rendered, "credential material must not enter logs or rendered errors")


class StaleTabRecoveryTests(unittest.TestCase):
    def test_http_401_clears_only_session_auth_and_routes_to_login(self):
        result = _app({
            "session": {"jarvis_token": "old-token", "jarvis_key": "old-key", "unrelated": "keep"},
            "local": {"jarvis_device_credential": DURABLE_CREDENTIAL, "unrelated": "keep"},
            "responses": [{"status": 401}],
            "setup": "doWake()",
        })
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "HTTP 401 must remove only both JARVIS session keys")
        self.assertTrue(result["session"].get("unrelated") == "keep", "HTTP 401 must preserve unrelated session state")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL and result["local"].get("unrelated") == "keep", "HTTP 401 must preserve local durable state")
        self.assertTrue(result["replacements"] == ["/login"], "HTTP 401 must route through login once")

    def test_control_websocket_4001_uses_the_same_auth_transition_once(self):
        result = _app({
            "setup": "__test.sockets[0].onclose({code:4001}); __test.sockets[0].onclose({code:4001})",
        })
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "control 4001 must clear session auth once")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "control 4001 must preserve durable state")
        self.assertTrue(result["replacements"] == ["/login"], "control 4001 must route once")
        self.assertTrue(result["fetches"] == [], "control 4001 must not start restart polling")

    def test_control_4001_wins_over_an_inflight_successful_restart_probe(self):
        result = _app({
            "responses": [{"defer": True, "status": 200}],
            "setup": "__test.sockets[0].onclose({code:1006})",
            "afterSettle": "__test.sockets[0].onclose({code:4001}); __test.resolveFetch()",
        })
        self.assertTrue(result["replacements"] == ["/login"], "4001 must route through login while restart polling is in flight")
        self.assertTrue(result["reloads"] == 0, "a late status success must not override the login transition")
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "the winning 4001 transition must clear session auth once")

    def test_audio_websocket_4001_uses_the_same_auth_transition_once(self):
        result = _app({
            "setup": "(async()=>{__test.status(true); await doMic(); __test.sockets[1].onclose({code:4001}); __test.sockets[1].onclose({code:4001})})()",
        })
        self.assertTrue(result["socketPaths"] == ["/ws", "/ws/phone-audio"], "audio scenario must exercise the real phone-audio socket")
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "audio 4001 must clear session auth once")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "audio 4001 must preserve durable state")
        self.assertTrue(result["replacements"] == ["/login"], "audio 4001 must route once")

    def test_abrupt_close_retries_network_then_401_without_a_loop(self):
        result = _app({
            "responses": [{"network": True}, {"status": 401}],
            "setup": "__test.sockets[0].onclose({code:1006}); __test.sockets[0].onclose({code:1006})",
            "runTimers": 1,
        })
        self.assertTrue(result["fetches"] == ["/api/status", "/api/status"], "restart recovery must retry only the authenticated status probe")
        self.assertTrue(result["timerDelays"] == [1000], "the first retry delay must be one second")
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "recovery 401 must clear only session auth")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "recovery 401 must preserve durable state")
        self.assertTrue(result["replacements"] == ["/login"] and result["pendingTimers"] == 0, "recovery 401 must route once and stop")

    def test_abrupt_close_status_200_reloads_once_without_parallel_recovery(self):
        result = _app({
            "responses": [{"status": 200}],
            "setup": "__test.sockets[0].onclose({code:1006}); __test.sockets[0].onclose({code:1006}); __test.sockets[0].onerror()",
        })
        self.assertTrue(result["fetches"] == ["/api/status"], "parallel close/error events must share one status probe")
        self.assertTrue(result["reloads"] == 1, "healthy restarted server must reload once")
        self.assertTrue(result["replacements"] == [] and result["removedSession"] == [], "successful recovery must preserve current session storage")
        self.assertTrue(result["socketCount"] == 1, "recovery must not create a new socket before reload")

    def test_recovery_backoff_doubles_and_caps_at_five_seconds(self):
        result = _app({
            "responses": [{"network": True}] * 6,
            "setup": "__test.sockets[0].onclose({code:1006})",
            "runTimers": 4,
        })
        self.assertTrue(result["timerDelays"] == [1000, 2000, 4000, 5000, 5000], "recovery delays must double and remain capped at five seconds")
        self.assertTrue(result["fetches"] == ["/api/status"] * 5, "each elapsed delay must produce only one status probe")
        self.assertTrue(result["socketCount"] == 1, "backoff must not create parallel sockets")

    def test_permanently_pending_status_probe_times_out_and_routes_to_login(self):
        result = _app({
            "responses": [{"defer": True}] * 4,
            "setup": "__test.sockets[0].onclose({code:1006})",
            "runTimers": 8,
        })
        self.assertTrue(result["fetchAborts"] >= 1, "a hung status probe must be aborted")
        self.assertTrue(result["replacements"] == ["/login"], "hung recovery must reach the bounded login fallback")
        self.assertTrue(result["pendingTimers"] == 0, "bounded recovery must leave no timer behind")
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"],
                        "bounded recovery fallback must clear only session authentication")

    def test_recovery_stops_microphone_and_blocks_stale_auto_resume(self):
        result = _app({
            "responses": [{"network": True}],
            "setup": "(async()=>{__test.status(true); await doMic(); __test.status(false); __test.sockets[0].onclose({code:1006}); __test.status(true)})()",
        })
        self.assertTrue(result["mediaCalls"] == 1, "recovery must not reacquire microphone from stale status")
        self.assertTrue(result["tracksStopped"] >= 1, "recovery must stop the active microphone stream")
        self.assertTrue(result["internals"]["recoveryActive"], "the network retry path must remain in recovery")
        self.assertTrue(not result["internals"]["captureAllowed"] and not result["internals"]["micAutoResume"] and result["micDisabled"], "recovery must hold microphone capture disabled")
        self.assertTrue(result["socketCount"] == 2, "recovery must not create another audio or control socket")

    def test_upload_401_uses_the_same_auth_transition(self):
        result = _app({
            "setup": "_uploadFile({name:'fixture.txt',size:1}); __test.xhrs[0].status=401; __test.xhrs[0].onload()",
        })
        self.assertTrue(result["xhrCount"] == 1, "upload scenario must exercise real XHR handling")
        self.assertTrue(result["removedSession"] == ["jarvis_token", "jarvis_key"], "upload 401 must clear session auth")
        self.assertTrue(result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "upload 401 must preserve durable state")
        self.assertTrue(result["replacements"] == ["/login"], "upload 401 must route through login")

    def test_unrelated_upload_error_preserves_existing_ui_auth_state(self):
        result = _app({
            "setup": "_uploadFile({name:'fixture.txt',size:1}); __test.xhrs[0].status=500; __test.xhrs[0].responseText='{}'; __test.xhrs[0].onload()",
        })
        self.assertTrue(result["removedSession"] == [] and result["replacements"] == [], "non-auth upload errors must retain existing behavior")
        self.assertTrue(result["session"].get("jarvis_token") == "session-token" and result["local"].get("jarvis_device_credential") == DURABLE_CREDENTIAL, "non-auth upload errors must preserve auth state")

    def test_pagehide_cancels_pending_recovery_timer(self):
        result = _app({
            "responses": [{"network": True}],
            "setup": "__test.sockets[0].onclose({code:1006})",
            "afterSettle": "__test.firePage('pagehide')",
        })
        self.assertTrue(result["timerDelays"] == [1000], "network recovery must schedule its first bounded delay")
        self.assertTrue(result["pendingTimers"] == 0, "pagehide must cancel the pending recovery timer")
        self.assertTrue(result["fetches"] == ["/api/status"], "pagehide must prevent another status probe")

    def test_pagehide_aborts_a_pending_status_probe(self):
        result = _app({
            "responses": [{"defer": True}],
            "setup": "__test.sockets[0].onclose({code:1006})",
            "afterSettle": "__test.firePage('pagehide')",
        })
        self.assertTrue(result["fetchAborts"] == 1, "pagehide must abort the in-flight status probe")
        self.assertTrue(result["pendingTimers"] == 0, "pagehide must cancel the probe timeout")
        self.assertTrue(result["replacements"] == [], "pagehide cancellation must not start a navigation race")


if __name__ == "__main__":
    unittest.main()
