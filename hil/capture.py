"""Mac 端取帧：通过 USB（AVFoundation 官方录屏通道，和 QuickTime「新建影片录制」同一条路）实时拿 iPhone 画面。

用法（库）：
    from hil.capture import Grabber
    g = Grabber.open_iphone()      # 或 Grabber.open(name_substr="FaceTime") 用别的采集设备
    img = g.grab()                 # PIL.Image (RGB)，总是最新一帧
    g.close()

用法（命令行）：
    python -m hil.capture devices            # 列出所有采集设备（会先打开"允许 iOS 屏幕采集设备"开关）
    python -m hil.capture grab out.png       # 抓一帧存盘
    python -m hil.capture bench 5            # 连续抓 5 秒，打印帧率/延迟
    python -m hil.capture stream dir/ 2      # 每 2 秒存一帧到目录（看稳定性）

原理：
  1. CoreMediaIO 的 kCMIOHardwarePropertyAllowScreenCaptureDevices 置 1（ffmpeg/QuickTime 内部也是这么做的），
     之后插着 USB 的 iPhone 会作为一个 muxed（视频+音频）AVCaptureDevice 出现，modelID 为 "iOS Device"。
  2. AVCaptureSession + AVCaptureVideoDataOutput(BGRA) 持续收帧，回调里只记住最新一帧的 CVPixelBuffer。
  3. grab() 时才把最新一帧拷成 numpy / PIL，不做多余转换。
"""
from __future__ import annotations

import ctypes
import os
import warnings
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

import objc
from Foundation import NSObject
import AVFoundation as AVF
import CoreMedia
import libdispatch

# ---------------------------------------------------------------------------
# 1. 打开"允许 iOS 设备作为屏幕采集设备"开关（CoreMediaIO）
# ---------------------------------------------------------------------------
_cmio = None


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


class _CMIOObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


def enable_ios_screen_capture_devices() -> int:
    """等价于 ffmpeg avfoundation 里的那段：让 USB 上的 iPhone/iPad 以 AVCaptureDevice 形式出现。返回 OSStatus（0=成功）。"""
    global _cmio
    if _cmio is None:
        _cmio = ctypes.CDLL("/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO")
        _cmio.CMIOObjectSetPropertyData.restype = ctypes.c_int32
        _cmio.CMIOObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_CMIOObjectPropertyAddress), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
    kCMIOObjectSystemObject = 1
    addr = _CMIOObjectPropertyAddress(_fourcc("yes "), _fourcc("glob"), 0)  # AllowScreenCaptureDevices / global / main
    allow = ctypes.c_uint32(1)
    return _cmio.CMIOObjectSetPropertyData(kCMIOObjectSystemObject, ctypes.byref(addr), 0, None, 4, ctypes.byref(allow))


def camera_auth_status() -> int:
    """0 未询问 / 1 受限 / 2 拒绝 / 3 已授权。iPhone 屏幕采集设备和摄像头走同一道 TCC「相机」权限。"""
    return int(AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo))


def request_camera_access(timeout: float = 120.0) -> bool:
    """没授权就弹系统框申请（归属到当前宿主 App：终端 / Claude 等），阻塞到用户点完或超时。"""
    st = camera_auth_status()
    if st == 3:
        return True
    if st in (1, 2):
        print("相机权限被拒绝/受限：到 系统设置 → 隐私与安全性 → 相机 里给当前终端/App 打开。", file=sys.stderr)
        return False
    done = threading.Event(); result = {"ok": False}

    def _cb(granted):
        result["ok"] = bool(granted); done.set()

    print("正在申请相机权限（iPhone 屏幕采集也靠它），请在弹出的系统对话框里点「允许」…", file=sys.stderr)
    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeVideo, _cb)
    done.wait(timeout)
    return result["ok"]


# ---------------------------------------------------------------------------
# 2. 设备枚举
# ---------------------------------------------------------------------------
@dataclass
class DeviceInfo:
    index: int
    name: str
    model: str
    uid: str
    muxed: bool
    formats: list  # [(w, h, max_fps)]

    @property
    def is_ios(self) -> bool:
        return "iOS" in self.model or "iPhone" in self.model or "iPad" in self.model

    def __str__(self):
        fm = ", ".join(f"{w}x{h}@{fps:g}" for w, h, fps in self.formats[:4])
        more = f" (+{len(self.formats)-4})" if len(self.formats) > 4 else ""
        tag = " [iOS 屏幕]" if self.is_ios else ""
        return f"[{self.index}] {self.name}{tag}  model={self.model!r} muxed={self.muxed} formats: {fm}{more}"


