"""Mac 端到 ESP32-S3 的链路：SerialLink（真串口）/ LoopbackLink（回环桩，可挂仿真手机）/ EchoLink（只打印）。

三者共用同一套高层 API（move / click / tap_key / type_text / paste_text ...），上层代码不用关心是真是假。
FirmwareModel 是固件命令解析器的 Python 参考实现：固件 .ino 里的逻辑与它一一对应，改协议先改这里和 protocol.py。
"""
from __future__ import annotations

import glob
import subprocess
import sys
import time
from typing import Callable, Optional

from hil import protocol as P
from hil.protocol import Reply


class LinkError(RuntimeError):
    pass


class Link:
    """抽象链路：子类只需实现 send(line) -> Reply。"""
    name = "abstract"

    def send(self, line: str) -> Reply:
        raise NotImplementedError

    def _must(self, line: str) -> Reply:
        r = self.send(line)
        if not r.ok:
            raise LinkError(f"{line!r} -> {r.raw}")
        return r

    # ---- 高层 API ----
    def ping(self) -> bool:
        try:
            return self.send(P.enc_ping()).ok
        except Exception:
            return False

    def status(self) -> dict:
        return self._must(P.enc_status()).fields

    def connected(self) -> bool:
        return self.status().get("conn") == "1"

    def move(self, dx: int, dy: int) -> Reply:
        if int(dx) == 0 and int(dy) == 0:
            return Reply(True, "noop", "OK noop")
        return self._must(P.enc_move(dx, dy))

    def button(self, btn: str, down: bool) -> Reply:
        return self._must(P.enc_button(btn, down))

    def click(self, btn: str = "l", n: int = 1) -> Reply:
        return self._must(P.enc_click(btn, n))

    def long_press(self, ms: int, btn: str = "l") -> Reply:
        return self._must(P.enc_long_press(ms, btn))

    def wheel(self, n: int) -> Reply:
        return self._must(P.enc_wheel(n))

    def key(self, spec: str, n: int = 1) -> Reply:
        return self._must(P.enc_key(spec, n))

    def clear_field(self, n: int = 40) -> None:
        """清空当前文本框：全选+退格，再补 n 个退格兜底。"""
        self.key("GUI+A"); time.sleep(0.3); self.key("BACKSPACE"); time.sleep(0.2); self.key("BACKSPACE", n)

    def key_down(self, spec: str) -> Reply:
        return self._must(P.enc_key_down(spec))

    def key_up(self, spec: str) -> Reply:
        return self._must(P.enc_key_up(spec))

    def type_text(self, text: str, chunk: int = 120) -> None:
        """ASCII 文本。长文本分段发，每段一条 TX。"""
        for i in range(0, len(text), chunk):
            self._must(P.enc_text(text[i:i + chunk]))

    def paste_text(self, text: str, settle: float = 5.0) -> None:
        """任意文本（含中文）：Mac 剪贴板 → 通用剪贴板(Handoff) 同步到 iPhone → Cmd+V。
        前提：Mac 与 iPhone 同一 Apple ID、都开了「接力」、蓝牙/WiFi 开着。
        实测（2026-10-27）：iPhone 是在收到 ⌘V 时才去拉 Mac 的剪贴板，而且要先收到 Mac 的蓝牙广播；
        复制后等 2 s 常常还没收到（⌘V 落空、什么都不粘），等 4~5 s 稳定；Mac 上的复制 2 分钟后过期，所以每次都重新 pbcopy。
        调用方粘贴后截图确认；没出来就隔 3 s 再发一次 key("GUI+v")（别盲发，成功时会粘两遍）。"""
        import os
        env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")  # pbcopy 按 locale 解码，没有 UTF-8 locale 时中文会变乱码
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, env=env)
        time.sleep(settle)
        self.key("GUI+v")  # 小写：大写会被固件当成 Shift+v，变成 ⌘⇧V（粘贴并匹配样式），搜索框等地方没反应

    def consumer(self, name: str) -> Reply:
        return self._must(P.enc_consumer(name))

    def home(self) -> Reply:
        return self.consumer("HOME")

    def release(self) -> Reply:
        return self._must(P.enc_release())

    def cfg(self, k: str, v) -> Reply:
        return self._must(P.enc_cfg(k, v))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
