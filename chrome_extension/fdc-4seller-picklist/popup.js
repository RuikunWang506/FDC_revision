/* global pdfjsLib, FdcPicklistPdf */

const pdfUrlInput = document.getElementById("pdf-url");
const fdcBaseInput = document.getElementById("fdc-base");
const exportBtn = document.getElementById("export-btn");
const previewBody = document.getElementById("preview-body");
const metaSummary = document.getElementById("meta-summary");
const statusEl = document.getElementById("status");

const DEFAULT_FDC_BASE = "https://www.fdcef.com";

let loadedPicklist = null;
let isLoading = false;

pdfjsLib.GlobalWorkerOptions.workerSrc = chrome.runtime.getURL("lib/pdf.worker.min.js");

function setStatus(message, kind = "") {
  statusEl.textContent = message || "";
  statusEl.className = `status${kind ? ` ${kind}` : ""}`;
}

function sendMessage(payload) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(payload, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response || { ok: false, error: "No response from extension." });
    });
  });
}

function isPicklistPdfUrl(url) {
  const value = String(url || "").trim();
  if (!value) {
    return false;
  }
  return /\.pdf($|\?|#)/i.test(value) || /4seller-res\.meiyunji\.net/i.test(value);
}

async function getCurrentTabPdfUrl() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = String(tab?.url || "").trim();
    return isPicklistPdfUrl(url) ? url : "";
  } catch (_err) {
    return "";
  }
}

async function resolvePdfUrl() {
  const tabPdfUrl = await getCurrentTabPdfUrl();
  if (tabPdfUrl) {
    return tabPdfUrl;
  }

  const state = await sendMessage({ type: "get-picklist-state" });
  if (state.fdcBaseUrl) {
    fdcBaseInput.value = state.fdcBaseUrl;
  } else if (!fdcBaseInput.value.trim()) {
    fdcBaseInput.value = DEFAULT_FDC_BASE;
  }

  if (state.pdfUrl && isPicklistPdfUrl(state.pdfUrl)) {
    return state.pdfUrl;
  }

  return "";
}

async function saveSettings() {
  await sendMessage({
    type: "save-settings",
    fdcBaseUrl: fdcBaseInput.value.trim() || DEFAULT_FDC_BASE,
  });
}

async function fetchPdfBytes(url) {
  const response = await fetch(url, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`PDF download failed (${response.status})`);
  }
  const buffer = await response.arrayBuffer();
  return new Uint8Array(buffer);
}

async function fetchLocations(skus, fdcBaseUrl) {
  const base = fdcBaseUrl.replace(/\/$/, "");
  const query = encodeURIComponent(skus.join(","));
  const primaryUrl = `${base}/4seller/picklist-locations?skus=${query}`;

  let response = await fetch(primaryUrl, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
  });

  if (!response.ok) {
    const fallbackUrl = `${base}/get_item_location?skus=${query}`;
    response = await fetch(fallbackUrl, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`Location API failed (${response.status})`);
    }
  }

  return response.json();
}

function setLoadingPreview(message) {
  previewBody.innerHTML = "";
  metaSummary.textContent = message;
  exportBtn.disabled = true;
  loadedPicklist = null;
}

function renderPreview(meta, rows) {
  previewBody.innerHTML = "";

  rows.forEach((row) => {
    const tr = document.createElement("tr");

    const numTd = document.createElement("td");
    numTd.textContent = row.num;
    tr.appendChild(numTd);

    const skuTd = document.createElement("td");
    skuTd.textContent = row.sku;
    tr.appendChild(skuTd);

    const locTd = document.createElement("td");
    const locLines = row.locationLines || [row.location];
    locLines.forEach((line, index) => {
      if (index > 0) {
        locTd.appendChild(document.createElement("br"));
      }
      locTd.appendChild(document.createTextNode(line));
    });
    const isMissing = locLines.length === 1 && locLines[0] === "N/A";
    locTd.className = isMissing ? "loc missing" : "loc";
    tr.appendChild(locTd);

    const qtyTd = document.createElement("td");
    qtyTd.textContent = row.qty;
    qtyTd.className = "qty";
    tr.appendChild(qtyTd);

    previewBody.appendChild(tr);
  });

  const bits = [];
  if (meta.packageQty) bits.push(`Package Qty: ${meta.packageQty}`);
  if (meta.skuQty) bits.push(`SKU Qty: ${meta.skuQty}`);
  if (meta.totalQty) bits.push(`Total Qty: ${meta.totalQty}`);
  metaSummary.textContent = bits.length ? bits.join(" · ") : `${rows.length} item(s)`;
}

async function loadPicklist() {
  if (isLoading) {
    return;
  }

  const pdfUrl = await resolvePdfUrl();
  const fdcBaseUrl = fdcBaseInput.value.trim() || DEFAULT_FDC_BASE;

  if (!pdfUrl) {
    setLoadingPreview("No picklist found");
    setStatus("Open the picklist PDF in a browser tab, then click the extension again.", "error");
    return;
  }

  pdfUrlInput.value = pdfUrl;
  isLoading = true;
  exportBtn.disabled = true;
  loadedPicklist = null;
  setLoadingPreview("Loading picklist…");
  setStatus("Downloading picklist PDF…");
  await saveSettings();

  try {
    const pdfBytes = await fetchPdfBytes(pdfUrl);
    setStatus("Reading picklist rows…");
    const { meta, rows } = await FdcPicklistPdf.extractPicklist(pdfBytes);

    const skus = [...new Set(rows.map((row) => row.sku).filter(Boolean))];
    setStatus(`Fetching locations for ${skus.length} SKU(s)…`);
    const locationMap = await fetchLocations(skus, fdcBaseUrl);
    const rowsWithLocations = FdcPicklistPdf.formatLocationMultiline(
      FdcPicklistPdf.attachLocations(rows, locationMap)
    );

    loadedPicklist = { meta, rows: rowsWithLocations, sourceUrl: pdfUrl };
    renderPreview(meta, rowsWithLocations);
    exportBtn.disabled = false;
    setStatus("");
  } catch (err) {
    setLoadingPreview("Could not load picklist");
    setStatus(String(err.message || err), "error");
  } finally {
    isLoading = false;
  }
}

exportBtn.addEventListener("click", () => {
  if (!loadedPicklist || !loadedPicklist.rows.length) {
    setStatus("Picklist is still loading.", "error");
    return;
  }

  const formatInput = document.querySelector('input[name="export-format"]:checked');
  const format = formatInput ? formatInput.value : "label-4x6";

  FdcPicklistPdf.openExportPage(loadedPicklist.meta, loadedPicklist.rows, format);
  const sizeLabel = format === "label-4x8" ? "4×8" : "4×6";
  setStatus(`Export opened (${sizeLabel}). Set paper to ${sizeLabel} in print dialog.`, "ok");
});

loadPicklist();