def _all_devices():
    """所有 AVCaptureDevice（视频/muxed）。先用 DiscoverySession，再并上老接口，去重。"""
    devs = []
    seen = set()
    types = []
    for tname in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeExternalUnknown",
                  "AVCaptureDeviceTypeBuiltInWideAngleCamera", "AVCaptureDeviceTypeContinuityCamera",
                  "AVCaptureDeviceTypeDeskViewCamera"):
        t = getattr(AVF, tname, None)
        if t is not None:
            types.append(t)
    for media in (AVF.AVMediaTypeMuxed, AVF.AVMediaTypeVideo):
        try:
            ds = AVF.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
                types, media, AVF.AVCaptureDevicePositionUnspecified)
            for d in ds.devices():
                if d.uniqueID() not in seen:
                    seen.add(d.uniqueID()); devs.append(d)
        except Exception:
            pass
    for media in (AVF.AVMediaTypeMuxed, AVF.AVMediaTypeVideo):
        try:
            for d in AVF.AVCaptureDevice.devicesWithMediaType_(media) or []:
                if d.uniqueID() not in seen:
                    seen.add(d.uniqueID()); devs.append(d)
        except Exception:
            pass
    return devs


def _format_info(fmt):
    desc = fmt.formatDescription()
    dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(desc)
    fps = 0.0
    for r in fmt.videoSupportedFrameRateRanges() or []:
        fps = max(fps, float(r.maxFrameRate()))
    return int(dims.width), int(dims.height), fps


def list_devices(enable_ios: bool = True, wait_ios: float = 0.0) -> list[DeviceInfo]:
    """列出采集设备。wait_ios>0 时最多等这么多秒直到出现 iOS 设备（插上 USB 后设备是异步冒出来的）。"""
    if enable_ios:
        enable_ios_screen_capture_devices()
    deadline = time.time() + wait_ios
    while True:
        infos = []
        for i, d in enumerate(_all_devices()):
            fmts = []
            try:
                fmts = [_format_info(f) for f in d.formats()]
            except Exception:
                pass
            infos.append(DeviceInfo(i, str(d.localizedName()), str(d.modelID()), str(d.uniqueID()),
                                    bool(d.hasMediaType_(AVF.AVMediaTypeMuxed)), fmts))
        if wait_ios <= 0 or any(x.is_ios for x in infos) or time.time() > deadline:
            return infos
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# 3. 抓帧器
# ---------------------------------------------------------------------------
_cv = ctypes.CDLL("/System/Library/Frameworks/CoreVideo.framework/CoreVideo")
_cv.CVPixelBufferLockBaseAddress.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cv.CVPixelBufferLockBaseAddress.restype = ctypes.c_int32
_cv.CVPixelBufferUnlockBaseAddress.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cv.CVPixelBufferUnlockBaseAddress.restype = ctypes.c_int32
_cv.CVPixelBufferGetBaseAddress.argtypes = [ctypes.c_void_p]
_cv.CVPixelBufferGetBaseAddress.restype = ctypes.c_void_p
for _fn in ("CVPixelBufferGetWidth", "CVPixelBufferGetHeight", "CVPixelBufferGetBytesPerRow"):
    getattr(_cv, _fn).argtypes = [ctypes.c_void_p]
    getattr(_cv, _fn).restype = ctypes.c_size_t
_cv.CVPixelBufferGetPixelFormatType.argtypes = [ctypes.c_void_p]
_cv.CVPixelBufferGetPixelFormatType.restype = ctypes.c_uint32
for _fn in ("CVPixelBufferGetWidthOfPlane", "CVPixelBufferGetHeightOfPlane", "CVPixelBufferGetBytesPerRowOfPlane"):
    getattr(_cv, _fn).argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    getattr(_cv, _fn).restype = ctypes.c_size_t
