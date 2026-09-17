(function () {
  var cfg = window.MINI_TOOLS || {};
  var S = cfg.strings || {};
  var PDFLib = window.PDFLib;
  var XLSX = window.XLSX;
  var bwipjs = window.bwipjs;
  var JSZip = window.JSZip;

  function $(id) {
    return document.getElementById(id);
  }

  function setStatus(el, msg, kind) {
    if (!el) return;
    el.textContent = msg || "";
    el.classList.remove("is-error", "is-ok");
    if (kind === "error") el.classList.add("is-error");
    if (kind === "ok") el.classList.add("is-ok");
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1500);
  }

  function bindDrop(zone, input, onFiles) {
    if (!zone || !input) return;
    zone.addEventListener("click", function () {
      input.click();
    });
    zone.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", function () {
      onFiles(Array.prototype.slice.call(input.files || []));
      input.value = "";
    });
    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (ev) {
        ev.preventDefault();
        zone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function (ev) {
        ev.preventDefault();
        zone.classList.remove("is-over");
      });
    });
    zone.addEventListener("drop", function (ev) {
      var files = Array.prototype.slice.call((ev.dataTransfer && ev.dataTransfer.files) || []);
      if (files.length) onFiles(files);
    });
  }

  function renderFileList(ul, files, opts) {
    opts = opts || {};
    ul.innerHTML = "";
    files.forEach(function (entry, idx) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.className = "mt-name";
      name.textContent = entry.file.name;
      var meta = document.createElement("span");
      meta.className = "mt-meta";
      meta.textContent = fmtSize(entry.file.size) + (entry.pages ? " · " + entry.pages + " " + (S.pages || "pages") : "");
      li.appendChild(name);
      li.appendChild(meta);
      if (opts.reorder) {
        var up = document.createElement("button");
        up.type = "button";
        up.className = "mt-icon-btn";
        up.textContent = "↑";
        up.addEventListener("click", function () {
          if (idx === 0) return;
          var item = files.splice(idx, 1)[0];
          files.splice(idx - 1, 0, item);
          renderFileList(ul, files, opts);
        });
        var down = document.createElement("button");
        down.type = "button";
        down.className = "mt-icon-btn";
        down.textContent = "↓";
        down.addEventListener("click", function () {
          if (idx >= files.length - 1) return;
          var item = files.splice(idx, 1)[0];
          files.splice(idx + 1, 0, item);
          renderFileList(ul, files, opts);
        });
        li.appendChild(up);
        li.appendChild(down);
      }
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "mt-icon-btn danger";
      rm.setAttribute("aria-label", S.remove || "Remove");
      rm.innerHTML = "&times;";
      rm.addEventListener("click", function () {
        files.splice(idx, 1);
        renderFileList(ul, files, opts);
        if (opts.onChange) opts.onChange();
      });
      li.appendChild(rm);
      ul.appendChild(li);
    });
  }

  function showTool(id) {
    document.querySelectorAll(".mt-tab").forEach(function (tab) {
      tab.classList.toggle("is-active", tab.getAttribute("data-tool") === id);
    });
    document.querySelectorAll(".mt-panel").forEach(function (panel) {
      panel.classList.toggle("is-active", panel.getAttribute("data-panel") === id);
    });
    if (history.replaceState) {
      history.replaceState(null, "", "#" + id);
    } else {
      location.hash = id;
    }
  }

  document.querySelectorAll(".mt-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      showTool(tab.getAttribute("data-tool"));
    });
  });

  var hash = (location.hash || "").replace("#", "");
  if (hash && document.querySelector('[data-panel="' + hash + '"]')) {
    showTool(hash);
  }

  /* ——— PDF ——— */
  var pdfFiles = [];

  async function loadPdfEntry(file) {
    var buf = await file.arrayBuffer();
    var doc = await PDFLib.PDFDocument.load(buf, { ignoreEncryption: true });
    return { file: file, bytes: new Uint8Array(buf), pages: doc.getPageCount() };
  }

  bindDrop($("mtPdfDrop"), $("mtPdfInput"), async function (files) {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      for (var i = 0; i < files.length; i++) {
        if (!/\.pdf$/i.test(files[i].name)) continue;
        pdfFiles.push(await loadPdfEntry(files[i]));
      }
      renderFileList($("mtPdfList"), pdfFiles, { reorder: true });
      setStatus($("mtPdfStatus"), pdfFiles.length + " " + (S.files || "files"), "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtPdfClear").addEventListener("click", function () {
    pdfFiles = [];
    renderFileList($("mtPdfList"), pdfFiles, { reorder: true });
    setStatus($("mtPdfStatus"), "");
  });

  async function mergedPdf() {
    if (!pdfFiles.length) throw new Error(S.needPdf || "Add a PDF first.");
    var out = await PDFLib.PDFDocument.create();
    for (var i = 0; i < pdfFiles.length; i++) {
      var src = await PDFLib.PDFDocument.load(pdfFiles[i].bytes, { ignoreEncryption: true });
      var pages = await out.copyPages(src, src.getPageIndices());
      pages.forEach(function (page) {
        out.addPage(page);
      });
    }
    return out;
  }

  $("mtPdfMerge").addEventListener("click", async function () {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      var out = await mergedPdf();
      var bytes = await out.save();
      downloadBlob(new Blob([bytes], { type: "application/pdf" }), "merged.pdf");
      setStatus($("mtPdfStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtPdfSplit").addEventListener("click", async function () {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      if (!pdfFiles.length) throw new Error(S.needPdf || "Add a PDF first.");
      var src = await PDFLib.PDFDocument.load(pdfFiles[0].bytes, { ignoreEncryption: true });
      var from = Math.max(1, parseInt($("mtPdfFrom").value, 10) || 1);
      var to = Math.max(from, parseInt($("mtPdfTo").value, 10) || from);
      var last = src.getPageCount();
      from = Math.min(from, last);
      to = Math.min(to, last);
      var out = await PDFLib.PDFDocument.create();
      var idxs = [];
      for (var p = from - 1; p < to; p++) idxs.push(p);
      var pages = await out.copyPages(src, idxs);
      pages.forEach(function (page) {
        out.addPage(page);
      });
      var bytes = await out.save();
      downloadBlob(new Blob([bytes], { type: "application/pdf" }), "pages-" + from + "-" + to + ".pdf");
      setStatus($("mtPdfStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtPdfSplitAll").addEventListener("click", async function () {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      if (!pdfFiles.length) throw new Error(S.needPdf || "Add a PDF first.");
      var src = await PDFLib.PDFDocument.load(pdfFiles[0].bytes, { ignoreEncryption: true });
      var zip = new JSZip();
      var count = src.getPageCount();
      for (var i = 0; i < count; i++) {
        var out = await PDFLib.PDFDocument.create();
        var copied = await out.copyPages(src, [i]);
        out.addPage(copied[0]);
        zip.file("page-" + (i + 1) + ".pdf", await out.save());
      }
      var blob = await zip.generateAsync({ type: "blob" });
      downloadBlob(blob, "split-pages.zip");
      setStatus($("mtPdfStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  function cropDoc(src, leftIn, rightIn, topIn, bottomIn) {
    src.getPages().forEach(function (page) {
      var box = page.getCropBox ? page.getCropBox() : page.getMediaBox();
      var l = (parseFloat(leftIn) || 0) * 72;
      var r = (parseFloat(rightIn) || 0) * 72;
      var t = (parseFloat(topIn) || 0) * 72;
      var b = (parseFloat(bottomIn) || 0) * 72;
      var x = box.x + l;
      var y = box.y + b;
      var w = Math.max(36, box.width - l - r);
      var h = Math.max(36, box.height - t - b);
      page.setCropBox(x, y, w, h);
      if (page.setMediaBox) page.setMediaBox(x, y, w, h);
    });
    return src;
  }

  $("mtPdfCrop").addEventListener("click", async function () {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      if (!pdfFiles.length) throw new Error(S.needPdf || "Add a PDF first.");
      var src = await PDFLib.PDFDocument.load(pdfFiles[0].bytes, { ignoreEncryption: true });
      cropDoc(src, $("mtCropL").value, $("mtCropR").value, $("mtCropT").value, $("mtCropB").value);
      var bytes = await src.save();
      downloadBlob(new Blob([bytes], { type: "application/pdf" }), "cropped.pdf");
      setStatus($("mtPdfStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtPdfCrop46").addEventListener("click", async function () {
    setStatus($("mtPdfStatus"), S.busy || "Working…");
    try {
      if (!pdfFiles.length) throw new Error(S.needPdf || "Add a PDF first.");
      var src = await PDFLib.PDFDocument.load(pdfFiles[0].bytes, { ignoreEncryption: true });
      src.getPages().forEach(function (page) {
        var box = page.getMediaBox();
        var w = 4 * 72;
        var h = 6 * 72;
        var x = box.x + Math.max(0, (box.width - w) / 2);
        var y = box.y + Math.max(0, box.height - h);
        page.setCropBox(x, y, Math.min(w, box.width), Math.min(h, box.height));
        if (page.setMediaBox) page.setMediaBox(x, y, Math.min(w, box.width), Math.min(h, box.height));
      });
      var bytes = await src.save();
      downloadBlob(new Blob([bytes], { type: "application/pdf" }), "label-4x6.pdf");
      setStatus($("mtPdfStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtPdfStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  /* ——— Convert ——— */
  var convertFiles = [];

  bindDrop($("mtConvertDrop"), $("mtConvertInput"), function (files) {
    files.forEach(function (file) {
      convertFiles.push({ file: file });
    });
    renderFileList($("mtConvertList"), convertFiles);
    setStatus($("mtConvertStatus"), convertFiles.length + " " + (S.files || "files"));
  });

  $("mtConvertClear").addEventListener("click", function () {
    convertFiles = [];
    renderFileList($("mtConvertList"), convertFiles);
    setStatus($("mtConvertStatus"), "");
  });

  $("mtConvertBtn").addEventListener("click", async function () {
    if (!cfg.convertAvailable) {
      setStatus($("mtConvertStatus"), S.convertOff || "Conversion is not configured.", "error");
      return;
    }
    if (!convertFiles.length) {
      setStatus($("mtConvertStatus"), S.needOffice || "Add a Word or Excel file first.", "error");
      return;
    }
    setStatus($("mtConvertStatus"), S.busy || "Working…");
    $("mtConvertBtn").disabled = true;
    try {
      var form = new FormData();
      convertFiles.forEach(function (entry) {
        form.append("files", entry.file, entry.file.name);
      });
      var resp = await fetch(cfg.convertUrl, { method: "POST", body: form });
      if (!resp.ok) {
        var payload = {};
        try {
          payload = await resp.json();
        } catch (e) {
          payload = {};
        }
        throw new Error(payload.error || resp.statusText || "Convert failed");
      }
      var blob = await resp.blob();
      var name = "converted.pdf";
      var disp = resp.headers.get("Content-Disposition") || "";
      var match = /filename="?([^"]+)"?/i.exec(disp);
      if (match) name = match[1];
      downloadBlob(blob, name);
      setStatus($("mtConvertStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtConvertStatus"), (S.error || "Failed") + ": " + err.message, "error");
    } finally {
      $("mtConvertBtn").disabled = !cfg.convertAvailable;
    }
  });

  /* ——— Compress ——— */
  var compressFiles = [];

  bindDrop($("mtCompressDrop"), $("mtCompressInput"), function (files) {
    files.forEach(function (file) {
      compressFiles.push({ file: file });
    });
    renderFileList($("mtCompressList"), compressFiles);
    setStatus($("mtCompressStatus"), compressFiles.length + " " + (S.files || "files"));
  });

  $("mtCompressClear").addEventListener("click", function () {
    compressFiles = [];
    renderFileList($("mtCompressList"), compressFiles);
    setStatus($("mtCompressStatus"), "");
  });

  function canvasToJpeg(img, width, height, quality) {
    return new Promise(function (resolve, reject) {
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, width);
      canvas.height = Math.max(1, height);
      var ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(
        function (blob) {
          if (!blob) return reject(new Error("Could not compress image"));
          resolve(blob);
        },
        "image/jpeg",
        quality
      );
    });
  }

  function loadImage(file) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        resolve(img);
      };
      img.onerror = function () {
        URL.revokeObjectURL(img.src);
        reject(new Error(file.name));
      };
      img.src = URL.createObjectURL(file);
    });
  }

  async function compressImage(file, maxWidth, quality, targetBytes) {
    var img = await loadImage(file);
    var width = img.width;
    var height = img.height;
    if (maxWidth && width > maxWidth) {
      var scale = maxWidth / width;
      width = Math.max(1, Math.round(width * scale));
      height = Math.max(1, Math.round(height * scale));
    }
    if (!targetBytes) {
      var blob = await canvasToJpeg(img, width, height, quality);
      URL.revokeObjectURL(img.src);
      return blob;
    }
    var lo = 0.35;
    var hi = 0.92;
    var best = await canvasToJpeg(img, width, height, Math.min(hi, Math.max(lo, quality)));
    for (var i = 0; i < 8; i++) {
      var q = (lo + hi) / 2;
      var trial = await canvasToJpeg(img, width, height, q);
      if (trial.size <= targetBytes) {
        best = trial;
        lo = q;
      } else {
        hi = q;
        best = trial;
      }
    }
    var w = width;
    var h = height;
    var guard = 0;
    while (best.size > targetBytes && w > 240 && guard < 8) {
      w = Math.max(240, Math.round(w * 0.82));
      h = Math.max(240, Math.round(h * 0.82));
      best = await canvasToJpeg(img, w, h, 0.72);
      guard += 1;
    }
    URL.revokeObjectURL(img.src);
    return best;
  }

  async function compressPdf(file) {
    var buf = await file.arrayBuffer();
    var src = await PDFLib.PDFDocument.load(buf, { ignoreEncryption: true });
    var out = await PDFLib.PDFDocument.create();
    var pages = await out.copyPages(src, src.getPageIndices());
    pages.forEach(function (page) {
      out.addPage(page);
    });
    var bytes = await out.save({ useObjectStreams: true });
    return new Blob([bytes], { type: "application/pdf" });
  }

  async function compressSheet(file) {
    var buf = await file.arrayBuffer();
    var wb = XLSX.read(buf, { type: "array" });
    var out = XLSX.write(wb, { bookType: "xlsx", type: "array", compression: true });
    return new Blob([out], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
  }

  $("mtCompressBtn").addEventListener("click", async function () {
    if (!compressFiles.length) {
      setStatus($("mtCompressStatus"), S.noFiles || "Add files first.", "error");
      return;
    }
    setStatus($("mtCompressStatus"), S.busy || "Working…");
    var maxWidth = Math.max(200, parseInt($("mtMaxWidth").value, 10) || 1920);
    var quality = Math.min(0.95, Math.max(0.4, (parseInt($("mtQuality").value, 10) || 75) / 100));
    var targetRaw = $("mtTargetSize") ? $("mtTargetSize").value : "auto";
    var targetBytes = targetRaw && targetRaw !== "auto" ? parseInt(targetRaw, 10) : 0;
    try {
      var zip = new JSZip();
      var single = null;
      var singleName = "";
      var notes = [];
      for (var i = 0; i < compressFiles.length; i++) {
        var file = compressFiles[i].file;
        var name = file.name.replace(/\.[^.]+$/, "");
        var blob;
        var outName;
        if (/\.pdf$/i.test(file.name)) {
          blob = await compressPdf(file);
          outName = name + "-small.pdf";
        } else if (/\.(xlsx|xls|csv)$/i.test(file.name)) {
          blob = await compressSheet(file);
          outName = name + "-small.xlsx";
        } else {
          blob = await compressImage(file, maxWidth, quality, targetBytes);
          outName = name + "-small.jpg";
        }
        zip.file(outName, blob);
        single = blob;
        singleName = outName;
        var line = file.name + " " + fmtSize(file.size) + " → " + fmtSize(blob.size);
        if (targetBytes && blob.size > targetBytes) line += " (" + (S.targetMiss || "still over target") + ")";
        notes.push(line);
      }
      if (compressFiles.length === 1) {
        downloadBlob(single, singleName);
      } else {
        downloadBlob(await zip.generateAsync({ type: "blob" }), "compressed.zip");
      }
      setStatus($("mtCompressStatus"), notes.join(" · "), "ok");
    } catch (err) {
      setStatus($("mtCompressStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  /* ——— Barcode ——— */
  var barcodeCanvases = [];

  function barcodeLines() {
    return ($("mtBcValues").value || "")
      .split(/\r?\n/)
      .map(function (line) {
        return line.trim();
      })
      .filter(Boolean);
  }

  $("mtBcGenerate").addEventListener("click", function () {
    var lines = barcodeLines();
    var out = $("mtBcOut");
    out.innerHTML = "";
    barcodeCanvases = [];
    if (!lines.length) {
      setStatus($("mtBcStatus"), S.noFiles || "Enter a SKU or URL first.", "error");
      return;
    }
    try {
      lines.forEach(function (text) {
        var card = document.createElement("div");
        card.className = "mt-barcode-card";
        var canvas = document.createElement("canvas");
        var opts = {
          bcid: $("mtBcType").value,
          text: text,
          scale: Math.max(1, parseInt($("mtBcScale").value, 10) || 3),
          includetext: $("mtBcText").checked,
          textxalign: "center",
        };
        if (opts.bcid !== "qrcode" && opts.bcid !== "datamatrix") {
          opts.height = 12;
        }
        bwipjs.toCanvas(canvas, opts);
        var cap = document.createElement("div");
        cap.className = "mt-meta";
        cap.textContent = text;
        card.appendChild(canvas);
        card.appendChild(cap);
        out.appendChild(card);
        barcodeCanvases.push({ canvas: canvas, text: text });
      });
      setStatus($("mtBcStatus"), lines.length + " " + (S.ready || "ready"), "ok");
    } catch (err) {
      setStatus($("mtBcStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtBcPdf").addEventListener("click", async function () {
    if (!barcodeCanvases.length) {
      $("mtBcGenerate").click();
    }
    if (!barcodeCanvases.length) return;
    setStatus($("mtBcStatus"), S.busy || "Working…");
    try {
      var doc = await PDFLib.PDFDocument.create();
      var page = doc.addPage([612, 792]);
      var x = 36;
      var y = 750;
      for (var i = 0; i < barcodeCanvases.length; i++) {
        var dataUrl = barcodeCanvases[i].canvas.toDataURL("image/png");
        var png = await doc.embedPng(dataUrl);
        var w = Math.min(240, png.width);
        var h = (png.height / png.width) * w;
        if (y - h < 36) {
          page = doc.addPage([612, 792]);
          x = 36;
          y = 750;
        }
        page.drawImage(png, { x: x, y: y - h, width: w, height: h });
        y -= h + 24;
      }
      var bytes = await doc.save();
      downloadBlob(new Blob([bytes], { type: "application/pdf" }), "barcodes.pdf");
      setStatus($("mtBcStatus"), S.done || "Done", "ok");
    } catch (err) {
      setStatus($("mtBcStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtBcZip").addEventListener("click", async function () {
    if (!barcodeCanvases.length) {
      $("mtBcGenerate").click();
    }
    if (!barcodeCanvases.length) return;
    var zip = new JSZip();
    for (var i = 0; i < barcodeCanvases.length; i++) {
      var item = barcodeCanvases[i];
      var blob = await new Promise(function (resolve) {
        item.canvas.toBlob(resolve, "image/png");
      });
      var safe = item.text.replace(/[^\w.-]+/g, "_").slice(0, 40) || "code";
      zip.file(safe + ".png", blob);
    }
    downloadBlob(await zip.generateAsync({ type: "blob" }), "barcodes.zip");
    setStatus($("mtBcStatus"), S.done || "Done", "ok");
  });

  /* ——— Excel ——— */
  var excelState = { headers: [], rows: [], colMap: [] };

  function renderExcelPreview(dupCol) {
    var wrap = $("mtExcelPreview");
    wrap.innerHTML = "";
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var hr = document.createElement("tr");
    excelState.headers.forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var counts = {};
    if (dupCol >= 0) {
      excelState.rows.forEach(function (row) {
        var key = String(row[dupCol] == null ? "" : row[dupCol]).trim().toLowerCase();
        if (!key) return;
        counts[key] = (counts[key] || 0) + 1;
      });
    }
    var tbody = document.createElement("tbody");
    excelState.rows.slice(0, 200).forEach(function (row) {
      var tr = document.createElement("tr");
      row.forEach(function (cell, idx) {
        var td = document.createElement("td");
        td.textContent = cell == null ? "" : cell;
        if (dupCol === idx) {
          var key = String(cell == null ? "" : cell).trim().toLowerCase();
          if (key && counts[key] > 1) td.className = "is-dup";
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return counts;
  }

  function fillExcelControls() {
    var sel = $("mtExcelCol");
    sel.innerHTML = "";
    excelState.headers.forEach(function (h, idx) {
      var opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = h || "Column " + (idx + 1);
      sel.appendChild(opt);
    });
    var map = $("mtExcelMap");
    map.innerHTML = "";
    excelState.colMap = excelState.headers.map(function (h, idx) {
      return { keep: true, name: h, idx: idx };
    });
    excelState.colMap.forEach(function (col) {
      var row = document.createElement("div");
      row.className = "mt-map-row";
      var ck = document.createElement("input");
      ck.type = "checkbox";
      ck.checked = true;
      ck.title = S.keep || "Keep";
      var input = document.createElement("input");
      input.type = "text";
      input.value = col.name;
      ck.addEventListener("change", function () {
        col.keep = ck.checked;
      });
      input.addEventListener("input", function () {
        col.name = input.value;
      });
      row.appendChild(ck);
      row.appendChild(input);
      map.appendChild(row);
    });
  }

  bindDrop($("mtExcelDrop"), $("mtExcelInput"), async function (files) {
    if (!files.length) return;
    setStatus($("mtExcelStatus"), S.busy || "Working…");
    try {
      var buf = await files[0].arrayBuffer();
      var wb = XLSX.read(buf, { type: "array" });
      var sheet = wb.Sheets[wb.SheetNames[0]];
      var grid = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: "" });
      if (!grid.length) throw new Error(S.noFiles || "Empty sheet");
      excelState.headers = (grid[0] || []).map(function (h, i) {
        return String(h || "Column " + (i + 1));
      });
      excelState.rows = grid.slice(1).filter(function (row) {
        return row.some(function (cell) {
          return String(cell || "").trim() !== "";
        });
      });
      fillExcelControls();
      renderExcelPreview(-1);
      setStatus(
        $("mtExcelStatus"),
        excelState.rows.length + " " + (S.rows || "rows"),
        "ok"
      );
    } catch (err) {
      setStatus($("mtExcelStatus"), (S.error || "Failed") + ": " + err.message, "error");
    }
  });

  $("mtExcelDups").addEventListener("click", function () {
    if (!excelState.rows.length) {
      setStatus($("mtExcelStatus"), S.noFiles || "Add a spreadsheet first.", "error");
      return;
    }
    var col = parseInt($("mtExcelCol").value, 10);
    if (isNaN(col)) col = 0;
    var counts = renderExcelPreview(col);
    var dups = Object.keys(counts).filter(function (k) {
      return counts[k] > 1;
    }).length;
    if (dups) {
      setStatus($("mtExcelStatus"), (S.duplicatesFound || "Duplicates") + ": " + dups, "error");
    } else {
      setStatus($("mtExcelStatus"), S.noDuplicates || "No duplicates", "ok");
    }
  });

  function mappedGrid() {
    var keep = (excelState.colMap || []).filter(function (c) {
      return c.keep;
    });
    if (!keep.length) keep = excelState.headers.map(function (h, idx) {
      return { keep: true, name: h, idx: idx };
    });
    var headers = keep.map(function (c) {
      return c.name || excelState.headers[c.idx];
    });
    var rows = excelState.rows.map(function (row) {
      return keep.map(function (c) {
        return row[c.idx] == null ? "" : row[c.idx];
      });
    });
    return [headers].concat(rows);
  }

  $("mtExcelApply").addEventListener("click", function () {
    if (!excelState.rows.length) return;
    var grid = mappedGrid();
    excelState.headers = grid[0];
    excelState.rows = grid.slice(1);
    fillExcelControls();
    renderExcelPreview(-1);
    setStatus($("mtExcelStatus"), S.done || "Done", "ok");
  });

  function exportSheet(kind) {
    if (!excelState.rows.length) {
      setStatus($("mtExcelStatus"), S.noFiles || "Add a spreadsheet first.", "error");
      return;
    }
    var grid = mappedGrid();
    var ws = XLSX.utils.aoa_to_sheet(grid);
    var wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Sheet1");
    XLSX.writeFile(wb, kind === "csv" ? "export.csv" : "export.xlsx");
    setStatus($("mtExcelStatus"), S.done || "Done", "ok");
  }

  $("mtExcelCsv").addEventListener("click", function () {
    exportSheet("csv");
  });
  $("mtExcelXlsx").addEventListener("click", function () {
    exportSheet("xlsx");
  });

  /* ——— OCR ——— */
  var ocrFile = null;

  bindDrop($("mtOcrDrop"), $("mtOcrInput"), function (files) {
    if (!files.length) return;
    ocrFile = files[0];
    var thumb = $("mtOcrThumb");
    thumb.src = URL.createObjectURL(ocrFile);
    thumb.hidden = false;
    setStatus($("mtOcrStatus"), ocrFile.name, "ok");
  });

  $("mtOcrRun").addEventListener("click", async function () {
    if (!ocrFile) {
      setStatus($("mtOcrStatus"), S.needImage || "Add a photo first.", "error");
      return;
    }
    $("mtOcrRun").disabled = true;
    setStatus($("mtOcrStatus"), S.busy || "Working…");
    try {
      if (!cfg.ocrUrl) throw new Error("OCR is not configured.");
      var form = new FormData();
      form.append("file", ocrFile, ocrFile.name);
      form.append("lang", $("mtOcrLang").value);
      form.append("engine", $("mtOcrEngine") ? $("mtOcrEngine").value : "auto");
      var resp = await fetch(cfg.ocrUrl, { method: "POST", body: form });
      var payload = {};
      try {
        payload = await resp.json();
      } catch (e) {
        payload = {};
      }
      if (!resp.ok || !payload.ok) {
        throw new Error(payload.error || resp.statusText || "OCR failed");
      }
      $("mtOcrOut").value = payload.text || "";
      var extra = [];
      if (payload.engine) extra.push(payload.engine);
      if (payload.model) extra.push(payload.model);
      if (payload.confidence) extra.push(payload.confidence + "%");
      setStatus($("mtOcrStatus"), (S.done || "Done") + (extra.length ? " · " + extra.join(" · ") : ""), "ok");
    } catch (err) {
      setStatus($("mtOcrStatus"), (S.error || "Failed") + ": " + err.message, "error");
    } finally {
      $("mtOcrRun").disabled = false;
    }
  });

  $("mtOcrCopy").addEventListener("click", async function () {
    var text = $("mtOcrOut").value || "";
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setStatus($("mtOcrStatus"), S.copied || "Copied", "ok");
    } catch (err) {
      $("mtOcrOut").select();
      document.execCommand("copy");
      setStatus($("mtOcrStatus"), S.copied || "Copied", "ok");
    }
  });

  /* ——— AI ——— */
  var aiFile = null;

  bindDrop($("mtAiDrop"), $("mtAiInput"), function (files) {
    if (!files.length) return;
    aiFile = files[0];
    var thumb = $("mtAiThumb");
    if (thumb) {
      thumb.src = URL.createObjectURL(aiFile);
      thumb.hidden = false;
    }
    setStatus($("mtAiStatus"), aiFile.name, "ok");
  });

  async function runAi(task) {
    var text = ($("mtAiIn") && $("mtAiIn").value) || "";
    if (!text.trim() && !aiFile) {
      setStatus($("mtAiStatus"), S.needText || "Paste text or add a photo.", "error");
      return;
    }
    if (!cfg.aiUrl) {
      setStatus($("mtAiStatus"), S.aiOff || "Ollama is not available.", "error");
      return;
    }
    setStatus($("mtAiStatus"), S.busy || "Working…");
    var buttons = document.querySelectorAll("[data-ai-task]");
    buttons.forEach(function (btn) {
      btn.disabled = true;
    });
    try {
      var form = new FormData();
      form.append("task", task);
      form.append("text", text);
      if (aiFile) form.append("file", aiFile, aiFile.name);
      var resp = await fetch(cfg.aiUrl, { method: "POST", body: form });
      var payload = {};
      try {
        payload = await resp.json();
      } catch (e) {
        payload = {};
      }
      if (!resp.ok || !payload.ok) {
        throw new Error(payload.error || resp.statusText || "AI failed");
      }
      $("mtAiOut").value = payload.text || "";
      var extra = [payload.task, payload.model, payload.host].filter(Boolean);
      setStatus($("mtAiStatus"), (S.done || "Done") + (extra.length ? " · " + extra.join(" · ") : ""), "ok");
    } catch (err) {
      setStatus($("mtAiStatus"), (S.error || "Failed") + ": " + err.message, "error");
    } finally {
      buttons.forEach(function (btn) {
        btn.disabled = false;
      });
    }
  }

  document.querySelectorAll("[data-ai-task]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      runAi(btn.getAttribute("data-ai-task"));
    });
  });

  if ($("mtAiCopy")) {
    $("mtAiCopy").addEventListener("click", async function () {
      var text = ($("mtAiOut") && $("mtAiOut").value) || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        setStatus($("mtAiStatus"), S.copied || "Copied", "ok");
      } catch (err) {
        $("mtAiOut").select();
        document.execCommand("copy");
        setStatus($("mtAiStatus"), S.copied || "Copied", "ok");
      }
    });
  }

  if ($("mtAiClear")) {
    $("mtAiClear").addEventListener("click", function () {
      aiFile = null;
      if ($("mtAiIn")) $("mtAiIn").value = "";
      if ($("mtAiOut")) $("mtAiOut").value = "";
      if ($("mtAiThumb")) {
        $("mtAiThumb").hidden = true;
        $("mtAiThumb").removeAttribute("src");
      }
      setStatus($("mtAiStatus"), "");
    });
  }
})();
