(() => {
  const PRODUCT_DETAIL_BASE = "https://www.fdcef.com/product_detail?search=";
  const POLL_MS = 1500;
  const DASH_CHARS_REGEX = /[\-\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]/g;

  const state = {
    lastClipboardRaw: "",
    currentCandidate: "",
    isDismissedForCandidate: false,
  };
  const productCheckCache = new Map();

  let rootEl;
  let valueEl;
  let searchStyleBtn;
  let searchSkuBtn;
  let closeBtn;

  function normalizeText(text) {
    return String(text || "")
      .replace(/\u00a0/g, " ")
      .replace(DASH_CHARS_REGEX, "-")
      .trim()
      .toUpperCase();
  }

  function looksLikeStyle(token) {
    // Common styles: T3305, M1141, EN1346, AB12345
    return /^[A-Z]{1,3}\d{4,5}$/.test(token);
  }

  function looksLikeSku(token) {
    // Flexible SKU detection: must contain letters and digits; allows dashes.
    if (!/^[A-Z0-9-]{5,20}$/.test(token)) {
      return false;
    }
    if (!/[A-Z]/.test(token) || !/\d/.test(token)) {
      return false;
    }
    if ((token.match(/-/g) || []).length > 3) {
      return false;
    }
    return true;
  }

  function pickCandidate(clipboardText) {
    const text = normalizeText(clipboardText);
    if (!text || text.length > 2000) {
      return "";
    }

    // Direct full-text check first.
    if (looksLikeStyle(text) || looksLikeSku(text)) {
      return text;
    }

    const tokens = text.match(/[A-Z0-9-]{2,24}/g) || [];
    if (!tokens.length) {
      return "";
    }

    // Pattern like "EN1346 GY" should be kept as full SKU candidate.
    if (
      tokens.length >= 2 &&
      looksLikeStyle(tokens[0]) &&
      /^[A-Z]{1,4}$/.test(tokens[1])
    ) {
      return `${tokens[0]} ${tokens[1]}`;
    }

    const styleFirst = tokens.find((t) => looksLikeStyle(t));
    if (styleFirst) {
      return styleFirst;
    }

    const skuFirst = tokens.find((t) => looksLikeSku(t));
    return skuFirst || "";
  }

  function normalizeCandidate(candidate) {
    return String(candidate || "")
      .replace(DASH_CHARS_REGEX, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function buildProductUrl(candidate) {
    return PRODUCT_DETAIL_BASE + encodeURIComponent(normalizeCandidate(candidate));
  }

  function getSearchTargets(candidate) {
    const normalized = normalizeCandidate(candidate);
    const parts = normalized.split(" ").filter(Boolean);

    let style = "";
    if (parts.length && looksLikeStyle(parts[0])) {
      style = parts[0];
    } else {
      style = parts.find((p) => looksLikeStyle(p)) || "";
    }

    const sku = normalized;
    return { style, sku };
  }

  function openSearch(value) {
    if (!value) {
      return;
    }
    const url = buildProductUrl(value);
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function requestBackgroundProductCheck(candidate) {
    return new Promise((resolve) => {
      try {
        if (!chrome || !chrome.runtime || !chrome.runtime.sendMessage) {
          resolve(true);
          return;
        }

        chrome.runtime.sendMessage(
          { type: "fdc-check-candidate", candidate },
          (response) => {
            if (chrome.runtime.lastError) {
              resolve(true);
              return;
            }
            resolve(Boolean(response && response.hasProduct));
          }
        );
      } catch (_err) {
        resolve(true);
      }
    });
  }

  async function hasProductForCandidate(candidate) {
    const normalized = normalizeCandidate(candidate);
    if (!normalized) {
      return false;
    }

    if (productCheckCache.has(normalized)) {
      return productCheckCache.get(normalized);
    }

    const hasProduct = await requestBackgroundProductCheck(normalized);
    productCheckCache.set(normalized, hasProduct);
    return hasProduct;
  }

  async function evaluateAndMaybeShow(candidate) {
    const normalizedCandidate = normalizeCandidate(candidate);
    if (!normalizedCandidate) {
      hideUi();
      return;
    }

    if (normalizedCandidate !== state.currentCandidate) {
      state.currentCandidate = normalizedCandidate;
      state.isDismissedForCandidate = false;
    }

    if (state.isDismissedForCandidate) {
      return;
    }

    const hasProduct = await hasProductForCandidate(normalizedCandidate);

    // Candidate changed while waiting for background check.
    if (state.currentCandidate !== normalizedCandidate) {
      return;
    }

    if (!hasProduct) {
      hideUi();
      return;
    }

    showUi(normalizedCandidate);
  }

  function ensureUi() {
    if (rootEl) {
      return;
    }

    rootEl = document.createElement("div");
    rootEl.className = "fdc-clip-helper fdc-hidden";
    rootEl.innerHTML = `
      <div class="fdc-clip-header">
        <div class="fdc-clip-title">Clipboard Match Found</div>
        <button class="fdc-clip-close" type="button" title="Dismiss">×</button>
      </div>
      <div class="fdc-clip-value"></div>
      <div class="fdc-clip-note">Choose style or SKU search.</div>
      <div class="fdc-clip-actions">
        <button class="fdc-open-btn fdc-style-btn" type="button">Search Style</button>
        <button class="fdc-open-btn fdc-sku-btn" type="button">Search SKU</button>
      </div>
    `;

    valueEl = rootEl.querySelector(".fdc-clip-value");
    searchStyleBtn = rootEl.querySelector(".fdc-style-btn");
    searchSkuBtn = rootEl.querySelector(".fdc-sku-btn");
    closeBtn = rootEl.querySelector(".fdc-clip-close");

    closeBtn.addEventListener("click", () => {
      state.isDismissedForCandidate = true;
      hideUi();
    });

    searchStyleBtn.addEventListener("click", () => {
      if (!state.currentCandidate) {
        return;
      }
      const targets = getSearchTargets(state.currentCandidate);
      openSearch(targets.style || targets.sku);
    });

    searchSkuBtn.addEventListener("click", () => {
      if (!state.currentCandidate) {
        return;
      }
      const targets = getSearchTargets(state.currentCandidate);
      openSearch(targets.sku);
    });

    document.documentElement.appendChild(rootEl);
  }

  function showUi(candidate) {
    ensureUi();
    const normalized = normalizeCandidate(candidate);
    const targets = getSearchTargets(normalized);
    valueEl.textContent = normalized;

    if (searchStyleBtn) {
      searchStyleBtn.disabled = !targets.style;
      searchStyleBtn.title = targets.style ? "Search by style" : "No style detected";
    }

    rootEl.classList.remove("fdc-hidden");
  }

  function hideUi() {
    if (rootEl) {
      rootEl.classList.add("fdc-hidden");
    }
  }

  async function readClipboardText() {
    try {
      if (!document.hasFocus() || document.visibilityState !== "visible") {
        return "";
      }
      return await navigator.clipboard.readText();
    } catch (_err) {
      return "";
    }
  }

  async function checkClipboard() {
    const raw = await readClipboardText();
    if (!raw) {
      return;
    }

    if (raw === state.lastClipboardRaw) {
      return;
    }

    state.lastClipboardRaw = raw;
    const candidate = pickCandidate(raw);

    if (!candidate) {
      state.currentCandidate = "";
      state.isDismissedForCandidate = false;
      hideUi();
      return;
    }

    await evaluateAndMaybeShow(candidate);
  }

  // Capture paste events for quicker detection and better reliability.
  document.addEventListener("paste", async (event) => {
    try {
      const pasted = event.clipboardData ? event.clipboardData.getData("text") : "";
      if (!pasted) {
        return;
      }

      state.lastClipboardRaw = pasted;
      const candidate = pickCandidate(pasted);
      if (!candidate) {
        return;
      }

      await evaluateAndMaybeShow(candidate);
    } catch (_err) {
      // Ignore paste-parsing errors.
    }
  });

  ensureUi();
  setInterval(checkClipboard, POLL_MS);
})();
