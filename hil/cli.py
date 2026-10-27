"""命令行入口：python -m hil <命令> ...

Claude Code 当大脑时就用这些命令：grab 看图 → tap/type/key 操作 → grab 再看。
  python -m hil app                      # 构建并启动 HIL Capture.app（取帧服务）
  python -m hil status                   # 取帧服务 + 链路状态
  python -m hil grab [out.png] [--scale 0.5] [--fresh]
  python -m hil find [--debug dbg.png]   # 在当前帧找指针
  python -m hil tap X Y | long X Y [ms] | drag X0 Y0 X1 Y1 | goto X Y   # 坐标按 --scale 换算（默认 hil.json brain_scale）
  python -m hil type "ascii text" | paste "任意文本" | key GUI+H | cc HOME | wheel 3 | move dx dy | click | release
  python -m hil park | calib | send "MV 10 10"
  python -m hil run script.txt           # 一行一个动作，走 Runner
  python -m hil sim-demo                 # 仿真闭环演示
全局参数：--link serial|loop|echo  --port /dev/cu.xxx  --capture remote|sim  --scale 0.5  -v
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from hil import config as C


def _log(s):
    print(s, file=sys.stderr)


def build_env(a):
    """按参数搭好 grab / link / locator。仿真模式下 link 和 grab 共享同一台 SimPhone。"""
    cfg = C.load()
    link_kind = a.link or cfg["link"]
    cap_kind = a.capture or cfg["capture"]
    spec = C.pointer_spec(cfg)
    phone = None
    if cap_kind == "sim" or link_kind == "loop":
        from hil.sim import SimPhone
        phone = SimPhone()
        if cap_kind == "sim":
            spec = C.pointer_spec(cfg)  # 仿真指针颜色就是默认 sim-red
    if cap_kind == "sim":
        from hil.sim import SimGrabber
        grab = SimGrabber(phone)
    else:
        from hil.client import RemoteGrabber
        grab = RemoteGrabber()
    from hil.link import open_link, LinkError

    class LazyLink:
        """第一次真正用到时才开串口；grab/find 这类不碰链路的命令不会因为没插板子而失败。"""
        name = link_kind
        _real = None

        def _get(self):
            if self._real is None:
                self._real = open_link(link_kind, port=a.port or cfg["port"], hid=phone, verbose=a.verbose)
            return self._real

        def __getattr__(self, item):
            return getattr(self._get(), item)

        def close(self):
            if self._real is not None:
                self._real.close()

    link = LazyLink()
    from hil.locate import Locator
    lc = C.loc_cfg(cfg)
    if cap_kind == "sim":
        lc.settle = lc.stable_gap = lc.per_report_s = lc.stable_timeout = 0.0; lc.corner_px = 0
    loc = Locator(link, grab.grab_array, spec, lc, C.calib(cfg), log=_log if a.verbose else None)
    scale = a.scale if a.scale is not None else cfg["brain_scale"]
    return cfg, grab, link, loc, scale, phone


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hil", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--link", choices=["serial", "direct", "loop", "echo"], default=None, help="serial=经守护进程(默认) direct=直接开串口(会复位板子)")
    ap.add_argument("--port", default=None)
    ap.add_argument("--capture", choices=["remote", "sim"], default=None)
    ap.add_argument("--scale", type=float, default=None, help="大脑看的图相对真实帧的缩放")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("app"); sub.add_parser("status"); sub.add_parser("devices")
    sub.add_parser("up", help="启动取帧服务和链路守护进程并报告状态（空闲 idle_quit_min 分钟后它们会自动退出）")
    sub.add_parser("stop", help="停掉两个守护进程")
    p = sub.add_parser("grab"); p.add_argument("out", nargs="?", default="captures/frame.png"); p.add_argument("--fresh", action="store_true")
    p.add_argument("--scale", type=float, dest="grab_scale", default=None, help="同全局 --scale")
    p = sub.add_parser("find"); p.add_argument("--debug", default=None)
    for name in ("tap", "goto"):
        p = sub.add_parser(name); p.add_argument("x", type=float); p.add_argument("y", type=float)
    p = sub.add_parser("long"); p.add_argument("x", type=float); p.add_argument("y", type=float); p.add_argument("ms", nargs="?", type=int, default=800)
    p = sub.add_parser("drag"); [p.add_argument(n, type=float) for n in ("x0", "y0", "x1", "y1")]
    p = sub.add_parser("type"); p.add_argument("text")
    p = sub.add_parser("paste"); p.add_argument("text"); p.add_argument("--settle", type=float, default=None, help="复制后等几秒再按 ⌘V（默认 5）")
    p = sub.add_parser("key"); p.add_argument("spec"); p.add_argument("n", nargs="?", type=int, default=1)
    p = sub.add_parser("cc"); p.add_argument("name")
    p = sub.add_parser("wheel"); p.add_argument("n", type=int)
    p = sub.add_parser("move"); p.add_argument("dx", type=int); p.add_argument("dy", type=int)
    p = sub.add_parser("click"); p.add_argument("btn", nargs="?", default="l")
    sub.add_parser("release"); sub.add_parser("park"); sub.add_parser("home")
    sub.add_parser("listen", help="把板子切到侦听模式（以蓝牙键盘身份广播），提示用户去 iPhone 设置→蓝牙手工连 HIL-Bridge")
    sub.add_parser("normal", help="把板子切回正常模式（不广播 HID 身份，空闲自动踢 iPhone 让屏幕键盘恢复）")
    p = sub.add_parser("calib"); p.add_argument("--steps", default="4,12,32,64,127")
    p = sub.add_parser("send"); p.add_argument("line")
    p = sub.add_parser("run"); p.add_argument("script"); p.add_argument("--max-steps", type=int, default=50)
    sub.add_parser("step", help="交互式：每步存帧，终端里输动作")
    p = sub.add_parser("sim-demo"); p.add_argument("--n", type=int, default=6); p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("pointer-sample", help="从一帧里采样指针颜色写入 hil.json"); p.add_argument("image"); p.add_argument("x", type=int); p.add_argument("y", type=int)
    a = ap.parse_args(argv)

    if a.cmd == "app":
        subprocess.run([os.path.join(C.ROOT, "tools", "make_app.sh")], check=True)
        from hil.client import ensure_server
        print(json.dumps(ensure_server(), ensure_ascii=False, indent=1)); return 0
    if a.cmd in ("listen", "normal"):
        from hil.link import set_adv_mode, listen_and_wait
        port = C.load().get("port")
        if a.cmd == "normal":
            st = set_adv_mode(False, port=port)
            print(f"板子已回正常模式（不以键盘身份广播；空闲 {st.get('hididle')} s 没操作命令就踢开 iPhone、屏幕键盘恢复）。")
            return 0
        st = listen_and_wait(port=port,
                             prompt=lambda: print(">>> 板子已进侦听模式。请在 iPhone 设置→蓝牙 点一下 HIL-Bridge 连接。"))
        if st.get("conn") == "1":
            print("✅ iPhone 已连上（conn=1），可以用了。用完 `hil normal` 或 `hil stop` 让它恢复空闲踢开、放回屏幕键盘。")
        else:
            print("⌛ 还没连上。去 iPhone 设置→蓝牙点 HIL-Bridge；连上后直接用命令即可，或再跑一次 `hil listen`。")
        return 0
    if a.cmd in ("up", "stop"):
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(url, t=5):
            try:
                with op.open(url, timeout=t) as r:
                    return json.loads(r.read())
            except Exception:
                return None
        if a.cmd == "stop":
            results = {}
            for name, port in (("capture", 8765), ("link", 8766)):
                ok = bool(get(f"http://127.0.0.1:{port}/quit", 3))
                results[name] = ok
                print(name, "stopped" if ok else "not running")
            if results.get("link"):   # 链路守护进程退出时会自己把板子切回正常模式
                print("板子会在守护进程退出时切回正常模式（iPhone 松开、屏幕键盘恢复）。")
            else:   # 它没在跑：兜底直接切一下
                try:
                    from hil.link import set_adv_mode
                    set_adv_mode(False, port=C.load().get("port"))
                    print("板子已切回正常模式（iPhone 松开、屏幕键盘恢复）。")
                except Exception as e:
                    print("切回正常模式失败（板子串口没插？）:", e)
            return 0
        from hil.client import ensure_server, RemoteGrabber
        cap = ensure_server()
        rg = RemoteGrabber(timeout=4)
        for _ in range(24):  # 取帧服务刚起来要几秒才找到 iPhone
            if cap.get("ok") and cap.get("seq", 0) > 0:
                break
            time.sleep(0.5)
            try:
                cap = rg.status()
            except Exception:
                pass
        if cap.get("ok") and cap.get("seq", 0) > 0:
            print(f"capture: ok  {cap.get('device')} {cap.get('width')}x{cap.get('height')}")
        else:
            print("capture:", cap.get("hint") or cap.get("error") or "没有画面（iPhone 接线、解锁了吗？）")
        # iPhone HID 连没连：先用直串口看；没连就切侦听模式让用户手工连。
        # （板子被踢开后 iOS 不会自动重连、软件 WAKE 也叫不回来——只有板子以键盘身份广播 + 用户手工点才可靠）
        from hil.link import open_link, SerialLink, listen_and_wait
        port = C.load().get("port")
        hid_note = "未知"
        try:
            sl = SerialLink(port=port); st0 = sl.status(); sl.close()
            if st0.get("conn") == "1":
                hid_note = "已连"
            else:
                print(">>> iPhone 没连。把板子切到侦听模式，请在 iPhone 设置→蓝牙 点一下 HIL-Bridge（最多等 60s）…")
                st0 = listen_and_wait(port=port)
                hid_note = "已连" if st0.get("conn") == "1" else "未连（去 iPhone 设置→蓝牙点 HIL-Bridge；连上后直接用命令即可）"
        except Exception as e:
            hid_note = f"串口检查失败: {e}（板子 USB 插了吗？）"
        try:
            lk = open_link("serial")  # BLE 守护进程按名字连板子；侦听模式下命令服务仍在
            fw = None
            for _ in range(30):
                try:
                    fw = lk.status(); break
                except Exception:
                    time.sleep(1)
            ds = lk.daemon_status()
            if fw:
                print("link:", ds.get("transport"), ds.get("port"), "| 固件", fw.get("fw"), "| iPhone HID", hid_note)
                hididle = fw.get("hididle")
                if hididle and hididle != "0":
                    print(f"用完 `hil normal` 或 `hil stop`：板子回正常模式、空闲 {hididle} s 没操作命令就踢开 iPhone，屏幕键盘恢复。再要用跑 `hil up`，按提示去设置→蓝牙点 HIL-Bridge 连上。")
            else:
                print("link:", ds.get("transport"), "ERROR", ds.get("error"))
        except Exception as e:
            print("link: ERROR", e)
        idle = C.load().get("idle_quit_min", 10)
        print(f"空闲 {idle} 分钟后两个服务会自动退出，再执行任何命令会重新拉起。")
        return 0
    if a.cmd == "devices":
        from hil.client import RemoteGrabber
        for d in RemoteGrabber().devices():
            print(d); return 0
    if a.cmd == "pointer-sample":
        import numpy as np
        from PIL import Image
        from hil.pointer import spec_from_sample, find_pointer
        from dataclasses import asdict
        arr = np.asarray(Image.open(a.image).convert("RGB"))
        spec = spec_from_sample(arr, a.x, a.y, name="iphone")
        hit = find_pointer(arr, spec)
        print("spec:", spec.to_json()); print("验证检测:", hit)
        cfg = C.load(); cfg["pointer"] = asdict(spec); C.save(cfg); print("已写入", C.PATH); return 0

    cfg, grab, link, loc, scale, phone = build_env(a)
    s = 1.0 / scale
    try:
        if a.cmd == "status":
            try:
                print("capture:", json.dumps(grab.status() if hasattr(grab, "status") else {"sim": True}, ensure_ascii=False))
            except Exception as e:
                print("capture: ERROR", e)
            try:
                ds = link.daemon_status() if hasattr(link, "daemon_status") else {}
                print("link:", ds.get("transport", link.name), ds.get("port", ""), link.status() if link.ping() else "no response")
            except Exception as e:
                print("link:", link.name, "ERROR", e)
        elif a.cmd == "grab":
            if a.grab_scale is not None:
                scale = a.grab_scale
            t0 = time.time(); img = grab.grab(fresh=a.fresh, scale=scale)
            os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True); img.save(a.out)
            print(f"{a.out} {img.width}x{img.height} (scale {scale}, {1000*(time.time()-t0):.0f} ms)")
        elif a.cmd == "find":
            from hil.pointer import find_pointer, debug_image
            frame = grab.grab_array(fresh=True); hit = find_pointer(frame, loc.spec)
            print("pointer:", hit, f"-> 大脑坐标 ({hit.x*scale:.0f},{hit.y*scale:.0f})" if hit else "")
            if a.debug:
                from PIL import Image
                Image.fromarray(debug_image(frame, loc.spec, hit)).save(a.debug); print("debug:", a.debug)
        elif a.cmd in ("tap", "goto"):
            hit = (loc.tap if a.cmd == "tap" else loc.move_to)(a.x * s, a.y * s)
            print(f"{a.cmd} -> 指针在 ({hit.x:.0f},{hit.y:.0f})，{len(loc.history)} 次迭代")
        elif a.cmd == "long":
            hit = loc.long_press(a.x * s, a.y * s, a.ms); print(f"long -> ({hit.x:.0f},{hit.y:.0f})")
        elif a.cmd == "drag":
            hit = loc.drag(a.x0 * s, a.y0 * s, a.x1 * s, a.y1 * s); print(f"drag -> ({hit.x:.0f},{hit.y:.0f})")
        elif a.cmd == "type":
            link.type_text(a.text); print("typed", len(a.text))
        elif a.cmd == "paste":
            link.paste_text(a.text, **({"settle": a.settle} if a.settle is not None else {})); print("pasted", len(a.text))
        elif a.cmd == "key":
            print(link.key(a.spec, a.n).raw)
        elif a.cmd == "cc":
            print(link.consumer(a.name).raw)
        elif a.cmd == "home":
            print(link.home().raw)
        elif a.cmd == "wheel":
            print(link.wheel(a.n).raw)
        elif a.cmd == "move":
            print(link.move(a.dx, a.dy).raw)
        elif a.cmd == "click":
            print(link.click(a.btn).raw)
        elif a.cmd == "release":
            print(link.release().raw)
        elif a.cmd == "park":
            print("park ->", loc.park())
        elif a.cmd == "calib":
            cal = loc.calibrate(tuple(int(x) for x in a.steps.split(",")))
            from dataclasses import asdict
            cfg["calib"] = asdict(cal); C.save(cfg); print("gains:", cal.gains, "已写入", C.PATH)
        elif a.cmd == "send":
            print(link.send(a.line).raw)
        elif a.cmd in ("run", "step"):
            from hil.loop import Runner, ScriptPolicy, InteractivePolicy
            if a.cmd == "run":
                lines = [l for l in open(a.script).read().splitlines() if l.strip() and not l.startswith("#")]
                pol = ScriptPolicy(lines)
            else:
                pol = InteractivePolicy()
            r = Runner(grab.grab_array, loc, link, pol, scale=scale, max_steps=a.max_steps if a.cmd == "run" else 500)
            for rec in r.run():
                print(json.dumps(rec, ensure_ascii=False))
            print("run dir:", r.run_dir)
        elif a.cmd == "sim-demo":
            from hil.sim_demo import demo
            return demo(n=a.n, seed=a.seed, verbose=a.verbose)
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
