# FDC iOS app

Native SwiftUI client for https://www.fdcef.com. Flask stays on the server; the phone app talks JSON (`/api/mobile/...`), not a website in a WebView.

## Native tools

These match the website cards and permissions:

- SKU / style lookup (inventory, history, purchases, adjustments, images)
- Bag Style Viewer
- List Creator
- Master Image Viewer
- Smart Image Upload
- Sales Orders
- Customer Analytics
- Invoice Lookup
- Loris Automation
- Container Hub
- 打印中心
- Mobile barcode scanner

Other dashboard tools still show on the home screen as web-only.

## Mac + $99

1. Join Apple Developer ($99/year).
2. Open `ios/FDCWebShell/FDCWebShell.xcodeproj` in Xcode.
3. Signing & Capabilities → your Team.
4. Run on the iPhone, then Archive → TestFlight.

Restart the Flask NSSM service after pulling so `/api/mobile/login` is live.
