# ESP32 固件：编译、刷写、与 iPhone 配对

固件源码：`hid_bridge/hid_bridge.ino`（Arduino，依赖 esp32 core 3.x + NimBLE-Arduino 2.x）。协议见 [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md)。
经真机验证的板子是**经典 ESP32**（ESP32-D0WD-V3，4 MB flash，CH340 串口桥，十几块钱的 DevKitC 类开发板）。
ESP32-S3 也支持（多一条 USB HID 备用后端），但没有真机验证过。

## 接法

```
Mac ──BLE 指令服务（AUTH 口令）──▶ ESP32 ──BLE HID 鼠标+键盘──▶ iPhone      ESP32 只要充电头供电
Mac ◀──数据线（屏幕采集）── iPhone
```

- iPhone 只有一个口，被 Mac 采集画面占了，所以 HID 只能走蓝牙。
- 串口（USB 数据线接 Mac）只在刷固件、看口令、改配置时需要；平时板子插充电头即可。
- **Mac 系统设置的蓝牙里不要去配对 HIL-Bridge**：macOS 一看到带 HID 外观的设备会自动配对并把它当成自己的鼠标键盘。
  Mac 端守护进程（`HIL Link.app`）连的是板子的自定义指令服务，不需要配对。

## 方法 A：刷现成的二进制（经典 ESP32，不装 Arduino）

`prebuilt/esp32/` 里是经真机验证的固件（版本见 `hid_bridge.ino` 的 `FW_VERSION`）。需要 `esptool.py`（`pip install esptool`）。

1. 板子用数据线接 Mac，看串口名：`ls /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.usbmodem*`
   （CH340 板子在 macOS 14+ 免驱；有的板子要先按住 BOOT 再按 RESET 进下载模式。）
2. 第一次刷：三段一起写（bootloader + 分区表 + app）：

```bash
esptool.py --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write_flash \
  0x1000 firmware/prebuilt/esp32/hid_bridge.ino.bootloader.bin \
  0x8000 firmware/prebuilt/esp32/hid_bridge.ino.partitions.bin \
  0xe000 firmware/prebuilt/esp32/boot_app0.bin \
  0x10000 firmware/prebuilt/esp32/hid_bridge.ino.bin
```

   921600 波特率在 CH340 板上可能报 "Unable to verify flash chip connection"，用 460800。

3. **以后升级固件只刷 app 分区**，别再整盘刷：

```bash
curl -s http://127.0.0.1:8766/quit      # 先让链路守护进程放开串口（它开着串口时刷不进去）
esptool.py --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write_flash 0x10000 firmware/prebuilt/esp32/hid_bridge.ino.bin
```

   原因：和 iPhone 的蓝牙绑定密钥、`CFG` 配置、BLE 口令都存在 NVS 分区里，整盘刷会清掉，iPhone 就一直连不上，
   只能到 iPhone 蓝牙里「忘记此设备」重新配对。

   刷到 0.3.0 以后，板子空闲 2 分钟会**主动放开 iPhone**（iPhone 设置→蓝牙里 HIL-Bridge 显示「未连接」是正常的），
   再用时要在 iPhone 设置→蓝牙里点一下 HIL-Bridge，见下面「空闲时放开 iPhone」。

## 方法 B：arduino-cli 自己编（改了源码）

```bash
arduino-cli core install esp32:esp32            # 3.x
arduino-cli lib install "NimBLE-Arduino"        # 2.x
firmware/build.sh esp32                          # 编译 → firmware/build/esp32/
firmware/build.sh esp32 --upload /dev/cu.usbserial-XXXX   # 编译并只刷 app 分区（保留 NVS）
```

`build.sh` 的其它目标：`ble`（ESP32-S3，命令走板子 USB 口的 Hardware CDC）、`usb`（ESP32-S3，native USB 口当有线 HID 插 iPhone，命令走 UART 口；只在不用 USB 采集画面、例如改用 AirPlay 时才有意义）。

Apple Silicon 上 arduino-cli 自带的 `ctags` 是 x86_64 的；没装 Rosetta 时脚本自动用 `tools/ctags-stub` 顶替（固件源码里函数都先定义后使用，不依赖自动原型）。装了 Rosetta（`softwareupdate --install-rosetta --agree-to-license`）就不需要。

## 方法 C：Arduino IDE 2.x

开发板管理器装 **esp32 by Espressif 3.x**，库管理器装 **NimBLE-Arduino 2.x**。
经典 ESP32 选 **ESP32 Dev Module**，Partition Scheme 选 **Huge APP (3MB No OTA)**；
ESP32-S3 选 **ESP32S3 Dev Module**，USB Mode = Hardware CDC and JTAG，USB CDC On Boot = Enabled，同样 Huge APP。
打开 `hid_bridge/hid_bridge.ino` 上传。

## 第一次通电：串口自检、拿口令

```bash
screen /dev/cu.usbserial-XXXX 115200      # 退出：Ctrl-A 然后 K
```
输入 `PING` 回车 → `OK PONG`；`ST` → `OK conn=0 backend=ble name=HIL-Bridge ... token=xxxxxxxxxxxx`。
**`token=` 后面那串就是 BLE 指令通道的口令**，填进项目根目录 `hil.json` 的 `ble_token`（第一次通电时固件随机生成并存 NVS）。
也可以在项目里：`python -m hil --link direct send ST`（直接开串口；注意 CH340 板子每次开串口都会复位一次）。

