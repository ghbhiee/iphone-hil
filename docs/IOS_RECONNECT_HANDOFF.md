# iPhone-HIL 待解难题交接：空闲踢掉 iPhone 后「软件唤醒自动重连」

> **2026-10-29 更新（固件 0.3.3）**：本文的「软件开侦听 + 用户手工点」和「切 appearance + RESTART」已被取代。板子默认以键盘身份
> （appearance=0x3C2）广播，parkadv=1 时 iPhone 被踢/板子重启后都会 1 s 内自己连回；代价是屏幕键盘基本一直被占着。
> 「自动回连」和「空闲放开键盘」互斥，见 `firmware/FLASH.md`「空闲时放开 iPhone」和 `HANDOFF.md` 最新一节。下面是 10-27 的历史记录。


> 写于 2026-10-27。给接手的模型/会话。本文只讲**一个没搞定的核心问题**，并把今天验证出的事实、根因、底层约束、以及还值得一试的方向写清楚。其余项目背景见 `HANDOFF.md`、`README.md`。

## 更新（2026-10-27 晚）：用户要的工作流已用「软件开侦听 + 用户手工点」实现，无需再解自动重连

用户拍板：**不要自动重连**；用 skill 时由软件把板子切到「侦听模式」（以蓝牙键盘身份广播，`appearance=0x3C2`），用户在 iPhone 设置→蓝牙**手工点一下 HIL-Bridge** 连上即可。这条已纯工具层实现（没改固件）并真机验证通过：
- `hil up`（iPhone 没连时）/ `hil listen` 经串口把板子切侦听模式并提示用户手工点；`hil normal` / `hil stop` / 空闲退出切回正常模式（`appearance=0`）+ SLEEP，松开 iPhone、屏幕键盘恢复。
- 实现：`hil/link.py` 的 `set_adv_mode`/`listen_and_wait`；`hil/cli.py` 的 `listen`/`normal` 命令、改写的 `up`/`stop`；`hil/link_server.py` 退出切正常 + 去掉 open 时的 WAKE。守护进程按名字广扫、NUS 命令服务在侦听模式也在，所以 Mac 照连。

**下面「没搞定的」特指「全自动重连」（板子踢掉后软件不靠用户手工点就把 iPhone 叫回来）**——用户已不需要它，但技术上仍未解，若以后想做全自动，看下文的底层约束和「定向广播」方向。

## 需求（用户原话，务必对齐）

板子是一个 BLE HID 键鼠，和 iPhone 配对，由 Mac/Claude 经 NUS 命令通道驱动它去点 iPhone。
痛点：只要板子作为蓝牙键盘连着，**iOS 会在所有 App 里把屏幕软键盘藏起来**，用户自己没法在 iPhone 上打字。

用户要的行为（**不是**我一度跑偏去设的 hididle=0 常连）：

1. 板子**空闲超时就主动断开 iPhone**（松开键盘），这样 iPhone 的屏幕键盘回来，用户能正常用手机。
2. 当用户通过 skill 要用 HIL 工具时，**由软件（Claude 发 WAKE）把 iPhone 叫回来重连**——**用户不去碰 iPhone**。

第 1 条已验证可行。**第 2 条没搞定，是本文的核心。**

## 今天验证到的事实（都有串口/截图证据）

| # | 测试 | 结果 |
|---|------|------|
| 1 | 连上后 SLEEP / 空闲超时 → 板子断开 iPhone HID | ✅ iPhone 屏幕软键盘**弹出且稳定**（conn=0 稳定 16s；截图 s3 无键盘→s4 整块键盘） |
| 2 | 之后发 WAKE（板子醒、adv=pub、iPhone 解锁亮屏） | ❌ iPhone **90s 没自动重连**（conn 一直 0） |
| 3 | 设 hididle=0（板子永不踢）后让用户手工点 HIL-Bridge | ❌ 正常模式下 **120s 也连不上**（板子收不到任何 `# conn`） |
| 4 | 全新配对：配对模式 adv（appearance=0x3C2，广播 HID 服务）+ 用户点一下 | ✅ 可靠连上（今天两次全新配对都这样成的） |
| 5 | 配对模式 adv 重启后、不点，等 iPhone 自己回连 | ❌ 40s 没回连 |

结论：**唯一可靠的连接方式是「配对模式广播 + 用户在设置里手工点」。软件这端（板子广播/Mac/Claude）无法让 iPhone 主动回连。**

## 今天查清并已修掉的两个干扰项（不是核心问题，但接手要知道）

