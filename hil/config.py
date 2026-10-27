"""hil.json：串口、指针颜色阈值、定位参数、标定结果。放在项目根目录。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from hil.locate import Calib, LocCfg
from hil.pointer import PointerSpec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("HIL_CONFIG", os.path.join(ROOT, "hil.json"))

DEFAULT = {
    "port": None,              # 串口；None = 自动找 /dev/cu.usbmodem*
    "link": "serial",          # serial | loop | echo（serial = 经守护进程）
    "link_transport": "serial",  # 守护进程到板子：serial | ble
    "ble_name": "HIL-Bridge",
    "ble_token": None,         # 固件串口 `ST` 里的 token=
    "capture": "remote",       # remote | sim
    "brain_scale": 0.5,        # 给大脑看的截图缩放；tap 坐标按这个缩放换算
    "idle_quit_min": 10,       # 两个守护进程空闲多少分钟自动退出（0=永不）；下次任何命令会自动拉起
    "pointer": asdict(PointerSpec()),
    "loc": asdict(LocCfg()),
    "calib": asdict(Calib()),
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULT))
    if os.path.exists(PATH):
        with open(PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save(cfg: dict):
    with open(PATH, "w") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)


def pointer_spec(cfg: dict) -> PointerSpec:
    return PointerSpec.from_dict(cfg["pointer"])


def loc_cfg(cfg: dict) -> LocCfg:
    return LocCfg(**cfg["loc"])


def calib(cfg: dict) -> Calib:
    return Calib(**cfg["calib"])
