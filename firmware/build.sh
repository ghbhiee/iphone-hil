#!/bin/zsh
# 用法: firmware/build.sh [ble|usb|esp32] [--upload /dev/cu.xxx]
#   ble   ESP32-S3：命令走板子 USB 口（Hardware CDC），BLE HID 连 iPhone
#   usb   ESP32-S3：native USB 口当 HID 插 iPhone，命令走 UART 口
#   esp32 经典 ESP32（无原生 USB，如 DevKitC/CH340 板）：命令走 UART0（CH340 口），BLE HID 连 iPhone
set -e
cd "$(dirname "$0")"
VARIANT="${1:-ble}"; shift || true
STUB="$(pwd)/tools/ctags-stub"
EXTRA=()
if [[ "$(uname -m)" == "arm64" ]] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
  EXTRA+=(--build-property "runtime.tools.ctags.path=$STUB")   # 没 Rosetta：用空壳 ctags
fi
if [[ "$VARIANT" == "ble" ]]; then
  FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=huge_app"
  EXTRA+=(--build-property "compiler.cpp.extra_flags=-DHID_BACKEND=1")
elif [[ "$VARIANT" == "esp32" ]]; then
  FQBN="esp32:esp32:esp32:PartitionScheme=huge_app"
  EXTRA+=(--build-property "compiler.cpp.extra_flags=-DHID_BACKEND=1")
else
  FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,PartitionScheme=huge_app"
  EXTRA+=(--build-property "compiler.cpp.extra_flags=-DHID_BACKEND=2")
fi
OUT="build/$VARIANT"
arduino-cli compile --fqbn "$FQBN" "${EXTRA[@]}" --output-dir "$OUT" hid_bridge
echo "固件: $OUT/hid_bridge.ino.bin  (merged: $OUT/hid_bridge.ino.merged.bin)"
if [[ "$1" == "--upload" ]]; then
  # 只刷 app 分区，保留 NVS（蓝牙绑定 + CFG 配置）。先停掉串口守护进程。
  curl -s --max-time 2 http://127.0.0.1:8766/quit >/dev/null 2>&1 || true; sleep 1   # 守护进程没跑时 curl 非零，别让 set -e 把刷写跳掉
  PORT="${2:-$(ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial* 2>/dev/null | head -1)}"
  CHIP=esp32; [[ "$VARIANT" != "esp32" ]] && CHIP=esp32s3
  esptool.py --chip $CHIP --port "$PORT" --baud 460800 --before default_reset --after hard_reset write_flash 0x10000 "$OUT/hid_bridge.ino.bin"
fi
