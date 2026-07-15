/* global pdfjsLib */

const FdcPicklistPdf = (() => {
  const QTY_HEADER_RE = /数量/;
  const SKU_HEADER_RE = /库存\s*sku|库存sku|stock\s*sku/i;
  const SKIP_TEXT_RE =
    /pick\s*l|package|packgae|print\s*time|sku\s*qty|total\s*qty|合计|总计|页码|拣货|备注/i;

  // e.g. "1 N1223 BK 2" or "4 N1868 WT 1"
  const ROW_LINE_RE =
    /^(\d{1,4})\s+([A-Z][A-Z0-9]{1,10}(?:\s+[A-Z]{1,4})?)\s+(\d+)\s*$/i;

  function copyPdfBytes(pdfBytes) {
    return new Uint8Array(pdfBytes);
  }

  function normalizeSkuKey(value) {
    return String(value || "")
      .replace(/\u00a0/g, " ")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "");
  }

  function alnumKey(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^0-9a-z]/g, "");
  }

  function normalizeLineText(text) {
    return String(text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function groupItemsByLine(items, tolerance = 3) {
    const lines = [];
    for (const item of items) {
      let line = lines.find((entry) => Math.abs(entry.y - item.y) <= tolerance);
      if (!line) {
        line = { y: item.y, items: [] };
        lines.push(line);
      }
      line.items.push(item);
    }

    lines.sort((a, b) => b.y - a.y);
    for (const line of lines) {
      line.items.sort((a, b) => a.x - b.x);
      line.text = normalizeLineText(line.items.map((entry) => entry.str).join(" "));
    }
    return lines;
  }

  function findTableHeader(lines) {
    const candidates = lines.filter((line) => {
      const texts = line.items.map((item) => String(item.str || "").trim());
      const hasHash = texts.some((t) => t === "#" || t === "＃");
      const hasSku = texts.some((t) => SKU_HEADER_RE.test(t));
      const hasQty = texts.some((t) => QTY_HEADER_RE.test(t));
      return hasHash && hasSku && hasQty;
    });

    if (!candidates.length) {
      return null;
    }

    // Bottom-most header row (just above the data table).
    return candidates[candidates.length - 1];
  }

  function parseRowFromLineText(text) {
    const cleaned = normalizeLineText(text);
    if (!cleaned || SKIP_TEXT_RE.test(cleaned)) {
      return null;
    }

    const match = cleaned.match(ROW_LINE_RE);
    if (!match) {
      return null;
    }

    return {
      num: match[1],
      sku: match[2].trim().toUpperCase(),
      qty: match[3],
    };
  }

  function isValidRow(row) {
    if (!row) {
      return false;
    }
    if (!/^\d+$/.test(row.num) || !/^\d+$/.test(row.qty)) {
      return false;
    }
    if (!row.sku || SKIP_TEXT_RE.test(row.sku)) {
      return false;
    }
    return ROW_LINE_RE.test(`${row.num} ${row.sku} ${row.qty}`);
  }

  function extractMeta(lines) {
    const meta = {
      packageQty: "",
      skuQty: "",
      totalQty: "",
      printTime: "",
    };

    const blob = lines.map((line) => line.text || "").join("\n");

    const pkg = blob.match(/pack(?:a|ae)?ge\s*qty\s*[:：]\s*(\d+)/i);
    const sku = blob.match(/sku\s*qty\s*[:：]\s*(\d+)/i);
    const total = blob.match(/total\s*qty\s*[:：]?\s*(\d+)/i);
    const printed = blob.match(/print\s*time\s*[:：]\s*([0-9\-:\s]+)/i);

    if (pkg) meta.packageQty = pkg[1];
    if (sku) meta.skuQty = sku[1];
    if (total) meta.totalQty = total[1];
    if (printed) meta.printTime = printed[1].trim();

    return meta;
  }

  function extractRowsFromPage(pageData) {
    const lines = groupItemsByLine(pageData.items);
    const headerLine = findTableHeader(lines);

    const rows = [];
    const seen = new Set();

    const dataLines = headerLine
      ? lines.filter((line) => line.y < headerLine.y - 5)
      : lines;

    for (const line of dataLines) {
      const row = parseRowFromLineText(line.text);
      if (!isValidRow(row)) {
        continue;
      }

      const key = `${row.num}|${row.sku}|${row.qty}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);

      rows.push({
        pageIndex: pageData.pageIndex,
        num: row.num,
        sku: row.sku,
        qty: row.qty,
      });
    }

    rows.sort((a, b) => Number(a.num) - Number(b.num));
    return rows;
  }

  function extractRowsFromFullText(pageText) {
    const rows = [];
    const seen = new Set();

    for (const rawLine of pageText.split(/\r?\n/)) {
      const row = parseRowFromLineText(rawLine);
      if (!isValidRow(row)) {
        continue;
      }
      const key = `${row.num}|${row.sku}|${row.qty}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      rows.push({ num: row.num, sku: row.sku, qty: row.qty });
    }

    rows.sort((a, b) => Number(a.num) - Number(b.num));
    return rows;
  }

  async function loadPdfPages(pdfBytes) {
    const data = copyPdfBytes(pdfBytes);
    const loadingTask = pdfjsLib.getDocument({ data });
    const pdf = await loadingTask.promise;
    const pages = [];

    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
      const page = await pdf.getPage(pageNumber);
      const viewport = page.getViewport({ scale: 1 });
      const textContent = await page.getTextContent();

      const items = textContent.items
        .map((item) => {
          const transform = pdfjsLib.Util.transform(viewport.transform, item.transform);
          return {
            str: item.str,
            x: transform[4],
            y: transform[5],
            width: item.width || 0,
            height: item.height || 8,
          };
        })
        .filter((item) => item.str && item.str.trim());

      const lineGroups = groupItemsByLine(items);
      const fullText = lineGroups.map((line) => line.text).join("\n");

      pages.push({
        pageIndex: pageNumber - 1,
        items,
        lines: lineGroups,
        fullText,
        width: viewport.width,
        height: viewport.height,
      });
    }

    return pages;
  }

  function resolveLocation(locationMap, sku) {
    const raw = String(sku || "").trim();
    const keys = [raw, raw.toLowerCase(), normalizeSkuKey(raw), alnumKey(raw)];

    for (const key of keys) {
      if (key && Object.prototype.hasOwnProperty.call(locationMap, key)) {
        const value = locationMap[key];
        if (value && value !== "N/A") {
          return value;
        }
      }
    }

    for (const key of keys) {
      if (key && Object.prototype.hasOwnProperty.call(locationMap, key)) {
        return locationMap[key];
      }
    }

    return "N/A";
  }

  function attachLocations(rows, locationMap) {
    return rows.map((row) => ({
      ...row,
      location: resolveLocation(locationMap, row.sku),
    }));
  }

  function parseLocationLines(location) {
    const locRaw = String(location || "N/A").trim();
    if (!locRaw || locRaw === "N/A") {
      return ["N/A"];
    }
    const parts = locRaw
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
    return parts.length ? parts : ["N/A"];
  }

  function formatLocationMultiline(rows) {
    return rows.map((row, index) => ({
      ...row,
      num: row.num || String(index + 1),
      locationLines: parseLocationLines(row.location),
    }));
  }

  async function extractPicklist(pdfBytes) {
    const pages = await loadPdfPages(pdfBytes);
    const allLines = pages.flatMap((page) => page.lines || []);
    const meta = extractMeta(allLines);

    const seen = new Set();
    const rows = [];

    function addRows(candidateRows) {
      for (const row of candidateRows) {
        const key = `${row.num}|${row.sku}|${row.qty}`;
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
        rows.push(row);
      }
    }

    for (const pageData of pages) {
      addRows(extractRowsFromPage(pageData));
    }

    if (!rows.length) {
      for (const pageData of pages) {
        addRows(
          extractRowsFromFullText(pageData.fullText || "").map((row) => ({
            ...row,
            pageIndex: pageData.pageIndex,
          }))
        );
      }
    }

    rows.sort((a, b) => Number(a.num) - Number(b.num));

    if (!rows.length) {
      throw new Error("No picklist rows found in the PDF. Open the 4Seller pick list PDF and try again.");
    }

    return { meta, rows };
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function locationCellHtml(row) {
    const lines = row.locationLines || parseLocationLines(row.location);
    return lines.map(escapeHtml).join("<br>");
  }

  function buildTableRowsHtml(rows) {
    return rows
      .map(
        (row) => `
      <tr>
        <td class="col-num">${escapeHtml(row.num)}</td>
        <td class="col-sku">${escapeHtml(row.sku)}</td>
        <td class="col-loc loc">${locationCellHtml(row)}</td>
        <td class="col-qty">${escapeHtml(row.qty)}</td>
      </tr>`
      )
      .join("");
  }

  function buildMetaBits(meta) {
    const metaBits = [];
    if (meta.packageQty) metaBits.push(`Package Qty : ${escapeHtml(meta.packageQty)}`);
    if (meta.skuQty) metaBits.push(`SKU Qty : ${escapeHtml(meta.skuQty)}`);
    if (meta.totalQty) metaBits.push(`Total Qty ${escapeHtml(meta.totalQty)}`);
    return metaBits;
  }

  function chunkRows(rows, perPage) {
    const chunks = [];
    for (let i = 0; i < rows.length; i += perPage) {
      chunks.push(rows.slice(i, i + perPage));
    }
    return chunks;
  }

  function buildLabelPicklistHtml(meta, rows, labelSize) {
    const is4x8 = labelSize === "label-4x8";
    const pageW = "4in";
    const pageHeight = is4x8 ? "8in" : "6in";
    const rowsPerPage = is4x8 ? 15 : 10;
    const sizeLabel = is4x8 ? "4×8" : "4×6";

    const fontSize = is4x8 ? "8.5pt" : "7.5pt";
    const headerFont = is4x8 ? "11pt" : "10pt";
    const rowHeight = is4x8 ? "0.44in" : "0.46in";
    const firstHeaderH = is4x8 ? "0.52in" : "0.48in";
    const contHeaderH = "0.22in";
    const theadH = "0.2in";

    const metaBits = buildMetaBits(meta);
    const metaLine = metaBits.join(" · ");
    const pages = chunkRows(rows, rowsPerPage);
    const totalPages = pages.length;

    const sheets = pages
      .map((pageRows, pageIndex) => {
        const pageNum = pageIndex + 1;
        const isFirst = pageIndex === 0;
        const tableRows = buildTableRowsHtml(pageRows);
        const headerBlock = isFirst
          ? `<div class="page-head first">
        ${meta.printTime ? `<div class="print-time">Print: ${escapeHtml(meta.printTime)}</div>` : ""}
        <h1>Pick List</h1>
        ${metaLine ? `<div class="meta">${metaLine}</div>` : ""}
      </div>`
          : `<div class="page-head cont">
        <div class="page-label">Pick List · page ${pageNum} of ${totalPages}</div>
      </div>`;

        return `<section class="sheet" style="--row-h:${rowHeight};--head-h:${isFirst ? firstHeaderH : contHeaderH};">
      ${headerBlock}
      <table>
        <thead>
          <tr>
            <th class="col-num">#</th>
            <th class="col-sku">库存SKU</th>
            <th class="col-loc">Location</th>
            <th class="col-qty">数量</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </section>`;
      })
      .join("");

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Pick List ${sizeLabel}</title>
  <style>
    @page { size: ${pageW} ${pageHeight}; margin: 0; }
    * { box-sizing: border-box; }
    body {
      font-family: Arial, "Microsoft YaHei", sans-serif;
      margin: 0;
      color: #111;
      width: ${pageW};
    }
    .sheet {
      width: ${pageW};
      height: ${pageHeight};
      padding: 0.1in 0.1in 0.08in;
      page-break-after: always;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .sheet:last-child { page-break-after: auto; }
    .page-head.first { height: ${firstHeaderH}; flex-shrink: 0; overflow: hidden; }
    .page-head.cont { height: ${contHeaderH}; flex-shrink: 0; }
    h1 { margin: 0; font-size: ${headerFont}; font-weight: 700; line-height: 1.15; }
    .meta { font-size: 6pt; line-height: 1.25; margin-top: 1px; }
    .print-time { font-size: 6pt; line-height: 1.2; }
    .page-label { font-size: 7pt; font-weight: 600; line-height: 1.2; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: ${fontSize};
      table-layout: fixed;
      flex: 1;
      min-height: 0;
    }
    thead { display: table-header-group; }
    thead tr { height: ${theadH}; }
    th, td {
      border: 1px solid #222;
      padding: 1px 3px;
      text-align: left;
      vertical-align: top;
      overflow: hidden;
      line-height: 1.15;
    }
    tbody tr { min-height: var(--row-h); height: auto; }
    th { background: #f0f0f0; font-weight: 700; }
    .col-num { width: 9%; text-align: center; vertical-align: middle; }
    .col-sku { width: 38%; font-weight: 700; word-break: break-word; vertical-align: middle; }
    .col-loc { width: 38%; font-weight: 700; word-break: break-word; text-align: center; vertical-align: middle; }
    .col-qty { width: 15%; text-align: center; vertical-align: middle; }
    td.loc { font-weight: 700; line-height: 1.2; white-space: normal; text-align: center; vertical-align: middle; }
    .toolbar { padding: 10px; }
    .toolbar button {
      background: #1f4db8; color: #fff; border: 0; border-radius: 6px;
      padding: 8px 12px; font-size: 12px; cursor: pointer;
    }
    @media print {
      .toolbar { display: none; }
      body { margin: 0; }
      .sheet { padding: 0.1in; }
    }
  </style>
</head>
<body>
  ${sheets}
  <div class="toolbar">
    <button type="button" onclick="window.print()">Print / Save as PDF</button>
    <p style="font-size:11px;color:#555;margin-top:8px;">
      ${sizeLabel} · ${rows.length} row(s) · ${totalPages} page(s) · up to ${rowsPerPage} rows/page
    </p>
  </div>
</body>
</html>`;
  }

  function buildExportHtml(meta, rows, format) {
    return buildLabelPicklistHtml(meta, rows, format === "label-4x8" ? "label-4x8" : "label-4x6");
  }

  function openExportPage(meta, rows, format = "label-4x6") {
    const html = buildExportHtml(meta, rows, format);
    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener,noreferrer");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  return {
    extractPicklist,
    attachLocations,
    formatLocationMultiline,
    parseLocationLines,
    openExportPage,
    resolveLocation,
  };
})();
