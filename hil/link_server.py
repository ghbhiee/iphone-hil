"""常驻串口守护进程：一直握着 ESP32 的串口，CLI 通过 HTTP 发命令。

为什么：CH340 这类桥接板每次打开串口都会复位 ESP32（DTR/RTS 脉冲），BLE 要重连、第一批报告会丢。
串口只开一次、常驻，就没这个问题。

接口（127.0.0.1:8766）：
  GET /cmd?line=MV%2010%2010   → {"ok":true,"text":"mv 10 10","raw":"OK mv 10 10"}
  GET /status                  → 串口、固件 ST、最近错误
  GET /quit
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
        except Exception as e:  # noqa
            S.err = f"{type(e).__name__}: {e}"
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
                                    "ports_seen": find_serial_ports()})
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
                with S.lock:
                    r = S.link.send(line)
                S.n_cmd += 1
                return self._json(200, {"ok": r.ok, "text": r.text, "raw": r.raw})
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
            S.link.close()
        print("[link] bye", flush=True)
        os._exit(0)


if __name__ == "__main__":
    main()
