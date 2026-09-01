(() => {
  function send(msg, cb) {
    chrome.runtime.sendMessage(msg, (res) => {
      void chrome.runtime.lastError;
      if (cb) cb(res || {});
    });
  }

  function announce() {
    window.postMessage({ type: "fdc-whatnot-bridge-ready" }, "*");
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data || {};
    if (data.type === "fdc-check-whatnot-session") {
      send({ type: "check-whatnot-session" }, (res) => {
        window.postMessage({ type: "fdc-whatnot-session", ...res }, "*");
      });
    }
    if (data.type === "fdc-open-whatnot-dashboard") {
      send({ type: "open-whatnot-dashboard", url: data.url }, (res) => {
        window.postMessage({ type: "fdc-whatnot-session", ...res }, "*");
      });
    }
  });

  announce();
  window.addEventListener("DOMContentLoaded", announce);
})();
