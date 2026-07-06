"""SunSpec model walk: chain parsing, identity decode, non-SunSpec refusal."""
import pytest

from multibus import discovery


def _s2r(s: str, nregs: int):
    """ASCII → registers (two chars/register, big-endian, NUL-padded)."""
    b = s.encode("ascii").ljust(nregs * 2, b"\x00")
    return [(b[i] << 8) | b[i + 1] for i in range(0, nregs * 2, 2)]


class _Result:
    def __init__(self, regs):
        self.registers = regs
    def isError(self):
        return self.registers is None


class _FakeSunSpec:
    """Fronius-like map: SunS @40000, Common(1), Inverter 3ph(103), MPPT(160)."""

    def __init__(self):
        ident = (_s2r("Fronius", 16) + _s2r("Symo 20.0-3-M", 16) +
                 _s2r("", 8) + _s2r("1.2.3", 8) + _s2r("29301000", 16) + [1] + [0])
        self.mem = {}
        def put(addr, regs):
            for i, v in enumerate(regs):
                self.mem[addr + i] = v
        put(40000, [0x5375, 0x6E53])
        put(40002, [1, 66]); put(40004, ident)
        put(40070, [103, 50]); put(40072, [0] * 50)
        put(40122, [160, 48]); put(40124, [0] * 48)
        put(40172, [0xFFFF, 0])

    def connect(self):
        return True
    def close(self):
        pass
    def read_holding_registers(self, address, count, slave):
        if any(address + i not in self.mem for i in range(count)):
            return _Result(None)
        return _Result([self.mem[address + i] for i in range(count)])


class _FakeNonSunSpec(_FakeSunSpec):
    def __init__(self):
        self.mem = {40000: 0, 40001: 0, 50000: 0, 50001: 0, 0: 0, 1: 0}


@pytest.fixture
def fake_tcp(monkeypatch):
    holder = {"cls": _FakeSunSpec}
    import pymodbus.client as pc
    monkeypatch.setattr(pc, "ModbusTcpClient",
                        lambda host, port, timeout: holder["cls"]())
    return holder


def test_walk_finds_models_and_identity(fake_tcp):
    out = discovery.sunspec_walk("192.168.1.50")
    assert out["ok"] and out["base"] == 40000
    assert [m["id"] for m in out["models"]] == [1, 103, 160]
    m103 = out["models"][1]
    assert m103["addr"] == 40070 and m103["length"] == 50
    assert m103["name"].startswith("Inverter — three phase")
    ident = out["identity"]
    assert ident["manufacturer"] == "Fronius"
    assert ident["model"] == "Symo 20.0-3-M"
    assert ident["serial"] == "29301000"


def test_walk_rejects_non_sunspec(fake_tcp):
    fake_tcp["cls"] = _FakeNonSunSpec
    out = discovery.sunspec_walk("192.168.1.51")
    assert out["ok"] is False and "SunS" in out["error"]


def test_walk_names_vendor_models():
    assert discovery.SUNSPEC_MODEL_NAMES[203].startswith("Meter — wye")
    # id necunoscut non-vendor și vendor
    from multibus.discovery import SUNSPEC_MODEL_NAMES as N
    assert 64001 not in N and 999 not in N   # cad pe fallback-urile din walk


def test_api_sunspec_lan_guard(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/discover/sunspec", json={"host": "8.8.8.8"})
    assert r.status_code == 422           # non-LAN refuzat
    r = client.post("/api/discover/sunspec", json={})
    assert r.status_code == 422           # host lipsă
