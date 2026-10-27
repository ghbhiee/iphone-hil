# iphone-hil — 让 AI Agent 看见并操作你的 iPhone

> **Hardware-in-the-loop iPhone assistant.** Mac 通过 USB 读到 iPhone 的屏幕画面，Agent（Claude Code 等）看图判断该做什么，再由一块十几块钱的 ESP32 扮演蓝牙鼠标键盘，借 iOS 的辅助功能把点击和输入送进手机。
> 对手机上的 App 来说，这就是一个人在用鼠标操作一台普通 iPhone。不越狱、不装描述文件、不碰 App 的进程和网络。
>
> [English README](README.en.md)

```
┌─────────────────────────────── Mac ───────────────────────────────┐
│  HIL Capture.app ◀── USB（QuickTime 录 iPhone 屏幕的那条官方通道）──┼─── iPhone
│        │ 帧                                                        │     ▲
│        ▼                                                          │     │ BLE HID
│  Agent（Claude Code）：看图 → 决定点哪 / 输什么                      │     │ 鼠标+键盘
│        │ 命令（MV/CK/KP/TX…）                                       │     │
│        ▼                                                          │     │
│  HIL Link.app ────── BLE 自定义指令服务 ──────────────────────────▶ ESP32（HIL-Bridge）
└───────────────────────────────────────────────────────────────────┘   只需充电头供电
```

实测（经典 ESP32 + 一台 1284×2778 的 iPhone）：取一帧约 20 ms；一次点击落点误差 2–5 px、耗时 1–4 s；
Mac→ESP32 一条命令中位 30 ms、连发 200 条零失败。第一个真实任务是在微信里找到一家医院的服务号，走「门诊服务 → 报告查询」，
把 8 份检验报告逐份打开读出来：全程 10 次点击没有一次点偏，没有输入过一个字。

---

## 目录

