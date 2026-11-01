# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""WS-LOG-1: a refused WebSocket handshake is logged once per client per minute.

A tab whose session ended re-dialled /ws every 3 s and every refusal wrote
two INFO lines (1,295 an hour). The records are uvicorn's own, so the tests
build them the way uvicorn does and, once, drive a real uvicorn server.
"""
import asyncio
import logging
import socket
import threading
import time

import pytest

from multibus import ws_log

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ws(client, status=403, path="/ws"):
    """The record uvicorn's websocket protocol logs for a refused handshake."""
    if status == 403:
        msg, args = '%s - "WebSocket %s" 403', (client, path)
    else:
        msg, args = '%s - "WebSocket %s" %d', (client, path, status)
    return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)


def _rec(msg, args=()):
    return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)


def _run(flt, records):
    return [r.getMessage() for r in records if flt.filter(r)]


def test_many_refusals_from_one_client_in_a_minute_make_one_line():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    out = []
    for _ in range(20):                       # one every 3 s: 57 s of re-dialling
        out += _run(flt, [_ws("192.168.1.140:0"), _rec("connection rejected (%d %s)", (403, "Forbidden"))])
        clk.t += 3
    assert out == ['192.168.1.140:0 - "WebSocket /ws" 403']


def test_the_next_line_after_the_window_counts_what_was_dropped():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    out = []
    for _ in range(22):                       # 0, 3, … 63 s
        out += _run(flt, [_ws("192.168.1.140:0")])
        clk.t += 3
    assert out == ['192.168.1.140:0 - "WebSocket /ws" 403',
                   '192.168.1.140:0 - "WebSocket /ws" 403 (19 more rejections from 192.168.1.140 in the last 60 s)']


def test_after_a_quiet_window_the_line_has_no_count():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    assert _run(flt, [_ws("10.0.0.5:4242")]) == ['10.0.0.5:4242 - "WebSocket /ws" 403']
    clk.t += 120
    assert _run(flt, [_ws("10.0.0.5:4243")]) == ['10.0.0.5:4243 - "WebSocket /ws" 403']


def test_two_clients_get_a_line_each():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    out = []
    for _ in range(10):
        out += _run(flt, [_ws("192.168.1.140:0"), _ws("192.168.1.20:51000")])
        clk.t += 3
    assert out == ['192.168.1.140:0 - "WebSocket /ws" 403',
                   '192.168.1.20:51000 - "WebSocket /ws" 403']


def test_the_client_is_the_host_not_the_port():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    out = _run(flt, [_ws("192.168.1.20:51000"), _ws("192.168.1.20:51002"), _ws("[::1]:4000"), _ws("[::1]:4001")])
    assert out == ['192.168.1.20:51000 - "WebSocket /ws" 403', '[::1]:4000 - "WebSocket /ws" 403']


def test_a_denial_response_counts_as_a_refusal():
    flt = ws_log.WsRejectFilter(60, clock=_Clock())
    assert _run(flt, [_ws("10.0.0.9:1", 401), _ws("10.0.0.9:2", 401)]) == ['10.0.0.9:1 - "WebSocket /ws" 401']


def test_every_other_line_passes_untouched():
    clk = _Clock()
    flt = ws_log.WsRejectFilter(60, clock=clk)
    keep = [
        _rec('%s - "WebSocket %s" [accepted]', ("192.168.1.140:0", "/ws")),
        _rec('%s - "WebSocket %s" [accepted]', ("192.168.1.140:0", "/ws")),
        _rec('%s - "%s %s HTTP/%s" %d', ("192.168.1.140:0", "GET", "/api/status", "1.1", 401)),
        _rec('%s - "%s %s HTTP/%s" %d', ("192.168.1.140:0", "GET", "/api/status", "1.1", 401)),
        _rec("connection open"),
        _rec("Application startup complete."),
        _rec("COMMAND %s via %s", ("gx/AcPowerSetPoint", "rule")),
    ]
    _run(flt, [_ws("192.168.1.140:0")])          # a refusal in the same window changes nothing
    before = [(r.msg, r.args) for r in keep]
    assert [flt.filter(r) for r in keep] == [True] * len(keep)
    assert [(r.msg, r.args) for r in keep] == before


def test_install_is_idempotent_and_on_uvicorns_logger():
    lg = logging.getLogger("uvicorn.error")
    saved = list(lg.filters)
    try:
        ws_log.install()
        ws_log.install()
        assert sum(isinstance(f, ws_log.WsRejectFilter) for f in lg.filters) == 1
    finally:
        lg.filters[:] = saved


def test_the_gateway_installs_the_filter_at_startup(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_devices import write_config
    from multibus.api import create_api
    lg = logging.getLogger("uvicorn.error")
    saved = list(lg.filters)
    lg.filters[:] = [f for f in saved if not isinstance(f, ws_log.WsRejectFilter)]
    try:
        cfg = write_config(tmp_path)
        app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
        with TestClient(app):
            assert any(isinstance(f, ws_log.WsRejectFilter) for f in lg.filters)
    finally:
        lg.filters[:] = saved


@pytest.mark.parametrize("impl", ["websockets", "websockets-sansio"])
@pytest.mark.parametrize("filtered", [False, True])
def test_through_a_real_uvicorn_server(impl, filtered):
    """uvicorn and websockets themselves write the records: the formats are theirs.
    Unfiltered, five refusals make ten lines (the flood); filtered, one."""
    import uvicorn
    import websockets
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    async def refuse(websocket):
        await websocket.close(code=1008)

    app = Starlette(routes=[WebSocketRoute("/ws", refuse)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="info", ws=impl, lifespan="off"))
    lines = []

    class _Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    lg = logging.getLogger("uvicorn.error")
    saved = list(lg.filters)
    grab = _Grab()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        lg.filters[:] = [f for f in saved if not isinstance(f, ws_log.WsRejectFilter)]
        if filtered:
            ws_log.install()
        lg.addHandler(grab)

        async def dial(n):
            for _ in range(n):
                with pytest.raises(Exception):
                    async with websockets.connect(f"ws://127.0.0.1:{port}/ws"):
                        pass

        asyncio.run(dial(5))
        time.sleep(0.2)
    finally:
        lg.removeHandler(grab)
        lg.filters[:] = saved
        server.should_exit = True
        t.join(5)
    refusals = [ln for ln in lines if "WebSocket /ws" in ln or "connection rejected" in ln]
    if not filtered:
        assert len(refusals) == 10, lines
        return
    assert len(refusals) == 1 and refusals[0].endswith('- "WebSocket /ws" 403'), lines
