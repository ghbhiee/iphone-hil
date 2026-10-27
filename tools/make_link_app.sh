#!/bin/zsh
# 生成 ~/Applications/HIL Link.app：跑 hil.link_server --transport ble 的壳。蓝牙权限按 App 发，Claude Code 宿主拿不到。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$HOME/Applications/HIL Link.app}"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>HIL Link</string>
  <key>CFBundleDisplayName</key><string>HIL Link</string>
  <key>CFBundleIdentifier</key><string>com.iphone-hil.link</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>hil-link</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSBluetoothAlwaysUsageDescription</key><string>HIL Link 通过蓝牙把指令发给 ESP32 HID 桥（HIL-Bridge）。</string>
</dict></plist>
PLIST
# 原生启动器（见 launcher.c）：把 python 真身、venv site-packages、项目目录、日志路径编进去
PY_REAL="$(readlink -f "$ROOT/.venv/bin/python")"
PYHOME="$(cd "$(dirname "$PY_REAL")/.." && pwd)"
LIBPY="$(ls "$PYHOME"/lib/libpython3*.dylib | head -1)"
SITEPKG="$(ls -d "$ROOT"/.venv/lib/python3.*/site-packages | head -1)"
clang -O2 -Wall -o "$APP/Contents/MacOS/hil-link" "$ROOT/tools/launcher.c" \
  -DLIBPYTHON="\"$LIBPY\"" -DPYHOME="\"$PYHOME\"" -DSITEPKG="\"$SITEPKG:$ROOT\"" -DROOT="\"$ROOT\"" -DLOGFILE="\"$ROOT/captures/link.log\""
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "已生成: $APP"
