# HANDOFF — iphone-hil（开发日志，2026-10-26 起）

## 最新（2026-10-29）：固件 0.3.3 + parkadv，工具层去掉「切 appearance + RESTART」
- **固件 0.3.3**：广播内容由 `updateAdvertising()` 按当前配置随时重建（改 appearance/parkadv/hididle、醒睡切换都不用重启）；
  默认 `appearance=0x3C2`、`parkadv=1`、`hididle=600`；非 hidpeer 的 HID 订阅者直接踢掉。`firmware/prebuilt/esp32/hid_bridge.ino.bin` 已更新（编译洗过路径、扫过无个人信息）。
- **真机结论**（同一块板子 + iPhone，2026-10-29）：
  - parkadv=1：板子空闲 600 s 踢掉 iPhone，**iPhone 0.3 s 内自己连回**；板子重启后 ~1 s 连回。所以工具随时可用，但屏幕键盘基本一直被占着。
  - parkadv=0：踢掉后 iPhone 被踢几轮（~6 s）就放弃，屏幕键盘稳定可用（用户确认）；但之后 `WAKE` 叫不回（70 s+ 不回连），只能用户在设置→蓝牙手工点。
    （一次在 iOS 刚放弃 ~1 分钟内切 parkadv=1+WAKE 3 s 就回连了，复测不成立，不可靠。）
  - 结论：「随时能操作（自动回连）」和「空闲放开屏幕键盘」在 iOS 上互斥；**用户选 parkadv=1**。iOS 蓝牙设置里没有「断开连接」，只有「忽略此设备」。
  - 10-27 的「被踢后 iOS 不会自动重连」是在 appearance=0（不以键盘身份广播）时得出的；以 0x3C2 广播时 iOS 会积极回连。
- **配对坏了的修法（第二次遇到）**：症状 iPhone 蓝牙里看得到但连不上 / 连上后 HID 输入全被无视；`CFG unbond all` 永远 `removed=1 left=1`。
  `esptool erase-region 0x9000 0x5000` 擦 NVS → 串口 `CFG token <原口令>`（其余配置回默认）→ iPhone「忽略此设备」→ 关开一次 iPhone 蓝牙 → 设置→蓝牙「其他设备」里点 HIL-Bridge 配对。修好后指针/键盘真机通过。
- **工具层**：删了 `link.set_adv_mode` / `listen_and_wait`，换成 `link.wait_iphone`（WAKE→轮询 ST→过 4 s 没连上就提示用户去设置里点）。
  `hil up`/`listen` 经链路守护进程 WAKE 并等连上；`hil normal` 发 SLEEP 后 2 s 复查，iPhone 回连了就照实说、给出放键盘办法；
  `hil stop`/守护进程退出前经同一条链路发 SLEEP（不再开串口、不再改 appearance/RESTART）；`link_server` 打开链路时恢复发 WAKE。
- **已知问题**：BLE 守护进程连上板子后约 30 s，macOS 看到 HID 外观会试着自动配对（板子日志 `auth enc=0 bonded=0`），这段指令通道卡顿/断一次，
  `hil up` 冷启动因此约 36~40 s（已做成等过去）。appearance=0 时没有这事，但那样 iPhone 看不到板子。没找到不靠 hack 的根治办法。
- 坑：macOS 上 CH340 每次开串口都会复位板子（`POWERON_RESET`），`--link direct` 会让 iPhone 断一下。
- 板子 NVS 擦过一次：如果以后发现 `hil.json` 的 `ble_token` 对不上，用串口 `ST` 看 `token=`。

