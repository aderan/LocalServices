#!/bin/bash
# Build "MyBrain Services.app" (menu bar + main window, LSUIElement) into menubar/build/.
set -euo pipefail
cd "$(dirname "$0")"
APP="build/MyBrain Services.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/MyBrainServices" main.swift MainWindow.swift
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.mybrain.localservices.menubar</string>
  <key>CFBundleName</key><string>MyBrain Services</string>
  <key>CFBundleExecutable</key><string>MyBrainServices</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
swiftc -O -o build/fsevents-check fsevents_check.swift
echo "built $APP"
