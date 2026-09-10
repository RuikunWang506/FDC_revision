(function () {
  function host() {
    var el = document.getElementById("fdcToastHost");
    if (el) return el;
    el = document.createElement("div");
    el.id = "fdcToastHost";
    el.className = "fdc-toast-host";
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
    return el;
  }

  window.fdcToast = function (message, type) {
    var text = String(message || "").trim();
    if (!text) return;
    var kind = (type || "info").toLowerCase();
    if (kind === "error") kind = "danger";
    var toast = document.createElement("div");
    toast.className = "fdc-toast fdc-toast-" + kind;
    toast.setAttribute("role", "status");
    toast.innerHTML =
      '<div class="fdc-toast-body"></div>' +
      '<button type="button" class="fdc-toast-close" aria-label="Close">&times;</button>';
    toast.querySelector(".fdc-toast-body").textContent = text;
    host().appendChild(toast);
    var closer = toast.querySelector(".fdc-toast-close");
    function remove() {
      toast.remove();
    }
    closer.addEventListener("click", remove);
    setTimeout(remove, kind === "danger" ? 7000 : 4200);
  };
})();