## 2026-10-27 晚（已被 10-29 取代，留作历史）：连接/放键盘的最终方案 = 「软件开侦听 + 用户手工点」，本文下面关于「WAKE 后 iPhone 1~3s 自动连回来」的说法已作废
真机验证结论：**板子被 parked 踢开后，iOS 不会因 `WAKE` 自动重连**（BLE 外设不能强迫 iPhone 来连；板子主动断过后 iOS 进重连退避，怎么广播都勾不回来，实测 WAKE 后 90s 不回连、正常模式手工点也连不上）。可靠连接只有一条路：板子**以蓝牙键盘身份广播**（`CFG appearance 0x3C2`，平时用 `appearance=0` 防 macOS 抢），iPhone 才在设置→蓝牙可见，由用户**手工点 HIL-Bridge**。
已纯工具层实现并真机端到端验证（没改固件）：`hil up`（iPhone 没连时）/ `hil listen` 经串口切侦听模式并提示用户手工点；`hil normal`/`hil stop`/空闲退出切回正常模式(`appearance=0`)+SLEEP，松开 iPhone、屏幕键盘恢复。连上后侦听模式 hidAlwaysOn 永不中途踢。改的文件：`hil/link.py`(`set_adv_mode`/`listen_and_wait`)、`hil/cli.py`(`listen`/`normal` + 改写 up/stop)、`hil/link_server.py`(退出切正常、去掉 open 时的 WAKE)。另：之前 iPhone 连不上还因为反复刷固件把 NVS 配对区搞坏（`unbond all` 永远 removed=1 left=1），已 `esptool erase-region 0x9000 0x5000` 擦 NVS、写回 token、干净重配修复。全量交接见 `docs/IOS_RECONNECT_HANDOFF.md`。

## 一句话
Mac 取帧 + Mac↔ESP32 协议/串口 + ESP32-S3 HID 固件 + 反馈式定位闭环，四部分都搭好、**全部用桩/仿真跑通**；
真机部分（iPhone 画面、ESP32 板子、iOS 蓝牙配对）等硬件到了做联调。

## 已验证 ✅
- 取帧：`HIL Capture.app` 独立进程 + HTTP 服务；用 Mac 摄像头验证了整条管线（fresh 帧 ~35 ms、最新帧 ~8 ms、按指定格式出帧）。相机权限已授予该 App（TCC status=3）。
- iOS 屏幕采集设备的开关（CoreMediaIO AllowScreenCaptureDevices）已置位，枚举逻辑按 muxed + modelID 含 "iOS" 找设备，插上 iPhone 应直接出现（**未真机验证**）。
- 协议 + 串口发送 + 回环桩：`hil/protocol.py` / `hil/link.py`，`FirmwareModel` 是固件的 Python 参考实现，17 个 pytest 全过。
- 固件：`firmware/hid_bridge/hid_bridge.ino` BLE 版和 USB 版都用 esp32 core 3.3.12 + NimBLE 2.5.2 编译通过，二进制在 `firmware/build/ble/`（含 merged.bin）。
- 定位闭环：仿真（含加速度、噪声、自动隐藏、死区、红色干扰元素）12 目标 × 多种子全部命中，平均误差 2–3 px，4–6 次迭代。

## 2026-10-26 晚 真机取帧第一次尝试
- iPhone（modelID "iOS Device"，muxed）插上后在新进程里能枚举到；常驻服务看不到 → 原因是主线程没跑 run loop，设备插拔通知不派发。已改：主线程跑 NSRunLoop，HTTP 在后台线程。
- 打开设备时它只报一个 0x0 @ 0fps 的格式，session 跑着但 11 s 没有任何帧；几分钟后设备从所有进程里消失（USB 上仍在）。判断是 **iPhone 锁屏了**：锁屏时屏幕采集设备会被撤掉/不出流。待用户解锁后重验。
- 已加：不强求 BGRA（iOS 设备多半给 420v，收到什么转什么，支持 420v/420f/2vuy/yuvs）；session 通知和连接状态放进 /status 的 diag；打开 20 s 没帧自动重开。