1. [为什么要做这个](#1-为什么要做这个)
2. [原理：两条苹果留着的官方通道](#2-原理两条苹果留着的官方通道)
3. [难点：蓝牙鼠标只有相对位移](#3-难点蓝牙鼠标只有相对位移)
4. [架构与组件](#4-架构与组件)
5. [能做什么、不做什么](#5-能做什么不做什么)
6. [硬件清单](#6-硬件清单)
7. [安装](#7-安装)
8. [使用](#8-使用)
9. [给 Claude Code 的 skill](#9-给-claude-code-的-skill)
10. [安全与隐私](#10-安全与隐私)
11. [已知限制与待做](#11-已知限制与待做)
12. [踩过的坑（给想改代码的人）](#12-踩过的坑给想改代码的人)
13. [仓库结构、测试、协议](#13-仓库结构测试协议)

---

## 1. 为什么要做这个

### 1.1 中文互联网的孤岛化

浏览器是 AI Agent 最好用的手和眼：让 Claude 在浏览器里比价跨国机票、翻几十个页面筛选过滤，几分钟就能做完一个人要花一下午的事。
但同一套能力一碰中文互联网就失效。原因不是技术，而是生态走到了一个特殊的位置。

**现象：服务从网页退回到了 App 里。** 医院挂号、检验报告、水电燃气缴费、社保公积金、政务办事，十年前多数有自己的网站；现在绝大部分只剩微信公众号 / 小程序、支付宝小程序，或者一个必须装在手机上的 App。外卖、买菜、打车、本地生活几乎从来没有过可用的网页版；电商和出行即便有网页，登录也要用手机扫码，许多功能一点就提示「请在 App 内打开」。一家三甲医院原本有自己的门户，后来下线了，只保留微信和支付宝里的两个窗口。

**为什么会这样：各方的理性选择加起来，就是一座孤岛。**

- 对平台，把用户圈在自己的生态里是最直接的利益：流量留在内部、支付走自己的通道、用户数据和时长都归自己。入口即权力——谁掌握了用户打开手机后的第一步，谁就能向下游所有服务收「过路费」。
- 对机构和商家，在超级 App 里开一个小程序比做一个网站划算得多：开发成本低、登录态和支付现成、服务通知能直接推到用户手机上，还有平台的流量。独立网站没有流量就没有人维护，基于成本放弃「真正的互联网门户」是完全理性的。
- 对普通用户，这一切的体验确实做到了极致：扫码即用、免注册、支付一键完成、消息送达及时。公平地说，在平台内部，中国的产品打磨水平是世界顶尖的。

**代价：内卷替代了创新。** 当所有服务都活在两三个超级 App 里，竞争就只发生在平台的规则之内——争的是流量分配、排名位置、抽成比例，而不是谁能做出新东西。更要紧的是，**生态对外部关上了门**：没有公开的网页，没有开放的接口，第三方工具、浏览器扩展、搜索引擎、无障碍辅助软件，以及现在的 AI Agent，统统进不来。互联网最初的那套东西——URL、超链接、可索引、可编程、任何人用任何设备任何浏览器都能访问——被整体废弃了。用户失去了选择权，数据不可迁移；开发者失去了独立于平台的生存空间；整个生态在自己的边界之内越打磨越精致，却和外面的世界越来越不互通。

**长期风险：加拉帕戈斯化。** 这个词最早用来形容日本的手机产业：i-mode 时代的日本功能机在本国市场体验极致、功能领先世界好几年，却完全按本国运营商的私有标准演进，和全球生态脱节；智能手机浪潮到来时，这个曾经最先进的产业几乎整体出局。日本汽车工业在电动化、智能化转型上的迟缓，也常被拿来作同一个类比——在自己的范式里做到极致，恰恰让转向变得最难。中文互联网的 App 孤岛正在走同样的路：在私有运行时里把体验做到极致，同时失去和开放世界的接口。而现在正好有一波新的范式转移——**AI Agent 正在成为新一代的「用户」**，它需要的是开放的、可编程的、可被程序访问的界面。一个把一切锁在私有运行时里的生态，在这波浪潮里会首先失去被接入的能力。

### 1.2 我们试过的路，和它们为什么走不通

这个项目之前，我们在另一条线上把能想到的办法都试了一遍（以「在电脑上查某医院公众号里的检验报告」为例）：

| 尝试 | 结果 |
|---|---|
| 抓包代理 + 根证书，把微信内置浏览器走完 OAuth 后拿到的登录态搬进 Chrome | 微信 Mac 版检测到异常环境，直接把账号踢下线 |
| 不用代理，从磁盘读微信内置浏览器（Chromium 内核）存的 cookie 注入 Chrome | Agent 用桌面自动化去点微信时同样被踢下线；手动点开再读 cookie 这一步在 Mac 上卡住 |
| 在 Windows 上用 `SendInput` 模拟硬件输入去点微信 | 一样不行。风控是服务端综合打分，不是只看注入方式 |
| 把小程序原样跑在浏览器里 | 不可能：没有 `wx.*` 运行时，解包别人的程序再模拟接口既不现实也不合适 |
| Mac App Store 里找「iPhone 兼容版」的微信 / 支付宝 / 政务 App | 这些 App 全部没有开放 Apple Silicon 兼容运行 |
| **macOS 的「iPhone 镜像」**（把手机屏幕投到 Mac 窗口里） | 画面能看，但 Apple 刻意封死了自动化：窗口**不暴露任何无障碍（AX）元素**，System Events / osascript **看不到这个进程**，**局部截图被拦成黑块**，只有窗口在前台激活时才接收点击。Agent 只能靠整屏截图算像素坐标去点，效率极低且不可靠 |

结论很清楚：**Agent 要接触中文互联网的服务，唯一干净的入口是「一台真实的手机，跑着真实的 App，收到真实的输入」**。
从 App 和风控的角度看，必须就是一个人在用手机。那剩下的问题就是：Mac 上的 Agent 怎样「看到」这台手机，又怎样「伸手」去点它。

### 1.3 这个项目的答案

- **看**：用 QuickTime 录 iPhone 屏幕用的那条 USB 通道，Mac 端 AVFoundation 直接把手机画面当一路视频源取帧。
- **点**：用 iOS 为肢体障碍用户准备的**辅助触控（AssistiveTouch）指针设备支持**，让 iPhone 接受一个标准蓝牙鼠标 + 键盘。这个鼠标键盘由一块 ESP32 扮演，Mac 通过蓝牙把「移动 / 点击 / 按键」命令发给它。
- **闭环**：蓝牙鼠标只有相对位移、iOS 还有指针加速度，所以 Agent 不能「告诉指针去 (x, y)」。本项目在画面里**识别指针位置**，看一步、挪一步，几次迭代把指针逼近到目标，再点击。

三样东西都是公开的、苹果自己留着的能力。没有越狱、没有私有 API、没有在 iPhone 上装任何东西。

这不是对孤岛问题的解决方案，只是一条绕路：围墙挡得住 API 和浏览器，挡不住屏幕上的像素和一只「拿着鼠标的手」。但它至少让 Agent 能替你去做那些只能在手机里做的事——也顺便说明了，一个生态如果只能靠这种方式被接入，它离开放世界已经有多远。

---

## 2. 原理：两条苹果留着的官方通道

### 2.1 看：USB 屏幕采集（QuickTime 的通道）

Mac 的 QuickTime Player「新建影片录制」能把接在 USB 上的 iPhone 选作摄像头，录下手机屏幕。这是 Apple 自 OS X Yosemite / iOS 8 起提供的功能，用于给开发者和媒体录制 App 演示。它的底层是 CoreMediaIO：进程设置 `kCMIOHardwarePropertyAllowScreenCaptureDevices` 后，已信任此电脑的 iPhone 会以一个 AVFoundation 采集设备出现（modelID "iOS Device"，设备类型 muxed）。ffmpeg、OBS 等工具用的也是同一条路。

本项目 `hil/capture.py` 就做这件事：枚举该设备、开 `AVCaptureSession`、`AVCaptureVideoDataOutput` 回调里把 `CVPixelBuffer` 转成 RGB 数组保存最新一帧。实测画面 1284×2778、约 60 fps 到达，取一帧（含编码成半尺寸 PNG）几十毫秒。

值得知道的几件事：

- **手机上没有录屏红点。** 这不是 iOS 的「屏幕录制」功能，而是 Mac 主动从 USB 上拉取的镜像流，iOS 不会在状态栏提示。副作用是：**状态栏会被 iOS 换成演示样式**（固定 9:41、满格信号、满电），Agent 不能信截图里的时间和电量。
- **权限是 Mac 这边的「相机」权限**，按 App 归属。Claude Code 之类的宿主进程拿不到相机 entitlement，所以采集程序必须做成独立的 `.app`（见 §12）。
- **iPhone 锁屏后采集设备会消失**，解锁后回来。使用期间要把自动锁定设为「永不」。
- 只要数据线接着、设备信任了电脑，Mac 上任何拿到相机权限的程序都能看到手机画面。这既是本项目的基础，也是你应该知道的隐私事实（§10）。

### 2.2 点：iOS 辅助触控的指针设备支持

**辅助触控（AssistiveTouch）** 是 iOS 5（2011）加入的无障碍功能，最初是给手部运动能力受限、按不了实体键或做不出多指手势的用户的：屏幕上一个悬浮球，点开后用单指就能完成「回主屏」「截屏」「摇动」「捏合」这些动作。

**iOS 13（2019）** 起，辅助触控下新增了**指针设备支持**：iPhone 可以连接蓝牙或 USB 的鼠标、触控板、轨迹球、操纵杆等辅助指点设备，屏幕上出现一个圆形指针，点击即等同于手指触摸。它的服务对象是依赖适配性输入设备的用户，比如用下巴操纵杆或头部跟踪器的人。因此它在设计上就是「把一个标准 HID 鼠标翻译成触摸」——这正是我们需要的。几点细节：

- 任何符合蓝牙 HID 规范的鼠标都可以，配对是 Just Works（无 PIN）。
- 指针可以在「设置 → 辅助功能 → 指针控制」里改颜色、大小、是否自动隐藏、是否带吸附动画、跟踪速度。本项目把指针设成**显眼的纯色、关掉自动隐藏和动画**，就是为了在画面里稳定识别它。
- iOS 的指针有**加速度曲线**：鼠标每次报告的位移越大，屏幕上走得越远（非线性）。这就是为什么必须做标定和视觉闭环（§3）。
- 鼠标滚轮会滚动指针下方的元素；右键打开长按菜单；长按鼠标键等于长按屏幕。
- **硬件键盘**是 iOS 4 起就有的独立能力，不依赖辅助触控：任何蓝牙键盘都能输入 ASCII、发组合键（`⌘+H` 回主屏、`⌘+Space` 搜索、`⌘+Tab` 切 App）。Consumer Control 的「AC Home」键也能回主屏。

一句话：**iOS 把「鼠标」当成一种无障碍输入，而不是一种自动化接口**。所以它没有被封，也不会被 App 察觉——App 看到的只是 `UITouch`。

### 2.3 手：ESP32 当蓝牙 HID，Mac 也用蓝牙指挥它

iPhone 唯一的接口被 Mac 的采集线占了，所以鼠标键盘只能走蓝牙。ESP32 是做这件事最省事的硬件：十几块钱、带 BLE、Arduino 生态里的 NimBLE 库能同时跑 HID 服务和自定义服务。

固件 `firmware/hid_bridge/hid_bridge.ino` 做了三件事：

1. **BLE HID 设备**（报告 ID：键盘 1、鼠标 2、Consumer 3），和 iPhone 绑定。
2. **一个 Nordic UART 风格的自定义指令服务**（RX 写入 / TX 通知），Mac 连上后先 `AUTH <口令>`，然后发文本命令：`MV dx dy`、`CK`、`LP ms`、`WH n`、`KP GUI+H`、`TX hello`、`CC HOME`……每条命令回一行 `OK` / `ERR`。完整协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。
3. **两个连接并存**：一个给 iPhone（HID，已绑定、按地址锁定），一个给 Mac（指令，不配对、靠口令）。没满两个就继续广播。

这样 ESP32 平时只要插一个充电头。Mac 端用 CoreBluetooth 连它（`hil/ble_central.py`），不需要在系统设置里配对——而且**不能**配对，否则 macOS 会把它当成自己的鼠标键盘接管。为此固件广播通用外观（appearance 0）而不是 HID 外观，并把 HID 目标锁定为第一台订阅鼠标报告的设备（iPhone）。

串口（USB 数据线接板子）仍保留，用于刷固件、读口令、改配置。

---

## 3. 难点：蓝牙鼠标只有相对位移

HID 鼠标报告的是「这次动了 dx, dy」，而不是绝对坐标；iOS 还叠加了加速度，并且每条报告的位移上限是 ±127。所以 Agent 没法说「指针去 (640, 1200)」。`hil/locate.py` 用视觉闭环解决：

1. **复位（park）**：发一个巨大的负位移（如 −6000, −6000），指针被屏幕边缘钳住，停在左上角——这是唯一确定的原点。再往里挪一点让它完整露出来。
2. **标定（calib）**：从原点分别发 4 / 12 / 32 / 64 / 127 counts，量像素位移，得到「每条报告的 count 量级 → 像素/count」的增益表（iOS 加速度的实测曲线，127 反而比 64 低，因为固件按 ±127 拆分大位移时报告数不同）。表存进 `hil.json`，`Calib.predict_px` 按固件同样的拆分方式预测，`counts_for` 二分反解。
3. **逼近（move_to）**：误差 × 阻尼 0.85 → 换算 counts → 发 → 等画面刷新并**等指针停稳**（连续两帧位置差 ≤ 2 px）→ 只在预测位置附近的 ROI 里找指针 → 用「观测 / 预测」在线修正增益（贴边时不学）。一般 3–8 次迭代进入 8 px 容差，然后点击。
4. **找回（recover）**：指针不见了先微动一下（防自动隐藏），再不行就复位重来；发了位移却两次不动 → 当前认的「指针」可能是别的东西 → 复位。
5. **识别（pointer.py）**：HSV 阈值 → 连通域 → 把外轮廓填实（iOS 彩色指针是「彩色圆环 + 半透明中心」）→ 用填充率 ≈ π/4 和周长圆度判圆，半圆（被屏幕边缘切掉）也成立，矩形永远是 1.0 会被排除。`pointer-sample` 命令从一张截图上采样指针颜色自动生成阈值。

没硬件也能跑：`hil/sim.py` 是一台仿真手机（收 HID 报告、模拟加速度、噪声、自动隐藏、红色干扰元素、渲染画面），`pytest` 里的定位闭环测试就跑在它上面。

---

## 4. 架构与组件

```
hil/capture.py         AVFoundation 取帧（打开 iOS 屏幕采集设备、保存最新一帧、像素格式转换）
hil/capture_server.py  取帧 HTTP 服务（127.0.0.1:8765），打包进 ~/Applications/HIL Capture.app
hil/client.py          取帧服务客户端 RemoteGrabber；没起服务会自动 open 这个 App
hil/protocol.py        Mac↔ESP32 指令协议（唯一定义处）；docs/PROTOCOL.md 由它生成
hil/link.py            Link 抽象：SerialLink（串口）/ HttpLink（经守护进程）/ LoopbackLink（回环桩）/ FirmwareModel（固件逻辑的 Python 参考实现）
hil/link_server.py     链路守护进程（127.0.0.1:8766）：持有串口或 BLE 连接，CLI 的每条命令经它转发
hil/ble_central.py     CoreBluetooth 中心端：扫描 HIL-Bridge → 订阅 TX → AUTH → 同步收发
hil/pointer.py         画面里找指针（颜色 + 圆度）
hil/locate.py          反馈式定位：park / calib / move_to / tap / long_press / drag
hil/loop.py            取帧 → Policy → 执行 的循环框架（run 脚本模式）
hil/sim.py             仿真手机 + 仿真取帧（无硬件测试）
hil/cli.py             python -m hil …  命令行（Agent 就用它）
firmware/hid_bridge/   ESP32 固件：BLE HID + BLE 指令服务 + 串口；firmware/prebuilt/esp32/ 是经真机验证的二进制
tools/make_app.sh      生成 HIL Capture.app（相机权限归它）
tools/make_link_app.sh + tools/launcher.c   生成 HIL Link.app（蓝牙权限归它；原生启动器加载 libpython）
skill/iphone-hil/      给 Claude Code 的 skill：什么时候用、怎么用、节奏、安全边界
tests/                 协议 / 固件模型 / 定位闭环（仿真）
```

两个 `.app` 都是**按需拉起、空闲 10 分钟自动退出**的常驻服务：任何一条 `hil` 命令发现服务没起就会 `open -g` 它，取帧和蓝牙各约 2 秒就绪。取帧服务只把「取帧」算作活动，链路服务只把「发命令」算作活动，单纯查状态不续命。资源本身很小（合计约 30 MB 内存、CPU 接近 0），自动退出主要是省 iPhone 的电——采集在跑时手机一直在做镜像输出。

---

## 5. 能做什么、不做什么

**目标场景**：那些「只有手机 App / 小程序里才有」的日常事务，让 Agent 替你点：

- 外卖、购物：按你的要求找店、找商品、加购物车、比价，停在付款前让你确认。
- 挂号、报告：医院公众号 / 小程序里查排班、挂号、把检验报告逐份打开读出来并整理成表。
- 水电燃气、物业、社保：缴费小程序里查账单、读用量。
- 政务 App：查进度、读通知、填写不涉及证件和密码的表单。
- 任何「帮我看看手机上那个 App 里……」的事。

**已经做过的**：在微信里从通讯录找到一家医院的服务号（走字母索引，绕开中文输入），进入「门诊服务 → 报告查询」，8 份报告逐份打开、翻页、读出结论。

**明确不做的**（写进了 skill 的安全规则，也建议你写进自己的 Agent 提示词）：

- 不输入证件号、密码、支付密码、验证码。页面要这些时停下来让人来。
- 不付款、不发送消息、不改手机设置，除非用户明确要求了这一步。
- 截图含个人数据，任务结束删掉 `captures/` 下的图片。

---

## 6. 硬件清单

| 件 | 要求 | 备注 |
|---|---|---|
| Mac | macOS 13+，Apple Silicon 或 Intel | 开发和验证在 Apple Silicon 上 |
| iPhone | iOS 13+（辅助触控指针支持） | 验证机画面 1284×2778；其它分辨率只需重新 `pointer-sample` + `calib` |
| 数据线 | iPhone ↔ Mac，要能传数据 | 取画面用 |
| ESP32 开发板 | 经典 ESP32（DevKitC 类，CH340/CP2102 串口桥）；ESP32-S3 也支持 | 十几到三十元 |
| 充电头 / USB 口 | 给 ESP32 供电 | 平时不需要接 Mac |

ESP32-S3 版固件多一个「native USB 口当有线 HID」的备用后端，只在不用 USB 采集画面（比如改用 AirPlay）时才有意义；真机只验证过经典 ESP32 的 BLE 路线。

---

## 7. 安装

### 7.1 Mac 端

```bash
git clone https://github.com/ghbhiee/iphone-hil.git ~/iphone-hil
cd ~/iphone-hil
uv venv .venv --python 3.12 && uv pip install -r requirements.txt   # 或 python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp hil.example.json hil.json
.venv/bin/python -m pytest -q tests                                  # 17 个测试，全在仿真上跑，不需要硬件
```

`tools/make_app.sh` 和 `tools/make_link_app.sh` 会在第一次 `hil up` 时自动调用，生成 `~/Applications/HIL Capture.app` 和 `HIL Link.app`。
第一次启动时 macOS 会分别弹**相机**和**蓝牙**授权框，点允许（相机权限就是 iPhone 屏幕采集的权限）。

### 7.2 ESP32 固件

见 [firmware/FLASH.md](firmware/FLASH.md)。简版：

```bash
pip install esptool
esptool.py --chip esp32 --port /dev/cu.usbserial-XXXX --baud 460800 write_flash \
  0x1000 firmware/prebuilt/esp32/hid_bridge.ino.bootloader.bin \
  0x8000 firmware/prebuilt/esp32/hid_bridge.ino.partitions.bin \
  0xe000 firmware/prebuilt/esp32/boot_app0.bin \
  0x10000 firmware/prebuilt/esp32/hid_bridge.ino.bin
screen /dev/cu.usbserial-XXXX 115200       # 输入 ST 回车，记下 token=xxxx；Ctrl-A K 退出
```

把 `token=` 后面那串填进 `hil.json` 的 `ble_token`。以后升级固件只刷 `0x10000` 的 app 分区，别整盘刷（会清掉与 iPhone 的绑定）。

### 7.3 iPhone 端（一次性）

1. 设置 → 辅助功能 → 触控 → **辅助触控：开**。
2. 同一页 → 设备 → 蓝牙设备 → 配对 **HIL-Bridge**。
3. 设置 → 辅助功能 → **指针控制**：颜色选红或橙、指针调大、**自动隐藏关、指针动画关**。
4. 辅助触控 → 闲置不透明度调最低。
5. 设置 → 通用 → 键盘 → 硬件键盘 → 布局「美国」。
6. 设置 → 显示与亮度 → **自动锁定：永不**。
7. 数据线接 Mac，解锁，点「信任此电脑」。

### 7.4 第一次联调

```bash
.venv/bin/python -m hil up                        # 拉起两个服务，报告画面分辨率 / 固件版本 / iPhone 是否已连
.venv/bin/python -m hil grab                      # captures/frame.png
.venv/bin/python -m hil move 80 80                # 指针该动一下
.venv/bin/python -m hil pointer-sample captures/frame.png X Y   # X Y 是截图里指针中心（半尺寸图的坐标）
.venv/bin/python -m hil find --debug dbg.png      # 确认只认出一个指针
.venv/bin/python -m hil park && .venv/bin/python -m hil calib   # 标定，写入 hil.json
.venv/bin/python -m hil tap 300 520               # 点一个图标试试
```

---

## 8. 使用

每一步都是一条独立命令，状态全在两个守护进程和 `hil.json` 里，所以任何能跑 shell 的 Agent 都能当「大脑」：

```bash
python -m hil grab                 # 截图到 captures/frame.png（默认缩放 0.5），让模型看图
python -m hil tap X Y              # 坐标按缩放后的图；自动逼近后点击
python -m hil long X Y 800         # 长按
python -m hil drag X0 Y0 X1 Y1     # 拖动（带惯性，适合回顶部；精细翻页用 wheel）
python -m hil wheel 6              # 滚轮往下翻约 8 行
python -m hil type "ascii"         # ASCII 文本（先确认 iPhone 在英文键盘）
python -m hil key GUI+H            # 组合键；GUI+SPACE 打开 Spotlight
python -m hil home                 # 回主屏
python -m hil goto X Y             # 只把指针挪过去不点（滚轮要落在列表上时用）
python -m hil status / up / stop   # 两个服务 + 固件状态 / 拉起 / 立刻停
python -m hil run script.txt       # 一行一个动作，走 Runner（每步存帧 + log.jsonl）
python -m hil step                 # 交互式：人来当大脑
```

全局参数：`--scale`（截图缩放，默认 `hil.json` 的 `brain_scale` 0.5）、`--link serial|direct|loop|echo`、`--capture remote|sim`、`-v`。
没硬件：`python -m hil --capture sim --link loop sim-demo` 看定位闭环在仿真手机上跑。

**节奏经验**：tap 之后等 1.5 s 再 grab；H5 页面加载等 3–4 s；滚轮事件落在指针下方的元素上，列表不滚就先 `goto` 到列表中间；手机被拿起会横竖屏切换，帧尺寸随之变，上一帧的坐标作废。

---

## 9. 给 Claude Code 的 skill

`skill/iphone-hil/SKILL.md` 把上面这些写成了一份 Claude Code skill：前提检查、`hil up`、工作循环、节奏、已知限制、安全规则。装法：

```bash
ln -s ~/iphone-hil/skill/iphone-hil ~/.claude/skills/iphone-hil   # 克隆到别处就先改 SKILL.md 里的路径
```

之后新开一个会话直接说人话就行：

- 「看一下我手机」
- 「打开微信，找到 XX 医院的公众号，把最近的报告都读出来」
- 「在美团上帮我找一家评分 4.8 以上的粥店，加购一份皮蛋瘦肉粥，停在付款前」
- 「打开国网的小程序看看这个月电费多少」

Claude 会自己 `hil up`、截图、点、再截图，并在要密码 / 付款 / 发消息的地方停下来问你。
服务空闲 10 分钟自动退出，不用管。

> 如果你的 Mac 上**没有**本项目，只想让 Claude 做一个「我自己翻手机、它在后台截帧读内容」的简化版，[docs/PROMPT_从零做后台截帧读报告.md](docs/PROMPT_从零做后台截帧读报告.md) 是一段可以直接贴给新会话的需求描述。

---

## 10. 安全与隐私

- **画面去了哪里。** 帧只存在本机 `captures/`。但 Agent「看图」意味着截图会作为输入发给你所用的模型服务（例如 Claude 的 API），这和你把截图贴进聊天窗口是一回事。截图里是什么，模型就看到什么；任务做完删掉 `captures/*.png`。
- **采集通道的含义。** 只要 iPhone 用数据线接着 Mac、信任了电脑、处于解锁状态，Mac 上任何拿到相机权限的程序都能看到手机画面，手机上没有任何提示。用完拔线，或在 iPhone 上「还原位置与隐私」撤销信任。
- **蓝牙指令通道。** ESP32 的指令服务不绑定、不加密，只靠口令（`AUTH`），口令在串口上可见。它的威胁模型是「别让旁边的设备误连」，不是「对抗有意的攻击者」。不要把 ESP32 带到你不信任的环境里持续供电。
- **Agent 的行为边界**写在 skill 里：不输入证件号 / 密码 / 支付信息，不付款、不发消息、不改设置，除非用户明确要求。建议你用自己的 Agent 时也照抄这几条。
- **风控视角。** 对手机上的 App 来说，这就是蓝牙鼠标 + 一个人在看屏幕。但「替人操作账号」是否符合某个平台的服务条款，由你自己判断；本项目只给你自己的设备、自己的账号用。

---

## 11. 已知限制与待做

- **中文输入没有可用方案。** 蓝牙键盘只发 ASCII；中文本来打算走「Mac 剪贴板 → 通用剪贴板 → iPhone ⌘+V」，需要两端都开「接力」，目前没验证。实践里靠列表、索引栏、菜单绕开输入；需要搜中文时让用户在手机上打。
- **滚轮负方向无效**（往上翻不动，原因待查），往上用 `drag` 或方向键。
- 中文拼音键盘会把字母吃进拼音合成、把数字当候选；`type` 前要切到英文键盘（`key CTRL+SPACE` 轮换后看截图确认）。
- `⌘+Space` 打开 Spotlight 偶尔第一下不响应。
- 一次点击 1–4 s：瓶颈是「等指针停稳 + 等画面刷新」，不是蓝牙。可以靠更激进的增益预测压到 1 s 内，还没做。
- 只验证过一台 iPhone、一块经典 ESP32。换机后 `pointer-sample` + `calib` 重做即可；ESP32-S3 固件能编译但没真机验证。
- 没有做「App 风控会不会识别蓝牙鼠标」的系统测试；目前微信里几十次操作没有任何异常提示。

---

## 12. 踩过的坑（给想改代码的人）

- **相机 / 蓝牙权限按 App 归属。** Claude Code 的宿主进程是 hardened runtime 且没有相机 entitlement，子进程申请相机权限被 TCC 静默拒绝（不弹框、状态停在 notDetermined）。所以采集服务打包成 `HIL Capture.app`。CoreBluetooth 更严：要求**可执行文件本身**所在的 bundle 带 `NSBluetoothAlwaysUsageDescription`，shell 壳 `exec` 出 venv 的 python 会被直接 SIGABRT（退出码 134），所以 `HIL Link.app` 的可执行文件是 `tools/launcher.c` 编出来的原生启动器，`dlopen` libpython 在本进程里跑 `hil.link_server`。
- **iOS 采集设备不报格式**（0x0 @ 0 fps），别去设 `activeFormat`；分辨率从首帧读。macOS 下 session 在 `startRunning` 前会把设备格式改回 preset 对应的 1080p，解法同 ffmpeg：`lockForConfiguration` 后设格式**不解锁**，`startRunning` 之后再解锁。
- **pyobjc 回调里的 `CMSampleBuffer` 是裸指针**（PyObjCPointer），没桥接成 CF 对象；帧处理全程用 ctypes 调 CoreMedia / CoreVideo，并且回调里的 Python 异常会变成 NSException 把进程 abort，所有 delegate 方法都包了一层 `_safe`。
- **主线程必须跑 NSRunLoop**，否则 AVFoundation 的设备插拔通知不派发，常驻服务永远看不到后插上的 iPhone。
- **CH340 板子每次开串口都会复位**，所以串口只由守护进程开一次，CLI 经 HTTP 转发。
- **BLE 报告会在协议栈里排队。** 固件把大位移拆成 ±127 的报告，报告间隔必须 ≥ 连接间隔（实测 25 ms），否则几十条报告堆在队列里，画面上看到的全是旧位置，标定结果是垃圾。
- **macOS 会自动接管 HID 外观的 BLE 设备。** Mac 一连上带 HID 外观的外设，bluetoothd 就自己发起配对并登记成鼠标。对策：广播通用外观 + 广播自定义服务 UUID 而不是 HID UUID + 固件按地址锁定 HID 目标。
- **整盘刷固件会清 NVS**，里面是与 iPhone 的绑定密钥和配置；升级只刷 app 分区。
- **Mac 睡眠时蓝牙会关闭再打开**，连接已失效但对象还在；中心端状态一离开 PoweredOn 就标记断开，主循环重连。
- **iOS 指针是圆环不是圆盘**，颜色采样要取环上的饱和像素，识别前要把外轮廓填实。
- 更多细节、时间线和实测数字在 [HANDOFF.md](HANDOFF.md)。

---

## 13. 仓库结构、测试、协议

```
.
├── hil/                 Mac 端 Python 包（取帧、链路、定位、CLI）
├── firmware/
│   ├── hid_bridge/      ESP32 固件源码（Arduino / NimBLE）
│   ├── prebuilt/esp32/  经真机验证的二进制
│   ├── build.sh         arduino-cli 编译/上传脚本（esp32 | ble | usb）
│   └── FLASH.md         刷写与 iPhone 配对
├── tools/               两个 .app 壳的生成脚本和原生启动器
├── skill/iphone-hil/    Claude Code skill
├── docs/PROTOCOL.md     Mac↔ESP32 指令协议（由 hil/protocol.py 生成）
├── tests/               pytest（仿真，无硬件）
├── hil.example.json     配置模板（复制为 hil.json；含一台真机的标定作参考）
├── requirements.txt
└── HANDOFF.md           开发日志：决定、坑、实测数字
```

```bash
.venv/bin/python -m pytest -q tests
```

许可：MIT。