# 真串口
# ---------------------------------------------------------------------------
_PORT_GLOBS = ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*"]  # 串口自动发现


def find_serial_ports() -> list[str]:
    ports = []
    for g in _PORT_GLOBS:
        ports += glob.glob(g)
    return sorted(set(ports))


class SerialLink(Link):
    name = "serial"

    def __init__(self, port: Optional[str] = None, baud: int = 115200, timeout: float = 3.0,
                 boot_wait: float = 1.2, verbose: bool = False, log: Optional[Callable[[str], None]] = None):
        import serial  # pyserial
        if port is None:
            ports = find_serial_ports()
            if not ports:
                raise LinkError("没找到串口（/dev/cu.usbmodem* 等）。ESP32-S3 插上了吗？")
            if len(ports) > 1:
                print(f"[serial] 多个串口，用第一个: {ports}", file=sys.stderr)
            port = ports[0]
        self.port, self.verbose, self.log = port, verbose, log
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.write_timeout = port, baud, timeout, timeout
        s.dtr = False  # 别去碰 DTR/RTS：某些板子会被拉进复位/下载模式
        s.rts = False
        s.open()
        self.ser = s
        time.sleep(boot_wait)
        self.ser.reset_input_buffer()
        ok = False
        for _ in range(4):  # 某些板子（CH340）开串口会触发复位，等它重新起来
            if self.ping():
                ok = True; break
            time.sleep(1.0); self.ser.reset_input_buffer()
        if not ok:
            raise LinkError(f"{port}: PING 没有回应（固件刷了吗？波特率 {baud}？）")

    def _readline(self) -> str:
        raw = self.ser.readline()
        return raw.decode("utf-8", "replace").strip()

    def send(self, line: str, retries: int = 1) -> Reply:
        for attempt in range(retries + 1):
            if self.verbose:
                print(f"→ {line}", file=sys.stderr)
            if self.log:
                self.log(f"→ {line}")
            self.ser.write((line + "\n").encode("utf-8"))
            self.ser.flush()
            deadline = time.time() + self.ser.timeout
            while time.time() < deadline:
                got = self._readline()
                if not got:
                    continue
                up = got.upper()
                if not (up.startswith("OK") or up.startswith("ERR")):  # 固件日志(#...)、ROM 启动信息等，一律忽略
                    if self.verbose:
                        print(f"  {got}", file=sys.stderr)
                    continue
                if self.verbose:
                    print(f"← {got}", file=sys.stderr)
                if self.log:
                    self.log(f"← {got}")
                return P.parse_reply(got)
            if attempt < retries:
                self.ser.reset_input_buffer()
        raise LinkError(f"{line!r}: 串口超时无回应")

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 让 iPhone 连上（固件 ≥0.3.3）
# ---------------------------------------------------------------------------
# 板子（appearance=0x3C2、parkadv=1）始终以蓝牙键盘身份广播，iPhone 在 设置→蓝牙 里一直看得到它、
# 板子重启后 iPhone ~1 s 自动连回。被空闲踢开后 iOS 不一定马上回连，这时只能让用户在 设置→蓝牙 点一下。
# 所以这里只做：WAKE（重置空闲计时、广播 HID）→ 轮询 ST 等 conn=1 → 过一会儿还没连上就提示用户去点。
# 不再改 appearance、不再 RESTART（那是 0.3.2 的「侦听/正常模式」做法，已删）。
def wait_iphone(link: "Link", wait_s: float = 60.0, prompt: Optional[Callable[[], None]] = None,
                prompt_after: float = 4.0) -> dict:
    """WAKE 后等 iPhone 的 HID 连上，返回最后一次 ST 字段（conn="1" 表示连上了）。
    prompt_after 秒后还没连上就调用一次 prompt（提示用户去 iPhone 设置→蓝牙 点 HIL-Bridge）。"""
    r = link.send("WAKE")
    if r.ok and r.fields.get("conn") == "1":
        return link.status()
    t0, prompted, st = time.time(), False, {}
    while time.time() - t0 < wait_s:
        try:
            st = link.status()
            if st.get("conn") == "1":
                return st
        except Exception:
            pass
        if prompt and not prompted and time.time() - t0 >= prompt_after:
            prompt(); prompted = True
        time.sleep(1.0)
    return st