## 2026-10-26 晚 真机取帧 ✅ 已验证
- 用户解锁 iPhone 后，服务的看门狗自动重开设备并出帧（不用人干预）。
- 画面：1284×2778，BGRA（输出支持 BGRA 转换，可用格式 2vuy/yuvs/420v/420f/BGRA），到达 59 fps；fresh 帧平均 19 ms、最新帧 14 ms；半尺寸 PNG 编码约 70–100 ms。画面清晰，文字原分辨率可读。
- iOS 设备不报格式（0x0 @ 0 fps），别去设 activeFormat；/status 的 width/height 改为取首帧尺寸。
- **锁屏 = 设备消失**：锁屏后屏幕采集设备从 Mac 上撤掉，解锁后自动回来。使用期间自动锁定设「永不」。
- **状态栏是假的**：USB 录屏时 iOS 把状态栏替换成演示样式（9:41、满格信号、满电）。大脑别信截图里的时间/电量/信号。

## 2026-10-26 晚 ESP32 刷机 ✅
- 到手的是**经典 ESP32（ESP32-D0WD-V3，4MB，CH340）**而非 S3；`firmware/build.sh esp32` 新增该目标（FQBN `esp32:esp32:esp32:PartitionScheme=huge_app`，`Serial`=UART0 即 CH340 口）。
- 刷写：merged.bin 从 0x0 写，921600 波特率会"Unable to verify flash chip connection"，460800 成功。
- 自检通过：PING/ST 正常；未配对时 MV/CK/KP/TX/CC 返回 `ERR not connected`，RL/CFG 可用。
- `SerialLink` 现在忽略一切非 OK/ERR 行（ROM 启动日志等），PING 最多等 4 秒（CH340 开串口会复位板子）。
- 下一步：iPhone 开 AssistiveTouch → 蓝牙配对 HIL-Bridge → `send "MV 80 80"` 看指针动 → pointer-sample → calib → tap。

## 2026-10-26 晚 真机联调 ✅（指针/点击/键盘/Home 全通）
- **BLE 配对成功**，固件 0.1.1。关键参数：`CFG mvdelay 25`（≥ BLE 连接间隔，报告不排队；8 ms 时 park 的几十条报告会在协议栈里排队，观察到的全是旧位置）。
- **串口守护进程** `hil/link_server.py`（127.0.0.1:8766）：CH340 板每次开串口都复位板子（POWERON_RESET），所以串口只开一次常驻，CLI 走 HTTP（`HttpLink`，没起会自动拉起）。每条 CLI 命令约 0.2 s。
- **指针**：iOS 彩色指针是「彩色圆环 + 灰色半透明中心」，默认灰色指针采不到颜色；用户已设红色。识别：阈值后把外轮廓填实再判圆（`fill_holes`），采样按圆环取饱和像素。主屏上红图标无误检。
- **标定（可完全重复）**：每条报告 count→像素：2→1.1、4→7、12→31、32→151、64→328、127→453（iOS 加速度，127 反而比 64 低）。增益表在 hil.json。
- **逼近**：等指针停住（连续两帧位置一致）再读位置；5 个图标全部 1.7–5.4 px 命中，3–8 次迭代，0.7–4.2 s。
- **点击**：从主屏 tap「设置」图标 → 设置 App 打开 ✓。**Home**：`CC HOME` ✓。**Spotlight**：`KP GUI+SPACE`（有时第一下不响应，要检查画面再按一次）。
- **键盘**：固件 0.1.0 的标点表因字符串转义错位一格（空格→/、/→.），0.1.1 改成显式配对表（待重新配对后复验）。`KP spec n` 连按；`clear_field()` 清输入框。
- **输入法**：iPhone 当前是中文拼音，字母进拼音合成、数字选候选。ASCII 输入前要切英文键盘（`KP CTRL+SPACE` 轮换，需看画面确认），或者用 paste。
- **通用剪贴板粘贴未成功，原因已定位：Mac 的「接力」是关的**（`defaults -currentHost read com.apple.coreservices.useractivityd` → ActivityAdvertisingAllowed=0 / ActivityReceivingAllowed=0）。要在 系统设置 → 通用 → 隔空播放与接力 打开「允许在这台 Mac 和 iCloud 设备之间使用接力」，iPhone 端 设置 → 通用 → 隔空播放与接力 → 接力 也要开。Mac 侧 pbcopy 的中文乱码已修（强制 UTF-8 locale）。
- 重新配对后复验：`x x/x-x=x.x 123!?` 原样打出，标点表修好了。Spotlight 搜索框里 Cmd+C/Cmd+V 没出内容、无系统弹框；Safari 里 Cmd+L 打开的是书签面板而不是地址栏，用指针点底部地址栏也没对上焦点（点到了分享按钮附近），本地复制粘贴**未验证**。等接力打开后用通用剪贴板直接测 Cmd+V 更有意义。
- **坑：整盘刷 merged.bin 会清 NVS**（绑定密钥 + CFG），iPhone 必须「忘记此设备」重新配对。更新固件只刷 0x10000 app 分区（build.sh --upload 已改）。
- **状态栏在录屏时是假的**（9:41 满电）；iPhone 锁屏采集设备消失。