## iPhone 配对与设置（一次性）

1. 设置 → 辅助功能 → 触控 → **辅助触控 → 打开**。iPhone 只有开了它才会显示鼠标指针、接受指点设备。
2. 同一页 → **设备 → 蓝牙设备** → 搜到 **HIL-Bridge** → 配对（Just Works，无 PIN）。
   固件串口会打 `# connected`，`ST` 的 `conn=1`。第一个订阅鼠标报告的设备地址会被固件记住（`hidpeer`），以后只给它发 HID。
   **固件 ≥0.3.0 配对时板子不能睡**：串口 `CFG appearance 0x3C2` 重启（配对模式下板子不睡、一直广播），
   或者 `WAKE` 后 120 s 内完成配对。配完 `CFG appearance 0` 重启，板子就回到「空闲放开 iPhone」的正常行为。
3. 设置 → 辅助功能 → **指针控制**：
   - **颜色**：选 App 里少见的颜色（红/橙），之后用 `python -m hil pointer-sample 帧.png x y` 从截图采样生成识别阈值；
   - **指针大小**：调大一点，识别更稳；
   - **自动隐藏：关**；**指针动画：关**（开着时指针会吸附到按钮上变形，识别会丢）；
   - 跟踪速度放中间，之后 `python -m hil calib` 标定。
4. 辅助触控 → **闲置不透明度**调到最低（悬浮球会挡画面）。
5. 设置 → 通用 → 键盘 → **硬件键盘**：布局 **美国 (ABC)**；确保有英文键盘可切换（`type` 只发 ASCII）。
6. 设置 → 显示与亮度 → **自动锁定：永不**（锁屏时 Mac 这边的屏幕采集设备会消失，而且 Face ID 没法远程解锁）。
7. iPhone 用数据线接 Mac，解锁、点「信任此电脑」。Mac 的 QuickTime「新建影片录制」能选到 iPhone，就说明采集通道通了。

### 换 iPhone / 多台设备
- 给**另一台** iPhone 配对：固件默认广播通用外观，有的 iOS 版本在蓝牙列表里不显示它；串口 `CFG appearance 0x3C2` 重启后再配，配完 `CFG appearance 0` 改回去（通用外观是为了防止 Mac 自动配对接管）。
- 换 HID 目标：`CFG hidpeer clear`，再让新 iPhone 配对一次。
- 删掉 Mac 误配对留下的绑定：`CFG unbond others`（保留 iPhone），`CFG unbond all`（全删）。

## 空闲时放开 iPhone（固件 ≥0.3.0）

iOS 只要连着一个蓝牙键盘，就把**所有 App 的屏幕键盘**藏起来。板子以前常年挂在 iPhone 上，用户在手机上就再也弹不出键盘。
现在（0.3.1 定稿）：

- `hididle` 秒（默认 120）没有需要 HID 的命令（MV/CK/KP/TX/CC …）→ 断开 iPhone（**不清绑定**）→ **停止广播**，直到被叫醒。
  `SLEEP` 立刻进入这个状态；`hil stop` 退出时会发。
- 叫醒：`WAKE` 或任何需要 HID 的命令（来自串口，或已经连着的 Mac 指令通道）→ 恢复广播 → iPhone 按绑定自动连回来（实测 1~3 s）。
  没连回来时，`hil up` 和各条命令会直接提示：到 iPhone 设置→蓝牙 点一下 HIL-Bridge（绑定还在，点完立刻连上）。
- 空闲时板子对外没有广播，所以**守护进程退出后再新起一个会连不上**：给板子断电重启（或按 EN 键）就行，上电时正常广播，
  iPhone 会自动连上，再过 2 分钟没命令又放开。守护进程本身空闲 10 分钟才退，日常用一般碰不到。
- `ST` 里看：`hid=on`（iPhone 连着）/ `waking`（在广播）/ `idle`（已放开），`idle=` 距上次活动秒数，`conns=` 连接数，`adv=pub|off`。
- 不想要这个行为：`CFG hididle 0`（一直连着，旧行为）；改时限：`CFG hididle 300`。配对模式（appearance≠0）下自动不睡。
- 验证：`python -m hil stop` 后看 iPhone 设置→蓝牙，HIL-Bridge 应变成「未连接」，任意 App 的输入框能弹出屏幕键盘；
  再用时 `python -m hil up`，看它报告「iPhone HID 已连」。
- 串口 `RESTART` = 硬复位（等于断电重启，绑定/配置都在）。
- 之前一版（未发布）在断开后静默 90 s 再恢复广播，结果 iPhone 在 90 s 后自己连回来、键盘又被藏，所以改成一直不广播直到被叫醒。

## 常用硬件键盘快捷键（iOS）

`KP GUI+H` 回主屏（或 `CC HOME`）、`KP GUI+SPACE` 搜索（偶尔第一下不响应）、`KP GUI+TAB` 切 App、`KP GUI+SHIFT+3` 截图、`KP ESC` 返回/取消。
