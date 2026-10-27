"""仿真闭环演示：随机目标 → 定位 → 点击，统计命中误差和迭代次数。真机到手前用它调定位算法。"""
from __future__ import annotations

import math
import random
import sys
import time

from hil.link import LoopbackLink
from hil.locate import LocCfg, Locator
from hil.pointer import PointerSpec
from hil.sim import MotionModel, SimGrabber, SimPhone


def demo(n: int = 6, seed: int = 0, verbose: bool = False, motion: MotionModel | None = None,
         auto_hide: int | None = 3, calibrate: bool = True) -> int:
    rng = random.Random(seed)
    phone = SimPhone(motion=motion or MotionModel(base_gain=1.3, accel=3.0, noise=0.4),
                     auto_hide_reports=auto_hide, seed=seed)
    grab = SimGrabber(phone)
    link = LoopbackLink(hid=phone, verbose=False)
    log = (lambda s: print("   ", s, file=sys.stderr)) if verbose else None
    loc = Locator(link, grab.grab_array, PointerSpec(), LocCfg(settle=0.0, stable_gap=0.0, per_report_s=0.0, stable_timeout=0.0, corner_px=0), log=log)
    t0 = time.time()
    if calibrate:
        cal = loc.calibrate()
        print("标定增益表 (px/count):", {k: round(v, 2) for k, v in cal.gains.items()})
    errs, iters, fails = [], [], 0
    for i in range(n):
        tx, ty = rng.uniform(40, phone.width - 40), rng.uniform(40, phone.height - 40)
        h0 = len(loc.history)
        try:
            loc.tap(tx, ty)
            x, y, _ = phone.taps[-1]
            e = math.hypot(x - tx, y - ty); it = len(loc.history) - h0
            errs.append(e); iters.append(it)
            print(f"目标 ({tx:.0f},{ty:.0f}) 点在 ({x:.0f},{y:.0f}) 误差 {e:.1f}px  迭代 {it}  报告数 {phone.report_count}")
        except Exception as ex:  # noqa
            fails += 1; print(f"目标 ({tx:.0f},{ty:.0f}) 失败: {ex}")
    # 文本
    link.type_text("hello 123\n"); link.key("GUI+H")
    print(f"输入回放: {phone.typed_text()!r}")
    print(f"汇总: {n} 个目标, 失败 {fails}, 平均误差 {sum(errs)/max(1,len(errs)):.1f}px, 最大 {max(errs) if errs else 0:.1f}px, "
          f"平均迭代 {sum(iters)/max(1,len(iters)):.1f}, 总耗时 {time.time()-t0:.2f}s, 共 {len(link.transcript)} 条指令")
    return 0 if fails == 0 else 1
