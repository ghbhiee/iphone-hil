"""常驻串口守护进程：一直握着 ESP32 的串口，CLI 通过 HTTP 发命令。

为什么：CH340 这类桥接板每次打开串口都会复位 ESP32（DTR/RTS 脉冲），BLE 要重连、第一批报告会丢。
串口只开一次、常驻，就没这个问题。

接口（127.0.0.1:8766）：
  GET /cmd?line=MV%2010%2010   → {"ok":true,"text":"mv 10 10","raw":"OK mv 10 10"}
  GET /status                  → 串口、固件 ST、最近错误
  GET /wake?timeout=15         → 让板子向 iPhone 广播并等 iPhone 的 HID 连回来（ST conn=1）
  GET /sleep                   → 让板子立刻放开 iPhone（屏幕键盘马上恢复）
  GET /quit

板子的 HID 空闲（固件 ≥0.3.2）：板子 hididle 秒（默认 120）没收到需要 HID 的命令就断开 iPhone 的 HID（键盘恢复），
但**板子始终继续广播、不失联**（iOS 连着蓝牙键盘时藏掉所有 App 的屏幕键盘，所以只断这条不用的连接）。所以这里：
  - 链路一打开就发 WAKE 让板子退出空闲；iPhone 解锁时 1~3 s 自动连回来；
  - /cmd 收到 "ERR not connected" 且命令需要 HID 时，WAKE+等 iPhone 连回来（最多 hid_wake_timeout_s）再重发一次，
    等不到就回「请解锁 iPhone 并到 设置→蓝牙 点一下 HIL-Bridge」（多半是 iPhone 锁屏，iOS 锁屏不自动重连 HID）；
  - 退出前发 SLEEP（板子立刻松开 iPhone、键盘恢复，但仍在广播）。板子一直可被新守护进程连上，不用断电重启。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from hil.link import LinkError, SerialLink, find_serial_ports
from hil import config as C

HOST, PORT = "127.0.0.1", int(os.environ.get("HIL_LINK_PORT", "8766"))


class S:
    link = None
    transport = "serial"
    ble_token = None
    ble_name = "HIL-Bridge"
    err: str | None = None
    lock = threading.Lock()
    port: str | None = None
    quit = threading.Event()
    opening = threading.Event()
    n_cmd = 0
    last_activity = time.time()
    idle_quit_min = 10.0
    hid_wake_timeout = 20.0   # 等 iPhone HID 连回来的上限（秒）；hil.json 的 hid_wake_timeout_s
    last_wake_s: float | None = None   # 最近一次唤醒等了多久（诊断）


HID_CMDS = {"MV", "BT", "CK", "LP", "WH", "KP", "KD", "KU", "TX", "CC"}   # 需要 iPhone 连着的命令（和固件 handleLine 的顺序一致）


def needs_hid(line: str) -> bool:
    return line.split(" ", 1)[0].upper() in HID_CMDS


def wait_hid(timeout: float) -> tuple[bool, float]:
    """发 WAKE 让板子向 iPhone 广播，然后轮询 ST 直到 conn=1。调用方需持有 S.lock。返回 (连上了吗, 等了几秒)。"""
    t0 = time.time()
    r = S.link.send("WAKE")
    if r.ok and r.fields.get("conn") == "1":
        return True, 0.0
    if not r.ok:
        print(f"[link] WAKE → {r.raw}", flush=True)   # 旧固件没有 WAKE（ERR unknown cmd）：只能干等
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        try:
            if S.link.status().get("conn") == "1":
                waited = time.time() - t0
                S.last_wake_s = waited
                return True, waited
        except LinkError:
            pass
    return False, time.time() - t0


def open_link():
    if S.opening.is_set():
        return
    S.opening.set()
    try:
        _open_link_locked()
    finally:
        S.opening.clear()


def _open_link_locked():
    with S.lock:
        if S.link:
            S.link.close(); S.link = None
        try:
            if S.transport == "ble":
                from hil.ble_central import BleLink
                S.link = BleLink(token=S.ble_token or "", device_name=S.ble_name, log=lambda m: print(f"[ble] {m}", flush=True))
            else:
                S.link = SerialLink(port=S.port)
            S.err = None
            print(f"[link] 已打开 {S.link.port}", flush=True)
            # 不再在打开时发 WAKE：iOS 不会因 WAKE 自动重连；而且板子处于正常(parked)时 WAKE 会把它叫醒、
            # iPhone 连回来又藏屏幕键盘。连接统一走「hil up/listen 切侦听模式 + 用户手工点」。
        except Exception as e:  # noqa
            S.err = f"{type(e).__name__}: {e}"
            if S.transport == "ble" and "没连上" in str(e):   # 固件 ≥0.3.2 板子始终广播，连不上基本只有板子断电/死机
                S.err += "。板子一直在广播，连不上基本是板子断电或死机了：检查 ESP32 供电，必要时断电重启"
            print(f"[link] 打开失败: {S.err}", flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端等不及先断了



    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/status":
            st = None
            if S.link and S.lock.acquire(timeout=3):
                try:
                    st = S.link.status()
                except Exception as e:  # noqa
                    S.err = f"{type(e).__name__}: {e}"
                finally:
                    S.lock.release()
            return self._json(200, {"ok": S.link is not None and S.err is None, "port": S.link.port if S.link else None,
                                    "transport": S.transport, "firmware": st, "error": S.err, "commands": S.n_cmd, "pid": os.getpid(),
                                    "ble_lines": getattr(S.link, "n_lines", None), "ble_extra": getattr(S.link, "n_extra", None),
                                    "ble_notif": getattr(S.link, "n_notif", None), "ble_writes": getattr(S.link, "n_writes", None),
                                    "idle_s": round(time.time() - S.last_activity), "idle_quit_min": S.idle_quit_min,
                                    "hid_wake_timeout_s": S.hid_wake_timeout, "last_wake_s": S.last_wake_s,
                                    "ports_seen": find_serial_ports()})
        if u.path in ("/wake", "/sleep"):
            if S.link is None:
                return self._json(503, {"ok": False, "text": S.err or "link not open"})
            try:
                with S.lock:
                    if u.path == "/sleep":
                        r = S.link.send("SLEEP")
                        return self._json(200, {"ok": r.ok, "raw": r.raw})
                    t = float(q.get("timeout", S.hid_wake_timeout))
                    ok, waited = wait_hid(t)
                    st = None
                    try:
                        st = S.link.status()
                    except LinkError:
                        pass
                    return self._json(200, {"ok": ok, "conn": ok, "waited_s": round(waited, 1), "firmware": st})
            except LinkError as e:
                S.err = str(e)
                return self._json(503, {"ok": False, "text": str(e)})
        if u.path == "/reopen":
            open_link(); return self._json(200 if S.link else 503, {"ok": S.link is not None, "error": S.err})
        if u.path == "/unpair":
            name = q.get("name", S.ble_name)
            try:
                import IOBluetooth
                devs = IOBluetooth.IOBluetoothDevice.pairedDevices() or []
                hit = [d for d in devs if str(d.name()) == name]
                res = [int(d.remove()) for d in hit]
                return self._json(200, {"paired": [(str(d.name()), str(d.addressString())) for d in devs], "removed": len(hit), "results": res})
            except Exception as e:  # noqa
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/quit":
            self._json(200, {"bye": True}); S.quit.set(); return
        if u.path == "/cmd":
            S.last_activity = time.time()
            line = q.get("line", "")
            if not line:
                return self._json(400, {"ok": False, "text": "no line"})
            if S.link is None:
                deadline = time.time() + 25  # 守护进程刚起来、BLE 还在连：等它连上再执行
                while S.link is None and S.opening.is_set() and time.time() < deadline:
                    time.sleep(0.2)
                if S.link is None and not S.opening.is_set():
                    open_link()
                if S.link is None:
                    return self._json(503, {"ok": False, "text": S.err or "link not open"})
            try:
                waited = None
                with S.lock:
                    r = S.link.send(line)
                    if not r.ok and r.text.lower().startswith("not connected") and needs_hid(line):
                        # 板子空闲中刚被这条命令唤醒：等 iPhone 的 HID 连回来再发一次
                        ok, waited = wait_hid(S.hid_wake_timeout)
                        if ok:
                            r = S.link.send(line)
                        else:
                            S.n_cmd += 1
                            return self._json(503, {"ok": False, "hid_wait_s": round(waited, 1),
                                                    "text": "iPhone 的 HID 没连上：请解锁 iPhone 并到 设置→蓝牙 点一下 HIL-Bridge（板子空闲时松开了手机，iPhone 锁屏时 iOS 不会自动重连）"})
                S.n_cmd += 1
                d = {"ok": r.ok, "text": r.text, "raw": r.raw}
                if waited is not None:
                    d["hid_wait_s"] = round(waited, 1)
                return self._json(200, d)
            except LinkError as e:
                S.err = str(e)
                return self._json(503, {"ok": False, "text": str(e)})  # BLE 掉线由主循环负责重连
        return self._json(404, {"error": "no such path"})


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--port", default=None, help="串口"); ap.add_argument("--http-port", type=int, default=PORT)
    ap.add_argument("--transport", choices=["serial", "ble"], default=None, help="默认取 hil.json 的 link_transport")
    ap.add_argument("--idle", type=float, default=None, help="空闲多少分钟自动退出（默认取 hil.json 的 idle_quit_min，0=永不）")
    a = ap.parse_args(argv)
    cfg = C.load()
    S.idle_quit_min = a.idle if a.idle is not None else float(cfg.get("idle_quit_min", 10))
    S.hid_wake_timeout = float(cfg.get("hid_wake_timeout_s", 20))
    S.last_activity = time.time()
    S.port = a.port or cfg.get("port")
    S.transport = a.transport or cfg.get("link_transport", "serial")
    S.ble_token = cfg.get("ble_token"); S.ble_name = cfg.get("ble_name", "HIL-Bridge")
    srv = ThreadingHTTPServer((HOST, a.http_port), H); srv.daemon_threads = True
    srv.handle_error = lambda request, client_address: None  # 不打印 BrokenPipe 之类的栈
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[link] http://{HOST}:{a.http_port} pid={os.getpid()}", flush=True)
    open_link()
    try:
        while not S.quit.is_set():
            time.sleep(0.2)
            if S.idle_quit_min > 0 and time.time() - S.last_activity > S.idle_quit_min * 60:
                print(f"[link] 空闲 {S.idle_quit_min:g} 分钟，自动退出", flush=True); break
            if S.link is None and not S.opening.is_set():
                open_link(); time.sleep(3)
            elif S.transport == "ble" and S.link is not None and not S.link.connected_ble():
                with S.lock:
                    try:
                        print("[link] BLE 掉线，重连…", flush=True)
                        S.link.reconnect(15); S.err = None
                        print("[link] BLE 已重连", flush=True)
                    except Exception as e:  # noqa
                        S.err = f"{type(e).__name__}: {e}"
                time.sleep(2)
    finally:
        if S.link:
            S.link.close(); S.link = None   # 先放掉 BLE，免得和串口切换抢板子
        try:   # 退出前经串口把板子切回正常模式：重启→iPhone 断开、板子恢复空闲自动踢→屏幕键盘恢复
            from hil.link import set_adv_mode
            set_adv_mode(False, port=S.port)
            print("[link] 已把板子切回正常模式（iPhone 松开，屏幕键盘恢复）", flush=True)
        except Exception as e:  # noqa
            print(f"[link] 退出时切回正常模式失败: {e}", flush=True)
        print("[link] bye", flush=True)
        os._exit(0)


if __name__ == "__main__":
    main()
