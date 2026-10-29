# Mac ↔ ESP32 指令协议

（自动从 `hil/protocol.py` 的文档字符串生成；改协议改那里，固件 `firmware/hid_bridge/hid_bridge.ino` 的解析器与之一一对应。）

```
Mac ↔ ESP32 指令协议（唯一定义处；固件 firmware/hid_bridge 里的解析器与此一一对应）。

传输：USB 串口（115200）或 BLE 指令服务（Nordic UART 风格：RX 特征写入、TX 特征通知；连上后先 AUTH <token>）。
形式：ASCII 文本行，'\n' 结尾，字段空格分隔。每条命令固件回一行：
    OK [信息]      或      ERR <原因>
命令（大小写不敏感）：
    AUTH <token>         BLE 通道专用：口令校验，没过之前其它命令一律 ERR（串口不需要；口令在串口 ST 回复的 token= 里）
    PING                 → OK PONG
    ST                   → OK conn=0|1 hid=on|waking|idle hididle=<秒> parkadv=0|1 idle=<秒> conns=<n> adv=pub|off appear=0x<hex> backend=ble|usb
                              name=<蓝牙名> mvdelay=<ms> typedelay=<ms> txfail=<n> fw=<版本> via=serial|ble hidpeer=<地址|-> ...
                           conn=1 即 iPhone 的 HID 连着；hid=waking 正在向 iPhone 广播等它连回来；hid=idle 空闲中已放开 iPhone；
                           idle= 距上次活动（需要 HID 的命令 / WAKE）的秒数；conns= 当前连接数；adv= 当前广播（off 仅在两个连接都占满时）。
                           串口上多一个 token=<口令>；BLE 上多 blew/bler/blet（收到的写入 / 行 / 发出的回复计数）
    WAKE                 → OK wake conn=0|1 hid=...  退出 parked、以 HID 广播并重置空闲计时（iPhone 没连着时不一定自己回连，
                           回不来就让用户在 iPhone 设置→蓝牙 点一下 HIL-Bridge；工具层 hil up/listen 会提示）
    SLEEP                → OK sleep   立刻断开 iPhone 的 HID 进入 parked（板子仍广播，方式看 parkadv）；hididle=0 时 ERR
    RESTART              → OK restart 硬复位板子（等于断电重启，绑定和配置都在）
    MV dx dy             相对移动指针（任意整数；固件拆成若干 ±127 的 HID 报告，间隔 mvdelay ms）
    BT <l|r|m> <down|up> 鼠标键按下 / 抬起
    CK [l|r|m] [n]       点击 n 次（默认左键 1 次）
    LP <ms> [l|r|m]      长按 ms 毫秒
    WH <n>               滚轮（iOS 上正数向下翻页；负数在实测中无效，待查）
    KP <spec> [n]        按键：按下+抬起 n 次（默认 1）。spec = [修饰+...]键名，如 ENTER、GUI+H、CTRL+ALT+DEL、a、SPACE
    KD <spec> / KU <spec> 按下 / 抬起（组合键保持用）
    TX <text>            输入 ASCII 文本（整行剩余部分；支持 \n \t \\ 转义；非 ASCII 字符会 ERR）
    CC <name>            Consumer 键：HOME BACK VOLUP VOLDN MUTE PLAY（iOS 对 AC Home=回主屏）
    RL                   释放所有键和鼠标键
    CFG <k> <v>          mvdelay <ms> | typedelay <ms> | appearance <hex>（默认 0x3C2=HID 鼠标外观，iOS 才在设置→蓝牙里列出它；0=不声明）
                         | hididle <秒>（默认 600：这么久没有需要 HID 的命令就断开 iPhone；0=一直连着）
                         | parkadv 0|1（默认 1：空闲时照样以键盘身份广播、接受 iPhone 自己连回来；0=空闲只播指令服务，iPhone 不回连）
                         | name <蓝牙名> | token <口令>（仅串口）| hidpeer <地址>|clear（锁定/清除 HID 目标）| unbond others|all（删蓝牙绑定）
                         appearance / parkadv / hididle 立刻生效（广播当场重建，不用重启）；name 的 GAP 名要重启；所有配置存 NVS，掉电保留
HID 空闲（固件 0.3.3）：iOS 只要连着一个蓝牙键盘就把所有 App 的屏幕键盘藏起来，所以板子不能一直占着 iPhone 的 HID：
    hididle 秒（默认 600）没有需要 HID 的命令（MV BT CK LP WH KP KD KU TX CC）就断开 iPhone 的 HID（绑定保留），进入 parked。
    **板子始终继续广播、始终可被 Mac/用户连上——不会把自己变成不可达。** parked 之后看 parkadv：
      parkadv=1（默认）：照样以键盘身份广播，iPhone 回连就接受（实测 0.3 s 内回连）→ 随时能用，但屏幕键盘基本一直被占着；
      parkadv=0：只播指令服务，iPhone 回连就踢（约 6 s 后 iOS 放弃）→ 屏幕键盘稳定可用，但之后 WAKE 叫不回 iPhone，
                 要用时得让用户在 iPhone 设置→蓝牙 点 HIL-Bridge。两者在 iOS 上互斥。
    需要 HID 的命令或 WAKE 都算活动、重置计时；iPhone 没连着时这些命令回 ERR not connected，
    Mac 端守护进程（hil.link_server）WAKE 后等 conn=1 再重发一次，等不到就提示用户到 iPhone 设置→蓝牙 点一下 HIL-Bridge
    （iOS 锁屏时不回连蓝牙 HID）。PING / ST / CFG / RL 不算活动。hididle=0 时不进 parked（一直连着）。
非 ASCII 文本（中文）走通用剪贴板：Mac pbcopy → iPhone 上 KP GUI+v（见 link.paste_text）。两端都要开「接力」；
    复制后等 4~5 s 再按 ⌘V（iPhone 收到 ⌘V 才去拉 Mac 的剪贴板，且要先收到广播），Mac 上的复制 2 分钟后过期。
组合键里的字母写小写：固件 ≤0.2.3 把大写字母当 Shift+字母，`KP GUI+V` 会变成 ⌘⇧V（粘贴并匹配样式）而不是 ⌘V；
    Mac 端 enc_key 已自动转小写，固件 0.3.0 起组合键里的字母也不再隐含 Shift（要 Shift 显式写 SHIFT+）。
```
