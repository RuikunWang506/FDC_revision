(function () {
  var cfg = window.SMART_SEARCH || {};
  var input = document.getElementById("ssInput");
  var box = document.getElementById("ssBox");
  var form = document.getElementById("ssForm");
  var clearBtn = document.getElementById("ssClear");
  var statusEl = document.getElementById("ssStatus");
  var resultsEl = document.getElementById("ssResults");
  var jumpEl = document.getElementById("ssJump");
  var jumpRail = document.getElementById("ssJumpRail");
  var chips = document.getElementById("ssChips");
  var toolkit = document.getElementById("ssToolkit");
  var toolkitBtns = document.getElementById("ssToolkitBtns");
  var previewEl = document.getElementById("ssPatternPreview");
  var heroHome = document.getElementById("ssHeroHome");
  var navSlot = document.getElementById("ssNavSlot");
  var nav = document.querySelector(".navbar");
  var strings = cfg.strings || {};
  var lastSubmitted = "";
  var seq = 0;
  var abortCtl = null;
  var activeIndex = -1;
  var livePattern = null;
  var liveCats = {};
  var collapsed = {};
  var showNoOrder = false;

  function setDocked(on) {
    var moveEl = form || box;
    document.body.classList.toggle("ss-docked", !!on);
    if (nav) nav.classList.toggle("navbar-ss-has-search", !!on);
    if (on) {
      if (navSlot && moveEl && moveEl.parentNode !== navSlot) {
        navSlot.hidden = false;
        navSlot.appendChild(moveEl);
      }
      if (toolkit) toolkit.hidden = true;
      if (chips) chips.hidden = true;
    } else {
      if (heroHome && moveEl && moveEl.parentNode !== heroHome) {
        heroHome.appendChild(moveEl);
      }
      if (navSlot) navSlot.hidden = true;
      if (toolkit) toolkit.hidden = false;
      if (chips) chips.hidden = false;
    }
  }

  function pageUrl(query) {
    if (!query) return "/smart_search";
    return "/smart_search/" + encodeURIComponent(query);
  }

  function pathQuery() {
    var path = location.pathname || "";
    var prefix = "/smart_search/";
    if (path.indexOf(prefix) === 0) {
      try {
        return decodeURIComponent(path.slice(prefix.length));
      } catch (err) {
        return path.slice(prefix.length);
      }
    }
    var params = new URLSearchParams(location.search || "");
    return (params.get("q") || "").trim();
  }

  function syncUrl(query) {
    var next = pageUrl(query);
    if (location.pathname + location.search === next) return;
    if (history.pushState) {
      history.pushState({ q: query }, "", next);
    }
  }

  function setStatus(text, show) {
    if (!statusEl) return;
    statusEl.hidden = !show;
    statusEl.textContent = text || "";
  }

  function currentRows() {
    return resultsEl
      ? Array.prototype.slice.call(resultsEl.querySelectorAll(".ss-category:not(.is-collapsed) .ss-row:not(.ss-pending-row)"))
      : [];
  }

  function setActive(next) {
    var rows = currentRows();
    if (!rows.length) {
      activeIndex = -1;
      return;
    }
    activeIndex = (next + rows.length) % rows.length;
    rows.forEach(function (row, i) {
      row.classList.toggle("is-active", i === activeIndex);
    });
    rows[activeIndex].scrollIntoView({ block: "nearest" });
  }

  function compilePattern(raw) {
    raw = (raw || "").trim();
    if (!raw) return null;
    if (raw.toLowerCase().indexOf("re:") === 0) {
      return { like: "%" + raw.slice(3).replace(/[^A-Za-z0-9]+/g, "") + "%", regex: raw.slice(3), mode: "regex", core: raw.slice(3), is_regex: true };
    }
    if (raw.charAt(0) === "/" && raw.charAt(raw.length - 1) === "/" && raw.length >= 3) {
      return { like: "%" + raw.slice(1, -1).replace(/[^A-Za-z0-9]+/g, "") + "%", regex: raw.slice(1, -1), mode: "regex", core: raw.slice(1, -1), is_regex: true };
    }
    var like = raw.replace(/\*/g, "%").replace(/\?/g, "_");
    if (!/[%_*?]/.test(raw)) like = "%" + raw + "%";
    var core = raw.replace(/[%_*?]+/g, "");
    return { like: like, regex: core, mode: /[%_*?]/.test(raw) ? "wildcard" : "text", core: core, is_regex: false };
  }

  function highlightNode(text, pat) {
    var span = document.createElement("span");
    text = text || "";
    var needle = ((pat && (pat.core || pat.raw)) || "").replace(/[%_*?/]/g, "").trim();
    if (!needle || needle.length < 2) {
      span.textContent = text;
      return span;
    }
    var rx;
    try {
      if (pat && pat.is_regex && pat.regex) {
        rx = new RegExp(pat.regex, "ig");
      } else {
        rx = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "ig");
      }
    } catch (err) {
      span.textContent = text;
      return span;
    }
    var last = 0;
    var match;
    rx.lastIndex = 0;
    while ((match = rx.exec(text))) {
      if (match.index > last) {
        span.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      var mark = document.createElement("mark");
      mark.className = "ss-hl";
      mark.textContent = match[0];
      span.appendChild(mark);
      last = match.index + match[0].length;
      if (!match[0].length) break;
    }
    if (last < text.length) {
      span.appendChild(document.createTextNode(text.slice(last)));
    }
    if (!span.childNodes.length) span.textContent = text;
    return span;
  }

  function categoryCount(section) {
    if (!section) return 0;
    if (section.classList.contains("is-pending")) return 0;
    return Number(section.dataset.count || 0);
  }

  function gridCols() {
    var w = (resultsEl && resultsEl.clientWidth) || window.innerWidth || 1200;
    if (w < 560) return 1;
    if (w < 820) return 2;
    if (w < 1200) return 6;
    return 10;
  }

  function wantedSpan(count, items, pending) {
    if (pending) return 2;
    var n = count || 0;
    var span = 2;
    if (n > 2) span = 3;
    if (n > 5) span = 4;
    if (n > 10) span = 5;
    if (n > 18) span = 6;
    if (n > 36) span = 10;
    var long = 0;
    (items || []).slice(0, 10).forEach(function (it) {
      var len = ((it.subtitle || "") + " " + (it.match_text || "")).length;
      if (len > 52) long += 1;
    });
    if (long >= 2 && span < 10) span += 1;
    if (long >= 5 && span < 10) span = Math.min(10, span + 1);
    return span;
  }

  function applySpan(node) {
    var want = Number(node.dataset.spanWant || 2);
    if (node.classList.contains("is-collapsed")) want = Math.min(want, 2);
    var span = Math.max(1, Math.min(want, gridCols()));
    node.style.setProperty("--ss-span", String(span));
    var cols = span >= 8 ? 3 : span >= 4 ? 2 : 1;
    if (gridCols() <= 2) cols = 1;
    node.dataset.cols = String(cols);
  }

  function matchLine(item) {
    if (!item.match_in && !item.match_text) return null;
    var wrap = document.createElement("div");
    wrap.className = "ss-match";
    var lab = document.createElement("span");
    lab.className = "ss-match-lab";
    lab.textContent = (strings.matchedIn || "Matched in") + " " + (item.match_in || "field") + ": ";
    wrap.appendChild(lab);
    wrap.appendChild(highlightNode(item.match_text || "", livePattern));
    return wrap;
  }

  function hasOrder(item) {
    return !item || item.has_order !== false;
  }

  function quietItems(cat) {
    return ((cat && cat.items) || []).filter(function (it) {
      return it.has_order === false;
    });
  }

  function displayItems(cat) {
    var items = (cat && cat.items) || [];
    if (!cat || cat.id !== "customers" || showNoOrder) return items;
    return items.filter(hasOrder);
  }

  function toggleCategory(catId, section) {
    collapsed[catId] = !collapsed[catId];
    section.classList.toggle("is-collapsed", !!collapsed[catId]);
    var btn = section.querySelector(".ss-toggle");
    if (btn) {
      btn.setAttribute("aria-expanded", collapsed[catId] ? "false" : "true");
      btn.title = collapsed[catId] ? strings.expand || "Expand" : strings.collapse || "Collapse";
    }
    rearrange();
  }

  function buildSection(cat, pending) {
    var section = document.createElement("section");
    section.className = "ss-category" + (pending ? " is-pending" : "") + (collapsed[cat.id] ? " is-collapsed" : "");
    section.id = "ss-cat-" + (cat.id || "x");
    section.dataset.cat = cat.id || "";
    var shown = pending ? [] : displayItems(cat);
    var n = pending ? 0 : shown.length;
    section.dataset.count = String(n);
    section.dataset.spanWant = String(wantedSpan(n, shown, pending));
    var head = document.createElement("div");
    head.className = "ss-category-head";
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "ss-toggle";
    toggle.setAttribute("aria-expanded", collapsed[cat.id] ? "false" : "true");
    toggle.title = collapsed[cat.id] ? strings.expand || "Expand" : strings.collapse || "Collapse";
    var chev = document.createElement("i");
    chev.className = "fas fa-chevron-down ss-toggle-icon";
    var icon = document.createElement("i");
    icon.className = "fas " + (cat.icon || "fa-search");
    var label = document.createElement("span");
    label.textContent = cat.label || cat.id;
    toggle.appendChild(chev);
    toggle.appendChild(icon);
    toggle.appendChild(label);
    toggle.addEventListener("click", function (ev) {
      ev.preventDefault();
      toggleCategory(cat.id, section);
    });
    var count = document.createElement("span");
    count.className = "ss-count";
    count.textContent = pending ? "…" : String(n);
    if (!pending && cat.id === "customers" && quietItems(cat).length) {
      var quietBtn = document.createElement("button");
      quietBtn.type = "button";
      quietBtn.className = "ss-quiet-toggle" + (showNoOrder ? " is-on" : "");
      quietBtn.textContent = showNoOrder
        ? strings.hideNoOrder || "Hide no order"
        : quietItems(cat).length + " " + (strings.noOrder || "no order");
      quietBtn.title = showNoOrder
        ? strings.hideNoOrder || "Hide customers with no order"
        : strings.showNoOrder || "Show customers with no order";
      quietBtn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        showNoOrder = !showNoOrder;
        if (liveCats.customers) upsertCategory(liveCats.customers, false);
      });
      head.appendChild(quietBtn);
    }
    var open = document.createElement("a");
    open.className = "ss-cat-open";
    open.href = cat.href || "#";
    open.title = strings.open || "Open";
    var openIcon = document.createElement("i");
    openIcon.className = "fas fa-external-link-alt";
    open.appendChild(openIcon);
    head.appendChild(toggle);
    head.appendChild(count);
    head.appendChild(open);
    section.appendChild(head);
    var body = document.createElement("div");
    body.className = "ss-category-body";
    if (pending) {
      var wait = document.createElement("div");
      wait.className = "ss-row ss-pending-row";
      wait.textContent = strings.loading || "Searching…";
      body.appendChild(wait);
      section.appendChild(body);
      return section;
    }
    shown.forEach(function (item) {
      var row = document.createElement("a");
      row.className = "ss-row";
      row.href = item.href || "#";
      var rowBody = document.createElement("div");
      rowBody.className = "ss-row-body";
      var name = document.createElement("div");
      name.className = "ss-row-title";
      name.appendChild(highlightNode(item.title || "", livePattern));
      if (item.badge) {
        var badge = document.createElement("span");
        badge.className = "ss-badge";
        badge.textContent = item.badge;
        name.appendChild(badge);
      }
      if (item.has_order === false) {
        var quietBadge = document.createElement("span");
        quietBadge.className = "ss-badge ss-badge-quiet";
        quietBadge.textContent = strings.noOrder || "no order";
        name.appendChild(quietBadge);
      }
      rowBody.appendChild(name);
      if (item.subtitle) {
        var sub = document.createElement("div");
        sub.className = "ss-row-sub";
        sub.appendChild(highlightNode(item.subtitle, livePattern));
        rowBody.appendChild(sub);
      }
      var found = matchLine(item);
      if (found) rowBody.appendChild(found);
      var chevron = document.createElement("i");
      chevron.className = "fas fa-chevron-right ss-chevron";
      row.appendChild(rowBody);
      row.appendChild(chevron);
      body.appendChild(row);
    });
    section.appendChild(body);
    return section;
  }

  function refreshJump() {
    if (!jumpEl || !jumpRail || !resultsEl) return;
    jumpRail.innerHTML = "";
    var sections = Array.prototype.slice.call(resultsEl.querySelectorAll(".ss-category"));
    if (!sections.length) {
      jumpEl.hidden = true;
      document.body.classList.remove("ss-has-jump");
      return;
    }
    jumpEl.hidden = false;
    document.body.classList.add("ss-has-jump");
    sections.forEach(function (section) {
      var cat = liveCats[section.dataset.cat] || {
        id: section.dataset.cat,
        label: section.dataset.cat,
        icon: "fa-search",
        count: 0,
      };
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ss-jump-item" + (section.classList.contains("is-pending") ? " is-pending" : "");
      btn.dataset.cat = cat.id;
      var ic = document.createElement("i");
      ic.className = "fas " + (cat.icon || "fa-search");
      var lab = document.createElement("span");
      lab.className = "ss-jump-label";
      lab.textContent = cat.label || cat.id;
      var n = document.createElement("span");
      n.className = "ss-jump-count";
      n.textContent = section.classList.contains("is-pending") ? "…" : String(section.dataset.count || cat.count || 0);
      btn.appendChild(ic);
      btn.appendChild(lab);
      btn.appendChild(n);
      btn.addEventListener("click", function () {
        collapsed[cat.id] = false;
        section.classList.remove("is-collapsed");
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      jumpRail.appendChild(btn);
    });
  }

  function rearrange() {
    if (!resultsEl) return;
    var nodes = Array.prototype.slice.call(resultsEl.children);
    nodes.sort(function (a, b) {
      var pa = a.classList.contains("is-pending") ? 1 : 0;
      var pb = b.classList.contains("is-pending") ? 1 : 0;
      var sa = Number(a.dataset.spanWant || 2);
      var sb = Number(b.dataset.spanWant || 2);
      var ca = categoryCount(a);
      var cb = categoryCount(b);
      if (sa !== sb) return sa - sb;
      if (pa !== pb) return pa - pb;
      if (ca !== cb) return ca - cb;
      return String(a.dataset.cat || "").localeCompare(String(b.dataset.cat || ""));
    });
    nodes.forEach(function (node) {
      applySpan(node);
      resultsEl.appendChild(node);
    });
    refreshJump();
  }

  function upsertCategory(cat, pending) {
    if (!resultsEl || !cat || !cat.id) return;
    var existing = resultsEl.querySelector('[data-cat="' + cat.id + '"]');
    var next = buildSection(cat, !!pending);
    if (existing) {
      resultsEl.replaceChild(next, existing);
    } else {
      resultsEl.appendChild(next);
    }
    rearrange();
  }

  function showPreview(fromServer) {
    if (!previewEl) return;
    var pat = fromServer || compilePattern(input ? input.value : "");
    if (!pat || !(pat.like || pat.regex)) {
      previewEl.hidden = true;
      previewEl.textContent = "";
      return;
    }
    var label = strings.pattern || "Searches as";
    previewEl.hidden = false;
    previewEl.textContent = label + " LIKE " + (pat.like || "") + "   ·   regex " + (pat.regex || "");
  }

  function innerToken(value) {
    var v = (value || "").trim();
    if (v.toLowerCase().indexOf("re:") === 0) v = v.slice(3);
    if (v.charAt(0) === "/" && v.charAt(v.length - 1) === "/" && v.length >= 3) v = v.slice(1, -1);
    return v.replace(/^[%*_?]+/, "").replace(/[%*_?]+$/, "") || v;
  }

  function applyToolkit(kind) {
    if (!input) return;
    var token = innerToken(input.value) || "3305";
    var next = token;
    if (kind === "contains") next = "%" + token + "%";
    else if (kind === "starts") next = token + "%";
    else if (kind === "ends") next = "%" + token;
    else if (kind === "any") next = (input.value || token) + "%";
    else if (kind === "one") next = (input.value || token) + "_";
    else if (kind === "regex") next = "/" + token + "/";
    input.value = next;
    input.focus();
    if (clearBtn) clearBtn.hidden = !next;
    showPreview();
  }

  function submitSearch(q, fromHistory) {
    var query = (q || "").trim();
    lastSubmitted = query;
    if (!fromHistory) syncUrl(query);
    search(query);
  }

  function readSse(res, onEvent) {
    if (!res.body || !res.body.getReader) {
      return res.json().then(function (body) {
        onEvent({ type: "legacy", body: body });
      });
    }
    var reader = res.body.getReader();
    var dec = new TextDecoder();
    var buf = "";
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;
        buf += dec.decode(chunk.value, { stream: true });
        var parts = buf.split("\n\n");
        buf = parts.pop();
        parts.forEach(function (block) {
          var data = block
            .split("\n")
            .filter(function (line) {
              return line.indexOf("data:") === 0;
            })
            .map(function (line) {
              return line.slice(5).replace(/^\s/, "");
            })
            .join("");
          if (!data) return;
          try {
            onEvent(JSON.parse(data));
          } catch (err) {}
        });
        return pump();
      });
    }
    return pump();
  }

  function search(q) {
    var query = (q || "").trim();
    setDocked(query.length >= 2);
    if (clearBtn) clearBtn.hidden = !query;
    showPreview();
    if (query.length < 2) {
      resultsEl.innerHTML = "";
      liveCats = {};
      collapsed = {};
      refreshJump();
      setStatus(query ? strings.min || "Type at least 2 characters." : "", !!query);
      return;
    }
    var mySeq = ++seq;
    liveCats = {};
    showNoOrder = false;
    livePattern = compilePattern(query);
    resultsEl.innerHTML = "";
    refreshJump();
    setStatus(strings.loading || "Searching…", true);
    if (abortCtl) abortCtl.abort();
    abortCtl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var url = (cfg.stream || "/api/smart_search/stream") + "?q=" + encodeURIComponent(query);
    fetch(url, {
      headers: { Accept: "text/event-stream", "X-Requested-With": "XMLHttpRequest" },
      signal: abortCtl ? abortCtl.signal : undefined,
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (body) {
            throw new Error((body && body.error) || strings.error || "Search failed.");
          });
        }
        return readSse(res, function (evt) {
          if (mySeq !== seq) return;
          if (evt.type === "legacy" && evt.body) {
            if (evt.body.pattern) {
              livePattern = evt.body.pattern;
              showPreview(evt.body.pattern);
            }
            (evt.body.categories || []).forEach(function (cat) {
              liveCats[cat.id] = cat;
              upsertCategory(cat, false);
            });
            setStatus(Object.keys(liveCats).length ? "" : strings.noResults, !Object.keys(liveCats).length);
            return;
          }
          if (evt.type === "start") {
            if (evt.pattern) {
              livePattern = evt.pattern;
              showPreview(evt.pattern);
            }
            (evt.planned || []).forEach(function (cat) {
              if (!liveCats[cat.id]) liveCats[cat.id] = cat;
              upsertCategory(cat, true);
            });
            return;
          }
          if (evt.type === "category" && evt.category) {
            if (evt.category.count) {
              liveCats[evt.category.id] = evt.category;
              upsertCategory(evt.category, false);
            } else {
              var empty = resultsEl.querySelector('[data-cat="' + evt.category.id + '"]');
              if (empty) empty.remove();
              refreshJump();
            }
            var found = Object.keys(liveCats).length;
            setStatus(found ? "" : strings.loading, !found);
            return;
          }
          if (evt.type === "done") {
            setStatus(Object.keys(liveCats).length ? "" : strings.noResults, !Object.keys(liveCats).length);
            refreshJump();
          }
        });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (mySeq !== seq) return;
        setStatus(err.message || strings.error || "Search failed.", true);
      });
  }

  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      submitSearch(input ? input.value : "");
    });
  }

  if (input) {
    input.addEventListener("input", function () {
      if (clearBtn) clearBtn.hidden = !(input.value || "").trim();
      showPreview();
    });
    input.addEventListener("focus", function () {
      if (box) box.classList.add("is-focus");
      this.select();
    });
    input.addEventListener("blur", function () {
      if (box) box.classList.remove("is-focus");
    });
    input.addEventListener("keydown", function (ev) {
      var rows = currentRows();
      var typed = (input.value || "").trim();
      if (ev.key === "ArrowDown" && rows.length) {
        ev.preventDefault();
        setActive(activeIndex + 1);
      } else if (ev.key === "ArrowUp" && rows.length) {
        ev.preventDefault();
        setActive(activeIndex < 0 ? rows.length - 1 : activeIndex - 1);
      } else if (ev.key === "Enter" && activeIndex >= 0 && rows[activeIndex] && typed === lastSubmitted) {
        ev.preventDefault();
        window.location.href = rows[activeIndex].href;
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        submitSearch(input.value);
      } else if (ev.key === "Escape") {
        input.value = "";
        submitSearch("");
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      input.value = "";
      input.focus();
      submitSearch("");
    });
  }

  if (chips) {
    chips.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-fill]");
      if (!btn) return;
      input.value = btn.getAttribute("data-fill") || "";
      input.focus();
      submitSearch(input.value);
    });
  }

  if (toolkitBtns) {
    toolkitBtns.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-pattern]");
      if (!btn) return;
      applyToolkit(btn.getAttribute("data-pattern"));
    });
  }

  window.addEventListener("popstate", function (ev) {
    var q = (ev.state && ev.state.q != null) ? ev.state.q : pathQuery();
    if (input) input.value = q;
    lastSubmitted = (q || "").trim();
    search(lastSubmitted);
  });

  var boot = (cfg.initial || pathQuery() || (input && input.value) || "").trim();
  if (boot) {
    lastSubmitted = boot;
    search(boot);
  }

  var fitTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(rearrange, 120);
  });
})();
