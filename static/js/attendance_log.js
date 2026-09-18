(function () {
  var cfg = window.ATTENDANCE_LOG || {};
  var S = cfg.strings || {};
  var state = {
    devices: [],
    poll: {},
    counts: {},
    selected: "",
    tab: "roll",
    chart: null,
    modalMac: "",
    skipClickUntil: 0,
    serverNow: "",
    sortKey: "ip",
    sortDir: "asc"
  };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function labelType(kind) {
    return ({
      phone: S.phone, computer: S.computer, printer: S.printer, other: S.other,
      warehouse_service: S.warehouse, personal: S.personal, business: S.business, unknown: S.unknown
    })[kind] || S.unset;
  }
  function chip(kind) {
    if (!kind) return '<span class="al-chip unknown">' + esc(S.unset) + "</span>";
    return '<span class="al-chip ' + esc(kind) + '">' + esc(labelType(kind)) + "</span>";
  }
  function statusCell(online) {
    return online
      ? '<span class="al-online is-on"><span class="al-dot"></span>' + esc(S.online) + "</span>"
      : '<span class="al-online is-off"><span class="al-dot"></span>' + esc(S.offline) + "</span>";
  }
  function deviceUrl(mac, params) {
    // Prefer hyphenated MAC in the path so proxies/Flask don't choke on colons.
    var safeMac = String(mac || "").trim().toLowerCase().replace(/:/g, "-");
    var url = String(cfg.device || "").replace("MAC", encodeURIComponent(safeMac));
    if (!params) return url;
    var qs = Object.keys(params).filter(function (k) { return params[k]; }).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
    }).join("&");
    return qs ? url + (url.indexOf("?") >= 0 ? "&" : "?") + qs : url;
  }
  function removeUrl(mac) {
    return deviceUrl(mac) + "/remove";
  }
  function matchQuery(device, q) {
    if (!q) return true;
    var blob = [device.alias, device.hostname, device.mac, device.last_ip, device.display_name]
      .join(" ").toLowerCase();
    return blob.indexOf(q) !== -1;
  }
  function nameOf(device) {
    return device.alias || device.hostname || S.noName;
  }
  function ipSortKey(ip) {
    var parts = String(ip || "").trim().split(".");
    if (parts.length !== 4) return Number.MAX_SAFE_INTEGER;
    var n = 0;
    var i;
    for (i = 0; i < 4; i += 1) {
      var part = parseInt(parts[i], 10);
      if (isNaN(part) || part < 0 || part > 255) return Number.MAX_SAFE_INTEGER;
      n = (n * 256) + part;
    }
    return n;
  }
  function byIpThenName(a, b) {
    var diff = ipSortKey(a.last_ip) - ipSortKey(b.last_ip);
    if (diff !== 0) return diff;
    return String(nameOf(a)).localeCompare(String(nameOf(b)));
  }

  function sortValue(device, key) {
    if (key === "status") return device.currently_online ? 1 : 0;
    if (key === "alias") return String(nameOf(device)).toLowerCase();
    if (key === "ip") return ipSortKey(device.last_ip);
    if (key === "mac") return String(device.mac || "");
    if (key === "owner") return String(device.owner_category || "");
    if (key === "type") return String(device.device_type || "");
    if (key === "auto") return String(device.auto_role || "unknown");
    if (key === "today") return String(device.last_today || "") + "|" + String(device.first_today || "");
    if (key === "first_seen") return String(device.first_seen || "");
    if (key === "last_seen") return String(device.last_seen || "");
    return "";
  }

  function compareDevices(a, b) {
    var key = state.sortKey || "ip";
    var dir = state.sortDir === "desc" ? -1 : 1;
    var av = sortValue(a, key);
    var bv = sortValue(b, key);
    var diff = 0;
    if (typeof av === "number" && typeof bv === "number") {
      diff = av - bv;
    } else {
      diff = String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" });
    }
    if (diff !== 0) return diff * dir;
    return byIpThenName(a, b);
  }

  function syncSortHeaders() {
    document.querySelectorAll(".al-table th[data-sort]").forEach(function (th) {
      var key = th.getAttribute("data-sort");
      var active = key === state.sortKey;
      th.classList.toggle("is-sorted", active);
      th.classList.toggle("is-asc", active && state.sortDir === "asc");
      th.classList.toggle("is-desc", active && state.sortDir === "desc");
      th.setAttribute("aria-sort", active
        ? (state.sortDir === "desc" ? "descending" : "ascending")
        : "none");
    });
  }

  function setSort(key) {
    if (!key) return;
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = key;
      // Online-first and newest-last-seen feel natural as first click.
      state.sortDir = (key === "status" || key === "last_seen" || key === "today") ? "desc" : "asc";
    }
    refreshView({ keepScroll: true });
  }
  function currentMonth() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
  }

  function renderStats() {
    var c = state.counts || {};
    $("alStats").innerHTML = [
      stat(S.present, c.online || 0),
      stat(S.rollPresent, c.roll_present || 0),
      stat(S.services, c.services || 0),
      stat(S.unknown, c.unknown || 0),
      stat(S.total, c.total || 0)
    ].join("");
    var poll = state.poll || {};
    var stale = isPollStale(poll.polled_at, state.serverNow);
    var cls = stale ? "is-bad"
      : (poll.complete ? "is-ok" : (poll.source === "arp" ? "is-warn" : "is-bad"));
    var source = poll.source === "netgear" ? S.sourceNetgear
      : poll.source === "arp" ? S.sourceArp : S.sourceNone;
    var html = '<span class="al-pill ' + cls + '"><span class="al-dot"></span>' + esc(source) + "</span>";
    if (poll.polled_at) {
      html += '<span class="al-pill' + (stale ? " is-bad" : "") + '">' + esc(poll.polled_at) + "</span>";
    }
    if (stale) {
      html += '<span class="al-pill is-bad">' + esc(S.pollStale || "Poll is stale — click Refresh now") + "</span>";
    }
    if (poll.error && !poll.complete) {
      html += '<span class="al-pill is-warn">' + esc(S.unknownStatus) + ": " + esc(poll.error) + "</span>";
    }
    $("alStatus").innerHTML = html;
  }

  function isPollStale(polledAt, serverNow) {
    var a = parseLocalStamp(polledAt);
    var b = parseLocalStamp(serverNow);
    if (!a || !b) return false;
    var aMin = Date.UTC(a.y, a.mo - 1, a.d, a.h, a.mi, a.s);
    var bMin = Date.UTC(b.y, b.mo - 1, b.d, b.h, b.mi, b.s);
    return (bMin - aMin) > 3 * 60 * 1000;
  }

  function stat(label, value) {
    return '<div class="al-stat"><div class="k">' + esc(label) + '</div><div class="v">' + esc(value) + "</div></div>";
  }

  function filterValues() {
    return {
      q: (($("alSearch") && $("alSearch").value) || "").trim().toLowerCase(),
      online: (($("alFilterOnline") && $("alFilterOnline").value) || "all"),
      owner: (($("alFilterOwner") && $("alFilterOwner").value) || ""),
      type: (($("alFilterType") && $("alFilterType").value) || ""),
      auto: (($("alFilterAuto") && $("alFilterAuto").value) || "")
    };
  }

  function isPhoneDevice(device) {
    return device.device_type === "phone" || device.auto_role === "phone";
  }

  function passesFilters(device, filters, tab) {
    if (device.hidden) return false;
    if (tab === "roll") {
      if (!(device.currently_online || device.first_today || device.last_today)) return false;
    }
    if (tab === "phones" && !isPhoneDevice(device)) return false;
    if (filters.online === "online" && !device.currently_online) return false;
    if (filters.online === "offline" && device.currently_online) return false;
    if (filters.owner === "unset") {
      if (device.owner_category) return false;
    } else if (filters.owner && device.owner_category !== filters.owner) {
      return false;
    }
    // Phones tab is already phone-only; ignore type filter there.
    if (tab !== "phones" && filters.type && device.device_type !== filters.type) return false;
    if (filters.auto && (device.auto_role || "unknown") !== filters.auto) return false;
    return matchQuery(device, filters.q);
  }

  function filtered(tab) {
    var filters = filterValues();
    return (state.devices || [])
      .filter(function (device) { return passesFilters(device, filters, tab); })
      .slice()
      .sort(compareDevices);
  }

  function renderTables() {
    renderTable("alRollBody", "alRollEmpty", filtered("roll"), true);
    renderTable("alDevBody", "alDevEmpty", filtered("devices"), false);
    renderTable("alPhoneBody", "alPhoneEmpty", filtered("phones"), false);
    syncSortHeaders();
  }

  function refreshView(opts) {
    opts = opts || {};
    var y = window.scrollY || window.pageYOffset || 0;
    var x = window.scrollX || window.pageXOffset || 0;
    renderStats();
    renderTables();
    if (opts.keepScroll !== false) {
      window.scrollTo(x, y);
    }
    writeUrlState();
  }

  function readUrlState() {
    var p = new URLSearchParams(window.location.search || "");
    var tab = p.get("tab") || "roll";
    if (tab !== "devices" && tab !== "phones" && tab !== "dns") tab = "roll";
    return {
      tab: tab,
      q: p.get("q") || "",
      online: p.get("online") || "all",
      owner: p.get("owner") || "",
      type: p.get("type") || "",
      auto: p.get("auto") || "",
      mac: (p.get("mac") || "").trim().toLowerCase()
    };
  }

  function writeUrlState() {
    var filters = filterValues();
    var p = new URLSearchParams();
    if (state.tab === "devices" || state.tab === "phones" || state.tab === "dns") p.set("tab", state.tab);
    if (filters.q) p.set("q", ($("alSearch") && $("alSearch").value) || "");
    if (filters.online && filters.online !== "all") p.set("online", filters.online);
    if (filters.owner) p.set("owner", filters.owner);
    if (state.tab !== "phones" && filters.type) p.set("type", filters.type);
    if (filters.auto) p.set("auto", filters.auto);
    var mac = state.modalMac || state.selected || "";
    if (mac) p.set("mac", String(mac).replace(/:/g, "-"));
    var qs = p.toString();
    var next = window.location.pathname + (qs ? "?" + qs : "");
    var cur = window.location.pathname + window.location.search;
    if (next !== cur) history.replaceState(null, "", next);
  }

  function applyUrlState(urlState) {
    urlState = urlState || readUrlState();
    if ($("alSearch")) $("alSearch").value = urlState.q || "";
    if ($("alFilterOnline")) $("alFilterOnline").value = urlState.online || "all";
    if ($("alFilterOwner")) $("alFilterOwner").value = urlState.owner || "";
    if ($("alFilterType")) $("alFilterType").value = urlState.type || "";
    if ($("alFilterAuto")) $("alFilterAuto").value = urlState.auto || "";
    showTab(urlState.tab || "roll", true);
    if (urlState.mac) {
      var mac = String(urlState.mac).replace(/-/g, ":");
      state.selected = mac;
      state.modalMac = mac;
    }
  }

  function editableAlias(device) {
    var hasAlias = !!(device.alias && String(device.alias).trim());
    var main = hasAlias ? esc(String(device.alias).trim()) : "";
    var sub = device.hostname || device.mac || "";
    // Avoid repeating the same text under a blank/missing alias.
    if (hasAlias && String(device.alias).trim().toLowerCase() === String(sub).toLowerCase()) {
      sub = device.mac && String(device.mac).toLowerCase() !== String(sub).toLowerCase()
        ? device.mac
        : "";
    }
    var cls = "al-edit" + (hasAlias ? "" : " is-missing-alias");
    return '<td class="' + cls + '" data-edit="alias" title="' + esc(S.dblClick) + '"><strong>' +
      main + '</strong>' +
      (sub ? '<div class="al-muted">' + esc(sub) + "</div>" : "") +
      "</td>";
  }

  function chipCell(device, field) {
    var value = field === "owner_category" ? device.owner_category : device.device_type;
    return '<td class="al-chip-edit" data-edit="' + esc(field) + '" title="' +
      esc(S.clickEdit || "Click to edit") + '">' + chip(value) + "</td>";
  }

  function renderTable(bodyId, emptyId, rows, roll) {
    var body = $(bodyId);
    var empty = $(emptyId);
    if (!rows.length) {
      body.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    body.innerHTML = rows.map(function (device) {
      var selected = state.selected === device.mac ? " is-selected" : "";
      if (roll) {
        return "<tr data-mac=\"" + esc(device.mac) + "\" class=\"" + selected + "\">" +
          "<td>" + statusCell(device.currently_online) + "</td>" +
          editableAlias(device) +
          "<td>" + esc(device.last_ip || "—") + "</td>" +
          '<td class="al-daybar-td">' + dayBarHtml(device) + "</td>" +
          chipCell(device, "device_type") +
          chipCell(device, "owner_category") +
          "<td>" + chip(device.auto_role) + "</td></tr>";
      }
      return "<tr data-mac=\"" + esc(device.mac) + "\" class=\"" + selected + "\">" +
        "<td>" + statusCell(device.currently_online) + "</td>" +
        editableAlias(device) +
        "<td class=\"al-mac\">" + esc(device.mac) + "</td>" +
        "<td>" + esc(device.last_ip || "") + "</td>" +
        chipCell(device, "owner_category") +
        chipCell(device, "device_type") +
        "<td>" + chip(device.auto_role) + "</td>" +
        "<td>" + esc(device.first_seen || "—") + "</td>" +
        "<td>" + esc(device.last_seen || "") + "</td>" +
        '<td><button type="button" class="al-btn al-btn-danger al-remove" data-remove-mac="' +
          esc(device.mac) + '">' + esc(S.remove || "Remove") + "</button></td></tr>";
    }).join("");
  }

  function destroyChart() {
    if (state.chart) {
      try { state.chart.destroy(); } catch (err) { /* ignore */ }
      state.chart = null;
    }
  }

  function closeModal() {
    var modal = $("alModal");
    if (modal) modal.hidden = true;
    state.modalMac = "";
    state.selected = "";
    destroyChart();
    refreshView();
  }

  function openModal(mac) {
    state.modalMac = mac;
    state.selected = mac;
    refreshView();
    var monthEl = $("alMonth");
    var startEl = $("alStart");
    var endEl = $("alEnd");
    if (monthEl && !monthEl.value) monthEl.value = currentMonth();
    if (startEl) startEl.value = "";
    if (endEl) endEl.value = "";
    $("alModal").hidden = false;
    writeUrlState();
    loadChart();
  }

  function chartParams() {
    var start = ($("alStart") && $("alStart").value) || "";
    var end = ($("alEnd") && $("alEnd").value) || "";
    if (start && end) return { start: start, end: end };
    return { month: (($("alMonth") && $("alMonth").value) || currentMonth()) };
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }
  function formatClock(hourFloat) {
    if (hourFloat == null || isNaN(hourFloat)) return "";
    var clamped = Math.max(0, Math.min(24, Number(hourFloat)));
    if (clamped >= 24) return "24:00";
    var h = Math.floor(clamped);
    var m = Math.round((clamped - h) * 60);
    if (m >= 60) { h += 1; m = 0; }
    if (h >= 24) return "24:00";
    return pad2(h) + ":" + pad2(m);
  }

  function parseLocalStamp(text) {
    var m = String(text || "").match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (!m) return null;
    return {
      y: Number(m[1]), mo: Number(m[2]), d: Number(m[3]),
      h: Number(m[4]), mi: Number(m[5]), s: Number(m[6] || 0)
    };
  }

  function stampToHour(text) {
    var p = parseLocalStamp(text);
    if (!p) return null;
    return p.h + p.mi / 60 + p.s / 3600;
  }

  function clockFromStamp(text) {
    var p = parseLocalStamp(text);
    if (!p) return "";
    return pad2(p.h) + ":" + pad2(p.mi);
  }

  function nowHourOfDay() {
    var fromServer = stampToHour(state.serverNow);
    if (fromServer != null) return fromServer;
    var d = new Date();
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
  }

  function todayRangesFor(device) {
    var ranges = Array.isArray(device.today_ranges) ? device.today_ranges.slice() : [];
    if (ranges.length) return ranges;
    var startH = stampToHour(device.first_today);
    var endH = stampToHour(device.last_today);
    if (startH == null || endH == null) return [];
    if (device.currently_online) endH = Math.max(endH, nowHourOfDay());
    if (endH <= startH) endH = Math.min(24, startH + (1 / 60));
    return [[startH, endH]];
  }

  function dayBarHtml(device) {
    var ranges = todayRangesFor(device);
    // Prefer clipped today_ranges for labels so overnight sessions read 00:00→… not yesterday's hour.
    var first = "";
    var last = "";
    if (ranges.length) {
      first = formatClock(ranges[0][0]);
      last = formatClock(Math.max.apply(null, ranges.map(function (r) { return Number(r[1]) || 0; })));
    } else {
      first = clockFromStamp(device.first_today);
      last = clockFromStamp(device.last_today);
    }
    if (!ranges.length) {
      return '<div class="al-daybar"><div class="al-daybar-track"></div>' +
        '<div class="al-daybar-times al-muted">—</div></div>';
    }
    var nowH = nowHourOfDay();
    var minVisual = 8 / 60; // ~8 minutes so short visits remain visible
    var segs = ranges.map(function (seg) {
      var lo = Number(seg[0]);
      var hi = Number(seg[1]);
      if (!(hi > lo)) hi = lo + (1 / 60);
      if (hi - lo < minVisual) {
        var mid = (lo + hi) / 2;
        lo = Math.max(0, mid - minVisual / 2);
        hi = Math.min(24, mid + minVisual / 2);
      }
      var left = (lo / 24) * 100;
      var width = ((hi - lo) / 24) * 100;
      var cls = "al-daybar-seg" + (device.currently_online ? " is-online" : "");
      return '<div class="' + cls + '" style="left:' + left.toFixed(3) +
        "%;width:" + Math.max(width, 0.35).toFixed(3) + '%"></div>';
    }).join("");
    var nowMark = "";
    if (nowH != null && nowH >= 0 && nowH <= 24) {
      nowMark = '<div class="al-daybar-now" style="left:' + ((nowH / 24) * 100).toFixed(3) + '%"></div>';
    }
    var tipParts = [];
    if (first) tipParts.push((S.firstToday || "First") + ": " + first);
    if (last) tipParts.push((S.lastToday || "Last") + ": " + last);
    var tip = tipParts.join(" · ");
    var timeLine = first && last
      ? esc(first) + ' → <span class="al-daybar-end">' + esc(last) + "</span>"
      : "—";
    return '<div class="al-daybar" title="' + esc(tip) + '">' +
      '<div class="al-daybar-track">' + segs + nowMark + "</div>" +
      '<div class="al-daybar-times">' + timeLine + "</div></div>";
  }

  function presenceDatasets(ranges, exactStore) {
    var maxSeg = 0;
    (ranges || []).forEach(function (day) {
      maxSeg = Math.max(maxSeg, (day || []).length);
    });
    var minVisualHours = 12 / 60; // ~12 minutes so short visits still read as vertical ticks
    var datasets = [];
    var i;
    for (i = 0; i < Math.max(maxSeg, 1); i += 1) {
      datasets.push({
        label: S.online || "Online",
        data: (ranges || []).map(function (day, dayIndex) {
          var seg = day && day[i];
          if (!seg || seg.length < 2) return null;
          var lo = Number(seg[0]);
          var hi = Number(seg[1]);
          if (!(hi > lo)) hi = lo + (1 / 60);
          if (exactStore) exactStore[dayIndex + ":" + i] = [lo, hi];
          if (hi - lo < minVisualHours) {
            var mid = (lo + hi) / 2;
            var half = minVisualHours / 2;
            lo = Math.max(0, mid - half);
            hi = Math.min(24, mid + half);
          }
          return [lo, hi];
        }),
        backgroundColor: "rgba(59, 130, 246, 0.85)",
        borderColor: "#2563eb",
        borderWidth: 1,
        borderRadius: 2,
        barPercentage: 0.55,
        categoryPercentage: 0.9
      });
    }
    return datasets;
  }

  function loadChart() {
    if (!state.modalMac) return;
    var mac = state.modalMac;
    fetch(deviceUrl(mac, chartParams())).then(function (res) { return res.json(); }).then(function (payload) {
      if (!payload || !payload.ok || state.modalMac !== mac) return;
      var d = payload.device || {};
      $("alModalTitle").textContent = S.hours;
      $("alModalSub").textContent = nameOf(d) + " · " + (d.mac || "") +
        (payload.start && payload.end ? " · " + payload.start + " → " + payload.end : "");
      $("alTotalHours").textContent = (S.totalHours || "Total") + ": " + (payload.total_hours || 0) + " h";
      if ($("alChartHint")) $("alChartHint").textContent = S.chartHint || "";
      if ($("alMonth") && payload.month && !$("alStart").value) $("alMonth").value = payload.month;
      destroyChart();
      var canvas = $("alHoursChart");
      if (!canvas || !window.Chart) return;
      var labels = payload.labels || (payload.days || []).map(function (day) {
        return String(day).slice(5).replace("-", "/");
      });
      var dayKeys = payload.days || [];
      var hoursByDay = payload.hours || [];
      var exactStore = {};
      state.chart = new window.Chart(canvas, {
        type: "bar",
        data: {
          labels: labels,
          datasets: presenceDatasets(payload.ranges || [], exactStore)
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          datasets: {
            bar: { grouped: false }
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: function (items) {
                  var i = items[0] && items[0].dataIndex;
                  var key = dayKeys[i] || labels[i] || "";
                  var total = hoursByDay[i];
                  if (total != null) {
                    return key + " · " + total + " h";
                  }
                  return key;
                },
                label: function (item) {
                  var key = item.dataIndex + ":" + item.datasetIndex;
                  var exact = exactStore[key];
                  var raw = exact || item.raw;
                  if (!raw || !Array.isArray(raw) || raw.length < 2) return "";
                  return (S.online || "Online") + ": " +
                    formatClock(raw[0]) + " – " + formatClock(raw[1]);
                }
              }
            }
          },
          scales: {
            x: {
              offset: true,
              stacked: false,
              title: { display: true, text: S.chartX || "Date" },
              ticks: {
                autoSkip: false,
                maxRotation: 90,
                minRotation: 45,
                font: { size: labels.length > 20 ? 8 : 10 }
              }
            },
            y: {
              min: 0,
              max: 24,
              reverse: true,
              title: { display: true, text: S.chartY || "Time of day (00:00 top → 24:00 bottom)" },
              ticks: {
                stepSize: 2,
                callback: function (value) { return formatClock(value); }
              }
            }
          }
        }
      });
    }).catch(function () { /* ignore */ });
  }

  function patchDevice(mac, body, opts) {
    opts = opts || {};
    return fetch(deviceUrl(mac), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) { return res.json(); }).then(function (payload) {
      if (payload && payload.ok && payload.device) {
        state.devices = (state.devices || []).map(function (item) {
          return item.mac === mac ? Object.assign({}, item, payload.device) : item;
        });
        var devices = state.devices || [];
        state.counts = Object.assign({}, state.counts, {
          online: devices.filter(function (d) { return d.currently_online && !d.hidden; }).length,
          total: devices.filter(function (d) { return !d.hidden; }).length,
          unknown: devices.filter(function (d) {
            return !d.hidden && !d.device_type && (d.auto_role || "unknown") === "unknown";
          }).length
        });
        refreshView({ keepScroll: true });
      }
      return payload;
    });
  }

  function removeDevice(mac) {
    if (!mac) return;
    var msg = S.removeConfirm || "Remove this device?";
    if (!window.confirm(msg)) return;
    fetch(removeUrl(mac), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    })
      .then(function (res) {
        return res.json().then(function (payload) {
          return { okHttp: res.ok, status: res.status, payload: payload || {} };
        }).catch(function () {
          return { okHttp: false, status: res.status, payload: { ok: false, error: S.error || "Error" } };
        });
      })
      .then(function (result) {
        var payload = result.payload || {};
        if (payload.ok) {
          if (state.modalMac === mac) closeModal();
          loadSummary();
          return;
        }
        window.alert(payload.error || (S.error || "Error") + " (" + result.status + ")");
      })
      .catch(function () {
        window.alert(S.error || "Error");
      });
  }

  function closeChipMenus() {
    document.querySelectorAll(".al-chip-menu").forEach(function (el) {
      el.remove();
    });
  }

  function openChipMenu(cell, mac, field, current) {
    closeChipMenus();
    var scrollY = window.scrollY || window.pageYOffset || 0;
    var scrollX = window.scrollX || window.pageXOffset || 0;
    var pairs = field === "owner_category"
      ? [["", S.unset], ["personal", S.personal], ["business", S.business]]
      : [["", S.unset], ["phone", S.phone], ["computer", S.computer], ["printer", S.printer], ["other", S.other]];

    var menu = document.createElement("div");
    menu.className = "al-chip-menu";
    menu.setAttribute("role", "listbox");
    pairs.forEach(function (pair) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "al-chip-menu-item" + (pair[0] === current ? " is-active" : "");
      btn.textContent = pair[1];
      btn.addEventListener("mousedown", function (ev) {
        // Prevent focus scroll / blur races before click fires.
        ev.preventDefault();
        ev.stopPropagation();
      });
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var value = pair[0];
        cleanup();
        if (value === current) return;
        var body = {};
        body[field] = value;
        patchDevice(mac, body, { quiet: true }).then(function (payload) {
          if (!payload || !payload.ok) {
            window.alert((payload && payload.error) || S.error || "Error");
          }
        });
      });
      menu.appendChild(btn);
    });

    document.body.appendChild(menu);
    var rect = cell.getBoundingClientRect();
    var menuRect = menu.getBoundingClientRect();
    var left = Math.min(rect.left, window.innerWidth - menuRect.width - 8);
    left = Math.max(8, left);
    var top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - menuRect.height - 4);
    }
    menu.style.left = left + "px";
    menu.style.top = top + "px";

    function cleanup() {
      document.removeEventListener("mousedown", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onScroll, true);
      closeChipMenus();
      window.scrollTo(scrollX, scrollY);
    }
    function onDoc(ev) {
      if (menu.contains(ev.target) || cell.contains(ev.target)) return;
      cleanup();
    }
    function onKey(ev) {
      if (ev.key === "Escape") cleanup();
    }
    function onScroll() {
      cleanup();
    }
    document.addEventListener("mousedown", onDoc, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
    // Undo any accidental scroll from the click.
    window.scrollTo(scrollX, scrollY);
    requestAnimationFrame(function () { window.scrollTo(scrollX, scrollY); });
  }

  function startInlineEdit(cell) {
    var row = cell.closest("tr[data-mac]");
    if (!row) return;
    var mac = row.getAttribute("data-mac");
    var field = cell.getAttribute("data-edit");
    var device = (state.devices || []).filter(function (item) { return item.mac === mac; })[0];
    if (!device || !field) return;
    state.skipClickUntil = Date.now() + 600;

    if (field === "alias") {
      if (cell.querySelector("input")) return;
      var input = document.createElement("input");
      input.className = "al-input al-inline";
      input.value = device.alias || "";
      input.placeholder = device.hostname || S.alias;
      cell.innerHTML = "";
      cell.appendChild(input);
      if (input.focus) input.focus({ preventScroll: true });
      input.select();
      function saveAlias() {
        var value = input.value.trim();
        if (value === (device.alias || "")) { renderTables(); return; }
        patchDevice(mac, { alias: value });
      }
      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); saveAlias(); }
        if (ev.key === "Escape") { ev.preventDefault(); renderTables(); }
      });
      input.addEventListener("blur", saveAlias);
      return;
    }

    if (field !== "owner_category" && field !== "device_type") return;
    var current = field === "owner_category" ? (device.owner_category || "") : (device.device_type || "");
    openChipMenu(cell, mac, field, current);
  }

  function loadSummary() {
    return fetch(cfg.summary).then(function (res) { return res.json(); }).then(function (payload) {
      if (!payload || !payload.ok) return;
      state.devices = payload.devices || [];
      state.poll = payload.poll || {};
      state.counts = payload.counts || {};
      state.serverNow = payload.server_now || "";
      refreshView({ keepScroll: true });
    });
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) { return res.json(); });
  }

  function showTab(tab, skipUrl) {
    if (tab === "devices" || tab === "phones" || tab === "dns") state.tab = tab;
    else state.tab = "roll";
    document.querySelectorAll(".al-tab").forEach(function (btn) {
      btn.classList.toggle("is-active", btn.getAttribute("data-tab") === state.tab);
    });
    document.querySelectorAll(".al-panel").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== state.tab;
    });
    var typeFilter = $("alFilterType");
    if (typeFilter) {
      typeFilter.hidden = state.tab === "phones" || state.tab === "dns";
      typeFilter.disabled = state.tab === "phones" || state.tab === "dns";
    }
    var filters = $("alFilters");
    if (filters) filters.hidden = state.tab === "dns";
    if (state.tab === "dns") loadDnsPanel();
    if (!skipUrl) writeUrlState();
  }

  $("alTabs").addEventListener("click", function (ev) {
    var btn = ev.target.closest(".al-tab");
    if (!btn) return;
    showTab(btn.getAttribute("data-tab"));
  });

  function onFilterChange() {
    refreshView({ keepScroll: true });
  }
  ["alSearch", "alFilterOnline", "alFilterOwner", "alFilterType", "alFilterAuto"].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("input", onFilterChange);
    if (el) el.addEventListener("change", onFilterChange);
  });

  document.addEventListener("click", function (ev) {
    var sortTh = ev.target.closest(".al-table th[data-sort]");
    if (sortTh) {
      ev.preventDefault();
      ev.stopPropagation();
      setSort(sortTh.getAttribute("data-sort"));
      return;
    }
    if (ev.target.closest("[data-close-modal]")) {
      closeModal();
      return;
    }
    var removeBtn = ev.target.closest("[data-remove-mac]");
    if (removeBtn) {
      ev.preventDefault();
      ev.stopPropagation();
      removeDevice(removeBtn.getAttribute("data-remove-mac"));
      return;
    }
    var chipCellEl = ev.target.closest("td.al-chip-edit");
    if (chipCellEl) {
      ev.preventDefault();
      ev.stopPropagation();
      startInlineEdit(chipCellEl);
      return;
    }
    if (Date.now() < state.skipClickUntil) return;
    if (ev.target.closest("td.al-edit, .al-edit input, .al-inline, .al-cell-select")) return;
    var row = ev.target.closest("tbody tr[data-mac]");
    if (row && !ev.target.closest(".al-modal")) {
      openModal(row.getAttribute("data-mac"));
    }
  });

  document.addEventListener("dblclick", function (ev) {
    var cell = ev.target.closest("td.al-edit[data-edit='alias']");
    if (!cell) return;
    ev.preventDefault();
    ev.stopPropagation();
    startInlineEdit(cell);
  });

  $("alApplyRange").addEventListener("click", function () {
    loadChart();
  });
  $("alMonth").addEventListener("change", function () {
    if ($("alStart")) $("alStart").value = "";
    if ($("alEnd")) $("alEnd").value = "";
    loadChart();
  });

  $("alPollBtn").addEventListener("click", function () {
    var btn = $("alPollBtn");
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = S.polling;
    postJson(cfg.poll, { immediate_absent: true }).then(function (payload) {
      if (payload && payload.busy) {
        window.alert(payload.error || S.polling);
      }
      return loadSummary();
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = original;
    });
  });

  if ($("alRestartPollerBtn")) {
    $("alRestartPollerBtn").addEventListener("click", function () {
      var msg = S.restartPollerConfirm || "Restart attendance poller service only?";
      if (!window.confirm(msg)) return;
      var btn = $("alRestartPollerBtn");
      var original = btn.textContent;
      btn.disabled = true;
      btn.textContent = S.restartingPoller || "Restarting…";
      postJson(cfg.restartPoller, {}).then(function (payload) {
        if (payload && payload.ok) {
          window.alert(S.restartPollerDone || "Restarted.");
          return loadSummary();
        }
        window.alert((payload && (payload.error || payload.detail)) || S.error || "Error");
      }).catch(function () {
        window.alert(S.error || "Error");
      }).finally(function () {
        btn.disabled = false;
        btn.textContent = original;
      });
    });
  }

  function formatDnsTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function renderDnsRows(rows) {
    var body = $("alDnsBody");
    var empty = $("alDnsEmpty");
    if (!body) return;
    body.innerHTML = "";
    if (!rows || !rows.length) {
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    rows.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(formatDnsTime(row.time)) + "</td>" +
        "<td>" + esc(row.qname || "") + "</td>" +
        "<td>" + esc(row.qtype || "") + "</td>" +
        "<td>" + esc(row.proto || "") + "</td>";
      body.appendChild(tr);
    });
  }

  function loadDnsClients(keepIp) {
    if (!cfg.dnsClients) return Promise.resolve();
    var selected = keepIp || ($("alDnsIp") && $("alDnsIp").value) || "";
    return fetch(cfg.dnsClients).then(function (res) { return res.json(); }).then(function (payload) {
      var sel = $("alDnsIp");
      if (!sel) return;
      var clients = (payload && payload.clients) || [];
      sel.innerHTML = "";
      var opt0 = document.createElement("option");
      opt0.value = "";
      opt0.textContent = S.dnsPick || "Select IP";
      sel.appendChild(opt0);
      clients.forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c.ip;
        opt.textContent = c.display || c.ip;
        sel.appendChild(opt);
      });
      if (selected) sel.value = selected;
      var meta = (payload && payload.meta) || {};
      var metaEl = $("alDnsMeta");
      if (metaEl) {
        var bits = [];
        if (meta.last_poll) bits.push((S.dnsMeta || "Last pull") + ": " + meta.last_poll);
        if (meta.last_error) bits.push(meta.last_error);
        if (!meta.password_set) bits.push("DNS_SSH_PASSWORD missing");
        metaEl.textContent = bits.join(" · ");
      }
    });
  }

  function loadDnsQueries() {
    var ip = $("alDnsIp") && $("alDnsIp").value;
    var hours = ($("alDnsHours") && $("alDnsHours").value) || "24";
    if (!ip) {
      renderDnsRows([]);
      return Promise.resolve();
    }
    var url = cfg.dnsQueries + "?ip=" + encodeURIComponent(ip) +
      "&hours=" + encodeURIComponent(hours) + "&limit=500";
    return fetch(url).then(function (res) { return res.json(); }).then(function (payload) {
      var rows = (payload && payload.rows) || [];
      renderDnsRows(rows);
      var meta = (payload && payload.meta) || {};
      var metaEl = $("alDnsMeta");
      if (metaEl) {
        var bits = [];
        if (meta.last_poll) bits.push((S.dnsMeta || "Last pull") + ": " + meta.last_poll);
        var total = payload && payload.total != null ? Number(payload.total) : rows.length;
        var shownTpl = S.dnsShowing || "Showing {shown} of {total}";
        bits.push(shownTpl.replace("{shown}", String(rows.length)).replace("{total}", String(total)));
        if (payload && payload.truncated) bits.push("CSV download has full window");
        if (meta.last_error) bits.push(meta.last_error);
        metaEl.textContent = bits.join(" · ");
      }
    });
  }

  function loadDnsPanel() {
    return loadDnsClients(true).then(function () { return loadDnsQueries(); });
  }

  if ($("alDnsIp")) $("alDnsIp").addEventListener("change", function () { loadDnsQueries(); });
  if ($("alDnsHours")) $("alDnsHours").addEventListener("change", function () { loadDnsQueries(); });
  if ($("alDnsRefresh")) $("alDnsRefresh").addEventListener("click", function () { loadDnsPanel(); });
  if ($("alDnsDownload")) $("alDnsDownload").addEventListener("click", function () {
    var ip = $("alDnsIp") && $("alDnsIp").value;
    if (!ip) {
      window.alert(S.dnsDownloadNeedIp || "Select a client IP first.");
      return;
    }
    var hours = ($("alDnsHours") && $("alDnsHours").value) || "24";
    var url = (cfg.dnsQueriesCsv || "") +
      "?ip=" + encodeURIComponent(ip) +
      "&hours=" + encodeURIComponent(hours);
    window.location.href = url;
  });
  if ($("alDnsIngest")) $("alDnsIngest").addEventListener("click", function () {
    var btn = $("alDnsIngest");
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = S.dnsIngesting || "Pulling…";
    postJson(cfg.dnsIngest, {}).then(function (payload) {
      if (payload && payload.error && !payload.ok) window.alert(payload.error);
      return loadDnsPanel();
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = original;
    });
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && $("alModal") && !$("alModal").hidden) closeModal();
  });

  if ($("alMonth")) $("alMonth").value = currentMonth();
  applyUrlState(readUrlState());
  loadSummary().then(function () {
    var mac = state.modalMac;
    if (mac) openModal(mac);
  });
  setInterval(function () { loadSummary(); }, 15000);
})();
