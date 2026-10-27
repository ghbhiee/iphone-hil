"""Mac 端 BLE 指令通道：用 CoreBluetooth 连 HIL-Bridge 的 Nordic UART 风格服务，send(line) 同步等回复。

必须跑在能拿到蓝牙权限的进程里（HIL Link.app，见 tools/make_link_app.sh）；Claude Code 宿主没有蓝牙 entitlement。
不和板子配对/绑定：否则 macOS 会把板子当成自己的鼠标键盘接管。安全靠 AUTH 口令（hil.json 的 ble_token）。
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

import objc
from Foundation import NSData, NSObject
import CoreBluetooth as CB
import libdispatch

from hil import protocol as P
from hil.link import Link, LinkError
from hil.protocol import Reply

NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


def _safe(fn):
    """delegate 回调的保护罩：Python 异常漏进 CoreBluetooth 会变成 NSException 把进程 abort 掉。"""
    def wrapper(self, *a):
        try:
            return fn(self, *a)
        except Exception as e:  # noqa
            try:
                self.owner._log(f"回调 {fn.__name__} 出错: {type(e).__name__}: {e}")
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    return wrapper


class _Delegate(NSObject, protocols=[objc.protocolNamed("CBCentralManagerDelegate"), objc.protocolNamed("CBPeripheralDelegate")]):
    def init(self):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self.owner = None
        return self

    # ---- central ----
    @_safe
    def centralManagerDidUpdateState_(self, central):
        self.owner._on_state(central.state())

    @_safe
    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, central, peripheral, adv, rssi):
        self.owner._on_discover(peripheral, adv, rssi)

    @_safe
    def centralManager_didConnectPeripheral_(self, central, peripheral):
        self.owner._on_connect(peripheral)

    @_safe
    def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
        self.owner._log(f"连接失败: {error}")
        self.owner._set_disconnected()

    @_safe
    def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
        self.owner._log(f"断开: {error}")
        self.owner._set_disconnected()

    # ---- peripheral ----
    @_safe
    def peripheral_didDiscoverServices_(self, peripheral, error):
        self.owner._on_services(peripheral, error)

    @_safe
    def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
        self.owner._on_chars(peripheral, service, error)

    @_safe
    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, peripheral, ch, error):
        self.owner._on_notify_state(ch, error)

    @_safe
    def peripheral_didUpdateValueForCharacteristic_error_(self, peripheral, ch, error):
        if error is None:
            self.owner._on_value(bytes(ch.value() or b""))

    @_safe
    def peripheral_didWriteValueForCharacteristic_error_(self, peripheral, ch, error):
        if error is not None:
            self.owner._log(f"写失败: {error}")


class BleLink(Link):
    """和 SerialLink 同接口。连接过程阻塞，最长 connect_timeout 秒。"""
    name = "ble"

    def __init__(self, token: str, device_name: str = "HIL-Bridge", connect_timeout: float = 30.0,
                 timeout: float = 5.0, verbose: bool = False, log=None):
        self.token, self.device_name, self.timeout, self.verbose = token, device_name, timeout, verbose
        self._logf = log or (lambda s: print(f"[ble] {s}", file=sys.stderr, flush=True))
        self._t0 = time.time()
        self.port = f"ble:{device_name}"
        self._lock = threading.Lock()          # 一次只发一条命令
        self._cv = threading.Condition()
        self._rxbuf = b""
        self._lines: list[str] = []
        self.n_lines = 0          # 收到的回复行总数（诊断用）
        self.n_notif = 0          # 收到的通知次数
        self.n_writes = 0         # 发出的写次数
        self.n_extra = 0          # 发命令前发现还有没消费的旧回复行（说明回复错位过）
        self._ready = threading.Event()        # 已订阅 TX、可以发命令
        self._powered = threading.Event()
        self._peripheral = None
        self._rx = self._tx = None
        self._write_type = CB.CBCharacteristicWriteWithoutResponse
        self._delegate = _Delegate.alloc().init(); self._delegate.owner = self
        self._queue = libdispatch.dispatch_queue_create(b"hil.ble", None)
        self._central = CB.CBCentralManager.alloc().initWithDelegate_queue_(self._delegate, self._queue)
        if not self._powered.wait(10):
            raise LinkError("蓝牙没有就绪（权限被拒或蓝牙关闭）：系统设置 → 隐私与安全性 → 蓝牙 → HIL Link")
        self._connect(connect_timeout)

    # ---- 连接流程 ----
    def _log(self, s):
        self._logf(f"+{time.time() - self._t0:6.1f}s {s}")

    def _on_state(self, state):
        self._log(f"central state={state}")
        if state == CB.CBManagerStatePoweredOn:
            self._powered.set()
        else:  # 蓝牙关了（Mac 睡眠时会关）：连接已失效，标记断开，等重新开了由守护进程主循环重连
            self._powered.clear()
            self._set_disconnected()

    def _connect(self, timeout: float):
        self._ready.clear()
        if not self._powered.wait(timeout):
            raise LinkError("蓝牙未开启")
        # 上一个进程留下的旧连接会被系统（bluetoothd）握着：附着上去发现服务要 20 多秒，而且板子因此不广播。
        # 先把它取消掉，等板子重新广播，再走干净的扫描路径（实测 1.6 s）。
        try:
            leftovers = [p for p in (self._central.retrieveConnectedPeripheralsWithServices_([CB.CBUUID.UUIDWithString_(NUS_SERVICE)]) or [])
                         if (p.name() or "") == self.device_name]
        except Exception:
            leftovers = []
        for p in leftovers:
            self._log("系统里有残留的旧连接，先取消")
            self._disconnected = threading.Event()
            p.setDelegate_(self._delegate)
            self._central.cancelPeripheralConnection_(p)
            self._disconnected.wait(4.0)
        self._log(f"扫描 {self.device_name} …")
        self._central.scanForPeripheralsWithServices_options_(None, None)
        if not self._ready.wait(timeout):
            self._central.stopScan()
            p, self._peripheral = self._peripheral, None
            self._rx = self._tx = None
            if p is not None:
                try:
                    self._central.cancelPeripheralConnection_(p)
                except Exception:
                    pass
            raise LinkError(f"{timeout}s 内没连上 {self.device_name}（板子通电了吗？是不是已经有两个连接了？）")
        # 鉴权
        r = self.send(f"AUTH {self.token}", _noauth=True)
        if not r.ok:
            raise LinkError(f"口令不对: {r.raw}（用串口 `ST` 看 token=，写进 hil.json 的 ble_token）")
        self._log("已连接并通过口令")

    def _on_discover(self, peripheral, adv, rssi):
        name = peripheral.name() or (adv.get(CB.CBAdvertisementDataLocalNameKey) if adv else None)
        if name != self.device_name or self._peripheral is not None or self._ready.is_set():
            return
        self._log(f"发现 {name} rssi={rssi}")
        self._central.stopScan()
        self._peripheral = peripheral
        peripheral.setDelegate_(self._delegate)
        self._central.connectPeripheral_options_(peripheral, None)

    def _on_connect(self, peripheral):
        self._log("已连接，发现服务…")
        peripheral.discoverServices_([CB.CBUUID.UUIDWithString_(NUS_SERVICE)])

    def _on_services(self, peripheral, error):
        if error is not None:
            self._log(f"发现服务失败: {error}"); return
        self._log(f"服务: {[str(x.UUID()) for x in (peripheral.services() or [])]}")
        for svc in peripheral.services() or []:
            if str(svc.UUID()).upper() == NUS_SERVICE:
                peripheral.discoverCharacteristics_forService_(None, svc)

    def _on_chars(self, peripheral, service, error):
        if error is not None:
            self._log(f"发现特征失败: {error}"); return
        for ch in service.characteristics() or []:
            u = str(ch.UUID()).upper()
            if u == NUS_RX:
                self._rx = ch
                props = int(ch.properties())
                self._write_type = CB.CBCharacteristicWriteWithoutResponse if (props & 0x04) else CB.CBCharacteristicWriteWithResponse
            elif u == NUS_TX:
                self._tx = ch
        self._log(f"特征: rx={self._rx is not None} tx={self._tx is not None}")
        if self._rx is not None and self._tx is not None:
            peripheral.setNotifyValue_forCharacteristic_(True, self._tx)

    def _on_notify_state(self, ch, error):
        if error is not None:
            self._log(f"订阅失败: {error}"); return
        p = self._peripheral
        if p is None:
            self._log("订阅回调迟到（连接已被放弃），忽略"); return
        self._log(f"已订阅回复通知，MTU 写长度 {p.maximumWriteValueLengthForType_(self._write_type)}")
        self._ready.set()

    def _on_value(self, data: bytes):
        with self._cv:
            self.n_notif += 1
            self._rxbuf += data
            while b"\n" in self._rxbuf:
                line, self._rxbuf = self._rxbuf.split(b"\n", 1)
                self._lines.append(line.decode("utf-8", "replace").strip()); self.n_lines += 1
            self._cv.notify_all()

    def _set_disconnected(self):
        self._ready.clear()
        self._peripheral = None
        self._rx = self._tx = None
        ev = getattr(self, "_disconnected", None)
        if ev is not None:
            ev.set()

    # ---- 发送 ----
    def connected_ble(self) -> bool:
        return self._ready.is_set()

    def reconnect(self, timeout: float = 15.0):
        self._set_disconnected()
        try:
            self._central.stopScan()
        except Exception:
            pass
        self._connect(timeout)

    def send(self, line: str, _noauth: bool = False) -> Reply:
        if not self._ready.is_set():
            raise LinkError("BLE 未连接")
        with self._lock:
            with self._cv:
                if self._lines:
                    self.n_extra += len(self._lines); self._log(f"丢弃旧回复 {self._lines}")
                self._lines.clear(); self._rxbuf = b""
            if self.verbose:
                print(f"→ {line}", file=sys.stderr)
            data = (line + "\n").encode("utf-8")
            self.n_writes += 1
            self._peripheral.writeValue_forCharacteristic_type_(NSData.dataWithBytes_length_(data, len(data)), self._rx, self._write_type)
            deadline = time.time() + self.timeout
            with self._cv:
                while True:
                    while self._lines:
                        got = self._lines.pop(0)
                        if not got:
                            continue
                        up = got.upper()
                        if up.startswith("OK") or up.startswith("ERR"):
                            if self.verbose:
                                print(f"← {got}", file=sys.stderr)
                            return P.parse_reply(got)
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise LinkError(f"{line!r}: BLE 超时无回复")
                    self._cv.wait(remaining)

    def close(self):
        try:
            if self._peripheral is not None:
                self._central.cancelPeripheralConnection_(self._peripheral)
        except Exception:
            pass
