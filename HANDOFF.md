# HANDOFF — iphone-hil（开发日志，2026-10-26 起）

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

## 未验证 / 待做 ⏳
1. ~~标点/空格输入~~ 已验证。**粘贴这条路用户决定先放一放（2026-10-26）**：Mac 接力未开，通用剪贴板不同步；本地 Cmd+C/Cmd+V 未验证。中文输入暂时没有可用方案，ASCII 用 `type`（先切英文键盘）。以后要做再按上面的接力步骤来。
2. 长按、拖动（滑动翻页）、滚轮在真机上的效果；`tap` 命令行形式（`python -m hil tap X Y`）端到端。
3. 自动隐藏指针建议关掉（现在开着，靠微动找回）。
4. 测试机 iPhone 上还开着「增强对比度」，指针外观可能变化，识别阈值若失效用 `pointer-sample` 重采。

## 旧的未验证清单（已过时）
1. ~~iPhone 插 USB 后取帧~~ 已验证（见上）。锁屏时不出帧（设备消失），解锁自动恢复。
2. ~~ESP32 真板串口、PING~~ 已验证。待验：BLE 在 iPhone 上配对（Just Works）、指针是否出现（需 AssistiveTouch 开）、键盘/Consumer HOME 是否生效、连接间隔是否被接受。
3. 指针识别阈值：真机指针颜色/大小（`python -m hil pointer-sample 帧.png x y` 从截图采样写入 `hil.json`）。
4. 标定：`python -m hil calib`，看增益表合不合理；`hil.json` 里 `loc.settle`（发完 MV 等画面刷新的秒数）真机估计 0.08–0.15。
5. 通用剪贴板 `paste` 的同步延迟（现在等 1.5 s）。

## 真机联调步骤（按顺序）
1. iPhone 接 Mac → `python -m hil status` 看 device 变成 iPhone → `python -m hil grab` 看图清不清楚。
2. 刷固件（`firmware/FLASH.md` 方法 A）→ `python -m hil send PING`。
3. iPhone 设置：AssistiveTouch、配对 HIL-Bridge、指针颜色/大小、关自动隐藏和指针动画（FLASH.md 末节）。
4. `python -m hil send "MV 50 50"` 看指针动；`grab` 后 `pointer-sample` 采样指针颜色；`find --debug` 确认只识别出指针。
5. `park` → `calib` → `tap X Y` 试点一个按钮；按需调 `hil.json` 的 `loc.settle` / `tol` / `max_counts`。
6. `type` / `paste` / `key GUI+H` / `cc HOME` 各试一次。

## 关键决定与坑
- **HID 走 BLE 而不是 USB**：iPhone 只有一个口，被 Mac 录屏占了；USB HID 版固件留着（改用 AirPlay 取画面时可用）。
- **取帧必须独立进程**：Claude Code 的宿主 `com.anthropic.claude-code` 是 hardened runtime 且无 `com.apple.security.device.camera` entitlement，子进程申请相机权限被 TCC 直接拒绝（不弹框、状态停在 notDetermined）。iPhone 屏幕采集设备和摄像头同一道权限。所以做了 `HIL Capture.app`（`tools/make_app.sh`），`open -g` 启动后父进程是 launchd。
- **分辨率**：macOS 下 session 在 `startRunning` 前会把设备 `activeFormat` 改回配合 preset 的（High=1080p），`InputPriority` preset 在 macOS 上 `canSetSessionPreset` 返回 NO。解法同 ffmpeg：`lockForConfiguration` 后设 activeFormat **不解锁**，`startRunning` 之后再解锁。
- **pyobjc 回调里的 CMSampleBuffer 是裸指针**（PyObjCPointer），没桥接成 CF 对象；帧处理全程用 ctypes（CMSampleBufferGetImageBuffer / CVPixelBufferRetain / Lock / GetBaseAddress / Release）。
- **`/quit` 必须真退出进程**，否则 `open` 第二次会超时（-1712）且旧代码一直在跑。
- **arduino-cli 的 ctags 是 x86_64**，本机没 Rosetta；`firmware/build.sh` 用 `runtime.tools.ctags.path` 指到空壳 stub。固件源码保持"先定义后使用"。装 Rosetta 后 stub 自动不用。
- 指针识别别用"填充率×长宽比"当圆度：ROI 把矩形截成正方形时会误判。现在用填充率 ∈ [0.6, 0.9]（圆 0.785）+ 周长圆度，并且最小面积要按真实指针定。
- 定位：没标定时前两步最多 100 counts 先学增益；指针贴屏幕边缘时不学增益；连续 2 次发了 ≥3 counts 不动 → 复位。

## 用户侧待办
- iPhone 接 Mac 试取帧（随时可做）。
- ESP32-S3 板子到了刷固件；iOS 侧设置见 `firmware/FLASH.md`。
- 可选：装 Rosetta（Arduino IDE 在 Apple Silicon 也需要它）。
