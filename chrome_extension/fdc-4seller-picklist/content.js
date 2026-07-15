(() => {
  const PICKLIST_PATH = "/api/v2/order-print/pick-list";

  function isPicklistUrl(url) {
    try {
      const parsed = new URL(url, window.location.origin);
      return parsed.pathname.includes(PICKLIST_PATH);
    } catch (_err) {
      return String(url || "").includes(PICKLIST_PATH);
    }
  }

  function savePicklistPdfUrl(pdfUrl) {
    if (!pdfUrl || typeof pdfUrl !== "string") {
      return;
    }
    chrome.runtime.sendMessage({
      type: "picklist-pdf-url",
      url: pdfUrl,
      capturedAt: Date.now(),
    });
  }

  function handlePicklistPayload(payload) {
    if (!payload || payload.code !== 0) {
      return;
    }
    const pdfUrl = payload.data;
    if (typeof pdfUrl === "string" && pdfUrl.toLowerCase().includes(".pdf")) {
      savePicklistPdfUrl(pdfUrl);
    }
  }

  const originalFetch = window.fetch;
  window.fetch = async function patchedFetch(...args) {
    const response = await originalFetch.apply(this, args);
    try {
      const requestUrl =
        typeof args[0] === "string" ? args[0] : args[0] && args[0].url;
      if (requestUrl && isPicklistUrl(requestUrl)) {
        response
          .clone()
          .json()
          .then(handlePicklistPayload)
          .catch(() => {});
      }
    } catch (_err) {
      // Ignore hook errors.
    }
    return response;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function patchedOpen(method, url, ...rest) {
    this.__fdcPicklistUrl = url;
    return originalOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function patchedSend(...args) {
    if (this.__fdcPicklistUrl && isPicklistUrl(this.__fdcPicklistUrl)) {
      this.addEventListener("load", function onLoad() {
        try {
          const payload = JSON.parse(this.responseText);
          handlePicklistPayload(payload);
        } catch (_err) {
          // Ignore non-JSON responses.
        }
      });
    }
    return originalSend.apply(this, args);
  };
})();