# ---------------------------------------------------------------------------
# 固件参考模型 + 回环桩
# ---------------------------------------------------------------------------
class FirmwareModel:
    """固件命令解析/执行的 Python 版。hid 是 HID 报告的接收方（SimPhone 或 None）。"""

    def __init__(self, hid=None, backend: str = "ble", name: str = "HIL Bridge"):
        self.hid = hid
        self.backend, self.name = backend, name
        self.cfg = {"mvdelay": 25, "typedelay": 12, "hididle": 600, "parkadv": 1, "appearance": 0x3C2}
        self.mods, self.keys, self.buttons = 0, [], 0
        self.hid_awake = True   # 固件 ≥0.3.0：空闲时主动断开 iPhone；桩里只记状态
        self.sleep = (lambda ms: None)  # 回环时不真等；想真实节奏可换成 time.sleep

    # --- HID 报告出口 ---
    def _mouse(self, dx=0, dy=0, wheel=0):
        if self.hid is not None:
            self.hid.mouse_report(self.buttons, dx, dy, wheel)

    def _kbd(self):
        if self.hid is not None:
            self.hid.key_report(self.mods, list(self.keys))

    def _consumer(self, usage):
        if self.hid is not None:
            self.hid.consumer_report(usage); self.hid.consumer_report(0)

    # --- 命令 ---
    def handle(self, line: str) -> str:
        toks = line.strip().split()
        if not toks:
            return "ERR empty"
        cmd = toks[0].upper()
        try:
            if cmd == "PING":
                return "OK PONG"
            if cmd == "AUTH":
                return "OK auth"
            if cmd == "ST":
                conn = 1 if (self.hid is None or getattr(self.hid, "connected", True)) else 0
                hid = "on" if conn else ("waking" if self.hid_awake else "idle")
                return f"OK conn={conn} hid={hid} hididle={self.cfg['hididle']} parkadv={self.cfg['parkadv']} idle=0 conns={conn} adv=pub appear=0x{self.cfg['appearance']:x} backend={self.backend} name={self.name} mvdelay={self.cfg['mvdelay']} typedelay={self.cfg['typedelay']} txfail=0"
            if cmd == "WAKE":
                self.hid_awake = True
                conn = 1 if (self.hid is None or getattr(self.hid, "connected", True)) else 0
                return f"OK wake conn={conn} hid={'on' if conn else 'waking'}"
            if cmd == "SLEEP":
                if self.cfg["hididle"] == 0:
                    return "ERR hididle=0 (always on)"
                self.hid_awake = False
                return "OK sleep"
            if cmd in ("MV", "BT", "CK", "LP", "WH", "KP", "KD", "KU", "TX", "CC"):
                self.hid_awake = True   # 需要 HID 的命令算活动并唤醒
            if cmd == "MV":
                dx, dy = int(toks[1]), int(toks[2])
                for sx, sy in P.split_move(dx, dy):
                    self._mouse(sx, sy); self.sleep(self.cfg["mvdelay"])
                return f"OK mv {dx} {dy}"
            if cmd == "BT":
                b = P.MOUSE_BUTTONS[toks[1].lower()]
                if toks[2].lower() == "down":
                    self.buttons |= b
                else:
                    self.buttons &= ~b
                self._mouse(); return "OK bt"
            if cmd == "CK":
                b = P.MOUSE_BUTTONS[toks[1].lower()] if len(toks) > 1 else 1
                n = int(toks[2]) if len(toks) > 2 else 1
                for _ in range(n):
                    self.buttons |= b; self._mouse(); self.sleep(30)
                    self.buttons &= ~b; self._mouse(); self.sleep(60)
                return f"OK ck {n}"
            if cmd == "LP":
                ms = int(toks[1]); b = P.MOUSE_BUTTONS[toks[2].lower()] if len(toks) > 2 else 1
                self.buttons |= b; self._mouse(); self.sleep(ms)
                self.buttons &= ~b; self._mouse(); return f"OK lp {ms}"
            if cmd == "WH":
                n = int(toks[1])
                step = 1 if n > 0 else -1
                for _ in range(abs(n)):
                    self._mouse(wheel=step); self.sleep(self.cfg["mvdelay"])
                return f"OK wh {n}"
            if cmd in ("KP", "KD", "KU"):
                mods, usage = P.parse_key_spec(toks[1])
                times = int(toks[2]) if (cmd == "KP" and len(toks) > 2) else 1
                for _ in range(max(1, times)):
                    if cmd in ("KP", "KD"):
                        self.mods |= mods
                        if usage and usage not in self.keys:
                            self.keys.append(usage)
                        self._kbd()
                    if cmd == "KP":
                        self.sleep(self.cfg["typedelay"])
                    if cmd in ("KP", "KU"):
                        self.mods &= ~mods
                        if usage in self.keys:
                            self.keys.remove(usage)
                        self._kbd()
                return f"OK {cmd.lower()} {times}"
            if cmd == "TX":
                text = P.unescape_text(line.strip()[3:]) if len(line.strip()) > 3 else ""
                for ch in text:
                    mods, usage = P.ascii_to_usage(ch)
                    self.mods, self.keys = mods, [usage]; self._kbd(); self.sleep(self.cfg["typedelay"])
                    self.mods, self.keys = 0, []; self._kbd(); self.sleep(self.cfg["typedelay"])
                return f"OK tx {len(text)}"
            if cmd == "CC":
                self._consumer(P.CONSUMER_KEYS[toks[1].upper()]); return "OK cc"
            if cmd == "RL":
                self.mods, self.keys, self.buttons = 0, [], 0
                self._kbd(); self._mouse(); return "OK rl"
            if cmd == "CFG":
                k = toks[1].lower()
                if k in self.cfg:
                    self.cfg[k] = int(toks[2], 0)
                elif k == "name":
                    self.name = " ".join(toks[2:])
                else:
                    return f"ERR unknown cfg {k}"
                return f"OK cfg {k}"
            return f"ERR unknown cmd {cmd}"
        except (IndexError, ValueError, KeyError) as e:
            return f"ERR bad args ({type(e).__name__}: {e})"


