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

   **`CFG unbond all` 一直回 `removed=1 left=1`（删不掉）= NVS 里的绑定区坏了**（反复刷固件/改外观后出现过两次），
   症状是 iPhone 蓝牙里看得到却连不上、或连上后 HID 输入全被无视。修法：擦 NVS、写回口令、重新配对：

```bash
esptool --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 erase-region 0x9000 0x5000   # NVS 分区
# 串口：CFG token <原口令>（hil.json 的 ble_token），其余配置都回默认（appearance=0x3C2 parkadv=1 hididle=600）
```

   然后 iPhone 上「忽略此设备」，按下面「iPhone 配对」重新配。

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
   固件 0.3.3 默认 `appearance=0x3C2`、`parkadv=1`：板子始终以蓝牙键盘身份广播，iPhone 随时看得到、点得着，不用切什么模式。
   （iPhone 的「设置 → 蓝牙」页里点 HIL-Bridge 配对也行，效果一样。）
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
- 给**另一台** iPhone 配对：板子默认就以键盘身份广播，直接配；旧固件留下的 `appearance=0` 要先 `CFG appearance 0x3C2`（立刻生效，不用重启）。
- 换 HID 目标：`CFG hidpeer clear`，再让新 iPhone 配对一次。
- 删掉 Mac 误配对留下的绑定：`CFG unbond others`（保留 iPhone），`CFG unbond all`（全删）。

## 空闲时放开 iPhone（固件 0.3.3）

iOS 只要连着一个蓝牙键盘，就把**所有 App 的屏幕键盘**藏起来。板子对 iPhone 来说就是蓝牙键盘。

- `hididle` 秒（默认 600）没有需要 HID 的命令（MV/CK/KP/TX/CC …）→ 断开 iPhone 的 HID（**不清绑定**），进入 parked。`SLEEP` 立刻进入。
  板子**始终在广播**，始终可被 Mac/用户连上。
- parked 后怎么广播、iPhone 回连怎么办，由 `parkadv` 决定（`CFG parkadv 0|1`，立刻生效）：
  - **`parkadv=1`（默认）**：照样以键盘身份广播，iPhone 回连就接受并醒来。实测 iPhone **0.3 s 内就自己连回来**，
    所以空闲踢只是让连接跳一下，**屏幕键盘仍被占着**；好处是板子重启/被踢后 iPhone 都自动连回，工具随时能用。
  - **`parkadv=0`**：只播指令服务，iPhone 回连就踢（iOS 被踢几轮、约 6 s 后放弃），**屏幕键盘稳定可用**；
    代价是之后 `WAKE` **叫不回** iPhone（实测 70 s+ 不回连），要用时得 `WAKE` 后让用户在 iPhone「设置 → 蓝牙」点 HIL-Bridge。
  - 结论：在 iOS 上「板子随时能操作（自动连回）」和「空闲放开屏幕键盘」互斥，`parkadv` 让你二选一。
    iOS 的蓝牙设置里**没有「断开连接」**（只有「忽略此设备」），所以 parkadv=1 下想临时要回键盘：`CFG parkadv 0` + `SLEEP`，
    或给板子断电，或在 iPhone 上关蓝牙。
- `ST` 里看：`hid=on`（iPhone 连着）/ `idle`（parked）/ `waking`（醒了、等 iPhone 连）；`parkadv=`、`appear=`、`idle=` 距上次活动秒数、
  `conns=` 连接数、`adv=pub|off`（off 只在 iPhone + Mac 两个连接占满时）。
- 一直连着、永不踢：`CFG hididle 0`；改时限：`CFG hididle 300`。
- 串口 `RESTART` = 硬复位（等于断电重启，绑定/配置都在）。CH340 板子在 macOS 上**每次打开串口也会复位一次**。
- 已知副作用：板子带 HID 外观广播时，Mac 的链路守护进程（BLE）连上后约 30 s macOS 会试着自动配对它、失败超时，
  这段时间指令通道会卡顿/断一次；`hil up` 会等过去。Mac 系统设置里别去配对 HIL-Bridge。
- 历史：0.3.0/0.3.1 空闲时停止广播→板子不可达；0.3.2 改为「断 HID 但一直广播」，但默认 `appearance=0`，iPhone 被踢后看不到、连不回，
  只能靠工具层「切 appearance + RESTART」进侦听模式；0.3.3 把广播内容改成随时按配置重建（不用重启），默认改为以键盘身份广播 + parkadv。

## 常用硬件键盘快捷键（iOS）

`KP GUI+H` 回主屏（或 `CC HOME`）、`KP GUI+SPACE` 搜索（偶尔第一下不响应）、`KP GUI+TAB` 切 App、`KP GUI+SHIFT+3` 截图、`KP ESC` 返回/取消。