- **A. iPhone 蓝牙列表里看不到板子**：固件默认 `appearance=0`，**故意不广播 HID 外观/服务 UUID**（防 macOS 自动把板子当键鼠抢走，见 `hid_bridge.ino` 的 `hidBegin()` 里 `if (cfg.appearance) adv->addServiceUUID(HID) else addServiceUUID(NUS)`）。所以 iOS 设置里不把它当可配对键盘列出来。**配对时必须临时 `CFG appearance 0x3C2` 再 RESTART**，配完再 `CFG appearance 0` RESTART 回正常。这一步 README/skill 里原本没写，是坑。
- **B. NVS 配对区被反复刷固件搞坏**：`CFG unbond all` 一直 `removed=1 left=1`（删不掉、计数不降），存着删不掉的旧 LTK，正常模式重连时用错密钥→`auth enc=0`→断开（reason 0x23d）。**已用 `esptool erase-region 0x9000 0x5000` 擦掉 NVS、写回 token、全新配对修好**，修好后正常模式重连成功过一次（重启后 ~1s）。注意：**重连成功那次是「全新配对刚完成」的新鲜上下文**；一旦经历一次 park 的主动踢，就回不来了（见事实表 #2/#3）。

## 核心难点的底层约束（为什么「软件唤醒重连」难）

1. **BLE 里是 central（iPhone）发起连接，peripheral（板子）不能强迫 central 连自己**。板子只能广播+等。
2. **板子主动断开 iPhone 后，iOS 会进入重连退避**，很久不再主动发起（实测 >90s，可能几分钟）。板子怎么广播（undirected、public 地址、带不带 HID UUID）都没可靠地把 iOS 勾回来。
3. 所以**软件端无法可靠地让 iPhone 重连；只有 iPhone 上的用户动作（设置里点 HIL-Bridge / 关开蓝牙）能可靠重连**。
4. **固件耦合**：`appearance!=0`（广播 HID，iOS 才可靠看见/重连）会让 `hidAlwaysOn()==true` → 板子**永不 park**。所以当前固件里「广播 HID 以便重连」和「空闲 park 放开键盘」**不能同时成立**。

今天我没能攻破第 2/3 条。老实说：用 undirected 广播这条路，我判断走不通。

## 还值得一试的方向（我没试或没试透，给接手的人）

1. **Directed advertising（ADV_DIRECT_IND）指向 iPhone 身份地址** —— 最该试的一条。这是 BLE 里 peripheral 向某个已绑定 central「请你现在来连我」的标准机制，iOS 对定向广播的响应**可能**比 undirected 快。板子有 bond（有 IRK），能对 iPhone 身份地址做定向广播。要点：NimBLE 定向广播 API；在 WAKE 时切定向广播；实测被板子主动踢过之后，定向广播能不能把 iOS 勾回来。**这是整个问题的关键实验，值得单独做。**
2. **解耦「广播 HID」和「always-on」**：改固件，让它可以「广播 HID 服务 UUID/appearance」的同时仍然 park（空闲踢）。这样能干净地测「广播 HID 到底帮不帮 iOS 在被踢后重连」。（我测过 appearance=0x3C2 不自动回连，但那是重启后、不是 WAKE 时做定向广播。）
3. **断开原因码**：板子断 iPhone 时 `g_server->disconnect(h, reason)` 用不同 HCI reason（如 0x13 remote-terminated vs 其他）可能改变 iOS 的退避行为。值得试。
4. **半自动兜底（可靠但用户不接受）**：板子照常 park 放键盘；要用工具时 skill **提示用户点一下 HIL-Bridge**。这条可靠，但要用户碰手机，用户明确不想要。skill 里已有这个提示逻辑。

## 当前硬件/配置状态（接手即用）

- 固件 `0.3.2`，源码 `firmware/hid_bridge/hid_bridge.ino`。串口 `/dev/cu.usbserial-*` @115200（`esptool` 连接时会打印板子 MAC）。
- NVS **已擦干净 + 单条干净 bond** 指向 iPhone（串口 `ST` 的 `hidpeer=` 字段）；token 已写回（`ST` 的 `token=` / `hil.json` 的 `ble_token`，两者一致）。
- 现在配置：`hididle=120`（空闲 120s 自动踢 iPhone = 用户要的放键盘行为）、`appearance=0`（正常模式）。
- **iPhone 当前是断开的**：要连回来，目前只能 `CFG appearance 0x3C2` + RESTART 进配对模式广播、用户去设置点一下 HIL-Bridge（正常模式下点也连不上，见事实 #3）。
- `esptool` 在 `~/.local/bin/esptool`；NVS 分区 `0x9000` 大小 `0x5000`（见 `firmware/hid_bridge/build/esp32.esp32.esp32/partitions.csv`）。擦 NVS 会丢 token/配置，用串口 `CFG token <值>` 写回。
- 固件/文档的 0.3.2 改动**仍未 commit、未 push**（因为验证发现了重连问题，没达到「验证通过再推」的条件）。

## 复现

1. 进配对模式（`CFG appearance 0x3C2` + RESTART），iPhone 设置→蓝牙点 HIL-Bridge 配上；再 `CFG appearance 0` + RESTART 回正常模式（此时 iPhone 会重连一次）。
2. `python -m hil up`，打开一个文本框（如桌面下滑 Spotlight）：此时屏幕**无软键盘**（蓝牙键盘连着）。
3. `python -m hil send SLEEP` → 屏幕软键盘**弹出**（✅ 放键盘成立）。
4. `python -m hil send WAKE` → iPhone **不重连**（❌ 核心问题）。
