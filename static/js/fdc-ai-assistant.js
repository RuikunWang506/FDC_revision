(function () {
  'use strict';
  if (window.__FDC_AI_ASSISTANT__) return;
  window.__FDC_AI_ASSISTANT__ = true;

  var CHAT_URL = '/api/fdc-ai/chat';
  var HEALTH_URL = '/api/fdc-ai/health';
  var RESET_URL = '/api/fdc-ai/reset';
  var LIST_URL = '/api/fdc-ai/conversations';
  var CURRENT_URL = '/api/fdc-ai/conversations/current';
  var open = false;
  var historyOpen = false;
  var loading = false;
  var thinkTimer = null;
  var thinkStarted = 0;
  var lastHealth = null;
  var conversationId = null;
  var threadTitle = '';
  var messages = [];
  var suggestions = [];
  var MAX_WAIT_SEC = 120;

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderLite(text) {
    return esc(text)
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, label, href) {
        var extra = href.indexOf('/api/') === 0 ? ' download target="_blank" rel="noopener"' : '';
        return '<a href="' + href + '"' + extra + '>' + label + '</a>';
      })
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
  }

  function ago(stamp) {
    if (!stamp) return '';
    var t = Date.parse(String(stamp).replace(' ', 'T'));
    if (!t) return String(stamp);
    var sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (sec < 60) return 'just now';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
  }

  function setThreadTitle(title) {
    threadTitle = title || '';
    var el = document.getElementById('fdc-ai-thread-title');
    if (el) el.textContent = threadTitle;
  }

  function setHistoryOpen(next) {
    historyOpen = !!next;
    var hist = document.getElementById('fdc-ai-history');
    var msgs = document.getElementById('fdc-ai-messages');
    var form = document.getElementById('fdc-ai-form');
    if (hist) hist.classList.toggle('open', historyOpen);
    if (msgs) msgs.style.display = historyOpen ? 'none' : '';
    if (form) form.style.display = historyOpen ? 'none' : '';
    if (historyOpen) refreshHistory();
  }

  function renderMessages() {
    var box = document.getElementById('fdc-ai-messages');
    if (!box) return;
    if (!messages.length) {
      var chips = suggestions.slice(0, 4).map(function (s) {
        return '<button type="button" class="fdc-ai-chip" data-suggest="' + esc(s) + '">' + esc(s) + '</button>';
      }).join('');
      box.innerHTML =
        '<div class="fdc-ai-empty"><strong>FDC AI</strong><br>Ask who bought a style, a SKU location, stock, or how to use a page.<br>Press <kbd>Ctrl+E</kbd>.<br>' +
        chips + '</div>';
      box.querySelectorAll('[data-suggest]').forEach(function (btn) {
        btn.addEventListener('click', function () { send(btn.getAttribute('data-suggest') || ''); });
      });
      return;
    }
    box.innerHTML = messages.map(function (m) {
      var extra = m.thinking ? ' thinking' : '';
      return '<div class="fdc-ai-msg ' + m.role + extra + '">' + renderLite(m.content) + '</div>';
    }).join('');
    box.scrollTop = box.scrollHeight;
  }

  function renderHistoryList(rows, currentId) {
    var box = document.getElementById('fdc-ai-history-list');
    if (!box) return;
    if (!rows || !rows.length) {
      box.innerHTML = '<div class="fdc-ai-empty">No chats in the last 7 days.</div>';
      return;
    }
    box.innerHTML = rows.map(function (c) {
      var active = c.id === currentId ? ' active' : '';
      return (
        '<div class="fdc-ai-hist-item' + active + '" data-id="' + esc(c.id) + '">' +
          '<div class="fdc-ai-hist-main">' +
            '<strong>' + esc(c.title || 'Chat') + '</strong>' +
            '<span>' + esc(ago(c.updated_at)) + '</span>' +
            '<p>' + esc(c.preview || '') + '</p>' +
          '</div>' +
          '<button type="button" class="fdc-ai-icon-btn fdc-ai-hist-del" data-del="' + esc(c.id) + '" title="Delete">🗑</button>' +
        '</div>'
      );
    }).join('');
    box.querySelectorAll('.fdc-ai-hist-item').forEach(function (item) {
      item.addEventListener('click', function (e) {
        if (e.target.closest('[data-del]')) return;
        loadConversation(item.getAttribute('data-id') || '');
      });
    });
    box.querySelectorAll('[data-del]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        deleteConversation(btn.getAttribute('data-del') || '');
      });
    });
  }

  function refreshHistory() {
    fetch(LIST_URL).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      renderHistoryList((data && data.conversations) || [], (data && data.current_id) || conversationId);
    }).catch(function () {
      renderHistoryList([], conversationId);
    });
  }

  function loadConversation(id) {
    if (!id) return;
    fetch(LIST_URL + '/' + encodeURIComponent(id)).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (!data || !data.ok) return;
      conversationId = data.id || id;
      messages = (data.messages || []).map(function (m) {
        return { role: m.role, content: m.content };
      });
      setThreadTitle(data.title || '');
      setHistoryOpen(false);
      renderMessages();
    }).catch(function () {});
  }

  function deleteConversation(id) {
    if (!id) return;
    fetch(LIST_URL + '/' + encodeURIComponent(id), { method: 'DELETE' }).then(function (r) { return r.json(); }).then(function () {
      if (conversationId === id) newChat(false);
      refreshHistory();
    }).catch(function () {});
  }

  function newChat(keepHistoryOpen) {
    conversationId = null;
    messages = [];
    setThreadTitle('');
    fetch(RESET_URL, { method: 'POST' }).catch(function () {});
    renderMessages();
    if (!keepHistoryOpen) setHistoryOpen(false);
  }

  function restoreCurrent() {
    fetch(CURRENT_URL).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (!data || !data.id || !(data.messages || []).length) return;
      conversationId = data.id;
      messages = (data.messages || []).map(function (m) {
        return { role: m.role, content: m.content };
      });
      setThreadTitle(data.title || '');
      renderMessages();
    }).catch(function () {});
  }

  function setOpen(next) {
    open = !!next;
    var backdrop = document.getElementById('fdc-ai-backdrop');
    var panel = document.getElementById('fdc-ai-panel');
    var fab = document.getElementById('fdc-ai-fab');
    if (backdrop) backdrop.classList.toggle('open', open);
    if (panel) panel.classList.toggle('open', open);
    if (fab) fab.textContent = open ? 'Close AI' : 'FDC AI';
    if (open) {
      var input = document.getElementById('fdc-ai-input');
      if (input) input.focus();
      refreshHealth();
    }
  }

  function refreshHealth() {
    fetch(HEALTH_URL).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (data && data.suggestions) {
        suggestions = data.suggestions;
        if (!historyOpen) renderMessages();
      }
      var sub = document.getElementById('fdc-ai-subtitle');
      if (sub) {
        var h = data && data.health;
        lastHealth = h || lastHealth;
        if (h && h.ok) {
          var inf = h.inference === 'cpu' ? 'CPU' : (h.inference === 'gpu' ? 'GPU' : (h.inference === 'mixed' ? 'CPU/GPU' : (h.model_ready ? 'ready' : 'pull model')));
          var host = String(h.base_url || '').replace(/^https?:\/\//, '').replace(/:11434$/, '');
          sub.textContent = (h.model || 'Ollama') + ' · ' + inf + (host ? ' · ' + host : '');
        } else {
          sub.textContent = 'Local LLM offline';
        }
      }
    }).catch(function () {});
  }

  function stopThinking() {
    if (thinkTimer) {
      clearInterval(thinkTimer);
      thinkTimer = null;
    }
  }

  function thinkingCopy(sec) {
    var cpu = lastHealth && lastHealth.inference === 'cpu';
    var left = Math.max(0, MAX_WAIT_SEC - sec);
    var line1 = 'Thinking… ' + sec + 's';
    var line2;
    if (sec < 6) {
      line2 = '查单个款号/客户一般几秒。';
    } else if (sec < 25) {
      line2 = cpu
        ? '本地模型在 CPU 上跑，常见 20–90 秒。'
        : '远程 GPU 还在跑，常见几秒到 20 秒。';
    } else {
      line2 = '仍在等 Ollama，最多约 ' + MAX_WAIT_SEC + ' 秒超时（大约还剩 ' + left + 's）。';
    }
    return line1 + '\n' + line2;
  }

  function startThinking() {
    stopThinking();
    thinkStarted = Date.now();
    var idx = messages.length - 1;
    function tick() {
      if (!loading || !messages[idx] || !messages[idx].thinking) {
        stopThinking();
        return;
      }
      var sec = Math.floor((Date.now() - thinkStarted) / 1000);
      messages[idx].content = thinkingCopy(sec);
      renderMessages();
    }
    tick();
    thinkTimer = setInterval(tick, 1000);
  }

  function send(text) {
    var message = (text || '').trim();
    if (!message || loading) return;
    if (historyOpen) setHistoryOpen(false);
    loading = true;
    messages.push({ role: 'user', content: message });
    messages.push({ role: 'assistant', content: thinkingCopy(0), thinking: true });
    renderMessages();
    startThinking();
    var sendBtn = document.getElementById('fdc-ai-send');
    if (sendBtn) sendBtn.disabled = true;
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var kill = setTimeout(function () { if (ctrl) ctrl.abort(); }, (MAX_WAIT_SEC + 10) * 1000);
    fetch(CHAT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message, conversation_id: conversationId }),
      signal: ctrl ? ctrl.signal : undefined,
    }).then(function (r) { return r.json(); }).then(function (data) {
      messages.pop();
      messages.push({ role: 'assistant', content: (data && (data.reply || data.error)) || 'No response.' });
      if (data && data.conversation_id) conversationId = data.conversation_id;
      if (data && data.title) setThreadTitle(data.title);
    }).catch(function () {
      messages.pop();
      messages.push({ role: 'assistant', content: 'Could not reach FDC AI（约 ' + MAX_WAIT_SEC + ' 秒超时）。Is Ollama running?' });
    }).finally(function () {
      clearTimeout(kill);
      stopThinking();
      loading = false;
      if (sendBtn) sendBtn.disabled = false;
      renderMessages();
    });
  }

  function mount() {
    if (document.getElementById('fdc-ai-root')) return;
    var wrap = document.createElement('div');
    wrap.id = 'fdc-ai-root';
    wrap.className = 'fdc-ai-root';
    wrap.innerHTML =
      '<button type="button" id="fdc-ai-fab" class="fdc-ai-fab" title="FDC AI (Ctrl+E)">FDC AI</button>' +
      '<div id="fdc-ai-backdrop" class="fdc-ai-backdrop"></div>' +
      '<aside id="fdc-ai-panel" class="fdc-ai-panel">' +
      '<header class="fdc-ai-header"><div><h2>FDC AI Assistant</h2>' +
      '<p id="fdc-ai-thread-title" class="fdc-ai-thread-title"></p>' +
      '<p id="fdc-ai-subtitle">Local Ollama · read-only DB</p></div>' +
      '<div class="fdc-ai-header-actions">' +
      '<button type="button" class="fdc-ai-icon-btn" id="fdc-ai-history-btn" title="History / 记录">🕘</button>' +
      '<button type="button" class="fdc-ai-icon-btn" id="fdc-ai-new-btn" title="New chat / 新聊天">＋</button>' +
      '<button type="button" class="fdc-ai-icon-btn" id="fdc-ai-close" title="Close">✕</button></div></header>' +
      '<div id="fdc-ai-history" class="fdc-ai-history">' +
      '<div class="fdc-ai-history-head"><strong>History</strong><span>Saved 7 days</span></div>' +
      '<div id="fdc-ai-history-list" class="fdc-ai-history-list"></div></div>' +
      '<div id="fdc-ai-messages" class="fdc-ai-messages"></div>' +
      '<form class="fdc-ai-form" id="fdc-ai-form"><textarea id="fdc-ai-input" rows="2" placeholder="Ask about a SKU…"></textarea>' +
      '<button type="submit" id="fdc-ai-send">➤</button></form></aside>';
    document.body.appendChild(wrap);
    document.getElementById('fdc-ai-fab').addEventListener('click', function () { setOpen(!open); });
    document.getElementById('fdc-ai-backdrop').addEventListener('click', function () { setOpen(false); });
    document.getElementById('fdc-ai-close').addEventListener('click', function () { setOpen(false); });
    document.getElementById('fdc-ai-history-btn').addEventListener('click', function () { setHistoryOpen(!historyOpen); });
    document.getElementById('fdc-ai-new-btn').addEventListener('click', function () { newChat(false); });
    document.getElementById('fdc-ai-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var input = document.getElementById('fdc-ai-input');
      var value = input ? input.value : '';
      if (input) input.value = '';
      send(value);
    });
    var input = document.getElementById('fdc-ai-input');
    if (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          if (input.value.trim() && !loading) {
            var value = input.value;
            input.value = '';
            send(value);
          }
        }
      });
    }
    renderMessages();
    restoreCurrent();
    if (open) refreshHealth();
  }

  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || (e.target && e.target.isContentEditable)) return;
    if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === 'e') {
      e.preventDefault();
      setOpen(!open);
    }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