## 2026-10-26 晚 第一个真实任务 ✅
微信 → 通讯录 → 服务号 → 右侧字母索引 → 医院服务号 → 底部菜单「门诊服务」→「报告查询」→ 就诊人已绑定直接「查询」→ 列表 8 份报告 → 打开最新一份超声报告读到结论。
全程 10 次 tap，每次 3–7 次迭代，没有一次点偏；没有输入任何文字（靠列表索引栏绕开了中文搜索）。
经验：公众号找账号走「通讯录 → 服务号/公众号 → 右侧字母索引」最稳；H5 页面加载后再截图要等 3–4 s。

## 2026-10-26 晚 读完 8 份报告的经验
- 长页面翻页：**滚轮正方向（WH +n）有效、负方向不动**（原因待查：固件发的 wheel=-1 报告 iOS 没响应？用 `KP` 方向键或拖动代替）。拖动会带惯性，一拖就回顶部，适合"回到顶部"不适合精细翻页。
- 滚轮事件落在指针下方的元素上：指针在页面空白边缘时列表不滚，先 `goto` 到列表中间再滚。
- 报告列表的滚动位置在进出详情页之间会保留；每格滚轮约 86 px（半尺寸图）。
- 竖屏一屏 16 行表格，`wheel 6` 一步约 7.7 行，有重叠不漏行；横屏一屏只有 7 行。
- 手机被拿起会横竖屏切换，取帧尺寸随之变（1284×2778 ↔ 2778×1284），定位器按当前帧工作，但上一帧的坐标会失效，必须重新截图。
- 列表远不止 8 份（翻到 2025-02 还有），需要时按日期继续。

