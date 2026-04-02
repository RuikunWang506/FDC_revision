const PRODUCT_DETAIL_BASE = "https://www.fdcef.com/product_detail?search=";
const NO_RESULT_TEXT = "no products found matching your search";

async function checkSearchHasProduct(searchTerm) {
  const query = String(searchTerm || "").trim();
  if (!query) {
    return false;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);

  try {
    const url = PRODUCT_DETAIL_BASE + encodeURIComponent(query);
    const response = await fetch(url, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    });

    const html = await response.text();
    const lowered = html.toLowerCase();

    return !lowered.includes(NO_RESULT_TEXT);
  } catch (_err) {
    // On transient failures, default to true so UX remains available.
    return true;
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "fdc-check-candidate") {
    return;
  }

  checkSearchHasProduct(message.candidate)
    .then((hasProduct) => {
      sendResponse({ hasProduct });
    })
    .catch(() => {
      sendResponse({ hasProduct: true });
    });

  return true;
});
