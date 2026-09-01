(() => {
  const POLL_MS = 900;
  let lastSent = "";
  let netBits = {};
  let held = {};
  let viewerZeroes = 0;
  let debugEnabled = false;
  let debugTimer = null;
  let lastDomSignature = "";

  function clipDebug(value, max) {
    const text = String(value || "");
    return text.length > max ? `${text.slice(0, max)}…[truncated]` : text;
  }

  function sendDebug(entry) {
    if (!debugEnabled) return;
    chrome.runtime.sendMessage({ type: "whatnot-debug-entry", entry }, () => {
      void chrome.runtime.lastError;
    });
  }

  function captureDom(reason) {
    if (!debugEnabled || !document.body) return;
    const text = document.body.innerText || "";
    const html = document.body.innerHTML || "";
    const signature = `${location.href}|${text.length}|${text.slice(0, 250)}|${text.slice(-250)}`;
    if (reason === "interval" && signature === lastDomSignature) return;
    lastDomSignature = signature;
    sendDebug({
      kind: "dom_snapshot",
      reason,
      url: location.href.split("#")[0],
      page_title: document.title,
      text: clipDebug(text, 120000),
      html: clipDebug(html, 180000),
    });
  }

  function setDebugEnabled(enabled) {
    debugEnabled = !!enabled;
    window.postMessage({ type: "fdc-whatnot-debug-config", enabled: debugEnabled }, "*");
    if (debugTimer) {
      clearInterval(debugTimer);
      debugTimer = null;
    }
    if (debugEnabled) {
      captureDom("enabled");
      debugTimer = setInterval(() => captureDom("interval"), 4000);
    }
  }

  chrome.runtime.sendMessage({ type: "whatnot-debug-status" }, (status) => {
    if (chrome.runtime.lastError) return;
    setDebugEnabled(!!(status && status.enabled));
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === "whatnot-debug-config") {
      setDebugEnabled(!!message.enabled);
    }
  });

  function eventTargetInfo(target) {
    const el = target && target.closest
      ? target.closest("button,[role='button'],a,input,select,textarea")
      : null;
    if (!el) return null;
    const type = String(el.getAttribute("type") || el.tagName || "").toLowerCase();
    return {
      tag: String(el.tagName || "").toLowerCase(),
      type,
      id: clipDebug(el.id || "", 120),
      name: clipDebug(el.getAttribute("name") || "", 120),
      label: clipDebug(el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || "", 180),
      value: /password|token|secret/i.test(type + " " + (el.getAttribute("name") || ""))
        ? "[redacted]"
        : /number|select|checkbox|radio/i.test(type)
          ? clipDebug(el.value || el.checked || "", 120)
          : "",
    };
  }

  document.addEventListener("click", (ev) => {
    if (!debugEnabled) return;
    const target = eventTargetInfo(ev.target);
    if (target) sendDebug({ kind: "user_event", event: "click", target });
  }, true);

  document.addEventListener("change", (ev) => {
    if (!debugEnabled) return;
    const target = eventTargetInfo(ev.target);
    if (target) sendDebug({ kind: "user_event", event: "change", target });
  }, true);

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    if (ev.data && ev.data.type === "fdc-whatnot-debug" && ev.data.entry) {
      sendDebug(ev.data.entry);
      return;
    }
    if (!ev.data || ev.data.type !== "fdc-whatnot-net" || !ev.data.payload) return;
    const incoming = ev.data.payload;
    if (incoming._pinned || incoming.title) {
      netBits = Object.assign({}, netBits, incoming, { _pinned: true });
      push();
      return;
    }
    ["bid", "bidder", "bid_count"].forEach((k) => {
      if (incoming[k] != null && incoming[k] !== "") netBits[k] = incoming[k];
    });
  });

  function textOf(el) {
    return (el && el.innerText ? el.innerText : "").replace(/\s+/g, " ").trim();
  }

  function moneyList(text) {
    return Array.from(String(text || "").matchAll(/\$\s*([\d,]+(?:\.\d+)?)/g)).map((m) => Number(m[1].replace(/,/g, "")));
  }

  function cleanTitle(raw) {
    return String(raw || "")
      .replace(/product actions/ig, "")
      .replace(/multiple options/ig, "")
      .replace(/flash sale/ig, "")
      .replace(/starting price[:\s$0-9,.]*/ig, "")
      .replace(/\$[\d,.]+(\s*\|\s*\$[\d,.]+)?/g, "")
      .replace(/\d+\s*Available/ig, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function productCards() {
    const cards = [];
    Array.from(document.querySelectorAll("button, [role='button']")).forEach((btn) => {
      if (!/product actions/i.test(textOf(btn))) return;
      let root = btn;
      for (let i = 0; i < 12 && root.parentElement; i++) {
        root = root.parentElement;
        const t = textOf(root);
        if (/product actions/i.test(t) && /\$\s*\d|starting price/i.test(t) && t.length < 2200) break;
      }
      const t = textOf(root);
      const avail = t.match(/(\d+)\s*Available/i);
      const start = t.match(/Starting price:\s*\$?\s*([\d,]+(?:\.\d+)?)/i);
      const prices = moneyList(t);
      const sale = start ? Number(start[1].replace(/,/g, "")) : (prices.length ? prices[prices.length - 1] : null);
      const title = cleanTitle(t.split(/product actions/i)[0] || t);
      if (!title || title.length < 3) return;
      const catM = t.match(/\b(handbags?|handbag|tote|wallet|backpack)\b/i);
      cards.push({
        title,
        available: avail ? Number(avail[1]) : null,
        category: catM ? catM[1].toLowerCase() : "",
        starting_bid: sale,
      });
    });
    return cards;
  }

  function pinnedFromPreview() {
    const nodes = Array.from(document.querySelectorAll("div, section, article"));
    let best = null;
    let bestScore = 0;
    nodes.forEach((el) => {
      const t = textOf(el);
      if (t.length < 24 || t.length > 900) return;
      const flash = /flash sale/i.test(t);
      const avail = t.match(/(\d+)\s*Available/i);
      const prices = moneyList(t);
      if (!prices.length) return;
      if (!flash && !avail) return;
      if (/product actions|search products|buy now|auction|giveaways/i.test(t) && !flash) return;
      const score = (flash ? 8 : 0) + (avail ? 4 : 0) + (prices.length >= 2 ? 2 : 0) + (t.length < 500 ? 1 : 0);
      if (score <= bestScore) return;
      bestScore = score;
      const title = cleanTitle(t.split(/\b(?:Black|Brown|White|Beige|Pink|Blue|Red|Green|Navy)\b|\d+\s*Available|Flash Sale/i)[0] || t);
      const catM = t.match(/\b(handbags?|handbag|tote|wallet)\b/i);
      best = {
        title: title || "",
        available: avail ? Number(avail[1]) : null,
        category: catM ? catM[1].toLowerCase() : "",
        starting_bid: prices[prices.length - 1],
        _pinned: true,
      };
    });
    return best && best.title ? best : null;
  }

  function pinnedFromDom() {
    const preview = pinnedFromPreview();
    if (preview) return preview;
    const cards = productCards();
    if (!cards.length) return null;
    const hay = textOf(document.body).slice(0, 25000);
    const video = document.querySelector("video");
    if (video) {
      let scope = video.parentElement;
      for (let i = 0; i < 10 && scope; i++) {
        const t = textOf(scope);
        const hit = cards.find((c) => c.title && t.includes(c.title.slice(0, 28)));
        if (hit) return Object.assign({ _pinned: true }, hit);
        scope = scope.parentElement;
      }
    }
    const hit = cards.find((c) => c.title && hay.includes(c.title.slice(0, 28))) || cards[0];
    return hit ? Object.assign({ _pinned: true }, hit) : null;
  }

  function findCurrentBid(blob) {
    if (netBits.bid != null && Number(netBits.bid) > 0) return Number(netBits.bid);
    const m = blob.match(/(?:current\s*bid|highest\s*bid)\s*[:$]?\s*\$?\s*([\d,]+(?:\.\d+)?)/i);
    if (!m) return 0;
    const n = Number(m[1].replace(/,/g, ""));
    const products = blob.match(/(\d+)\s+Products/i);
    if (products && n === Number(products[1])) return 0;
    return n > 0 ? n : 0;
  }

  function findTimeLeft(blob) {
    if (/sales match|minutes completed/i.test(blob) && !/current bid/i.test(blob)) return null;
    const near = blob.match(/(?:time\s*left|ends?\s*in|sudden death)[^\d]{0,16}(\d{1,3})\s*(?:s|sec)/i);
    return near ? Number(near[1]) : null;
  }

  function findViewers(blob) {
    const watching = blob.match(/([\d,]+)\s*(?:watching|viewers)/i);
    if (watching) return Number(watching[1].replace(/,/g, ""));
    const offLive = blob.match(/\b(?:OFF|LIVE)\s+(\d{1,5})\b/);
    if (offLive) return Number(offLive[1]);
    const labeled = Array.from(document.querySelectorAll("[aria-label], [title]"));
    for (const el of labeled) {
      const a = `${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""}`;
      const m = a.match(/([\d,]+)\s*(?:watching|viewers)/i);
      if (m) return Number(m[1].replace(/,/g, ""));
    }
    return null;
  }

  function findSales(blob) {
    const g = blob.match(/gross\s*sales[^\d$]{0,12}\$?\s*([\d,]+(?:\.\d+)?)/i);
    if (g) return Number(g[1].replace(/,/g, ""));
    const m = blob.match(/\bSales\s*\$\s*([\d,]+(?:\.\d+)?)/i);
    return m ? Number(m[1].replace(/,/g, "")) : null;
  }

  function findOrders(blob) {
    const e = blob.match(/Estimated Orders\s*([\d,]+)/i);
    if (e) return Number(e[1].replace(/,/g, ""));
    const m = blob.match(/\bOrders\s+(?!\/hr)([\d,]+)/i);
    return m ? Number(m[1].replace(/,/g, "")) : null;
  }

  function collectRaw() {
    const page = textOf(document.body).slice(0, 25000);
    const pin = (netBits.title && (netBits._pinned || netBits.starting_bid != null))
      ? netBits
      : pinnedFromDom();
    const payload = {};
    if (pin && pin.title) payload.title = pin.title;
    if (pin && pin.category) payload.category = pin.category;
    if (pin && pin.starting_bid != null) payload.starting_bid = pin.starting_bid;
    if (pin && pin.available != null) payload.available = pin.available;

    payload.bid = findCurrentBid(page);
    payload.bidder = "";
    payload.time_left = findTimeLeft(page);
    const viewers = findViewers(page);
    if (viewers != null) payload.viewers = viewers;
    const gross = findSales(page);
    if (gross != null) payload.gross_sales = gross;
    const orders = findOrders(page);
    if (orders != null) payload.estimated_orders = orders;

    payload.show_live = Number(payload.viewers || 0) > 0 || Number(payload.bid || 0) > 0;
    payload.live = payload.show_live;
    const liveMatch = String(location.href || "").match(/whatnot\.com\/(?:dashboard\/)?live\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
    if (liveMatch) {
      payload.live_id = liveMatch[1];
      payload.dashboard_url = "https://www.whatnot.com/dashboard/live/" + liveMatch[1].toLowerCase();
    }
    return payload;
  }

  function stabilize(next) {
    const titleChanged = next.title && held.title && next.title !== held.title;
    if (titleChanged || (next.title && !held.title)) {
      held = Object.assign({}, next);
      if (!next.bid) {
        held.bid = 0;
        held.bidder = "";
      }
      held.time_left = next.time_left == null ? null : next.time_left;
      viewerZeroes = 0;
      return held;
    }
    ["title", "category", "starting_bid", "available", "bid_increment", "gross_sales", "estimated_orders", "live_id", "dashboard_url"].forEach((k) => {
      if (next[k] != null && next[k] !== "") held[k] = next[k];
    });
    held.bid = next.bid == null ? 0 : next.bid;
    held.bidder = next.bidder || "";
    held.time_left = next.time_left == null ? null : next.time_left;
    if (next.viewers != null) {
      if (next.viewers > 0) {
        held.viewers = next.viewers;
        viewerZeroes = 0;
      } else {
        viewerZeroes += 1;
        if (viewerZeroes >= 3) held.viewers = 0;
      }
    }
    held.show_live = !!next.show_live;
    held.live = held.show_live;
    return held;
  }

  function push() {
    const payload = stabilize(collectRaw());
    if (!payload.title && !payload.live_id) return;
    const key = JSON.stringify({
      title: payload.title || "",
      starting_bid: payload.starting_bid || 0,
      available: payload.available || 0,
      bid: payload.bid || 0,
      viewers: payload.viewers || 0,
      time_left: payload.time_left == null ? "" : payload.time_left,
      live_id: payload.live_id || "",
    });
    if (key === lastSent) return;
    lastSent = key;
    chrome.runtime.sendMessage({ type: "whatnot-ingest", payload }, () => {
      void chrome.runtime.lastError;
    });
  }

  setInterval(push, POLL_MS);
  push();
})();