## 2026-10-27 Mac↔ESP32 改为 BLE ✅
- 固件 0.2.1：加 Nordic UART 风格指令服务（RX 写 / TX 通知），不绑定不加密，靠 `AUTH <token>`；口令存 NVS，串口 `ST` 的 token= 可见；BLE 收到的行进队列由 loop() 执行；回复按来源路由（串口/BLE）；两个连接并存（iPhone HID + Mac 指令），没满就继续广播。比 0.1.1 只多 4.4 KB flash / 336 B RAM。
- **macOS 自动配对接管坑**：Mac 的 CoreBluetooth 一连上带 HID 外观的设备，bluetoothd 就自己发起配对并把它登记成 HID 设备，板子那边鉴权后几秒被断。对策三条：广播外观改通用（appearance 默认 0，不再广播 HID UUID，改播 NUS UUID；iPhone 已绑定按地址重连不受影响）；`hidpeer` 锁定 HID 目标（第一次订阅鼠标报告的地址自动记住，即 iPhone 的蓝牙地址，其它设备订阅不理）；去掉"未加密就断开"。以后给**新** iPhone 配对时先串口 `CFG appearance 0x3C2` 重启、配完改回 0；换 iPhone 要 `CFG hidpeer clear`。
- Mac 端：`hil/ble_central.py`（CoreBluetooth）+ `hil/link_server.py --transport ble`，打包成 `~/Applications/HIL Link.app`。**CoreBluetooth 要求可执行文件本身在带 NSBluetoothAlwaysUsageDescription 的 bundle 里**，exec 出 .venv 的 python 会被 TCC 直接 SIGABRT（退出码 134，崩溃报告写得很清楚），相机那套壳的办法不够用；所以 `tools/launcher.c` 是一个原生启动器，dlopen libpython 在本进程里跑 `hil.link_server`（`tools/make_link_app.sh` 用 clang 编译并注入路径）。
- 固件 0.2.3 最终实测（2026-10-27 凌晨）：200 条命令零失败、零错位，中位 30 ms，95% 32 ms，最大 74 ms；闲置 3 分钟不掉线；两端计数对账一致（Mac 写 204 = 板子收 204 = 回复 204）。早些的一轮出现过 9 行重复回复，没复现，ST 里留了 blew/bler/blet 计数器（板子收到的写/行/发出的回复）和 /status 的 ble_writes/ble_notif/ble_lines/ble_extra 便于下次对账。
- 启动行为：守护进程 `/quit` 会正常断开；若上次是被 kill 的，系统里会残留旧连接（板子因此不广播），`BleLink._connect` 先取消残留再扫描，启动到可用约 2 s。
- Mac **没有**和板子配对（IOBluetooth 已配对列表里没有它；系统蓝牙列表里出现只是因为守护进程连着）。守护进程有 `/unpair?name=HIL-Bridge` 可在 App 进程里删配对记录（在 Claude 宿主下跑 IOBluetooth 会被 TCC 杀）。
- Mac 端回调全部包了异常（`_safe`）：pyobjc 回调里漏出的 Python 异常会变成 NSException 把进程 abort。
- 串口路径保留：hil.json `link_transport` 改回 serial 即可。ESP32 现在可以不插 Mac，用充电头供电。
- 2026-10-27 早 亮屏验证 ✅：经 BLE 发 MV 指针动（帧差 6k/12k 像素）；经 BLE tap 主屏「设置」误差 4.7 px、4 次迭代、1.3 s，设置 App 打开。
- 夜里发现的坑：Mac 睡眠时蓝牙关闭（central state 4/5 来回），连接失效但 `_ready` 没清，守护进程以为还连着，命令全部超时。已修：state≠PoweredOn 即标记断开，主循环在蓝牙恢复后重连。

## 2026-10-27 按需启停 + skill ✅
- 两个守护进程都加了空闲自动退出：取帧服务以 /frame、/reopen 为活动，链路守护进程以 /cmd 为活动，空闲 `idle_quit_min`（hil.json，默认 10）分钟退出；实测 0.15 分钟阈值下 9 s 后双双退出。任何 CLI 命令会重新拉起（取帧约 2 s、BLE 约 2 s）。资源本身很小（合计 ~30 MB、CPU≈0），主要是省 iPhone 的电：取帧在跑时 iPhone 一直在做镜像输出。
- `hil up`：拉起两个服务并等它们就绪，报告画面分辨率 / 固件版本 / iPhone HID 是否已连；`hil stop` 立刻停。链路守护进程刚起、BLE 还没连上时收到命令会等最多 25 s 而不是直接报错。
- skill：`~/.claude/skills/iphone-hil/SKILL.md`（用户级，任何会话可用），写了前提、命令、节奏、已知限制和安全边界。

## 2026-10-27 开源发布
- 公开仓库 github.com/ghbhiee/iphone-hil。入库前脱敏：`hil.json`（含 BLE 口令、标定）改为 gitignore，模板是 `hil.example.json`；`captures/` 不入库；两个 App 壳的 bundle id 改成中性的 `com.iphone-hil.capture` / `com.iphone-hil.link`（**改 id 等于换了一个 App，macOS 会重新弹一次相机 / 蓝牙授权框**）；skill 复制到 `skill/iphone-hil/`（路径写成 `~/iphone-hil`，本机 `~/.claude/skills/iphone-hil` 那份仍指向实际目录）。
- `docs/PROTOCOL.md` 按固件 0.2.3 重新生成（AUTH、ST 字段、CFG 子命令）；`firmware/FLASH.md` 改为以经典 ESP32 为主，`firmware/prebuilt/esp32/` 放经真机验证的二进制（刷写偏移 0x1000/0x8000/0xe000/0x10000，来自 arduino 的 flash_args）。
- README 重写：背景（中文互联网只剩 App 入口、浏览器/镜像/cookie 各条路为什么不通）、两条官方通道（USB 采集、辅助触控指针设备）、闭环定位、安装、skill、安全边界。

