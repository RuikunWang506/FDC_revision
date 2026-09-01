const DEFAULT_ENDPOINT = "https://www.fdcef.com/whatnot_live/ingest";
const AUTH_COOKIE_HINT = /session|token|auth|sid|user|access|refresh|jwt|login/i;
const DEBUG_STORAGE_KEY = "whatnotDebugLog";
const DEBUG_MAX_ENTRIES = 600;
const DEBUG_MAX_BYTES = 5_500_000;
let debugWrite = Promise.resolve();

function debugStoreGet() {
  return chrome.storage.local.get({
    [DEBUG_STORAGE_KEY]: { enabled: false, entries: [] },
  }).then((data) => data[DEBUG_STORAGE_KEY] || { enabled: false, entries: [] });
}

function trimDebugEntries(entries) {
  const kept = Array.isArray(entries) ? entries.slice(-DEBUG_MAX_ENTRIES) : [];
  while (kept.length && JSON.stringify(kept).length > DEBUG_MAX_BYTES) kept.shift();
  return kept;
}

function appendDebugEntry(entry) {
  debugWrite = debugWrite.then(async () => {
    const state = await debugStoreGet();
    if (!state.enabled) return;
    const entries = trimDebugEntries([...(state.entries || []), {
      ts: new Date().toISOString(),
      ...entry,
    }]);
    await chrome.storage.local.set({
      [DEBUG_STORAGE_KEY]: { enabled: true, entries },
    });
  }).catch(() => {});
  return debugWrite;
}

async function debugStatus() {
  const state = await debugStoreGet();
  const entries = Array.isArray(state.entries) ? state.entries : [];
  return {
    enabled: !!state.enabled,
    count: entries.length,
    firstTs: entries[0] && entries[0].ts || "",
    lastTs: entries[entries.length - 1] && entries[entries.length - 1].ts || "",
  };
}

async function sendDebugConfig(enabled) {
  const tabs = await chrome.tabs.query({
    url: ["https://www.whatnot.com/*", "https://whatnot.com/*"],
  });
  await Promise.all(tabs.map((tab) => new Promise((resolve) => {
    chrome.tabs.sendMessage(tab.id, {
      type: "whatnot-debug-config",
      enabled: !!enabled,
    }, () => {
      void chrome.runtime.lastError;
      resolve();
    });
  })));
}

function dashboardUrl(raw) {
  const text = String(raw || "").trim();
  const m = text.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  if (m) return `https://www.whatnot.com/dashboard/live/${m[0].toLowerCase()}`;
  return text;
}

async function whatnotCookies() {
  const a = await chrome.cookies.getAll({ domain: "whatnot.com" });
  const b = await chrome.cookies.getAll({ domain: ".whatnot.com" });
  const seen = new Set();
  const out = [];
  for (const cookie of [...a, ...b]) {
    const key = `${cookie.name}@${cookie.domain}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(cookie);
  }
  return out;
}

async function hasWhatnotSession() {
  const cookies = await whatnotCookies();
  return cookies.some((c) => c.value && AUTH_COOKIE_HINT.test(c.name));
}

function isLoginUrl(url) {
  return /whatnot\.com\/(login|signin|sign-in|authenticate)/i.test(url || "");
}

function isDashboardUrl(url) {
  return /whatnot\.com\/dashboard\/live\//i.test(url || "");
}

function waitForDashboard(tabId, targetUrl) {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve({ ok: true, loggedIn: false, needLogin: true, tabId });
    }, 5 * 60 * 1000);

    function onUpdated(id, info, tab) {
      if (id !== tabId || !tab || !tab.url) return;
      const url = info.url || tab.url;
      if (isDashboardUrl(url) && !isLoginUrl(url)) {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve({ ok: true, loggedIn: true, needLogin: false, tabId });
      }
    }

    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

async function openWhatnotDashboard(rawUrl) {
  const url = dashboardUrl(rawUrl);
  if (!url) return { ok: false, error: "Missing Whatnot dashboard URL" };
  const loggedIn = await hasWhatnotSession();
  const tab = await chrome.tabs.create({ url, active: true });
  if (loggedIn && !isLoginUrl(url)) {
    return { ok: true, loggedIn: true, needLogin: false, tabId: tab.id };
  }
  const result = await waitForDashboard(tab.id, url);
  return result;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) return;

  if (message.type === "whatnot-debug-status") {
    debugStatus().then(sendResponse).catch((err) => sendResponse({ enabled: false, count: 0, error: String(err) }));
    return true;
  }

  if (message.type === "whatnot-debug-set") {
    const enabled = !!message.enabled;
    debugStoreGet().then(async (state) => {
      const entries = enabled && message.keep ? trimDebugEntries(state.entries || []) : [];
      await chrome.storage.local.set({
        [DEBUG_STORAGE_KEY]: { enabled, entries },
      });
      await appendDebugEntry({ kind: "debug_session", event: enabled ? "started" : "stopped" });
      await sendDebugConfig(enabled);
      sendResponse({ ok: true, ...(await debugStatus()) });
    }).catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (message.type === "whatnot-debug-entry") {
    appendDebugEntry(message.entry || {}).then(() => debugStatus()).then(sendResponse);
    return true;
  }

  if (message.type === "whatnot-debug-get") {
    debugStoreGet().then((state) => sendResponse({
      enabled: !!state.enabled,
      entries: Array.isArray(state.entries) ? state.entries : [],
    })).catch((err) => sendResponse({ enabled: false, entries: [], error: String(err) }));
    return true;
  }

  if (message.type === "whatnot-debug-clear") {
    debugStoreGet().then(async (state) => {
      await chrome.storage.local.set({
        [DEBUG_STORAGE_KEY]: { enabled: !!state.enabled, entries: [] },
      });
      sendResponse({ ok: true, ...(await debugStatus()) });
    }).catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (message.type === "check-whatnot-session") {
    hasWhatnotSession()
      .then((loggedIn) => sendResponse({ ok: true, loggedIn, needLogin: !loggedIn }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (message.type === "open-whatnot-dashboard") {
    openWhatnotDashboard(message.url)
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (message.type !== "whatnot-ingest") return;
  chrome.storage.sync.get({ endpoint: DEFAULT_ENDPOINT, token: "" }, (cfg) => {
    const endpoint = String(cfg.endpoint || DEFAULT_ENDPOINT).replace(/\/$/, "");
    const headers = {
      "Content-Type": "application/json",
      ...(cfg.token ? { "X-Whatnot-Token": cfg.token } : {}),
    };
    const body = JSON.stringify(message.payload || {});
    debugStoreGet().then((state) => {
      if (state.enabled) appendDebugEntry({ kind: "ingest", payload: message.payload || {} });
    }).catch(() => {});
    function send(tries) {
      fetch(endpoint, { method: "POST", headers, body })
        .then(async (res) => {
          const data = await res.json().catch(() => ({}));
          const ok = res.ok && data.ok !== false;
          if (!ok && tries < 2) {
            setTimeout(() => send(tries + 1), 400);
            return;
          }
          sendResponse({ ok, status: res.status, data });
        })
        .catch((err) => {
          if (tries < 2) {
            setTimeout(() => send(tries + 1), 400);
            return;
          }
          sendResponse({ ok: false, error: String(err) });
        });
    }
    send(0);
  });
  return true;
});