_cv.CVPixelBufferGetBaseAddressOfPlane.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_cv.CVPixelBufferGetBaseAddressOfPlane.restype = ctypes.c_void_p
_cv.CVPixelBufferGetPlaneCount.argtypes = [ctypes.c_void_p]
_cv.CVPixelBufferGetPlaneCount.restype = ctypes.c_size_t
_cv.CVPixelBufferRetain.argtypes = [ctypes.c_void_p]
_cv.CVPixelBufferRetain.restype = ctypes.c_void_p
_cv.CVPixelBufferRelease.argtypes = [ctypes.c_void_p]
_cv.CVPixelBufferRelease.restype = None
_cm = ctypes.CDLL("/System/Library/Frameworks/CoreMedia.framework/CoreMedia")
_cm.CMSampleBufferGetImageBuffer.argtypes = [ctypes.c_void_p]
_cm.CMSampleBufferGetImageBuffer.restype = ctypes.c_void_p
kCVPixelFormatType_32BGRA = _fourcc("BGRA")
PIXFMT_420V, PIXFMT_420F, PIXFMT_2VUY, PIXFMT_YUVS = _fourcc("420v"), _fourcc("420f"), _fourcc("2vuy"), _fourcc("yuvs")
warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)


def _raw_ptr(obj) -> int:
    """pyobjc 可能把 CF 对象给成桥接对象、PyObjCPointer 或 int，统一取裸指针。"""
    if isinstance(obj, int):
        return obj
    if hasattr(obj, "pointerAsInteger"):
        return obj.pointerAsInteger
    return objc.pyobjc_id(obj)


def _pixelbuffer_to_rgb(ptr: int) -> np.ndarray:
    """CVPixelBuffer 裸指针 -> HxWx3 uint8 RGB numpy（拷贝一份）。支持 BGRA / 420v / 420f (NV12) / 2vuy / yuvs。"""
    import cv2
    _cv.CVPixelBufferLockBaseAddress(ptr, 1)
    try:
        w = _cv.CVPixelBufferGetWidth(ptr)
        h = _cv.CVPixelBufferGetHeight(ptr)
        fmt = _cv.CVPixelBufferGetPixelFormatType(ptr)
        if fmt == kCVPixelFormatType_32BGRA:
            bpr = _cv.CVPixelBufferGetBytesPerRow(ptr)
            raw = ctypes.string_at(_cv.CVPixelBufferGetBaseAddress(ptr), bpr * h)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, bpr // 4, 4)[:, :w, :]
            return np.ascontiguousarray(arr[:, :, [2, 1, 0]])
        if fmt in (PIXFMT_420V, PIXFMT_420F):
            bpr0 = _cv.CVPixelBufferGetBytesPerRowOfPlane(ptr, 0)
            y = np.frombuffer(ctypes.string_at(_cv.CVPixelBufferGetBaseAddressOfPlane(ptr, 0), bpr0 * h), np.uint8).reshape(h, bpr0)[:, :w]
            h1 = _cv.CVPixelBufferGetHeightOfPlane(ptr, 1); bpr1 = _cv.CVPixelBufferGetBytesPerRowOfPlane(ptr, 1)
            uv = np.frombuffer(ctypes.string_at(_cv.CVPixelBufferGetBaseAddressOfPlane(ptr, 1), bpr1 * h1), np.uint8).reshape(h1, bpr1)[:, :w]
            yuv = np.vstack([y, uv])
            return cv2.cvtColor(np.ascontiguousarray(yuv), cv2.COLOR_YUV2RGB_NV12)
        if fmt in (PIXFMT_2VUY, PIXFMT_YUVS):
            bpr = _cv.CVPixelBufferGetBytesPerRow(ptr)
            raw = np.frombuffer(ctypes.string_at(_cv.CVPixelBufferGetBaseAddress(ptr), bpr * h), np.uint8).reshape(h, bpr)[:, :w * 2]
            packed = np.ascontiguousarray(raw).reshape(h, w, 2)
            return cv2.cvtColor(packed, cv2.COLOR_YUV2RGB_UYVY if fmt == PIXFMT_2VUY else cv2.COLOR_YUV2RGB_YUYV)
        raise RuntimeError(f"unsupported pixel format {fmt.to_bytes(4, 'big')!r}")
    finally:
        _cv.CVPixelBufferUnlockBaseAddress(ptr, 1)


