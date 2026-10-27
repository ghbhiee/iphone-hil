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
    ST                   → OK conn=0|1 backend=ble|usb name=<蓝牙名> mvdelay=<ms> typedelay=<ms> txfail=<n> fw=<版本> via=serial|ble hidpeer=<地址|-> ...
                           串口上多一个 token=<口令>；BLE 上多 blew/bler/blet（收到的写入 / 行 / 发出的回复计数，用于对账）
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
                         | name <蓝牙名> | token <口令>（仅串口）| hidpeer <地址>|clear（锁定/清除 HID 目标）| unbond others|all（删蓝牙绑定）
                         name / appearance 改完要重启生效；所有配置存 NVS，掉电保留
非 ASCII 文本（中文）目前没有可靠输入路径：通用剪贴板（Mac pbcopy → iPhone Cmd+V）依赖两端都开「接力」。
```
