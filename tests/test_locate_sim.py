import math
import random

import pytest

from hil.link import LoopbackLink
from hil.locate import Calib, LocCfg, LocateError, Locator
from hil.pointer import PointerSpec, find_pointer
from hil.sim import MotionModel, SimGrabber, SimPhone


def make(seed=0, **phone_kw):
    phone = SimPhone(seed=seed, **phone_kw)
    link = LoopbackLink(phone, verbose=False)
    loc = Locator(link, SimGrabber(phone).grab_array, PointerSpec(), LocCfg(settle=0.0, stable_gap=0.0, per_report_s=0.0, stable_timeout=0.0, corner_px=0))
    return phone, link, loc


def test_find_pointer_ignores_red_button():
    phone, _, _ = make()
    frame = SimGrabber(phone).grab_array()
    hit = find_pointer(frame, PointerSpec())
    assert hit is not None and abs(hit.x - phone.x) < 2 and abs(hit.y - phone.y) < 2
    phone.visible = False
    assert find_pointer(SimGrabber(phone).grab_array(), PointerSpec()) is None  # 红色大按钮不会被当成指针


def test_park_and_calibrate():
    phone, link, loc = make()
    hit = loc.park()
    assert hit is not None and abs(hit.x - phone.x) < 3 and abs(hit.y - phone.y) < 3 and hit.x < 400
    cal = loc.calibrate()
    gains = [cal.gains[k] for k in ("4", "32", "127")]
    assert gains[0] < gains[1] < gains[2]  # 加速度：大步增益更高


@pytest.mark.parametrize("motion", [
    MotionModel(base_gain=1.0, accel=0.0, noise=0.0),
    MotionModel(base_gain=1.3, accel=3.0, noise=0.5),
    MotionModel(base_gain=4.0, accel=1.5, noise=1.0, dead_zone=1),
    MotionModel(base_gain=0.4, accel=6.0, noise=0.2),
])
def test_tap_accuracy_various_motion(motion):
    phone, link, loc = make(motion=motion, auto_hide_reports=3)
    loc.calibrate()
    rng = random.Random(1)
    for _ in range(6):
        tx, ty = rng.uniform(30, phone.width - 30), rng.uniform(30, phone.height - 30)
        loc.tap(tx, ty)
        x, y, _ = phone.taps[-1]
        quant = 2.0 * motion.base_gain if motion.dead_zone else 0.0  # 有死区时最细只能到 2 count
        assert math.hypot(x - tx, y - ty) <= max(loc.cfg.tol, quant) + 1.0


def test_tap_without_calibration_still_converges():
    phone, link, loc = make(motion=MotionModel(base_gain=3.0, accel=2.0))
    loc.tap(900, 2000)
    x, y, _ = phone.taps[-1]
    assert math.hypot(x - 900, y - 2000) <= loc.cfg.tol + 1.0


def test_recover_after_hidden_pointer():
    phone, link, loc = make(auto_hide_reports=0)
    phone.visible = False
    loc.tap(500, 500)
    assert math.hypot(phone.taps[-1][0] - 500, phone.taps[-1][1] - 500) <= loc.cfg.tol + 1.0


def test_drag():
    phone, link, loc = make()
    loc.drag(300, 1500, 300, 600)
    assert phone.press_pos is not None and abs(phone.press_pos[1] - 1500) < 10
    assert abs(phone.taps[-1][1] - 600) < 10


def test_locate_error_when_pointer_never_visible():
    phone, link, loc = make()
    phone.background[:] = 0
    phone.visible = False
    phone.mouse_report = lambda *a, **k: None  # 指针彻底不动不显示
    loc.cfg.max_iter = 3
    with pytest.raises(LocateError):
        loc.tap(100, 100)


def test_calib_predict_and_inverse():
    cal = Calib(gains={"4": 1.0, "32": 2.0, "127": 5.0})
    px = cal.predict_px(300, 2.0, "x")
    c = cal.counts_for(px, 2.0, "x", 500)
    assert abs(c - 300) <= 1
    assert cal.counts_for(-px, 2.0, "y", 500) == -c
    assert cal.counts_for(0, 2.0, "x", 500) == 0
