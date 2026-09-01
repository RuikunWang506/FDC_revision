(() => {
  let debugEnabled = false;
  const DEBUG_TEXT_LIMIT = 60000;

  function clip(value, max = DEBUG_TEXT_LIMIT) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    if (!text) return "";
    return text.length > max ? `${text.slice(0, max)}…[truncated]` : text;
  }

  function redact(value, depth = 0) {
    if (depth > 7 || value == null) return value;
    if (typeof value === "string") return clip(value, 20000);
    if (Array.isArray(value)) return value.slice(0, 80).map((item) => redact(item, depth + 1));
    if (typeof value !== "object") return value;
    const out = {};
    Object.entries(value).slice(0, 160).forEach(([key, item]) => {
      if (/password|authorization|cookie|csrf|secret|token|refresh|session/i.test(key)) {
        out[key] = "[redacted]";
      } else {
        out[key] = redact(item, depth + 1);
      }
    });
    return out;
  }

  function cleanUrl(raw) {
    try {
      const url = new URL(String(raw || ""), location.href);
      url.search = "";
      url.hash = "";
      return clip(url.toString(), 500);
    } catch (_e) {
      return clip(raw, 500);
    }
  }

  function operationName(blob) {
    const text = String(blob || "");
    const named = text.match(/["']operationName["']\s*:\s*["']([^"']+)/i)
      || text.match(/\b(?:query|mutation|subscription)\s+([A-Za-z0-9_]+)/i);
    return named ? named[1] : "";
  }

  function requestBody(raw) {
    const text = String(raw || "");
    if (!text) return "";
    try {
      return redact(JSON.parse(text));
    } catch (_e) {
      return clip(text);
    }
  }

  function requestUrl(args) {
    return cleanUrl(args && args[0] && args[0].url ? args[0].url : args && args[0]);
  }

  function logNonGraphqlResponse(res, meta) {
    if (!debugEnabled) return;
    const contentType = String(res.headers.get("content-type") || "");
    const entry = Object.assign({}, meta, {
      kind: "network",
      status: res.status,
      content_type: contentType,
    });
    if (!/json|text|graphql|javascript/i.test(contentType)) {
      debugEntry(entry);
      return;
    }
    res.clone().text().then((text) => {
      debugEntry(Object.assign({}, entry, { response: clip(text) }));
    }).catch(() => debugEntry(Object.assign({}, entry, { response: "[unreadable response]" })));
  }

  function debugEntry(entry) {
    if (!debugEnabled) return;
    try {
      window.postMessage({
        type: "fdc-whatnot-debug",
        entry: Object.assign({
          ts: new Date().toISOString(),
          page: cleanUrl(location.href),
        }, entry),
      }, "*");
    } catch (_e) {}
  }

  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || ev.data.type !== "fdc-whatnot-debug-config") return;
    debugEnabled = !!ev.data.enabled;
  });

  function asInt(v) {
    const n = Number(String(v).replace(/,/g, ""));
    return Number.isFinite(n) && n >= 0 && n < 1e7 ? Math.round(n) : null;
  }
  function asText(v, max) {
    const s = String(v || "").replace(/\s+/g, " ").trim();
    if (!s || s.length > (max || 80)) return "";
    if (/^(true|false|null|undefined)$/i.test(s)) return "";
    return s;
  }
  function moneyCents(node) {
    if (!node || typeof node !== "object") return null;
    const n = node.amountSafe != null ? Number(node.amountSafe) : Number(node.amount);
    if (!Number.isFinite(n) || n < 0) return null;
    return n / 100;
  }
  function salePrice(node) {
    const sales = node.salesFormatConfiguration || {};
    const flash = sales.flashSaleConfiguration || {};
    const bin = sales.buyItNowConfiguration || {};
    const auction = sales.auctionConfiguration || {};
    return moneyCents(flash.discountPrice)
      || moneyCents(flash.discount_price)
      || moneyCents(bin.price)
      || moneyCents(auction.price)
      || moneyCents(node.localizedPrice)
      || moneyCents(node.price);
  }
  function listingFrom(node, pinned) {
    if (!node || typeof node !== "object") return {};
    const out = {};
    if (pinned) out._pinned = true;
    const title = asText(node.title, 240);
    if (title) out.title = title;
    const cat = asText(node.description, 80);
    if (cat) out.category = cat;
    if (node.quantity != null) {
      const q = asInt(node.quantity);
      if (q != null) out.available = q;
    }
    const start = salePrice(node);
    if (start != null) out.starting_bid = start;
    const live = node.liveAuctionInfo || node.auctionState || {};
    const bid = moneyCents(node.currentBid)
      || moneyCents(node.localizedCurrentBid)
      || moneyCents(live.currentPrice)
      || moneyCents(live.localizedCurrentPrice);
    if (bid != null) out.bid = bid;
    else if (pinned) out.bid = 0;
    const user = node.currentBidUser || live.currentBidUser;
    if (user && user.username) out.bidder = String(user.username).replace(/^@/, "");
    else if (pinned) out.bidder = "";
    const tx = String(node.transactionType || "").toUpperCase();
    if (tx) out.transaction_type = tx;
    const status = String(node.status || node.publicStatus || "").toLowerCase();
    if (status === "sold") out.sold = true;
    return out;
  }

  function extract(obj, pinned) {
    const out = {};
    const seen = new Set();
    function walk(node, depth) {
      if (!node || depth > 8 || typeof node !== "object") return;
      if (seen.has(node)) return;
      seen.add(node);
      if (node.getListing) {
        Object.assign(out, listingFrom(node.getListing, true));
      }
      if (node.__typename === "ListingNode" && node.title && !out._pinned) {
        Object.assign(out, listingFrom(node, pinned));
      }
      if (Array.isArray(node)) {
        node.slice(0, 40).forEach((x) => walk(x, depth + 1));
        return;
      }
      Object.values(node).forEach((v) => {
        if (v && typeof v === "object") walk(v, depth + 1);
      });
    }
    walk(obj, 0);
    return out;
  }

  function blobOf(args) {
    const a0 = args[0];
    const url = String((a0 && a0.url) || a0 || "");
    let body = "";
    try {
      if (args[1] && typeof args[1].body === "string") body = args[1].body;
    } catch (_e) {}
    return url + " " + body;
  }

  function isGraphql(blob) {
    return /graphql/i.test(blob);
  }
  function isPin(blob) {
    return /GetSellerLiveListing|SellerLiveListing|PinLive|PinnedListing|LiveShopListing|FlashSale|FeaturedListing|BuyNowListing/i.test(blob);
  }

  function publish(raw, pinned) {
    try {
      const payload = extract(raw, pinned);
      if (!payload || !Object.keys(payload).length) return;
      if (!payload._pinned && !pinned && !payload.title) return;
      window.postMessage({ type: "fdc-whatnot-net", payload }, "*");
    } catch (_e) {}
  }

  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const blob = blobOf(args);
    const graphql = isGraphql(blob);
    if (!graphql && !debugEnabled) return origFetch.apply(this, args);
    const pinned = isPin(blob);
    const p = origFetch.apply(this, args);
    try {
      p.then((res) => {
        if (!graphql) {
          logNonGraphqlResponse(res, {
            transport: "fetch",
            method: String((args[1] && args[1].method) || "GET").toUpperCase(),
            operation: operationName(blob),
            url: requestUrl(args),
          });
          return;
        }
        res.clone().json().then((json) => {
          publish(json, pinned);
          debugEntry({
            kind: "graphql",
            transport: "fetch",
            operation: operationName(blob),
            url: requestUrl(args),
            pinned,
            request: requestBody(args[1] && args[1].body),
            response: redact(json),
          });
        }).catch(() => {
          debugEntry({
            kind: "graphql",
            transport: "fetch",
            operation: operationName(blob),
            url: requestUrl(args),
            pinned,
            request: requestBody(args[1] && args[1].body),
            response: "[non-JSON response]",
          });
        });
      }).catch(() => {});
    } catch (_e) {}
    return p;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this._fdcMethod = method;
    this._fdcUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const blob = String(this._fdcUrl || "") + " " + String(body || "");
    const graphql = isGraphql(blob);
    if (graphql || debugEnabled) {
      const pinned = graphql && isPin(blob);
      this.addEventListener("load", function () {
        try {
          const text = this.responseText || "";
          if (!graphql) {
            debugEntry({
              kind: "network",
              transport: "xhr",
              method: String(this._fdcMethod || "GET").toUpperCase(),
              url: cleanUrl(this._fdcUrl),
              status: this.status,
              response: clip(text),
            });
            return;
          }
          if (!text || (text[0] !== "{" && text[0] !== "[")) return;
          const json = JSON.parse(text);
          publish(json, pinned);
          debugEntry({
            kind: "graphql",
            transport: "xhr",
            operation: operationName(blob),
            url: cleanUrl(this._fdcUrl),
            pinned,
            request: requestBody(this._fdcBody),
            response: redact(json),
          });
        } catch (_e) {}
      });
    }
    this._fdcBody = body;
    return origSend.apply(this, arguments);
  };
})();
