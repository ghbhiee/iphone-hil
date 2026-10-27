"""独立进程的取帧服务（HTTP）。

为什么独立：Claude Code / 很多终端宿主是 hardened runtime 且没有 camera entitlement，
其子进程申请「相机」权限会被 TCC 直接拒绝且不弹框；iPhone 屏幕采集设备和摄像头走同一道权限。
所以把 AVCaptureSession 放进一个有 Info.plist 的小 App（tools/make_app.sh 生成 HIL Capture.app），
别的进程只用 HTTP 取帧。

接口（都是 GET，127.0.0.1:8765）：
  /status                     JSON：设备、分辨率、帧序号、到达帧率、错误
  /devices                    JSON：当前能看到的采集设备
  /frame.png | /frame.jpg     最新一帧；?fresh=1 等动作之后的新帧；?scale=0.5 缩放；?timeout=5
  /frame.raw                  RGB 裸字节，头 X-Width/X-Height（给闭环用，零编码开销）
  /reopen?ios=1 | ?name=xx | ?index=n     重新打开设备
  /quit                       退出
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from hil import capture

HOST = "127.0.0.1"
PORT = int(os.environ.get("HIL_CAPTURE_PORT", "8765"))


class _State:
    grabber: capture.Grabber | None = None
    err: str | None = None
    lock = threading.Lock()
    sel = {"ios": True, "name": None, "index": None}
    perm = "unknown"
    opened_at = 0.0
    perm_ready = threading.Event()
    quit = threading.Event()
    last_activity = time.time()   # 最近一次取帧/重开的时间；空闲超过 idle_quit_min 分钟自动退出
    idle_quit_min = 10.0
    fps_win = []  # (t, seq)


S = _State()


def open_device(**sel):
    with S.lock:
        if S.grabber is not None:
            S.grabber.close()
            S.grabber = None
        S.sel.update({k: v for k, v in sel.items() if k in S.sel})
        try:
            g = capture.Grabber.open(name_substr=S.sel["name"], index=S.sel["index"], ios=S.sel["ios"],
                                     wait_ios=float(sel.get("wait", 6.0)),
                                     want_format=sel.get("format"), preset=sel.get("preset"))
            S.grabber, S.err = g, None
            S.opened_at = time.time()
            print(f"[server] 已打开 {g.name} {g.width}x{g.height}", flush=True)
        except Exception as e:
            S.err = f"{type(e).__name__}: {e}"
            print(f"[server] 打开失败: {S.err}", flush=True)


def _arrival_fps() -> float:
    g = S.grabber
    if g is None:
        return 0.0
    now = time.time()
    S.fps_win.append((now, g.seq))
    S.fps_win[:] = [x for x in S.fps_win if now - x[0] <= 3.0]
    if len(S.fps_win) < 2:
        return 0.0
    (t0, s0), (t1, s1) = S.fps_win[0], S.fps_win[-1]
    return (s1 - s0) / max(t1 - t0, 1e-6)


class Handler(BaseHTTPRequestHandler):
    server_version = "hil-capture/0.1"

    def log_message(self, fmt, *args):  # 安静一点
        if "/frame" not in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/status":
                return self._json(200, self.status())
            if u.path == "/devices":
                return self._json(200, [d.__dict__ for d in capture.list_devices(wait_ios=float(q.get("wait", 1)))])
            if u.path.startswith("/frame") or u.path == "/reopen":
                S.last_activity = time.time()
            if u.path == "/reopen":
                sel = {}
                if "ios" in q:
                    sel["ios"] = q["ios"] == "1"
                    if sel["ios"]:
                        sel["name"] = None; sel["index"] = None
                if "name" in q: sel["name"] = q["name"]; sel["ios"] = False
                if "index" in q: sel["index"] = int(q["index"]); sel["ios"] = False
                if "wait" in q: sel["wait"] = float(q["wait"])
                if "format" in q: sel["format"] = q["format"]
                if "preset" in q: sel["preset"] = q["preset"]
                open_device(**sel)
                return self._json(200 if S.grabber else 503, self.status())
            if u.path == "/quit":
                self._json(200, {"bye": True})
                S.quit.set()  # 主线程的 run loop 看到后退出
                return
            if u.path.startswith("/frame"):
                return self.frame(u.path, q)
            return self._json(404, {"error": "no such path", "paths": ["/status", "/devices", "/frame.png", "/frame.jpg", "/frame.raw", "/reopen", "/quit"]})
        except Exception as e:  # noqa
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def status(self):
        g = S.grabber
        w, h = (g.width, g.height) if g else (None, None)
        if g and not w and g.sink.first_info:  # iOS 屏幕设备不报格式(0x0)，用首帧尺寸
            try:
                w, h = (int(v) for v in g.sink.first_info.split()[0].split("x"))
            except Exception:
                pass
        return {
            "ok": g is not None and S.err is None,
            "camera_permission": capture.camera_auth_status(),
            "device": g.name if g else None,
            "width": w, "height": h,
            "preset": getattr(g, "preset", None) if g else None, "active_format": getattr(g, "active_format", None) if g else None,
            "seq": g.seq if g else 0, "arrival_fps": round(_arrival_fps(), 1),
            "last_frame_age_s": round(time.time() - g.sink.t_last, 2) if g and g.sink.t_last else None,
            "selector": S.sel, "error": S.err, "pid": os.getpid(),
            "diag": g.diag() if g else None,
            "idle_s": round(time.time() - S.last_activity), "idle_quit_min": S.idle_quit_min,
            "hint": ("iPhone 可能锁屏了：解锁后设备会重新出现并出帧" if (g is None or not g.sink.t_last) and S.sel["ios"] else None),
        }

    def frame(self, path: str, q: dict):
        g = S.grabber
        if g is None:
            return self._json(503, {"error": S.err or "没有打开的设备", "hint": "GET /reopen?ios=1"})
        fresh = q.get("fresh", "0") == "1"
        scale = float(q.get("scale", "1"))
        try:
            arr = g.grab_array(fresh=fresh, timeout=float(q.get("timeout", "5")))
        except TimeoutError as e:
            return self._json(503, {"error": str(e)})
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
        seq = g.seq
        if ext == "raw":
            return self._send(200, "application/octet-stream", arr.tobytes(),
                              {"X-Width": arr.shape[1], "X-Height": arr.shape[0], "X-Seq": seq})
        img = Image.fromarray(arr)
        if scale != 1.0:
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        if ext in ("jpg", "jpeg"):
            img.save(buf, "JPEG", quality=int(q.get("q", "90")))
            ctype = "image/jpeg"
        else:
            img.save(buf, "PNG", compress_level=int(q.get("z", "1")))
            ctype = "image/png"
        return self._send(200, ctype, buf.getvalue(), {"X-Width": img.width, "X-Height": img.height, "X-Seq": seq})


def watchdog():
    """iPhone 拔掉/锁屏后帧停了，就定期试着重开；空闲太久就自动退出。（守护线程）"""
    while True:
        time.sleep(5)
        if S.idle_quit_min > 0 and time.time() - S.last_activity > S.idle_quit_min * 60:
            print(f"[server] 空闲 {S.idle_quit_min:g} 分钟，自动退出", flush=True)
            S.quit.set(); return
        if not S.perm_ready.is_set():
            continue
        g = S.grabber
        stale = g is not None and g.sink.t_last and time.time() - g.sink.t_last > 15
        never = g is not None and not g.sink.t_last and time.time() - S.opened_at > 20  # 打开了却一直没帧（iPhone 锁屏时会这样）
        if g is None or stale or never:
            if S.perm == "granted":
                print("[server] 无设备或帧停了，尝试重开…", flush=True)
                open_device(wait=3)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="hil capture server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--device", default=None, help="设备名子串（默认：iOS 设备）")
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--any", action="store_true", help="不限定 iOS 设备")
    ap.add_argument("--idle", type=float, default=None, help="空闲多少分钟自动退出（默认取 hil.json 的 idle_quit_min，0=永不）")
    a = ap.parse_args(argv)
    try:
        from hil import config as C
        S.idle_quit_min = float(C.load().get("idle_quit_min", 10))
    except Exception:
        pass
    if a.idle is not None:
        S.idle_quit_min = a.idle
    S.last_activity = time.time()

    import signal
    srv = ThreadingHTTPServer((HOST, a.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[server] http://{HOST}:{a.port}  pid={os.getpid()}", flush=True)
    threading.Thread(target=watchdog, daemon=True).start()

    def setup():
        ok = capture.request_camera_access(timeout=600)
        S.perm = "granted" if ok else "denied"
        print(f"[server] 相机权限: {S.perm} (status={capture.camera_auth_status()})", flush=True)
        if ok:
            open_device(ios=not (a.any or a.device or a.index is not None), name=a.device, index=a.index, wait=8)
            S.perm_ready.set()
        else:
            S.err = "相机权限未授予：系统设置 → 隐私与安全性 → 相机 → 打开「HIL Capture」"

    threading.Thread(target=setup, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: S.quit.set())
    # 主线程必须跑 Cocoa run loop：AVFoundation 的设备插拔通知靠它派发，否则 iPhone 插上后设备列表不更新
    from Foundation import NSDate, NSRunLoop
    rl = NSRunLoop.currentRunLoop()
    try:
        while not S.quit.is_set():
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))
    except KeyboardInterrupt:
        pass
    finally:
        if S.grabber:
            S.grabber.close()
        print("[server] bye", flush=True)
        os._exit(0)


if __name__ == "__main__":
    main()