class _FrameSink(NSObject, protocols=[objc.protocolNamed("AVCaptureVideoDataOutputSampleBufferDelegate")]):
    """AVCaptureVideoDataOutputSampleBufferDelegate：只记最新一帧。"""

    def init(self):
        self = objc.super(_FrameSink, self).init()
        if self is None:
            return None
        self.latest = None
        self.seq = 0
        self.dropped = 0
        self.t_last = 0.0
        self.first_info = None
        self.cond = threading.Condition()
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
        pb = _cm.CMSampleBufferGetImageBuffer(_raw_ptr(sample_buffer))
        if not pb:
            return
        _cv.CVPixelBufferRetain(pb)
        if self.first_info is None:
            fmt = _cv.CVPixelBufferGetPixelFormatType(pb)
            self.first_info = f"{_cv.CVPixelBufferGetWidth(pb)}x{_cv.CVPixelBufferGetHeight(pb)} {fmt.to_bytes(4, 'big').decode('latin1')}"
            print(f"[capture] first frame {self.first_info}", file=sys.stderr, flush=True)
        with self.cond:
            old, self.latest = self.latest, pb  # 持有 retain 过的裸指针
            self.seq += 1
            self.t_last = time.time()
            self.cond.notify_all()
        if old:
            _cv.CVPixelBufferRelease(old)

    def captureOutput_didDropSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
        self.dropped += 1
        if self.dropped <= 3:
            print(f"[capture] dropped frame #{self.dropped}", file=sys.stderr, flush=True)


