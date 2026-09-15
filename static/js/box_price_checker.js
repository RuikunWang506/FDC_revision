(function () {
  var cfg = window.BOX_PRICE_CHECKER || {};
  var strings = cfg.strings || {};
  var body = document.getElementById("bpcBody");
  var addBtn = document.getElementById("bpcAdd");
  var checkBtn = document.getElementById("bpcCheck");
  var statusEl = document.getElementById("bpcStatus");
  var resultsEl = document.getElementById("bpcResults");
  var toleranceEl = document.getElementById("bpcTolerance");
  var STORAGE_KEY = "fdc.box_price_checker.last";
  var rowSeq = 0;

  function loadLastSearch() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      if (!data || !Array.isArray(data.boxes) || !data.boxes.length) return null;
      return data;
    } catch (err) {
      return null;
    }
  }

  function saveLastSearch(boxes, tolerance, payload) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        boxes: (boxes || []).map(function (box) {
          return {
            length: box.length,
            width: box.width,
            height: box.height,
          };
        }),
        tolerance: tolerance,
        results: payload || null,
      }));
    } catch (err) {
      /* ignore quota / private mode */
    }
  }

  function restoreRows(saved) {
    var boxes = saved && Array.isArray(saved.boxes) ? saved.boxes : [];
    if (!boxes.length) {
      addRow({ length: "12", width: "8", height: "6" });
      return;
    }
    boxes.forEach(function (box) {
      addRow({
        length: box.length,
        width: box.width,
        height: box.height,
      });
    });
    if (saved.tolerance != null && toleranceEl) {
      toleranceEl.value = saved.tolerance;
    }
    if (saved.results && saved.results.results) {
      renderResults(saved.results);
    }
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(value) {
    if (value == null || value === "" || Number.isNaN(Number(value))) return "—";
    return "$" + Number(value).toFixed(2);
  }

  function renderTiers(tiers) {
    if (!tiers || !tiers.length) return "";
    var html = "<table class='bpc-tiers'><thead><tr><th>" +
      escapeHtml(strings.qty || "Qty") + "</th><th>" +
      escapeHtml(strings.unit || "Each") + "</th></tr></thead><tbody>";
    tiers.forEach(function (tier) {
      var priceText = tier.price == null
        ? escapeHtml(tier.note || strings.quote || "Quote")
        : money(tier.price);
      html += "<tr><td>" + escapeHtml(tier.qty || "") + "</td><td>" + priceText + "</td></tr>";
    });
    return html + "</tbody></table>";
  }

  function bundleLine(item) {
    var parts = [];
    if (item.bundle_qty && item.bale_qty) {
      parts.push(escapeHtml(strings.bundleBale || "Bundle / bale") + " " + item.bundle_qty + " / " + item.bale_qty);
    } else if (item.bundle_qty && item.skid_qty) {
      parts.push(escapeHtml(strings.bundle || "Bundle") + " " + item.bundle_qty + " · " +
        escapeHtml(strings.skid || "Skid") + " " + item.skid_qty);
    } else if (item.bundle_qty) {
      parts.push(escapeHtml(strings.bundle || "Bundle") + " " + item.bundle_qty);
    }
    return parts.length ? '<p class="bpc-meta">' + parts.join(" · ") + "</p>" : "";
  }

  function addRow(values) {
    rowSeq += 1;
    var tr = document.createElement("tr");
    tr.dataset.rowId = String(rowSeq);
    tr.innerHTML =
      '<td><input type="number" min="0.25" max="120" step="0.25" inputmode="decimal" aria-label="' +
      escapeHtml(strings.length) +
      '" value="' +
      escapeHtml((values && values.length) || "") +
      '"></td>' +
      '<td><input type="number" min="0.25" max="120" step="0.25" inputmode="decimal" aria-label="' +
      escapeHtml(strings.width) +
      '" value="' +
      escapeHtml((values && values.width) || "") +
      '"></td>' +
      '<td><input type="number" min="0.25" max="120" step="0.25" inputmode="decimal" aria-label="' +
      escapeHtml(strings.height) +
      '" value="' +
      escapeHtml((values && values.height) || "") +
      '"></td>' +
      '<td class="bpc-actions-col"><button type="button" class="bpc-remove" title="' +
      escapeHtml(strings.remove) +
      '" aria-label="' +
      escapeHtml(strings.remove) +
      '"><i class="fas fa-times"></i></button></td>';
    body.appendChild(tr);
  }

  function collectBoxes() {
    var boxes = [];
    Array.prototype.forEach.call(body.querySelectorAll("tr"), function (tr) {
      var inputs = tr.querySelectorAll("input");
      var length = Number(inputs[0].value);
      var width = Number(inputs[1].value);
      var height = Number(inputs[2].value);
      if (!inputs[0].value && !inputs[1].value && !inputs[2].value) return;
      boxes.push({
        id: tr.dataset.rowId,
        length: length,
        width: width,
        height: height,
      });
    });
    return boxes;
  }

  function setStatus(text, isError) {
    if (!text) {
      statusEl.hidden = true;
      statusEl.textContent = "";
      return;
    }
    statusEl.hidden = false;
    statusEl.textContent = text;
    statusEl.classList.toggle("is-error", !!isError);
  }

  function vendorCard(vendor) {
    vendor = vendor || {};
    var matches = vendor.matches || [];
    var best = matches[0];
    var html = '<article class="bpc-vendor"><h3>' + escapeHtml(vendor.label || vendor.vendor || "") +
      (vendor.search_url
        ? ' <a href="' + escapeHtml(vendor.search_url) + '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(strings.catalog) + "</a>"
        : "") +
      "</h3>";
    if (!best) {
      html += '<p class="' + (vendor.ok === false ? "bpc-err" : "bpc-empty") + '">' +
        escapeHtml(vendor.error || strings.noMatch) + "</p></article>";
      return html;
    }
    html += '<div class="bpc-badge' + (best.exact ? "" : " is-close") + '">' +
      escapeHtml(best.exact ? strings.exact : strings.close) + "</div>";
    html += '<p class="bpc-offer-title">' + escapeHtml(best.sku || "") +
      (best.title ? " · " + escapeHtml(best.title) : "") + "</p>";
    if (best.dims && best.dims.length === 3) {
      html += '<p class="bpc-meta">' + escapeHtml(best.dims[0] + " × " + best.dims[1] + " × " + best.dims[2] + '"') + "</p>";
    }
    if (best.description) html += '<p class="bpc-meta">' + escapeHtml(best.description) + "</p>";
    html += bundleLine(best);
    html += renderTiers(best.price_tiers);
    if (best.url) {
      html += '<p class="bpc-meta"><a href="' + escapeHtml(best.url) + '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(strings.open) + "</a></p>";
    }
    if (matches.length > 1) {
      html += "<table class='bpc-more'><thead><tr><th>" + escapeHtml(strings.more) +
        "</th><th>" + escapeHtml(strings.unit) + "</th></tr></thead><tbody>";
      matches.slice(1).forEach(function (item) {
        html += "<tr><td>" + (item.url
          ? '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(item.title || item.sku) + "</a>"
          : escapeHtml(item.title || item.sku)) +
          "</td><td>" + money(item.unit_price) + "</td></tr>";
      });
      html += "</tbody></table>";
    }
    html += "</article>";
    return html;
  }

  function renderResults(payload) {
    resultsEl.innerHTML = "";
    (payload.results || []).forEach(function (row) {
      var wrap = document.createElement("section");
      wrap.className = "bpc-result";
      var vendors = row.vendors || {};
      wrap.innerHTML =
        '<div class="bpc-result-head">' +
        escapeHtml(row.length) + " × " + escapeHtml(row.width) + " × " + escapeHtml(row.height) +
        '"</div><div class="bpc-vendors">' +
        vendorCard(vendors.uline) +
        vendorCard(vendors.eightnet) +
        "</div>";
      resultsEl.appendChild(wrap);
    });
  }

  addBtn.addEventListener("click", function () {
    addRow();
  });

  body.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".bpc-remove");
    if (!btn) return;
    var row = btn.closest("tr");
    if (body.rows.length <= 1) {
      setStatus(strings.needRow, true);
      return;
    }
    row.remove();
  });

  checkBtn.addEventListener("click", function () {
    var boxes = collectBoxes();
    if (!boxes.length) {
      setStatus(strings.invalid, true);
      return;
    }
    checkBtn.disabled = true;
    setStatus(strings.loading, false);
    resultsEl.innerHTML = "";
    fetch(cfg.api, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        boxes: boxes,
        tolerance: Number(toleranceEl.value || 0.5),
      }),
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok || !data.ok) {
            throw new Error((data && data.error) || strings.error);
          }
          return data;
        });
      })
      .then(function (data) {
        setStatus("", false);
        renderResults(data);
        saveLastSearch(boxes, Number(toleranceEl.value || 0.5), data);
      })
      .catch(function (err) {
        setStatus(err.message || strings.error, true);
      })
      .finally(function () {
        checkBtn.disabled = false;
      });
  });

  restoreRows(loadLastSearch());
})();
