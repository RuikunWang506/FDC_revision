const STORAGE_KEYS = {
  pdfUrl: "lastPicklistPdfUrl",
  capturedAt: "lastPicklistCapturedAt",
  fdcBaseUrl: "fdcBaseUrl",
};

const DEFAULT_FDC_BASE = "https://www.fdcef.com";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) {
    return;
  }

  if (message.type === "picklist-pdf-url") {
    chrome.storage.local.set({
      [STORAGE_KEYS.pdfUrl]: message.url,
      [STORAGE_KEYS.capturedAt]: message.capturedAt || Date.now(),
    });
    return;
  }

  if (message.type === "get-picklist-state") {
    chrome.storage.local.get(
      [STORAGE_KEYS.pdfUrl, STORAGE_KEYS.capturedAt, STORAGE_KEYS.fdcBaseUrl],
      (data) => {
        sendResponse({
          pdfUrl: data[STORAGE_KEYS.pdfUrl] || "",
          capturedAt: data[STORAGE_KEYS.capturedAt] || 0,
          fdcBaseUrl: data[STORAGE_KEYS.fdcBaseUrl] || DEFAULT_FDC_BASE,
        });
      }
    );
    return true;
  }

  if (message.type === "save-settings") {
    chrome.storage.local.set({
      [STORAGE_KEYS.fdcBaseUrl]: message.fdcBaseUrl || DEFAULT_FDC_BASE,
    });
    sendResponse({ ok: true });
    return;
  }
});
