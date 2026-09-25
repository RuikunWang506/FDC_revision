(function () {
  function token() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute("content") || "") : "";
  }

  function needsCsrf(method) {
    var m = String(method || "GET").toUpperCase();
    return m !== "GET" && m !== "HEAD" && m !== "OPTIONS" && m !== "TRACE";
  }

  function addHidden(form) {
    if (!form || !form.method || String(form.method).toLowerCase() === "get") return;
    var t = token();
    var existing = form.querySelector('input[name="csrf_token"]');
    if (existing) {
      if (!existing.value && t) existing.value = t;
      return;
    }
    if (!t) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = t;
    form.appendChild(input);
  }

  document.addEventListener("submit", function (ev) {
    if (ev.target && ev.target.tagName === "FORM") addHidden(ev.target);
  }, true);

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("form"), addHidden);
  });

  // HTMLFormElement.submit() does not fire the submit event — patch so
  // programmatic posts (dynamically created forms) still carry the token.
  if (typeof HTMLFormElement !== "undefined" && !HTMLFormElement.prototype.submit.__fdcCsrf) {
    var nativeSubmit = HTMLFormElement.prototype.submit;
    function patchedSubmit() {
      addHidden(this);
      return nativeSubmit.apply(this, arguments);
    }
    patchedSubmit.__fdcCsrf = true;
    HTMLFormElement.prototype.submit = patchedSubmit;
  }

  function methodOf(input, init) {
    if (init && init.method) return String(init.method).toUpperCase();
    if (input && typeof input === "object" && input.method) return String(input.method).toUpperCase();
    return "GET";
  }

  // Install fetch wrapping first so a missing jQuery.ajaxSetup cannot skip CSRF headers.
  if (typeof window.fetch === "function" && !window.fetch.__fdcCsrf) {
    var originalFetch = window.fetch;
    var wrappedFetch = function (input, init) {
      init = init || {};
      if (needsCsrf(methodOf(input, init))) {
        var headers = new Headers(
          init.headers || (input && typeof input === "object" ? input.headers : undefined) || {}
        );
        if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token());
        init.headers = headers;
      }
      return originalFetch.call(this, input, init);
    };
    wrappedFetch.__fdcCsrf = true;
    window.fetch = wrappedFetch;
  }

  function hookJquery() {
    var jq = window.jQuery;
    if (!jq || jq.__fdcCsrf) return;
    // jquery.slim has no ajaxSetup; wait until the full build replaces it.
    if (typeof jq.ajaxSetup !== "function") return;
    jq.__fdcCsrf = true;
    jq.ajaxSetup({
      beforeSend: function (xhr, settings) {
        var method = (settings && settings.type ? settings.type : "GET").toUpperCase();
        if (needsCsrf(method)) {
          xhr.setRequestHeader("X-CSRF-Token", token());
        }
      }
    });
  }
  try { hookJquery(); } catch (e) { /* slim or incomplete jQuery */ }
  document.addEventListener("DOMContentLoaded", function () {
    try { hookJquery(); } catch (e) {}
  });
})();
