"""在画面里找指针（iOS AssistiveTouch 的圆形指针）。

思路：把指针设成和 App 内容区分度高的颜色（设置 → 辅助功能 → 指针控制 → 颜色），
HSV 阈值 → 连通域 → 挑面积、圆度都像指针的那块 → 质心。
真机颜色/大小到手后用 spec_from_sample() 从一帧里采样生成阈值，存到 hil.json。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class PointerSpec:
    name: str = "sim-red"
    # OpenCV HSV: H 0~179, S/V 0~255。hue 可能绕 0（红色），用两段
    h_ranges: list = field(default_factory=lambda: [[0, 8], [172, 179]])
    s_min: int = 150
    v_min: int = 120
    min_area: int = 600      # 指针面积范围：真机用 pointer-sample 从实际 blob 定
    max_area: int = 20000
    fill_lo: float = 0.60   # 面积/外接框面积：圆 0.785（被边缘切成半圆/四分圆也是 0.785），矩形 1.0
    fill_hi: float = 0.90
    min_aspect: float = 0.45
    open_ksize: int = 3
    fill_holes: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "PointerSpec":
        return cls(**d)


@dataclass
class PointerHit:
    x: float
    y: float
    area: int
    circularity: float   # 4πA/P²，圆≈1
    bbox: tuple          # x, y, w, h
    score: float = 0.0

    @property
    def xy(self) -> tuple[float, float]:
        return self.x, self.y


def _mask(rgb: np.ndarray, spec: PointerSpec) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    m = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in spec.h_ranges:
        m |= cv2.inRange(hsv, (lo, spec.s_min, spec.v_min), (hi, 255, 255))
    if spec.open_ksize > 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (spec.open_ksize, spec.open_ksize))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    if spec.fill_holes:  # iOS 彩色指针是「彩色圆环 + 灰色半透明中心」：把外轮廓填实，环变成实心圆再判形状
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(m, cnts, -1, 255, thickness=cv2.FILLED)
    return m


def find_pointer_candidates(rgb: np.ndarray, spec: PointerSpec, roi: Optional[tuple] = None) -> list[PointerHit]:
    """所有像指针的候选，按得分降序。roi=(x,y,w,h) 只在局部找；返回全图坐标。"""
    ox = oy = 0
    img = rgb
    if roi is not None:
        x, y, w, h = [int(v) for v in roi]
        x, y = max(0, x), max(0, y)
        img = rgb[y:y + h, x:x + w]
        ox, oy = x, y
        if img.size == 0:
            return []
    m = _mask(img, spec)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < spec.min_area or area > spec.max_area:
            continue
        x, y, w, h = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
        fill = area / float(w * h)
        aspect = min(w, h) / float(max(w, h))
        if not (spec.fill_lo <= fill <= spec.fill_hi) or aspect < spec.min_aspect:
            continue
        comp = (labels[y:y + h, x:x + w] == i).astype(np.uint8)
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cnts:
            continue
        per = max(cv2.arcLength(c, True) for c in cnts)
        circ = 4 * np.pi * area / (per * per) if per > 0 else 0.0
        score = min(1.0, circ) * (1.0 - abs(fill - 0.785) / 0.785) * (0.5 + 0.5 * aspect)
        out.append(PointerHit(float(cents[i][0]) + ox, float(cents[i][1]) + oy, area, float(circ), (x + ox, y + oy, w, h), float(score)))
    out.sort(key=lambda h: -h.score)
    return out


def find_pointer(rgb: np.ndarray, spec: PointerSpec, roi: Optional[tuple] = None,
                 expect: Optional[tuple] = None, expect_radius: float = 250.0) -> Optional[PointerHit]:
    """最像指针的那个；给了 expect=(x,y) 时，优先选离预测位置最近且得分不太差的候选。"""
    cands = find_pointer_candidates(rgb, spec, roi)
    if not cands:
        return None
    if expect is not None:
        near = [c for c in cands if np.hypot(c.x - expect[0], c.y - expect[1]) <= expect_radius and c.score >= cands[0].score * 0.6]
        if near:
            return min(near, key=lambda c: np.hypot(c.x - expect[0], c.y - expect[1]))
    return cands[0]


def spec_from_sample(rgb: np.ndarray, x: int, y: int, radius: int = 80, name: str = "sampled",
                     hue_tol: int = 10, s_floor: int = 90) -> PointerSpec:
    """从一帧里已知指针位置附近采样颜色，生成阈值（真机标定用）。
    取 (x,y) 周围 radius 内饱和度 ≥ s_floor 的像素当作指针的彩色部分（iOS 彩色指针是圆环，圆心是灰的）。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    y0, y1, x0, x1 = max(0, y - radius), y + radius + 1, max(0, x - radius), x + radius + 1
    patch = hsv[y0:y1, x0:x1].reshape(-1, 3)
    sat = patch[patch[:, 1] >= s_floor]
    if len(sat) < 30:
        sat = patch[np.argsort(patch[:, 1])[-200:]]  # 实在没饱和色就取最饱和的 200 个
    # 色相取众数（红色绕 0，用直方图而不是中位数）
    hist = np.bincount(sat[:, 0], minlength=180).astype(float)
    hist = np.convolve(np.concatenate([hist[-5:], hist, hist[:5]]), np.ones(5), "same")[5:-5]
    h = int(np.argmax(hist))
    s_min = int(max(40, np.percentile(sat[:, 1], 20) - 30)); v_min = int(max(40, np.percentile(sat[:, 2], 20) - 50))
    lo, hi = h - hue_tol, h + hue_tol
    ranges = [[0, hi], [180 + lo, 179]] if lo < 0 else ([[lo, 179], [0, hi - 180]] if hi > 179 else [[lo, hi]])
    spec = PointerSpec(name=name, h_ranges=ranges, s_min=s_min, v_min=v_min, min_area=50, max_area=10**6)
    cands = find_pointer_candidates(rgb, spec)
    near = [c for c in cands if np.hypot(c.x - x, c.y - y) < radius]
    if near:
        a = near[0].area
        spec.min_area, spec.max_area = int(a * 0.3), int(a * 2.5)
    return spec


def debug_image(rgb: np.ndarray, spec: PointerSpec, hit: Optional[PointerHit]) -> np.ndarray:
    out = rgb.copy()
    m = _mask(rgb, spec)
    out[m > 0] = (out[m > 0] * 0.5 + np.array([0, 255, 0]) * 0.5).astype(np.uint8)
    if hit:
        cv2.circle(out, (int(hit.x), int(hit.y)), 30, (255, 0, 255), 3)
    return out
