# Mac ↔ ESP32 指令协议

（自动从 `hil/protocol.py` 的文档字符串生成；改协议改那里，固件 `firmware/hid_bridge/hid_bridge.ino` 的解析器与之一一对应。）

```
Mac ↔ ESP32 指令协议（唯一定义处；固件 firmware/hid_bridge 里的解析器与此一一对应）。

传输：USB 串口（115200）或 BLE 指令服务（Nordic UART 风格：RX 特征写入、TX 特征通知；连上后先 AUTH <token>）。
形式：ASCII 文本行，'
' 结尾，字段空格分隔。每条命令固件回一行：
    OK [信息]      或      ERR <原因>
命令（大小写不敏感）：
    AUTH <token>         BLE 通道专用：口令校验，没过之前其它命令一律 ERR（串口不需要；口令在串口 ST 回复的 token= 里）
    PING                 → OK PONG
    ST                   → OK conn=0|1 hid=on|waking|idle hididle=<秒> idle=<秒> conns=<n> adv=pub|off backend=ble|usb
                              name=<蓝牙名> mvdelay=<ms> typedelay=<ms> txfail=<n> fw=<版本> via=serial|ble hidpeer=<地址|-> ...
                           conn=1 即 iPhone 的 HID 连着；hid=waking 正在向 iPhone 广播等它连回来；hid=idle 空闲中已放开 iPhone；
                           idle= 距上次活动（需要 HID 的命令 / WAKE）的秒数；conns= 当前连接数；adv= 当前广播（off = 满了或断开后的静默期）。
                           串口上多一个 token=<口令>；BLE 上多 blew/bler/blet（收到的写入 / 行 / 发出的回复计数）
    WAKE                 → OK wake conn=0|1 hid=...  保证板子在广播并重置空闲计时（iPhone 只在断开后约 60 s 内会自动连回来）
    SLEEP                → OK sleep   立刻断开 iPhone 的 HID 并停止广播（屏幕键盘马上恢复）；hididle=0 或配对模式下 ERR
    RESTART              → OK restart 硬复位板子（等于断电重启，绑定和配置都在）
    MV dx dy             相对移动指针（任意整数；固件拆成若干 ±127 的 HID 报告，间隔 mvdelay ms）
    BT <l|r|m> <down|up> 鼠标键按下 / 抬起
    CK [l|r|m] [n]       点击 n 次（默认左键 1 次）
    LP <ms> [l|r|m]      长按 ms 毫秒
    WH <n>               滚轮（iOS 上正数向下翻页；负数在实测中无效，待查）
    KP <spec> [n]        按键：按下+抬起 n 次（默认 1）。spec = [修饰+...]键名，如 ENTER、GUI+H、CTRL+ALT+DEL、a、SPACE
    KD <spec> / KU <spec> 按下 / 抬起（组合键保持用）
    TX <text>            输入 ASCII 文本（整行剩余部分；支持 
 	 \ 转义；非 ASCII 字符会 ERR）
    CC <name>            Consumer 键：HOME BACK VOLUP VOLDN MUTE PLAY（iOS 对 AC Home=回主屏）
    RL                   释放所有键和鼠标键
    CFG <k> <v>          mvdelay <ms> | typedelay <ms> | appearance <hex>（0=通用，0x3C2=HID 鼠标外观，仅配对新 iPhone 时临时用）
                         | hididle <秒>（默认 120：这么久没有需要 HID 的命令就断开 iPhone；0=一直连着）
                         | name <蓝牙名> | token <口令>（仅串口）| hidpeer <地址>|clear（锁定/清除 HID 目标）| unbond others|all（删蓝牙绑定）
                         name / appearance 改完要重启生效；所有配置存 NVS，掉电保留
HID 空闲（固件 ≥0.3.0）：iOS 只要连着一个蓝牙键盘就把所有 App 的屏幕键盘藏起来，所以板子不能一直挂在 iPhone 上：
    hididle 秒（默认 120）没有需要 HID 的命令（MV BT CK LP WH KP KD KU TX CC）就断开 iPhone（绑定保留）并停止广播，
    直到收到 WAKE 或需要 HID 的命令（来自串口或已连着的 Mac 指令通道）才恢复广播，iPhone 通常 1~3 s 自动连回来。
    上电时正常广播。需要 HID 的命令或 WAKE 都算活动、重置计时；iPhone 没连着时这些命令回 ERR not connected，
    Mac 端守护进程（hil.link_server）会等 conn=1 再重发一次，等不到就提示用户到 iPhone 设置→蓝牙点一下 HIL-Bridge。
    空闲且 Mac 也没连着时板子对外没有广播，新起的守护进程连不上：给板子断电重启即可。
    PING / ST / CFG / RL 不算活动。appearance≠0（配对模式）时不睡。
    iOS 硬约束：配件自己断开后 iOS 只扫描几十秒就放弃自动重连（任何广播都无效），所以断开后再用要用户到 iPhone 设置→蓝牙 点一下 HIL-Bridge；
    板子断开后静默 90 s 不广播就是为了让 iOS 放弃（否则 60 s 内它又连回来，键盘又没了）。
非 ASCII 文本（中文）走通用剪贴板：Mac pbcopy → iPhone 上 KP GUI+v（见 link.paste_text）。两端都要开「接力」；
    复制后等 4~5 s 再按 ⌘V（iPhone 收到 ⌘V 才去拉 Mac 的剪贴板，且要先收到广播），Mac 上的复制 2 分钟后过期。
组合键里的字母写小写：固件 ≤0.2.3 把大写字母当 Shift+字母，`KP GUI+V` 会变成 ⌘⇧V（粘贴并匹配样式）而不是 ⌘V；
    Mac 端 enc_key 已自动转小写，固件 0.3.0 起组合键里的字母也不再隐含 Shift（要 Shift 显式写 SHIFT+）。
```
