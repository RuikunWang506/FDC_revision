const DEFAULT_ENDPOINT = "https://www.fdcef.com/whatnot_live/ingest";
const msg = document.getElementById("msg");

function say(text, ok) {
  msg.className = ok ? "ok" : "err";
  msg.textContent = text;
}

chrome.storage.sync.get({ endpoint: DEFAULT_ENDPOINT, token: "" }, (cfg) => {
  document.getElementById("endpoint").value = cfg.endpoint || DEFAULT_ENDPOINT;
  document.getElementById("token").value = cfg.token || "";
});

function refreshDebugStatus() {
  chrome.runtime.sendMessage({ type: "whatnot-debug-status" }, (status) => {
    if (chrome.runtime.lastError || !status) return;
    document.getElementById("debugEnabled").checked = !!status.enabled;
    const count = Number(status.count || 0);
    document.getElementById("debugCount").textContent = count
      ? `${count} local log entries`
      : "No diagnostic log";
  });
}

refreshDebugStatus();

document.getElementById("save").onclick = () => {
  chrome.storage.sync.set({
    endpoint: document.getElementById("endpoint").value.trim() || DEFAULT_ENDPOINT,
    token: document.getElementById("token").value.trim(),
  }, () => say("Saved.", true));
};

document.getElementById("debugEnabled").onchange = (ev) => {
  chrome.runtime.sendMessage({
    type: "whatnot-debug-set",
    enabled: !!ev.target.checked,
    keep: true,
  }, (res) => {
    if (chrome.runtime.lastError) {
      say(chrome.runtime.lastError.message, false);
      return;
    }
    refreshDebugStatus();
    say(res && res.ok
      ? (ev.target.checked ? "Diagnostic logging started locally." : "Diagnostic logging stopped.")
      : ("Failed: " + (res && res.error)), !!(res && res.ok));
  });
};

document.getElementById("exportDebug").onclick = () => {
  chrome.runtime.sendMessage({ type: "whatnot-debug-get" }, (res) => {
    if (chrome.runtime.lastError || !res) {
      say(chrome.runtime.lastError && chrome.runtime.lastError.message || "Could not read log.", false);
      return;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const blob = new Blob([JSON.stringify({
      exported_at: new Date().toISOString(),
      extension: "FDC Whatnot Live",
      entries: res.entries || [],
    }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `fdc-whatnot-debug-${stamp}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    say(`Exported ${(res.entries || []).length} log entries.`, true);
  });
};

document.getElementById("clearDebug").onclick = () => {
  if (!confirm("Clear the local Whatnot diagnostic log?")) return;
  chrome.runtime.sendMessage({ type: "whatnot-debug-clear" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) {
      say(chrome.runtime.lastError && chrome.runtime.lastError.message || "Could not clear log.", false);
      return;
    }
    refreshDebugStatus();
    say("Diagnostic log cleared.", true);
  });
};

document.getElementById("test").onclick = () => {
  chrome.runtime.sendMessage({
    type: "whatnot-ingest",
    payload: { bid: 25, viewers: 1, time_left: 20, event: "high_bid", live: true },
  }, (res) => {
    if (chrome.runtime.lastError) {
      say(chrome.runtime.lastError.message, false);
      return;
    }
    say(res && res.ok ? "Test bid sent." : ("Failed: " + (res && (res.error || res.status))), !!(res && res.ok));
  });
};
