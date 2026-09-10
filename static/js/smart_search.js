(function () {
  var cfg = window.SMART_SEARCH || {};
  var input = document.getElementById("ssInput");
  var box = document.getElementById("ssBox");
  var clearBtn = document.getElementById("ssClear");
  var statusEl = document.getElementById("ssStatus");
  var resultsEl = document.getElementById("ssResults");
  var chips = document.getElementById("ssChips");
  var strings = cfg.strings || {};
  var timer = null;
  var seq = 0;
  var abortCtl = null;
  var activeIndex = -1;

  function setStatus(text, show) {
    if (!statusEl) return;
    statusEl.hidden = !show;
    statusEl.textContent = text || "";
  }

  function currentRows() {
    return resultsEl ? Array.prototype.slice.call(resultsEl.querySelectorAll(".ss-row")) : [];
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

  function render(data) {
    resultsEl.innerHTML = "";
    activeIndex = -1;
    var cats = (data && data.categories) || [];
    if (!cats.length) {
      setStatus(strings.noResults || "No matching categories.", true);
      return;
    }
    setStatus("", false);
    cats.forEach(function (cat) {
      var section = document.createElement("section");
      section.className = "ss-category";
      var head = document.createElement("div");
      head.className = "ss-category-head";
      var title = document.createElement("a");
      title.href = cat.href || "#";
      title.innerHTML =
        '<i class="fas ' +
        (cat.icon || "fa-search") +
        '"></i><span></span>';
      title.querySelector("span").textContent = cat.label || cat.id;
      var count = document.createElement("span");
      count.className = "ss-count";
      count.textContent = String(cat.count || (cat.items || []).length);
      head.appendChild(title);
      head.appendChild(count);
      section.appendChild(head);
      (cat.items || []).forEach(function (item) {
        var row = document.createElement("a");
        row.className = "ss-row";
        row.href = item.href || "#";
        var body = document.createElement("div");
        body.className = "ss-row-body";
        var name = document.createElement("div");
        name.className = "ss-row-title";
        var nameText = document.createElement("span");
        nameText.textContent = item.title || "";
        name.appendChild(nameText);
        if (item.badge) {
          var badge = document.createElement("span");
          badge.className = "ss-badge";
          badge.textContent = item.badge;
          name.appendChild(badge);
        }
        body.appendChild(name);
        if (item.subtitle) {
          var sub = document.createElement("div");
          sub.className = "ss-row-sub";
          sub.textContent = item.subtitle;
          body.appendChild(sub);
        }
        var chevron = document.createElement("i");
        chevron.className = "fas fa-chevron-right ss-chevron";
        row.appendChild(body);
        row.appendChild(chevron);
        section.appendChild(row);
      });
      resultsEl.appendChild(section);
    });
  }

  function search(q) {
    var query = (q || "").trim();
    if (clearBtn) clearBtn.hidden = !query;
    if (query.length < 2) {
      resultsEl.innerHTML = "";
      setStatus(query ? strings.min || "Type at least 2 characters." : "", !!query);
      return;
    }
    var mySeq = ++seq;
    setStatus(strings.loading || "Searching…", true);
    if (abortCtl) abortCtl.abort();
    abortCtl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var url = (cfg.api || "/api/smart_search") + "?q=" + encodeURIComponent(query);
    fetch(url, {
      headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
      signal: abortCtl ? abortCtl.signal : undefined,
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (pack) {
        if (mySeq !== seq) return;
        if (!pack.ok || !pack.body || !pack.body.success) {
          setStatus((pack.body && pack.body.error) || strings.error || "Search failed.", true);
          resultsEl.innerHTML = "";
          return;
        }
        render(pack.body);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (mySeq !== seq) return;
        setStatus(strings.error || "Search failed.", true);
      });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      search(input.value);
    }, 280);
  }

  if (input) {
    input.addEventListener("input", schedule);
    input.addEventListener("focus", function () {
      if (box) box.classList.add("is-focus");
      this.select();
    });
    input.addEventListener("blur", function () {
      if (box) box.classList.remove("is-focus");
    });
    input.addEventListener("keydown", function (ev) {
      var rows = currentRows();
      if (ev.key === "ArrowDown" && rows.length) {
        ev.preventDefault();
        setActive(activeIndex + 1);
      } else if (ev.key === "ArrowUp" && rows.length) {
        ev.preventDefault();
        setActive(activeIndex < 0 ? rows.length - 1 : activeIndex - 1);
      } else if (ev.key === "Enter" && activeIndex >= 0 && rows[activeIndex]) {
        ev.preventDefault();
        window.location.href = rows[activeIndex].href;
      } else if (ev.key === "Enter") {
        clearTimeout(timer);
        search(input.value);
      } else if (ev.key === "Escape") {
        input.value = "";
        search("");
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      input.value = "";
      input.focus();
      search("");
    });
  }

  if (chips) {
    chips.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-fill]");
      if (!btn) return;
      input.value = btn.getAttribute("data-fill") || "";
      input.focus();
      search(input.value);
    });
  }

  if (input && (cfg.initial || input.value)) {
    search(input.value);
  }
})();