class LoopbackLink(Link):
    """回环桩：命令进 FirmwareModel，HID 报告落到 hid（如 SimPhone）。verbose 时逐行打印。"""
    name = "loopback"

    def __init__(self, hid=None, verbose: bool = True, realtime: bool = False):
        self.model = FirmwareModel(hid)
        if realtime:
            self.model.sleep = lambda ms: time.sleep(ms / 1000)
        self.verbose = verbose
        self.transcript: list[tuple[str, str]] = []

    def send(self, line: str) -> Reply:
        reply = self.model.handle(line)
        self.transcript.append((line, reply))
        if self.verbose:
            print(f"→ {line}\n← {reply}", file=sys.stderr)
        return P.parse_reply(reply)


class EchoLink(LoopbackLink):
    """只打印、永远 OK（没有仿真手机）。"""
    name = "echo"

    def __init__(self):
        super().__init__(hid=None, verbose=True)


class HttpLink(Link):
    """通过常驻的串口守护进程（hil.link_server）发命令；守护进程没起就自动拉起来。"""
    name = "http"
    BASE = "http://127.0.0.1:8766"

    def __init__(self, port: Optional[str] = None, verbose: bool = False, autostart: bool = True):
        import json, urllib.request, urllib.parse
        self._json, self._quote = json, urllib.parse.quote
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不查系统代理，否则每个进程多 1~2 s
        self.verbose = verbose
        if not self._alive():
            if not autostart:
                raise LinkError("串口守护进程没在跑（python -m hil.link_server）")
            self._start(port)

    def _alive(self) -> bool:
        try:
            with self._opener.open(self.BASE + "/status", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def _start(self, port):
        import os, subprocess
        from hil import config as C
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(os.path.join(root, "captures"), exist_ok=True)
        transport = C.load().get("link_transport", "serial")
        if transport == "ble":
            app = os.path.expanduser("~/Applications/HIL Link.app")
            if not os.path.exists(app):  # 第一次用：生成 App 壳（蓝牙权限按 App 归属，第一次启动会弹授权框）
                subprocess.run(["/bin/zsh", os.path.join(root, "tools", "make_link_app.sh")], check=True)
            subprocess.run(["open", "-g", app], check=True)  # BLE 要蓝牙权限，只有 App 壳拿得到
            waits = 120  # 扫描 + 连接 + 鉴权，最长 30 s
        else:
            log = open(os.path.join(root, "captures", "link.log"), "ab")
            cmd = [sys.executable, "-m", "hil.link_server"] + (["--port", port] if port else [])
            subprocess.Popen(cmd, cwd=root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            waits = 40  # 串口打开 + 板子复位 + PING 大约 3~5 s
        for _ in range(waits):
            time.sleep(0.25)
            if self._alive():
                return
        raise LinkError("串口/蓝牙守护进程没起来，看 captures/link.log")

    def send(self, line: str) -> Reply:
        if self.verbose:
            print(f"→ {line}", file=sys.stderr)
        try:
            with self._opener.open(self.BASE + "/cmd?line=" + self._quote(line), timeout=30) as r:
                d = self._json.loads(r.read())
        except Exception as e:  # noqa
            if hasattr(e, "read"):
                try:
                    d = self._json.loads(e.read())
                except Exception:
                    d = {"ok": False, "text": str(e)}
            else:
                raise LinkError(f"串口守护进程不可达: {e}") from e
        raw = d.get("raw") or (("OK " if d.get("ok") else "ERR ") + str(d.get("text", "")))
        if self.verbose:
            print(f"← {raw}", file=sys.stderr)
        return P.parse_reply(raw)

    def daemon_status(self) -> dict:
        with self._opener.open(self.BASE + "/status", timeout=5) as r:
            return self._json.loads(r.read())

    def wake(self, timeout: float = 15.0) -> dict:
        """让板子向 iPhone 广播并等它的 HID 连回来。返回 {"conn": bool, "waited_s": 秒, "firmware": ST 字段}。"""
        with self._opener.open(self.BASE + f"/wake?timeout={timeout}", timeout=timeout + 15) as r:
            return self._json.loads(r.read())


def open_link(kind: str = "serial", port: Optional[str] = None, hid=None, verbose: bool = False) -> Link:
    if kind == "serial":  # 默认走守护进程（避免每次开串口复位板子）；要直连用 kind="direct"
        return HttpLink(port=port, verbose=verbose)
    if kind == "direct":
        return SerialLink(port=port, verbose=verbose)
    if kind in ("loop", "loopback"):
        return LoopbackLink(hid=hid, verbose=verbose)
    if kind == "echo":
        return EchoLink()
    raise ValueError(kind)