## 2026-10-27 傍晚 空闲放开 iPhone 的最终设计（固件 0.3.2）✅ 固件/BLE 级已验，键盘可视化待手机解锁

- **用户反馈（0.3.1 不行）**：在叮咚买菜那个 session 实测，ESP32「自己会挂掉、无法唤醒、手机连不上」。根因就是 0.3.1 的设计缺陷：
  空闲后**停止广播**——守护进程退出（空闲 10 min 自动退）后板子对外不可达，新守护进程连不上、iPhone 也看不到，只能断电重启。
  用户明确：超时断开只是「把不用的蓝牙连接断掉让键盘恢复」，**不是让板子把自己杀死**。
- **0.3.2 设计**：空闲 hididle 秒 → 断开 iPhone 的 HID（键盘恢复），**板子始终继续广播、始终可被 Mac/用户连上**。
  这个状态叫 parked。parked 期间 iPhone 若自己连回来（onSubscribe），loop() 里把它再断开（`g_evt_dropHid`/`g_dropHidConn`），键盘才一直在。
  要操作手机：`WAKE`/HID 命令退出 parked，之后接受 iPhone 连接；iPhone 解锁时 1~3 s 自动连回来，没连就提示用户去设置→蓝牙点一下。
  `updateAdvertising` 只在「两个连接都占满」时 off，其余一律 pub。版本 0.3.2。
- **已验证（固件/BLE 级，不需要手机解锁）**：
  - `SLEEP`（parked）后 `ST` 仍 `adv=pub`（0.3.1 是 `adv=off`）——板子没把自己变不可达 ✅。
  - 守护进程 `/quit` 后重启，新守护进程照样连上 parked 的板子（拿到完整 ST，fw=0.3.2）✅——不再「挂掉」。
  - esp32 + usb 两个变体都编译通过；17 个 pytest 全过；`firmware/prebuilt/esp32/` 已更新为 0.3.2，板子已刷。
- **待验证（需要 iPhone 解锁 + 自动锁定设「永不」）**：parked 时键盘是否真的一直在（iPhone 自动回连被板子踢掉，会不会反复重连导致键盘闪烁）；
  `WAKE` 后 iPhone 是否 1~3 s 连回来。这次本想验，但手机自动锁屏了（取帧拿不到帧、iOS 锁屏也不自动重连 HID），只能等解锁再看。
  注意：iPhone 锁屏时 iOS 不会自动重连蓝牙 HID——所以「WAKE 后没连上」最常见的原因就是手机锁着。
- **纠正 0.3.1 HANDOFF 的误判**：早先「iOS ≥60 s 后永不自动重连」是错的（那些失败样本都是空闲时用随机地址广播搞坏了 iOS 重连）。
  0.3.2 用公开地址一直广播，解锁状态下 iPhone 正常自动回连（本会话早些时候实测 2.8 s）。
- **Mac 端 / CLI / skill**：守护进程启动发 WAKE、`ERR not connected` 时 WAKE+轮询+重发（`hid_wake_timeout_s`）；
  `hil up` 和命令未连时提示「解锁 iPhone 并到设置→蓝牙点一下」，连不上板子才提示查供电/断电重启（现在很少见，因为板子一直广播）。
  两份 skill 都加了**开工前必须确认 iPhone HID 已连、没连就原话提示用户手动去设置→蓝牙点一下**的硬步骤。
