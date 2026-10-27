"""反馈式指针定位：蓝牙鼠标只有相对位移，所以「看指针在哪 → 挪一点 → 再看」直到落到目标上。

关键点：
  * iOS 有鼠标加速：每 count 对应的像素数随每条报告的速度变化。固件把大位移拆成 ±127/报告，
    所以 |counts|≥127 时增益基本饱和，小位移增益更低。calibrate() 在几个量级上实测增益表，
    predict_px() 按「拆报告」的方式预测像素，counts_for() 反解。
  * 真机参数一定和仿真不同：在线自适应（观测/预测 的比值）+ 阻尼，保证即便标定不准也能收敛。
  * 指针丢了（自动隐藏/被遮挡）：先微动让它显形，再不行就 park 到左上角重来（边缘钳制 = 确定的原点）。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import numpy as np

from hil import protocol as P
from hil.pointer import PointerHit, PointerSpec, find_pointer


class LocateError(RuntimeError):
    pass


@dataclass
class LocCfg:
    tol: float = 6.0          # 到目标多少像素内算到
    max_iter: int = 16
    settle: float = 0.12      # 发完 MV 等画面刷新（真机按延迟调；仿真 0）
    max_counts: int = 500     # 单次迭代每轴最多 counts（避免一把甩飞）
    gain0: float = 2.0        # 没标定时的 像素/count
    damping: float = 0.85
    adapt: float = 0.5        # 在线增益修正混合系数
    park_counts: int = 1500   # 够把指针从任何位置甩到角落即可（127 count/报告 ≈ 450 px，12 条报告够横跨整屏）
    corner_px: int = 70       # 指针中心离左上角小于此值 = 还贴在角落（nudge 还没送达）
    nudge_counts: int = 60    # park 后往里挪一点让指针完整露出来
    roi_margin: int = 300     # 迭代中只在预测位置附近找
    min_counts: int = 1
    probe_counts: int = 100   # 没标定时前两步最多发这么多 counts，先学增益再大步走
    edge_margin: int = 6      # 指针贴到屏幕边缘（被钳制）时不学增益
    per_report_s: float = 0.01  # 固件 mvdelay≥连接间隔时报告不排队，这里只补一点余量
    stable_tol: float = 2.0     # 连续两帧指针位置差小于此值才算「停住」
    stable_timeout: float = 2.5 # 最多等这么久指针停住
    stable_gap: float = 0.08    # 两次观察之间的间隔


@dataclass
class Calib:
    """增益表：{每条报告的 count 量级: 像素/count}。"""
    gains: dict = field(default_factory=dict)   # {"4": 1.1, "32": 1.9, "127": 5.6}
    scale_x: float = 1.0                        # 在线修正系数
    scale_y: float = 1.0

    def gain_at(self, per_report: float, gain0: float) -> float:
        if not self.gains:
            return gain0
        xs = sorted(float(k) for k in self.gains)
        ys = [self.gains[str(int(k)) if str(int(k)) in self.gains else str(k)] for k in xs]
        return float(np.interp(min(abs(per_report), 127.0), xs, ys))

    def predict_px(self, counts: int, gain0: float, axis: str = "x") -> float:
        """按固件拆分方式（±127/报告）预测位移像素（同轴单独算，忽略另一轴对速度的影响）。"""
        s = self.scale_x if axis == "x" else self.scale_y
        total = 0.0
        for step, _ in P.split_move(counts, 0):
            total += step * self.gain_at(step, gain0)
        return total * s

    def counts_for(self, px: float, gain0: float, axis: str, max_counts: int) -> int:
        """二分反解：要走 px 像素需要多少 counts。"""
        if abs(px) < 1e-6:
            return 0
        sign = 1 if px > 0 else -1
        lo, hi = 0, max_counts
        target = abs(px)
        if self.predict_px(hi, gain0, axis) * 1 < target:
            return sign * hi
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if abs(self.predict_px(mid, gain0, axis)) < target:
                lo = mid
            else:
                hi = mid
        # 取更接近的那个
        a, b = abs(abs(self.predict_px(lo, gain0, axis)) - target), abs(abs(self.predict_px(hi, gain0, axis)) - target)
        return sign * (lo if a <= b else hi)


class Locator:
    def __init__(self, link, grab: Callable[..., np.ndarray], spec: PointerSpec, cfg: Optional[LocCfg] = None,
                 calib: Optional[Calib] = None, log: Optional[Callable[[str], None]] = None):
        self.link, self.grab, self.spec = link, grab, spec
        self.cfg = cfg or LocCfg()
        self.calib = calib or Calib()
        self.log = log or (lambda s: None)
        self.last: Optional[PointerHit] = None
        self.frame: Optional[np.ndarray] = None
        self.history: list[dict] = []

    # ---- 发送 ----
    def _move(self, dx: int, dy: int):
        """发 MV 并按报告条数等待传输（BLE 一条报告约一个连接间隔）。"""
        self.link.move(dx, dy)
        n = max(len(list(P.split_move(dx, 0))), len(list(P.split_move(0, dy))))
        time.sleep(self.cfg.settle + n * self.cfg.per_report_s)

    # ---- 观察 ----
    def _find(self, roi, expect) -> Optional[PointerHit]:
        self.frame = self.grab(fresh=True)
        hit = find_pointer(self.frame, self.spec, roi, expect=expect) if roi else None
        if hit is None:
            hit = find_pointer(self.frame, self.spec, expect=expect)
        return hit

    def observe(self, roi: Optional[tuple] = None, fresh: bool = True,
                expect: Optional[tuple] = None) -> Optional[PointerHit]:
        """取帧找指针，并等它停住（连续两帧位置一致），避免把 BLE 排队中、还在动的位置当结果。"""
        cfg = self.cfg
        prev = self._find(roi, expect)
        deadline = time.time() + cfg.stable_timeout
        while time.time() < deadline:
            time.sleep(cfg.stable_gap)
            cur = self._find(roi, expect)
            if prev is not None and cur is not None and math.hypot(cur.x - prev.x, cur.y - prev.y) <= cfg.stable_tol:
                self.last = cur
                return cur
            if prev is None and cur is None:
                break  # 两次都没找到，别干等
            prev = cur
        self.last = prev
        return prev

    def _roi_around(self, x: float, y: float) -> tuple:
        m = self.cfg.roi_margin
        return (x - m, y - m, 2 * m, 2 * m)

    # ---- 复位 / 找回 ----
    def park(self) -> Optional[PointerHit]:
        """甩到左上角（边缘钳制 → 确定位置），再往里挪一点让指针完整露出。"""
        n = self.cfg.park_counts
        self._move(-n, -n)
        if self.cfg.nudge_counts:
            self._move(self.cfg.nudge_counts, self.cfg.nudge_counts)
        hit = self.observe()
        for _ in range(3):  # nudge 还没送到时指针仍贴角落：再等等
            if hit is None or not (hit.x < self.cfg.corner_px and hit.y < self.cfg.corner_px) or not self.cfg.nudge_counts:
                break
            time.sleep(0.3)
            hit = self.observe()
        self.log(f"park -> {hit and (round(hit.x), round(hit.y))}")
        return hit

    def recover(self) -> PointerHit:
        # 1) 可能是自动隐藏了：微动一下
        self._move(2, 0); self._move(-2, 0)
        hit = self.observe()
        if hit:
            return hit
        # 2) 复位
        hit = self.park()
        if hit:
            return hit
        raise LocateError("画面里找不到指针：检查指针颜色阈值(hil.json pointer)、AssistiveTouch 是否开、iPhone 是否亮屏")

    # ---- 标定 ----
    def calibrate(self, steps=(4, 12, 32, 64, 127), repeats: int = 3) -> Calib:
        gains = {}
        for n in steps:
            vals = []
            for _ in range(repeats):
                p0 = self.park()
                if p0 is None:
                    raise LocateError("标定时找不到指针（park 后）")
                self._move(n, n)
                p1 = self.observe()
                if p1 is None:
                    raise LocateError(f"标定 n={n} 后找不到指针")
                gx, gy = (p1.x - p0.x) / n, (p1.y - p0.y) / n
                vals.append((gx + gy) / 2)
                self.log(f"calib n={n}: dx={p1.x-p0.x:.1f} dy={p1.y-p0.y:.1f} -> gain {vals[-1]:.3f} px/count")
            med = float(np.median(vals))
            vals = [v for v in vals if v <= 3 * med] or vals  # 丢掉 BLE 排队造成的离群大值
            gains[str(n)] = float(np.median(vals))
        self.calib = Calib(gains=gains)
        return self.calib

    # ---- 逼近 ----
    def move_to(self, tx: float, ty: float) -> PointerHit:
        cfg, cal = self.cfg, self.calib
        hit = self.last if (self.last is not None and self.frame is not None) else None
        if hit is None:
            hit = self.observe() or self.recover()
        stuck = 0
        quant_hits = 0  # 发了 ±1~2 count 却没动的次数（系统有死区/量化），到了就放宽容差
        for it in range(cfg.max_iter):
            ex, ey = tx - hit.x, ty - hit.y
            dist = math.hypot(ex, ey)
            self.history.append({"iter": it, "pos": (hit.x, hit.y), "err": (ex, ey)})
            gain1 = max(cal.gain_at(1, cfg.gain0) * cal.scale_x, cal.gain_at(1, cfg.gain0) * cal.scale_y)
            tol = cfg.tol if quant_hits < 2 else max(cfg.tol, 2.0 * gain1)
            if dist <= tol:
                return hit
            cap = cfg.max_counts if (cal.gains or it >= 2) else cfg.probe_counts
            cx = cal.counts_for(ex * cfg.damping, cfg.gain0, "x", cap)
            cy = cal.counts_for(ey * cfg.damping, cfg.gain0, "y", cap)
            if cx == 0 and abs(ex) > cfg.tol:
                cx = int(math.copysign(cfg.min_counts, ex))
            if cy == 0 and abs(ey) > cfg.tol:
                cy = int(math.copysign(cfg.min_counts, ey))
            pred_x, pred_y = cal.predict_px(cx, cfg.gain0, "x"), cal.predict_px(cy, cfg.gain0, "y")
            self._move(cx, cy)
            expect = (hit.x + pred_x, hit.y + pred_y)
            new = self.observe(self._roi_around(*expect), expect=expect)
            if new is None:
                new = self.recover()
                hit = new
                continue
            moved = math.hypot(new.x - hit.x, new.y - hit.y)
            if moved < 0.5 and max(abs(cx), abs(cy)) <= 2:
                quant_hits += 1
            if moved < 0.5 and (abs(cx) >= 3 or abs(cy) >= 3):
                stuck += 1
                if stuck >= 2:  # 发了位移却纹丝不动：多半认错了指针，复位重来
                    self.log("stuck -> park")
                    cal.scale_x = cal.scale_y = 1.0
                    hit = self.park() or self.recover(); stuck = 0
                    continue
            else:
                stuck = 0
            # 在线修正：观测/预测（方向一致、位移不太小、且没被屏幕边缘钳住时才学）
            H, W = self.frame.shape[:2]
            at_edge = new.x < cfg.edge_margin or new.y < cfg.edge_margin or new.x > W - 1 - cfg.edge_margin or new.y > H - 1 - cfg.edge_margin
            for axis, c, pred, obs in (("x", cx, pred_x, new.x - hit.x), ("y", cy, pred_y, new.y - hit.y)):
                if abs(c) >= 2 and abs(pred) > 1.5 and obs * pred > 0 and not at_edge:
                    ratio = max(0.3, min(3.0, obs / pred))
                    cur = cal.scale_x if axis == "x" else cal.scale_y
                    val = max(0.2, min(5.0, cur * ((1 - cfg.adapt) + cfg.adapt * ratio)))
                    if axis == "x": cal.scale_x = val
                    else: cal.scale_y = val
            self.log(f"it{it}: pos=({hit.x:.0f},{hit.y:.0f}) err=({ex:.0f},{ey:.0f}) send=({cx},{cy}) "
                     f"-> ({new.x:.0f},{new.y:.0f}) scale=({cal.scale_x:.2f},{cal.scale_y:.2f})")
            hit = new
        raise LocateError(f"{cfg.max_iter} 次迭代仍未到达 ({tx},{ty})，最后在 ({hit.x:.0f},{hit.y:.0f})")

    # ---- 动作 ----
    def tap(self, x: float, y: float, btn: str = "l") -> PointerHit:
        hit = self.move_to(x, y)
        self.link.click(btn)
        return hit

    def long_press(self, x: float, y: float, ms: int = 800) -> PointerHit:
        hit = self.move_to(x, y)
        self.link.long_press(ms)
        return hit

    def drag(self, x0: float, y0: float, x1: float, y1: float, hold_ms: int = 150) -> PointerHit:
        self.move_to(x0, y0)
        self.link.button("l", True)
        time.sleep(hold_ms / 1000)
        try:
            hit = self.move_to(x1, y1)
        finally:
            self.link.button("l", False)
        return hit

    def save_calib(self, path: str):
        with open(path, "w") as f:
            json.dump(asdict(self.calib), f, indent=1)

    def load_calib(self, path: str):
        with open(path) as f:
            self.calib = Calib(**json.load(f))
