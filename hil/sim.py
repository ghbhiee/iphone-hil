"""仿真手机：接收 HID 报告（和真 iPhone 收到的一样），维护 AssistiveTouch 风格的相对指针，渲染画面。

用途：没硬件时把「取帧 → 找指针 → 发 MV → 再取帧」整条闭环跑通，并给定位算法当测试台。
运动模型模仿 iOS 鼠标加速：每条报告位移 (dx,dy)，像素 = count * gain(speed)，speed 越大每 count 像素越多；
可加噪声、可自动隐藏指针、边缘钳制。参数到真机后按 calib 结果调。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from hil import protocol as P


def make_fake_ui(w: int, h: int, seed: int = 1) -> np.ndarray:
    """画一个像 App 的假界面：白底、彩色按钮、文字、列表线，里面故意放些红色元素干扰指针检测。"""
    rng = random.Random(seed)
    img = np.full((h, w, 3), 248, dtype=np.uint8)
    cv2.rectangle(img, (0, 0), (w, int(h * 0.09)), (255, 255, 255), -1)
    cv2.putText(img, "Health Report", (int(w * 0.06), int(h * 0.065)), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (30, 30, 30), 4)
    y = int(h * 0.12)
    colors = [(52, 120, 246), (52, 199, 89), (255, 149, 0), (255, 59, 48), (175, 82, 222), (90, 200, 250)]
    for i in range(10):
        c = colors[i % len(colors)]
        cv2.rectangle(img, (int(w * 0.06), y), (int(w * 0.94), y + int(h * 0.055)), (255, 255, 255), -1)
        cv2.rectangle(img, (int(w * 0.08), y + 18), (int(w * 0.08) + 70, y + int(h * 0.055) - 18), c, -1)
        cv2.putText(img, f"Item {i+1}  value {rng.randint(10,99)}", (int(w * 0.18), y + int(h * 0.036)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (40, 40, 40), 3)
        y += int(h * 0.065)
    # 一个红色大按钮当干扰（矩形，不是圆）
    cv2.rectangle(img, (int(w * 0.2), int(h * 0.86)), (int(w * 0.8), int(h * 0.92)), (255, 59, 48), -1)
    cv2.putText(img, "Delete", (int(w * 0.4), int(h * 0.9)), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 3)
    return img


@dataclass
class MotionModel:
    base_gain: float = 1.6     # 慢速时 像素/count
    accel: float = 2.5         # 速度到 127 时增益乘 (1+accel)
    noise: float = 0.0         # 每条报告像素级高斯噪声
    dead_zone: int = 0         # 小于此 count 不动（有些系统会吃掉 1 count）

    def pixels(self, dx: int, dy: int, rng: random.Random) -> tuple[float, float]:
        speed = min(127.0, math.hypot(dx, dy))
        g = self.base_gain * (1.0 + self.accel * (speed / 127.0) ** 1.3)
        if abs(dx) <= self.dead_zone: dx = 0
        if abs(dy) <= self.dead_zone: dy = 0
        px, py = dx * g, dy * g
        if self.noise:
            px += rng.gauss(0, self.noise); py += rng.gauss(0, self.noise)
        return px, py


@dataclass
class SimPhone:
    width: int = 1179
    height: int = 2556
    background: Optional[np.ndarray] = None
    pointer_color: tuple = (255, 59, 48)   # RGB
    pointer_radius: int = 22
    motion: MotionModel = field(default_factory=MotionModel)
    auto_hide_reports: Optional[int] = None  # 多少次「非移动」渲染后隐藏指针（模拟自动隐藏）；None 不隐藏
    seed: int = 0
    connected: bool = True

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        if self.background is None:
            self.background = make_fake_ui(self.width, self.height)
        self.x, self.y = self.width / 2.0, self.height / 2.0
        self.buttons = 0
        self.visible = True
        self.idle = 0
        self.taps: list[tuple[float, float, int]] = []   # (x, y, button)
        self.typed: list[tuple[int, list]] = []           # (mods, usages)
        self.consumer_log: list[int] = []
        self.report_count = 0
        self.press_pos: Optional[tuple[float, float]] = None

    # ---- HID 入口（FirmwareModel 调用） ----
    def mouse_report(self, buttons: int, dx: int, dy: int, wheel: int):
        self.report_count += 1
        if dx or dy:
            px, py = self.motion.pixels(dx, dy, self.rng)
            self.x = min(self.width - 1.0, max(0.0, self.x + px))
            self.y = min(self.height - 1.0, max(0.0, self.y + py))
            self.visible, self.idle = True, 0
        else:
            self.idle += 1
            if self.auto_hide_reports is not None and self.idle > self.auto_hide_reports:
                self.visible = False
        pressed = buttons & ~self.buttons
        released = self.buttons & ~buttons
        for b in (1, 2, 4):
            if pressed & b:
                self.press_pos = (self.x, self.y)
            if released & b:
                self.taps.append((self.x, self.y, b))
        self.buttons = buttons

    def key_report(self, mods: int, usages: list):
        if usages or mods:
            self.typed.append((mods, list(usages)))

    def consumer_report(self, usage: int):
        if usage:
            self.consumer_log.append(usage)

    # ---- 观察 ----
    def render(self) -> np.ndarray:
        img = self.background.copy()
        if self.visible:
            c = (int(self.x), int(self.y))
            cv2.circle(img, c, self.pointer_radius + 3, (255, 255, 255), -1, lineType=cv2.LINE_AA)
            cv2.circle(img, c, self.pointer_radius, self.pointer_color, -1, lineType=cv2.LINE_AA)
        return img

    def typed_text(self) -> str:
        """把键盘报告翻回 ASCII（只为测试断言）。"""
        inv = {}
        for ch in [chr(i) for i in range(32, 127)] + ["\n", "\t"]:
            try:
                inv[P.ascii_to_usage(ch)] = ch
            except ValueError:
                pass
        out = []
        for mods, usages in self.typed:
            for u in usages:
                out.append(inv.get((mods & 0x22 and 0x02 or 0, u), f"<{mods:02x}:{u:02x}>"))
        return "".join(out)


class SimGrabber:
    """和 RemoteGrabber 同接口。"""
    name = "sim"

    def __init__(self, phone: SimPhone):
        self.phone = phone
        self.seq = 0

    def grab_array(self, fresh: bool = False, timeout: float = 0) -> np.ndarray:
        self.seq += 1
        return self.phone.render()

    def grab(self, fresh: bool = False, scale: float = 1.0, timeout: float = 0):
        from PIL import Image
        img = Image.fromarray(self.grab_array())
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        return img

    @property
    def size(self):
        return self.phone.width, self.phone.height
