(function () {
  function token() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute("content") || "") : "";
  }

  function addHidden(form) {
    if (!form || form.method.toLowerCase() === "get") return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = token();
    form.appendChild(input);
  }

  document.addEventListener("submit", function (ev) {
    if (ev.target && ev.target.tagName === "FORM") addHidden(ev.target);
  }, true);

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("form"), addHidden);
  });

  function hookJquery() {
    if (!window.jQuery || window.jQuery.__fdcCsrf) return;
    window.jQuery.__fdcCsrf = true;
    window.jQuery.ajaxSetup({
      beforeSend: function (xhr, settings) {
        var method = (settings && settings.type ? settings.type : "GET").toUpperCase();
        if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
          xhr.setRequestHeader("X-CSRF-Token", token());
        }
      }
    });
  }
  hookJquery();
  document.addEventListener("DOMContentLoaded", hookJquery);

  if (typeof window.fetch === "function") {
    var original = window.fetch;
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method || (typeof input === "object" && input.method) || "GET").toUpperCase();
      if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
        var headers = new Headers(init.headers || (typeof input === "object" ? input.headers : undefined) || {});
        if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token());
        init.headers = headers;
      }
      return original.call(this, input, init);
    };
  }
})();
