(function () {
  var cfgEl = document.getElementById("bulletinConfig");
  if (!cfgEl) return;
  var cfg = {};
  try {
    cfg = JSON.parse(cfgEl.textContent || "{}");
  } catch (e) {
    cfg = {};
  }
  var i18n = cfg.i18n || {};
  var editor = document.getElementById("bulletinEditor");
  var feed = document.getElementById("bulletinFeed");
  var emptyEl = document.getElementById("bulletinEmpty");
  var pendingEl = document.getElementById("bulletinPending");
  var statusEl = document.getElementById("bulletinStatus");
  var fileInput = document.getElementById("bulletinFileInput");
  var recipientsEl = document.getElementById("bulletinRecipients");
  var pending = [];

  function formatTs(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso || "";
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function setStatus(msg, isError) {
    if (!statusEl) return;
    if (!msg) {
      statusEl.hidden = true;
      statusEl.textContent = "";
      return;
    }
    statusEl.hidden = false;
    statusEl.textContent = msg;
    statusEl.classList.toggle("is-error", !!isError);
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderPost(post) {
    var card = document.createElement("article");
    card.className = "bulletin-card";
    card.id = "post-" + post.id;
    var html = post.html || "";
    var files = (post.attachments || [])
      .filter(function (att) {
        return !(att.is_image && html.indexOf(att.url) !== -1);
      })
      .map(function (att) {
        if (att.is_image) {
          return (
            '<a class="bulletin-file" href="' +
            escapeHtml(att.url) +
            '" target="_blank" rel="noopener">' +
            '<img class="bulletin-thumb" src="' +
            escapeHtml(att.url) +
            '" alt="' +
            escapeHtml(att.name) +
            '">' +
            "</a>"
          );
        }
        return (
          '<a class="bulletin-file" href="' +
          escapeHtml(att.url) +
          '" download="' +
          escapeHtml(att.name) +
          '">📎 ' +
          escapeHtml(att.name) +
          " · " +
          formatSize(att.size) +
          "</a>"
        );
      })
      .join("");
    var share = "";
    if (post.visibility === "private" && post.recipients && post.recipients.length) {
      share =
        '<p class="bulletin-share">' +
        escapeHtml(i18n.shared_with || "Shared with") +
        ": " +
        escapeHtml(post.recipients.join(", ")) +
        "</p>";
    }
    var del = post.can_delete
      ? '<button type="button" class="bulletin-delete" data-del="' +
        escapeHtml(post.id) +
        '">' +
        escapeHtml(i18n.delete || "Delete") +
        "</button>"
      : "";
    card.innerHTML =
      '<div class="bulletin-card-head"><strong>' +
      escapeHtml(post.author) +
      "</strong><time datetime=\"" +
      escapeHtml(post.created_at) +
      '">' +
      escapeHtml(formatTs(post.created_at)) +
      "</time></div>" +
      '<div class="bulletin-card-body">' +
      (post.html || "") +
      "</div>" +
      share +
      (files ? '<div class="bulletin-files">' + files + "</div>" : "") +
      del;
    return card;
  }

  function renderFeed(posts) {
    if (!feed) return;
    feed.innerHTML = "";
    (posts || []).forEach(function (post) {
      feed.appendChild(renderPost(post));
    });
    if (emptyEl) emptyEl.hidden = !!(posts && posts.length);
  }

  function editorEmpty() {
    var text = (editor && editor.innerText ? editor.innerText : "").replace(/\s+/g, "");
    var hasImg = !!(editor && editor.querySelector("img"));
    return !text && !hasImg;
  }

  function renderPending() {
    if (!pendingEl) return;
    pendingEl.hidden = pending.length === 0;
    pendingEl.innerHTML = pending
      .map(function (item, idx) {
        return (
          '<span class="bulletin-chip">' +
          escapeHtml(item.file.name) +
          ' <button type="button" data-rm="' +
          idx +
          '">×</button></span>'
        );
      })
      .join("");
  }

  function addFiles(fileList) {
    Array.prototype.forEach.call(fileList || [], function (file) {
      pending.push({ file: file, inline: false });
    });
    renderPending();
  }

  function insertPastedFile(file) {
    var idx = pending.length;
    pending.push({ file: file, inline: true });
    renderPending();
    if (!editor) return;
    if (file.type && file.type.indexOf("image/") === 0) {
      var url = URL.createObjectURL(file);
      document.execCommand("insertHTML", false, '<img src="' + url + '" data-fdc-file="' + idx + '" alt="">');
    } else {
      document.execCommand("insertText", false, file.name);
    }
  }

  function collectHtml() {
    if (!editor) return "";
    var clone = editor.cloneNode(true);
    clone.querySelectorAll("img[data-fdc-file]").forEach(function (img) {
      var idx = img.getAttribute("data-fdc-file");
      img.setAttribute("src", "__FDCFILE_" + idx + "__");
      img.removeAttribute("data-fdc-file");
    });
    return clone.innerHTML;
  }

  function currentVisibility() {
    var el = document.querySelector('input[name="bulletinVisibility"]:checked');
    return el ? el.value : "public";
  }

  function syncRecipients() {
    if (!recipientsEl) return;
    recipientsEl.hidden = currentVisibility() !== "private";
  }

  document.querySelectorAll('input[name="bulletinVisibility"]').forEach(function (el) {
    el.addEventListener("change", syncRecipients);
  });
  syncRecipients();

  var toolbar = document.getElementById("bulletinToolbar");
  if (toolbar) {
    toolbar.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-cmd]");
      if (!btn) return;
      ev.preventDefault();
      editor && editor.focus();
      document.execCommand(btn.getAttribute("data-cmd"), false, null);
    });
    toolbar.addEventListener("change", function (ev) {
      var el = ev.target;
      if (!el || !el.getAttribute) return;
      var cmd = el.getAttribute("data-cmd");
      if (!cmd) return;
      editor && editor.focus();
      if (cmd === "fontSizePx") {
        document.execCommand("styleWithCSS", false, true);
        document.execCommand("fontSize", false, "7");
        (editor ? editor.querySelectorAll('font[size="7"], span[style*="xxx-large"]') : []).forEach(function (node) {
          var span = document.createElement("span");
          span.style.fontSize = el.value + "px";
          span.innerHTML = node.innerHTML;
          node.replaceWith(span);
        });
        return;
      }
      document.execCommand(cmd, false, el.value);
    });
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      addFiles(fileInput.files);
      fileInput.value = "";
    });
  }

  if (pendingEl) {
    pendingEl.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-rm]");
      if (!btn) return;
      pending.splice(Number(btn.getAttribute("data-rm")), 1);
      renderPending();
    });
  }

  if (editor) {
    editor.addEventListener("paste", function (ev) {
      var dt = ev.clipboardData;
      if (!dt) return;
      var files = [];
      if (dt.files && dt.files.length) {
        files = Array.prototype.slice.call(dt.files);
      } else if (dt.items) {
        Array.prototype.forEach.call(dt.items, function (item) {
          if (item.kind === "file") {
            var f = item.getAsFile();
            if (f) files.push(f);
          }
        });
      }
      if (files.length) {
        ev.preventDefault();
        files.forEach(insertPastedFile);
      }
    });
    ["dragenter", "dragover"].forEach(function (name) {
      editor.addEventListener(name, function (ev) {
        ev.preventDefault();
      });
    });
    editor.addEventListener("drop", function (ev) {
      ev.preventDefault();
      if (ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files.length) {
        Array.prototype.forEach.call(ev.dataTransfer.files, insertPastedFile);
      }
    });
  }

  var postBtn = document.getElementById("bulletinPostBtn");
  if (postBtn) {
    postBtn.addEventListener("click", function () {
      if (editorEmpty() && !pending.length) {
        setStatus(i18n.post_error || "Write a note or attach a file.", true);
        return;
      }
      var body = new FormData();
      body.append("html", collectHtml());
      body.append("visibility", currentVisibility());
      body.append("recipients", recipientsEl ? recipientsEl.value : "");
      pending.forEach(function (item) {
        body.append("files", item.file, item.file.name);
      });
      postBtn.disabled = true;
      fetch("/bulletin/posts", { method: "POST", body: body })
        .then(function (res) {
          return res.json().then(function (data) {
            return { ok: res.ok, data: data };
          });
        })
        .then(function (out) {
          if (!out.ok || !out.data || !out.data.ok) {
            setStatus((out.data && out.data.error) || i18n.post_error || "Could not post.", true);
            return;
          }
          if (editor) editor.innerHTML = "";
          pending = [];
          renderPending();
          setStatus(i18n.posted || "Posted.", false);
          var vis = currentVisibility();
          if (out.data.post && out.data.post.visibility === vis && feed) {
            feed.insertBefore(renderPost(out.data.post), feed.firstChild);
            if (emptyEl) emptyEl.hidden = true;
          }
        })
        .catch(function () {
          setStatus(i18n.post_error || "Could not post.", true);
        })
        .finally(function () {
          postBtn.disabled = false;
        });
    });
  }

  if (feed) {
    feed.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-del]");
      if (!btn) return;
      var id = btn.getAttribute("data-del");
      fetch("/bulletin/posts/" + encodeURIComponent(id), { method: "DELETE" })
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          if (!data || !data.ok) return;
          var card = document.getElementById("post-" + id);
          if (card) card.remove();
          if (emptyEl) emptyEl.hidden = !!feed.querySelector(".bulletin-card");
        });
    });
  }

  renderFeed(cfg.posts || []);
  if (location.hash) {
    var target = document.getElementById(location.hash.replace(/^#/, ""));
    if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
})();
