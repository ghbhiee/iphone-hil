"""Mac ↔ ESP32 指令协议（唯一定义处；固件 firmware/hid_bridge 里的解析器与此一一对应）。

传输：USB 串口（115200）或 BLE 指令服务（Nordic UART 风格：RX 特征写入、TX 特征通知；连上后先 AUTH <token>）。
形式：ASCII 文本行，'\n' 结尾，字段空格分隔。每条命令固件回一行：
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
    TX <text>            输入 ASCII 文本（整行剩余部分；支持 \n \t \\ 转义；非 ASCII 字符会 ERR）
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
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

MOUSE_BUTTONS = {"l": 1, "r": 2, "m": 4}

# HID 键盘 usage id（US 布局）。固件里有同一张表。
KEY_NAMES = {
    "ENTER": 0x28, "RETURN": 0x28, "ESC": 0x29, "ESCAPE": 0x29, "BACKSPACE": 0x2A, "BS": 0x2A, "TAB": 0x2B,
    "SPACE": 0x2C, "MINUS": 0x2D, "EQUAL": 0x2E, "LBRACKET": 0x2F, "RBRACKET": 0x30, "BACKSLASH": 0x31,
    "SEMICOLON": 0x33, "QUOTE": 0x34, "GRAVE": 0x35, "COMMA": 0x36, "PERIOD": 0x37, "SLASH": 0x38,
    "CAPSLOCK": 0x39,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D, "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PRINTSCREEN": 0x46, "SCROLLLOCK": 0x47, "PAUSE": 0x48, "INSERT": 0x49, "HOME": 0x4A, "PAGEUP": 0x4B,
    "DELETE": 0x4C, "DEL": 0x4C, "END": 0x4D, "PAGEDOWN": 0x4E, "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52,
}
for _i in range(26):
    KEY_NAMES[chr(ord("A") + _i)] = 0x04 + _i
for _i, _c in enumerate("1234567890"):
    KEY_NAMES[_c] = 0x1E + _i

MODIFIERS = {"CTRL": 0x01, "CONTROL": 0x01, "SHIFT": 0x02, "ALT": 0x04, "OPTION": 0x04, "GUI": 0x08, "CMD": 0x08,
             "WIN": 0x08, "META": 0x08, "RCTRL": 0x10, "RSHIFT": 0x20, "RALT": 0x40, "RGUI": 0x80}

CONSUMER_KEYS = {"HOME": 0x0223, "BACK": 0x0224, "VOLUP": 0x00E9, "VOLDN": 0x00EA, "MUTE": 0x00E2,
                 "PLAY": 0x00CD, "NEXT": 0x00B5, "PREV": 0x00B6, "BRIGHTUP": 0x006F, "BRIGHTDN": 0x0070}

# ASCII → (需要 shift?, usage)。固件同表。
_SHIFT_MAP = {')': '0', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9',
              '_': 'MINUS', '+': 'EQUAL', '{': 'LBRACKET', '}': 'RBRACKET', '|': 'BACKSLASH', ':': 'SEMICOLON',
              '"': 'QUOTE', '~': 'GRAVE', '<': 'COMMA', '>': 'PERIOD', '?': 'SLASH'}
_PLAIN_MAP = {'-': 'MINUS', '=': 'EQUAL', '[': 'LBRACKET', ']': 'RBRACKET', '\\': 'BACKSLASH', ';': 'SEMICOLON',
              "'": 'QUOTE', '`': 'GRAVE', ',': 'COMMA', '.': 'PERIOD', '/': 'SLASH', ' ': 'SPACE', '\n': 'ENTER',
              '\t': 'TAB', '\x08': 'BACKSPACE', '\x1b': 'ESC'}


def ascii_to_usage(ch: str) -> tuple[int, int]:
    """返回 (modifier, usage)。不可输入的字符抛 ValueError。"""
    if ch in _PLAIN_MAP:
        return 0, KEY_NAMES[_PLAIN_MAP[ch]]
    if ch in _SHIFT_MAP:
        return MODIFIERS["SHIFT"], KEY_NAMES[_SHIFT_MAP[ch]]
    if "a" <= ch <= "z":
        return 0, KEY_NAMES[ch.upper()]
    if "A" <= ch <= "Z":
        return MODIFIERS["SHIFT"], KEY_NAMES[ch]
    if "0" <= ch <= "9":
        return 0, KEY_NAMES[ch]
    raise ValueError(f"字符 {ch!r} 不能用 HID 键盘直接输入（非 ASCII 请走剪贴板）")


def parse_key_spec(spec: str) -> tuple[int, int]:
    """'GUI+SHIFT+H' / 'ENTER' / 'a' → (modifier bits, usage)。只有修饰键（如 'GUI'）时 usage=0。"""
    mods, usage = 0, 0
    parts = [p for p in re.split(r"[+\-]", spec) if p != ""] if len(spec) > 1 else [spec]
    for p in parts:
        up = p.upper()
        if up in MODIFIERS:
            mods |= MODIFIERS[up]
        elif up in KEY_NAMES:
            if usage:
                raise ValueError(f"一次只能按一个非修饰键: {spec}")
            usage = KEY_NAMES[up]
        elif len(p) == 1:
            m, usage = ascii_to_usage(p)
            mods |= m
        else:
            raise ValueError(f"未知键名 {p!r}（见 protocol.KEY_NAMES）")
    return mods, usage


def escape_text(text: str) -> str:
    out = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "")
    if any(ord(c) > 126 or ord(c) < 32 for c in out):
        raise ValueError("TX 只能发可打印 ASCII；中文等请用 link.paste_text()（剪贴板→GUI+V）")
    return out


def unescape_text(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}.get(m.group(1), m.group(1)), s)


# ---------------- 编码（Mac 端发出的行，不含结尾换行） ----------------
def enc_ping() -> str: return "PING"
def enc_status() -> str: return "ST"
def enc_move(dx: int, dy: int) -> str: return f"MV {int(dx)} {int(dy)}"
def enc_button(btn: str, down: bool) -> str:
    assert btn in MOUSE_BUTTONS, btn
    return f"BT {btn} {'down' if down else 'up'}"
def enc_click(btn: str = "l", n: int = 1) -> str: return f"CK {btn} {int(n)}"
def enc_long_press(ms: int, btn: str = "l") -> str: return f"LP {int(ms)} {btn}"
def enc_wheel(n: int) -> str: return f"WH {int(n)}"
def normalize_key_spec(spec: str) -> str:
    """组合键里的单个字母统一发小写：固件把大写字母当成 Shift+字母（'GUI+V' 会变成 ⌘⇧V），
    而 ⌘V 这类组合键里的字母本意只是键位。要 Shift 请显式写 SHIFT+。单字符 spec（如 'A'）保持原样。"""
    if len(spec) <= 1:
        return spec
    return "+".join(t.lower() if len(t) == 1 and t.isalpha() else t for t in re.split(r"[+\-]", spec) if t != "")
def enc_key(spec: str, n: int = 1) -> str:
    parse_key_spec(spec); spec = normalize_key_spec(spec); return f"KP {spec}" if n == 1 else f"KP {spec} {int(n)}"
def enc_key_down(spec: str) -> str:
    parse_key_spec(spec); return f"KD {normalize_key_spec(spec)}"
def enc_key_up(spec: str) -> str:
    parse_key_spec(spec); return f"KU {normalize_key_spec(spec)}"
def enc_text(text: str) -> str: return f"TX {escape_text(text)}"
def enc_consumer(name: str) -> str:
    assert name.upper() in CONSUMER_KEYS, name
    return f"CC {name.upper()}"
def enc_release() -> str: return "RL"
def enc_cfg(k: str, v) -> str: return f"CFG {k} {v}"


@dataclass
class Reply:
    ok: bool
    text: str
    raw: str

    @property
    def fields(self) -> dict:
        d = {}
        for tok in self.text.split():
            if "=" in tok:
                k, v = tok.split("=", 1); d[k] = v
        return d


def parse_reply(line: str) -> Reply:
    s = line.strip()
    if s.upper().startswith("OK"):
        return Reply(True, s[2:].strip(), s)
    if s.upper().startswith("ERR"):
        return Reply(False, s[3:].strip(), s)
    return Reply(False, f"unexpected: {s}", s)


def split_move(dx: int, dy: int, step: int = 127) -> Iterable[tuple[int, int]]:
    """把大位移拆成 ±step 的报告序列（和固件同一算法，仿真用）。"""
    dx, dy = int(dx), int(dy)
    while dx or dy:
        sx = max(-step, min(step, dx)); sy = max(-step, min(step, dy))
        yield sx, sy
        dx -= sx; dy -= sy
