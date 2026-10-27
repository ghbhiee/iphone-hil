import pytest
from hil import protocol as P
from hil.link import FirmwareModel, LoopbackLink
from hil.sim import SimPhone


def test_key_spec():
    assert P.parse_key_spec("GUI+H") == (0x08, 0x0B)
    assert P.parse_key_spec("ENTER") == (0, 0x28)
    assert P.parse_key_spec("CTRL+ALT+DEL") == (0x05, 0x4C)
    assert P.parse_key_spec("?") == (0x02, 0x38)
    assert P.parse_key_spec("a") == (0, 0x04)
    assert P.parse_key_spec("GUI") == (0x08, 0)
    with pytest.raises(ValueError):
        P.parse_key_spec("NOPE")


def test_text_escape_roundtrip():
    s = "a b\n\tc\\d!"
    assert P.unescape_text(P.escape_text(s)) == s
    with pytest.raises(ValueError):
        P.escape_text("中文")


def test_split_move():
    assert list(P.split_move(300, -140)) == [(127, -127), (127, -13), (46, 0)]
    assert list(P.split_move(0, 0)) == []
    assert sum(x for x, _ in P.split_move(-1000, 5)) == -1000


def test_reply():
    r = P.parse_reply("OK conn=1 backend=ble")
    assert r.ok and r.fields == {"conn": "1", "backend": "ble"}
    assert not P.parse_reply("ERR bad").ok
    assert not P.parse_reply("garbage").ok


def test_firmware_model_commands():
    ph = SimPhone(width=400, height=800)
    fm = FirmwareModel(ph)
    assert fm.handle("PING") == "OK PONG"
    assert fm.handle("ST").startswith("OK conn=1")
    assert fm.handle("MV 300 -140").startswith("OK")
    assert ph.report_count == 3
    assert fm.handle("CK").startswith("OK") and len(ph.taps) == 1
    assert fm.handle("CK r 2").startswith("OK") and len(ph.taps) == 3 and ph.taps[-1][2] == 2
    assert fm.handle("LP 500").startswith("OK") and len(ph.taps) == 4
    assert fm.handle("BT l down").startswith("OK") and fm.buttons == 1
    assert fm.handle("BT l up").startswith("OK") and fm.buttons == 0
    assert fm.handle("WH -3").startswith("OK")
    assert fm.handle("KD GUI").startswith("OK") and fm.mods == 8
    assert fm.handle("KP H").startswith("OK") and fm.mods == 8
    assert fm.handle("KU GUI").startswith("OK") and fm.mods == 0 and fm.keys == []
    assert fm.handle("TX Hi!\\n") == "OK tx 4"
    assert fm.handle("CC HOME") == "OK cc" and ph.consumer_log[-1] == 0x223
    assert fm.handle("RL") == "OK rl"
    assert fm.handle("CFG mvdelay 3") == "OK cfg mvdelay" and fm.cfg["mvdelay"] == 3
    assert fm.handle("CFG bogus 1").startswith("ERR")
    assert fm.handle("MV x y").startswith("ERR")
    assert fm.handle("NOPE").startswith("ERR")
    assert fm.handle("").startswith("ERR")
    assert fm.handle("TX 中文").startswith("ERR")


def test_loopback_link_api():
    ph = SimPhone(width=400, height=800)
    lk = LoopbackLink(ph, verbose=False)
    assert lk.ping() and lk.connected()
    lk.move(10, 10); lk.click(); lk.type_text("ok\n"); lk.key("GUI+H"); lk.home(); lk.release()
    assert ph.typed_text() == "ok\nh"
    assert len(lk.transcript) == 8