- **顺带修复**：BLE ST 分支和 `handleBleLine` 补了 `#if HID_BACKEND==1`，usb 变体（0.2.1 起坏的）恢复编译。
- **build.sh**：`curl /quit || true`（守护进程没跑时别被 set -e 跳过刷写）。配对新 iPhone 仍：`CFG appearance 0x3C2` 重启→配对→`CFG appearance 0` 重启。
- **残留**：NVS 旧键 `sleepmode=1`（当前固件不读，无害）；`hil up` 偶发「固件 None」（刚连上那一下 ST 还没拿全，不复现、不影响功能）。

## 2026-10-27 下午 通用剪贴板粘贴实测（接力已开）

用户把 Mac 和 iPhone 的「接力」都打开后，在 Spotlight 搜索框里测 `paste`。先后排除了三件事，结论都写进了 README §11/§12、协议文档和 skill。

- **第一个坑是组合键大小写。** 固件 `parseKeySpec` 对组合键里的单字母走 `asciiToUsage`，大写字母带 Shift 位，`KP GUI+V` 发出去是 ⌘⇧V（粘贴并匹配样式），Spotlight 里毫无反应；Python 端 `parse_key_spec` 把 A–Z 放进了 KEY_NAMES、不带 Shift，两边的镜像一直不一致，测试全绿但真机不对。修法：Mac 端 `enc_key/enc_key_down/enc_key_up` 先 `normalize_key_spec`（组合键里的字母转小写），固件 `parseKeySpec` 组合键里的字母不区分大小写、不隐含 Shift（随 0.3.0 一起刷）。
- **第二个是通用剪贴板的时序。** 隔离试验：`type 123` → ⌘a → ⌘x 后搜索框清空、**Mac 的 pbpaste 立刻变成 123**（iPhone→Mac 即时同步）→ ⌘v 又把 123 贴回来（本地剪贴板路径没问题）。反方向：Mac `pbcopy mac8` → 等 4 s → ⌘v → 搜索框出现 `123 mac8`，**Mac→iPhone 通了**。失败的几次分别是：复制后只等 2 s（iPhone 还没收到 Mac 的广播，⌘V 落空、什么都不粘）、复制已超过 2 分钟（通用剪贴板过期，iPhone 用回本地剪贴板）。所以 `paste_text` 默认 settle 改 5 s，每次重新 pbcopy，CLI 加 `--settle`；调用方粘贴后截图确认，没出来隔 3 s 再发一次 `key GUI+v`。
- **第三个不是坑，是撞车。** 中文那一轮正要验，板子被另一个会话（「板子空闲时断开 iPhone」，固件 0.3.0）刷写重启，⌘a 报 BLE 超时，随后截到的画面已是别人在操作。等它把板子设回常连（hididle 0、用户在设置→蓝牙里点了一下 HIL-Bridge）后复验：`paste "你好世界"`（pbcopy → 5 s → ⌘v）粘进 Spotlight 成功，之后 `type 123` 得到「你好世界 123」，⌘a 全选、⌘v 用本地剪贴板覆盖都正常——**中文输入这条路通了**。（板子断开/重连的最终设计见上一节 0.3.1：空闲后停广播，下次命令自动叫醒、iPhone 1~3 s 连回来。）
- 一个没解释的现象：两次 `hil paste`（pbcopy + ⌘v）之后 1 s 内 Spotlight 都被收起回到了主屏（内容已经粘进去，再点搜索还带着上次的词）；而单发 `key GUI+v` 用本地剪贴板粘贴时 Spotlight 不收起。可能是用户当时在手机上按了 Home，也可能与远程粘贴有关，没有再追。粘贴后截图若发现回了主屏，先别重发，再点搜索看内容在不在。
- 顺带观察：测试中 iPhone 自己锁了一次屏（辅助触控的指针/按键活动似乎不算「用户活动」，不阻止自动锁定），skill 里「自动锁定改永不」这一条确实是硬要求；锁屏后采集设备没消失，取到的是锁屏画面。`⌘a` + `BACKSPACE` 清空搜索框后 Spotlight 自己收起了一次，之后 ⌘v 落在主屏上没反应——清空用右侧 ⊗ 或直接 ⌘a 后粘贴覆盖更稳。
- 截图含个人数据（撞车时截到的页面），已按 skill 规则删除。
