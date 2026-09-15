# FDC iOS app

Native SwiftUI client for https://www.fdcef.com. Flask stays on the server; the phone app talks JSON (`/api/mobile/...`), not a website in a WebView.

## What is native today

- Sign in / log out
- Home tool list (same permissions as the website)
- SKU / style lookup
- Sales order lookup

Other dashboard tools are listed as “Not on iOS yet”. They still live on the website.

## Mac + $99

1. Join Apple Developer ($99/year).
2. Open `ios/FDCWebShell/FDCWebShell.xcodeproj` in Xcode.
3. Signing & Capabilities → your Team.
4. Run on the iPhone, then Archive → TestFlight.

Restart the Flask NSSM service after pulling so `/api/mobile/login` is live.
