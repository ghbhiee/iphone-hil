"""取帧服务（hil.capture_server / HIL Capture.app）的客户端。和 SimGrabber 同接口：grab_array(fresh) / grab()。"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from typing import Optional

import numpy as np
from PIL import Image

DEFAULT_BASE = "http://127.0.0.1:8765"
APP_PATH = "~/Applications/HIL Capture.app"


class CaptureError(RuntimeError):
    pass


class RemoteGrabber:
    name = "remote"

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 8.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不查系统代理（macOS 上那一步要 1~2 s）

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise CaptureError(f"连不上取帧服务 {self.base}（{e}）。先 `python -m hil app` 启动 HIL Capture.app") from e

    def status(self) -> dict:
        return json.loads(self._get("/status")[2])

    def devices(self) -> list:
        return json.loads(self._get("/devices")[2])

    def reopen(self, **sel) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in sel.items())
        return json.loads(self._get("/reopen" + ("?" + qs if qs else ""))[2])

    def grab_array(self, fresh: bool = False, timeout: Optional[float] = None) -> np.ndarray:
        t = timeout or self.timeout - 1
        code, hdr, body = self._get(f"/frame.raw?fresh={1 if fresh else 0}&timeout={t}")
        if code != 200:
            raise CaptureError(json.loads(body).get("error", body[:200]))
        w, h = int(hdr["X-Width"]), int(hdr["X-Height"])
        return np.frombuffer(body, dtype=np.uint8).reshape(h, w, 3)

    def grab(self, fresh: bool = False, scale: float = 1.0, timeout: Optional[float] = None) -> Image.Image:
        img = Image.fromarray(self.grab_array(fresh=fresh, timeout=timeout))
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        return img

    @property
    def size(self) -> tuple[int, int]:
        s = self.status()
        return int(s["width"] or 0), int(s["height"] or 0)


def ensure_server(base: str = DEFAULT_BASE, launch: bool = True, wait: float = 10.0) -> dict:
    """服务没起就 open HIL Capture.app，等它就绪。返回 /status。"""
    import os
    g = RemoteGrabber(base, timeout=3)
    try:
        return g.status()
    except CaptureError:
        if not launch:
            raise
    app = os.path.expanduser(APP_PATH)
    if not os.path.exists(app):  # 第一次用：生成 App 壳（相机权限按 App 归属，第一次启动会弹授权框）
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(["/bin/zsh", os.path.join(root, "tools", "make_app.sh")], check=True)
    subprocess.run(["open", "-g", app], check=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            return g.status()
        except CaptureError:
            time.sleep(0.5)
    raise CaptureError("取帧服务没起来，看 captures/server.log")