class Grabber:
    """持续采集、按需取最新一帧。"""

    def __init__(self, device, prefer_max_res: bool = True, fps: Optional[float] = None,
                 want_format: Optional[str] = None, preset: Optional[str] = None):
        self.device = device
        self.want_format, self.want_preset = want_format, preset
        self.name = str(device.localizedName())
        self._locked = False
        self.sink = _FrameSink.alloc().init()
        self.session = AVF.AVCaptureSession.alloc().init()
        self.width = self.height = 0
        inp, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if inp is None:
            raise RuntimeError(f"无法打开采集设备 {self.name}: {err}")
        if not self.session.canAddInput_(inp):
            raise RuntimeError("session.canAddInput == False")
        self.session.addInput_(inp)
        # macOS 默认 preset=High 会把输出压到 1080p，而且 addInput 时会把设备 activeFormat 改成配合 preset 的；
        # 所以顺序必须是：addInput → preset=InputPriority → 再设 activeFormat（iPhone 才能出原生分辨率）
        self.preset = "High"
        presets = {"input": "AVCaptureSessionPresetInputPriority", "photo": "AVCaptureSessionPresetPhoto",
                   "high": "AVCaptureSessionPresetHigh", "medium": "AVCaptureSessionPresetMedium",
                   "1080": "AVCaptureSessionPreset1920x1080", "720": "AVCaptureSessionPreset1280x720"}
        want = self.want_preset or ("input" if prefer_max_res else None)
        if want and want != "none":
            const = getattr(AVF, presets.get(want, ""), None)
            if const is not None and self.session.canSetSessionPreset_(const):
                self.session.setSessionPreset_(const); self.preset = want
            else:
                self.preset = f"High(can't set {want})"
        self._configure_format(prefer_max_res or bool(self.want_format), fps)
        out = AVF.AVCaptureVideoDataOutput.alloc().init()
        try:
            avail = [int(x) for x in (out.availableVideoCVPixelFormatTypes() or [])]
        except Exception:
            avail = []
        self.avail_pixfmts = [int(x).to_bytes(4, "big").decode("latin1") for x in avail]
        if kCVPixelFormatType_32BGRA in avail:
            out.setVideoSettings_({"PixelFormatType": kCVPixelFormatType_32BGRA}); self.pixfmt = "BGRA"
        else:
            self.pixfmt = "native"  # 不强求，收到什么转什么
        out.setAlwaysDiscardsLateVideoFrames_(True)
        self.queue = libdispatch.dispatch_queue_create(b"hil.capture.frames", None)
        out.setSampleBufferDelegate_queue_(self.sink, self.queue)
        if not self.session.canAddOutput_(out):
            raise RuntimeError("session.canAddOutput == False")
        self.session.addOutput_(out)
        self.output = out
        self.events: list[str] = []
        self._observe_session()
        self.session.startRunning()
        if self._locked:
            self.device.unlockForConfiguration(); self._locked = False
        try:
            w, h, _ = _format_info(self.device.activeFormat())
            self.active_format = f"{w}x{h}"
        except Exception:
            self.active_format = "?"

    def _observe_session(self):
        from Foundation import NSNotificationCenter
        nc = NSNotificationCenter.defaultCenter()
        self._observers = []
        for name in ("AVCaptureSessionRuntimeErrorNotification", "AVCaptureSessionDidStartRunningNotification",
                     "AVCaptureSessionDidStopRunningNotification", "AVCaptureSessionWasInterruptedNotification",
                     "AVCaptureSessionInterruptionEndedNotification", "AVCaptureDeviceWasDisconnectedNotification"):
            const = getattr(AVF, name, None)
            if const is None:
                continue
            obs = nc.addObserverForName_object_queue_usingBlock_(const, None, None, (lambda n, _name=name: self._on_note(_name, n)))
            self._observers.append(obs)

    def _on_note(self, name, note):
        info = ""
        try:
            ui = note.userInfo() or {}
            err = ui.get(getattr(AVF, "AVCaptureSessionErrorKey", "AVCaptureSessionErrorKey"))
            if err is not None:
                info = f" error={err}"
        except Exception:
            pass
        msg = f"{time.strftime('%H:%M:%S')} {name}{info}"
        self.events.append(msg)
        print(f"[capture] {msg}", file=sys.stderr, flush=True)

    def diag(self) -> dict:
        conns = []
        try:
            for c in self.output.connections():
                conns.append({"active": bool(c.isActive()), "enabled": bool(c.isEnabled())})
        except Exception:
            pass
        return {"running": bool(self.session.isRunning()), "interrupted": bool(getattr(self.session, "isInterrupted", lambda: False)()),
                "connections": conns, "pixfmt": self.pixfmt, "avail_pixfmts": self.avail_pixfmts[:8],
                "first_frame": self.sink.first_info, "dropped": self.sink.dropped, "events": self.events[-5:]}

    def _configure_format(self, prefer_max_res, fps):
        try:
            fmts = list(self.device.formats())
        except Exception:
            return
        if not fmts:
            return
        best = None
        for f in fmts:
            w, h, mfps = _format_info(f)
            if self.want_format:
                if f"{w}x{h}" == self.want_format and (best is None or mfps > best[3]):
                    best = (f, w, h, mfps)
            elif best is None or (w * h, mfps) > (best[1] * best[2], best[3]):
                best = (f, w, h, mfps)
        if best and prefer_max_res:
            ok, err = self.device.lockForConfiguration_(None)
            if ok:
                try:
                    self.device.setActiveFormat_(best[0])
                    if fps:
                        self.device.setActiveVideoMinFrameDuration_(CoreMedia.CMTimeMake(1, int(fps)))
                    self._locked = True  # 先别解锁：session 在 startRunning 前会把 activeFormat 改回去（ffmpeg 同款处理）
                except Exception:
                    self.device.unlockForConfiguration()
            self.width, self.height = best[1], best[2]

    # ---- 构造 ----
    @classmethod
    def open(cls, name_substr: Optional[str] = None, index: Optional[int] = None, ios: bool = False,
             wait_ios: float = 8.0, **kw) -> "Grabber":
        if not request_camera_access():
            raise PermissionError("没有相机权限，拿不到画面（系统设置 → 隐私与安全性 → 相机）")
        enable_ios_screen_capture_devices()
        deadline = time.time() + wait_ios
        while True:
            devs = _all_devices()
            cands = []
            for i, d in enumerate(devs):
                if index is not None and i != index:
                    continue
                if name_substr and name_substr.lower() not in str(d.localizedName()).lower():
                    continue
                if ios and "iOS" not in str(d.modelID()):
                    continue
                cands.append(d)
            if cands:
                return cls(cands[0], **kw)
            if time.time() > deadline:
                names = [f"[{i}] {d.localizedName()} ({d.modelID()})" for i, d in enumerate(devs)]
                raise RuntimeError("找不到匹配的采集设备。现有设备：\n  " + "\n  ".join(names) +
                                   "\n(iPhone 要用数据线插在 Mac 上、已解锁并点过「信任此电脑」)")
            time.sleep(0.5)

    @classmethod
    def open_iphone(cls, **kw) -> "Grabber":
        return cls.open(ios=True, **kw)

    # ---- 取帧 ----
    def wait_frame(self, timeout: float = 5.0, min_seq: Optional[int] = None):
        """阻塞直到有新帧（seq > min_seq）。返回 (CVPixelBuffer 裸指针, seq)。Condition 用的是 RLock，可嵌套持锁调用。"""
        deadline = time.time() + timeout
        with self.sink.cond:
            while self.sink.latest is None or (min_seq is not None and self.sink.seq <= min_seq):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"{timeout}s 内没有收到画面帧（设备 {self.name}）。iPhone 是否锁屏/拔线？")
                self.sink.cond.wait(remaining)
            return self.sink.latest, self.sink.seq

    def grab_array(self, fresh: bool = False, timeout: float = 5.0) -> np.ndarray:
        """返回最新一帧 HxWx3 RGB。fresh=True 时等待一帧「从现在起」才到的新画面（动作之后想看结果就用它）。"""
        min_seq = self.sink.seq if fresh else None
        with self.sink.cond:
            pb, _ = self.wait_frame(timeout, min_seq)
            _cv.CVPixelBufferRetain(pb)
        try:
            return _pixelbuffer_to_rgb(pb)
        finally:
            _cv.CVPixelBufferRelease(pb)

    def grab(self, fresh: bool = False, timeout: float = 5.0) -> Image.Image:
        return Image.fromarray(self.grab_array(fresh=fresh, timeout=timeout))

    @property
    def seq(self) -> int:
        return self.sink.seq

    def close(self):
        try:
            self.session.stopRunning()
        except Exception:
            pass
        try:
            from Foundation import NSNotificationCenter
            for o in getattr(self, "_observers", []):
                NSNotificationCenter.defaultCenter().removeObserver_(o)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
