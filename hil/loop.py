"""闭环框架：取帧 → 判断(Policy) → 执行(Action) → 记录，循环。

Policy 是「大脑」接口：给它当前画面（numpy RGB）和上下文，返回一个 Action 或 None(结束)。
内置：ScriptPolicy（固定动作表）、InteractivePolicy（终端里人来当大脑，每步存帧给你看）。
Claude Code 当大脑时不走 Policy：直接用 `python -m hil grab` 看图，再 `python -m hil tap X Y` 等命令，
每一步都是独立进程，状态都在 hil.json / 取帧服务 / 固件里，所以也不需要常驻循环。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image

from hil.locate import Locator


@dataclass
class Action:
    kind: str            # tap | long | drag | type | paste | key | cc | wheel | move | click | wait | done
    args: dict = field(default_factory=dict)
    note: str = ""

    @classmethod
    def parse(cls, line: str) -> "Action":
        """'tap 300 500' / 'type hello' / 'key GUI+H' / 'drag 100 900 100 300' / 'wait 1.5' / 'done'"""
        toks = line.strip().split()
        if not toks:
            raise ValueError("empty")
        k = toks[0].lower()
        if k in ("tap", "long", "goto"):
            a = {"x": float(toks[1]), "y": float(toks[2])}
            if k == "long" and len(toks) > 3:
                a["ms"] = int(toks[3])
            return cls(k, a)
        if k == "drag":
            return cls(k, {"x0": float(toks[1]), "y0": float(toks[2]), "x1": float(toks[3]), "y1": float(toks[4])})
        if k in ("type", "paste"):
            return cls(k, {"text": line.strip()[len(k):].strip()})
        if k in ("key", "cc"):
            return cls(k, {"spec": toks[1]})
        if k == "wheel":
            return cls(k, {"n": int(toks[1])})
        if k == "move":
            return cls(k, {"dx": int(toks[1]), "dy": int(toks[2])})
        if k == "click":
            return cls(k, {"btn": toks[1] if len(toks) > 1 else "l"})
        if k == "wait":
            return cls(k, {"s": float(toks[1]) if len(toks) > 1 else 1.0})
        if k in ("done", "park", "home", "release"):
            return cls(k)
        raise ValueError(f"未知动作 {k}")


def execute(action: Action, loc: Locator, link, scale: float = 1.0) -> Any:
    """执行一个动作。scale：大脑看到的图相对真实帧的缩放（0.5 表示坐标要 ×2）。"""
    a, k = action.args, action.kind
    s = 1.0 / scale
    if k == "tap":
        return loc.tap(a["x"] * s, a["y"] * s)
    if k == "goto":
        return loc.move_to(a["x"] * s, a["y"] * s)
    if k == "long":
        return loc.long_press(a["x"] * s, a["y"] * s, a.get("ms", 800))
    if k == "drag":
        return loc.drag(a["x0"] * s, a["y0"] * s, a["x1"] * s, a["y1"] * s)
    if k == "type":
        return link.type_text(a["text"])
    if k == "paste":
        return link.paste_text(a["text"])
    if k == "key":
        return link.key(a["spec"])
    if k == "cc":
        return link.consumer(a["spec"])
    if k == "wheel":
        return link.wheel(a["n"])
    if k == "move":
        return link.move(a["dx"], a["dy"])
    if k == "click":
        return link.click(a.get("btn", "l"))
    if k == "wait":
        return time.sleep(a["s"])
    if k == "park":
        return loc.park()
    if k == "home":
        return link.home()
    if k == "release":
        return link.release()
    if k == "done":
        return None
    raise ValueError(k)


class Policy:
    def decide(self, frame: np.ndarray, ctx: dict) -> Optional[Action]:
        raise NotImplementedError


class ScriptPolicy(Policy):
    def __init__(self, actions: list):
        self.actions = [a if isinstance(a, Action) else Action.parse(a) for a in actions]
        self.i = 0

    def decide(self, frame, ctx):
        if self.i >= len(self.actions):
            return None
        a = self.actions[self.i]; self.i += 1
        return a


class InteractivePolicy(Policy):
    """每步把帧存成文件，终端里输入动作行。"""

    def decide(self, frame, ctx):
        print(f"[step {ctx['step']}] 当前帧: {ctx['frame_path']}  (动作: tap x y | long x y ms | drag.. | type .. | paste .. | key SPEC | cc HOME | wheel n | wait s | done)")
        while True:
            try:
                line = input("> ")
            except EOFError:
                return None
            if not line.strip():
                continue
            try:
                return Action.parse(line)
            except Exception as e:
                print("  ?", e)


class Runner:
    def __init__(self, grab: Callable[..., np.ndarray], loc: Locator, link, policy: Policy,
                 run_dir: str = "captures/runs", scale: float = 1.0, max_steps: int = 50):
        self.grab, self.loc, self.link, self.policy = grab, loc, link, policy
        self.scale, self.max_steps = scale, max_steps
        self.run_dir = os.path.join(run_dir, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.run_dir, exist_ok=True)
        self.log_path = os.path.join(self.run_dir, "log.jsonl")

    def _save_frame(self, frame: np.ndarray, step: int) -> str:
        img = Image.fromarray(frame)
        if self.scale != 1.0:
            img = img.resize((int(img.width * self.scale), int(img.height * self.scale)), Image.LANCZOS)
        p = os.path.join(self.run_dir, f"{step:03d}.png"); img.save(p)
        return p

    def run(self) -> list[dict]:
        records = []
        with open(self.log_path, "a") as logf:
            for step in range(self.max_steps):
                frame = self.grab(fresh=True)
                fp = self._save_frame(frame, step)
                ctx = {"step": step, "frame_path": fp, "size": frame.shape[:2][::-1], "scale": self.scale}
                action = self.policy.decide(frame, ctx)
                rec = {"step": step, "frame": fp, "action": None if action is None else {"kind": action.kind, **action.args}}
                t0 = time.time()
                if action is None or action.kind == "done":
                    rec["result"] = "done"; records.append(rec); logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    break
                try:
                    r = execute(action, self.loc, self.link, self.scale)
                    rec["result"] = "ok" if r is None else (list(r.xy) if hasattr(r, "xy") else str(r))
                except Exception as e:  # noqa
                    rec["result"] = f"error: {type(e).__name__}: {e}"
                rec["dt"] = round(time.time() - t0, 3)
                records.append(rec); logf.write(json.dumps(rec, ensure_ascii=False) + "\n"); logf.flush()
        return records
