(function () {
  if (window.__FDC_PRESENCE__) return;
  window.__FDC_PRESENCE__ = true;

  var INTERACT_IDLE_MS = 10 * 60 * 1000;
  var lastInput = Date.now();

  function bumpActivity() {
    lastInput = Date.now();
  }
  ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'click'].forEach(function (ev) {
    document.addEventListener(ev, bumpActivity, { passive: true });
  });

  function isActiveTab() {
    if (document.visibilityState !== 'visible') return false;
    return (Date.now() - lastInput) < INTERACT_IDLE_MS;
  }

  function fillMenu(users) {
    var menu = document.getElementById('fdcOnlineMenu');
    var countEl = document.getElementById('fdcOnlineCount');
    if (countEl) countEl.textContent = String((users || []).length);
    if (!menu) return;
    if (!users || !users.length) {
      menu.innerHTML = '<div class="dropdown-item-text text-muted small px-3 py-2">暂无在线用户</div>' +
        '<a class="dropdown-item small" href="/log/online">查看在线时长 →</a>';
      return;
    }
    menu.innerHTML = users.map(function (u) {
      return '<div class="live-mini">' +
        '<strong>' + String(u.username || '') + '</strong>' +
        '<div class="meta">自 ' + (u.since || '') + ' · 已在线 ' + (u.elapsed_display || '') + '</div>' +
        '</div>';
    }).join('') + '<a class="dropdown-item menu-foot" href="/log/online">查看在线时长 →</a>';
  }

  function heartbeat() {
    fetch('/api/presence/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: isActiveTab() })
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && typeof data.online_count === 'number') {
          var countEl = document.getElementById('fdcOnlineCount');
          if (countEl) countEl.textContent = String(data.online_count);
        }
      })
      .catch(function () {});
  }

  function refreshList() {
    fetch('/api/presence/online')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data && data.ok) fillMenu(data.users || []); })
      .catch(function () {});
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!document.getElementById('fdcOnlineNavItem')) return;
    heartbeat();
    setInterval(heartbeat, 30000);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') {
        bumpActivity();
        heartbeat();
      }
    });
    var toggle = document.getElementById('fdcOnlineDropdown');
    if (toggle) toggle.addEventListener('click', refreshList);
  });
})();