# 4. ffmpeg 兜底（不依赖 pyobjc，慢一些：每次起进程 ~1s）
# ---------------------------------------------------------------------------
def ffmpeg_grab(device_name: str, out_path: str, ffmpeg: str = "ffmpeg") -> str:
    import subprocess
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "avfoundation", "-capture_raw_data", "0",
           "-i", f"{device_name}:none", "-frames:v", "1", out_path]
    subprocess.run(cmd, check=True, timeout=20)
    return out_path


# ---------------------------------------------------------------------------
# 5. 命令行
# ---------------------------------------------------------------------------
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("devices", help="列出采集设备"); p.add_argument("--wait", type=float, default=2.0)
    for name in ("grab", "bench", "stream"):
        p = sub.add_parser(name)
        p.add_argument("--device", "-d", default=None, help="设备名子串（默认：iOS 设备）")
        p.add_argument("--index", "-i", type=int, default=None)
        p.add_argument("--any", action="store_true", help="不限定 iOS 设备（配合 --device 测摄像头）")
        if name == "grab":
            p.add_argument("out", nargs="?", default="captures/frame.png")
            p.add_argument("--scale", type=float, default=1.0)
        elif name == "bench":
            p.add_argument("seconds", nargs="?", type=float, default=5.0)
        else:
            p.add_argument("dir"); p.add_argument("interval", nargs="?", type=float, default=2.0)
    a = ap.parse_args(argv)

    if a.cmd == "devices":
        for d in list_devices(wait_ios=a.wait):
            print(d)
        return 0

    g = Grabber.open(name_substr=a.device, index=a.index, ios=not (a.any or a.device or a.index is not None))
    print(f"已打开: {g.name}  格式 {g.width}x{g.height}", file=sys.stderr)
    try:
        if a.cmd == "grab":
            t0 = time.time(); img = g.grab(fresh=True)
            if a.scale != 1.0:
                img = img.resize((int(img.width * a.scale), int(img.height * a.scale)), Image.LANCZOS)
            os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
            img.save(a.out)
            print(f"{a.out}  {img.width}x{img.height}  耗时 {time.time()-t0:.2f}s")
        elif a.cmd == "bench":
            g.wait_frame(10)
            s0, t0 = g.seq, time.time(); n = 0; lat = []
            while time.time() - t0 < a.seconds:
                t1 = time.time(); g.grab_array(fresh=True); lat.append(time.time() - t1); n += 1
            dt = time.time() - t0
            print(f"采集 {g.seq - s0} 帧 / {dt:.1f}s = {(g.seq - s0)/dt:.1f} fps 到达；"
                  f"grab(fresh) {n} 次，平均 {1000*sum(lat)/len(lat):.0f} ms，最大 {1000*max(lat):.0f} ms")
        else:
            os.makedirs(a.dir, exist_ok=True); k = 0
            while True:
                img = g.grab(fresh=True); p = os.path.join(a.dir, f"{k:04d}.png"); img.save(p)
                print(time.strftime("%H:%M:%S"), p, img.size); k += 1; time.sleep(a.interval)
    finally:
        g.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
