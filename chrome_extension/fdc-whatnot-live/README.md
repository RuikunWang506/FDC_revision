# FDC Whatnot Live (Chrome extension)

Reads bid / viewer / countdown / **pinned listing** from an open Whatnot live tab and POSTs JSON to the Flask overlay.

`https://www.fdcef.com/whatnot_live/ingest`

OBS does **not** get bids from Whatnot. The chain is:

Whatnot live tab → this extension → Flask → **one** OBS Browser Source (`/whatnot_live/overlay`)

That overlay page is the camera, microphone, and graphics. You do not add a second browser or a phone camera unless you opt into iPhone mode.

## Install

1. Chrome → `chrome://extensions`
2. Enable **Developer mode**
3. **Load unpacked**
4. Select the folder that contains `manifest.json` (`chrome_extension/fdc-whatnot-live`).

Or download the zip from the Whatnot Live hub, unzip, and Load unpacked on that unzipped folder (the one with `manifest.json` at the top, not a parent folder).

## Use

1. Point OBS Browser Source at `/whatnot_live/overlay` (1080×1920, not transparent). Enable **Control audio via OBS**. Uncheck **Shutdown source when not visible**. If the camera is blocked: right-click the source → **Interact** → click Allow.
2. Open your Whatnot seller live tab and keep it in the foreground.
3. Click the extension icon and confirm the ingest URL.
4. On the FDC control panel, load the SKU so color + 3× MSRP come from inventory.

Phone camera: open `/whatnot_live/camera`, allow camera + microphone, and pick a lens. OBS continues using the same `/whatnot_live/overlay` URL.

## Diagnostic log

The popup has an optional **Save Whatnot diagnostic log locally** switch. When enabled, it keeps a bounded local log of GraphQL/fetch/XHR requests and responses, including mutations used to edit products, minimum bids, and bid increments, plus Whatnot page snapshots and the payload sent to FDC. Sensitive fields such as cookies, authorization values, passwords, tokens, and CSRF values are redacted.

Use **Export log** in the popup to download a JSON file for later review. The diagnostic log stays in Chrome local storage and is not sent to FDC. Turn it off when the investigation is finished.

## JSON example

```json
{
  "title": "handbags #2",
  "starting_bid": 5,
  "available": 1,
  "sku": "CF101",
  "bid": 72,
  "bidder": "buyer123",
  "time_left": 8,
  "viewers": 232
}
```

Pinning a product in the seller dashboard fires `GetSellerLiveListing`. The extension reads that GraphQL response (title, quantity, starting price in cents) and updates FDC immediately. Whatnot's page markup still works as fallback. Optional server env: `WHATNOT_LIVE_INGEST_TOKEN`.
