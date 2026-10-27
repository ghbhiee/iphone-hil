#!/bin/zsh
# 生成 ~/Applications/HIL Capture.app：一个只负责跑 hil.capture_server 的壳 App。
# 用 App 壳的原因：相机权限（含 iPhone 屏幕采集）按 App 归属，Claude Code / 某些终端的宿主进程拿不到。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$HOME/Applications/HIL Capture.app}"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>HIL Capture</string>
  <key>CFBundleDisplayName</key><string>HIL Capture</string>
  <key>CFBundleIdentifier</key><string>com.iphone-hil.capture</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>hil-capture</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSCameraUsageDescription</key><string>HIL Capture 通过 USB 采集 iPhone 屏幕画面（和 QuickTime 录屏同一通道），需要相机权限。</string>
  <key>NSHumanReadableCopyright</key><string>local tool</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/hil-capture" <<SH
#!/bin/zsh
cd "$ROOT"
mkdir -p captures
exec "$ROOT/.venv/bin/python" -m hil.capture_server "\$@" >> "$ROOT/captures/server.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/hil-capture"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "已生成: $APP"
echo "启动:   open -g \"$APP\"      （第一次会弹相机授权框）"
echo "日志:   $ROOT/captures/server.log"
