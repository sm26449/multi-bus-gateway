# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The command dispatcher alone (ADR-0018): lanes, slots, coalescing of the
same intent, expiry, the revert at the head, round-robin, the lane thread's
life. A fake clock decides deadlines; the lanes are real threads, held at
will by events."""
import threading
import time

import pytest

from multibus.command_dispatch import CommandDispatcher, Job


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while not pred() and time.time() < end:
        time.sleep(0.005)
    return pred()


class _Gate:
    """A command that blocks its lane until released, and says when it runs."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, job):
        self.started.set()
        assert self.release.wait(5)
        return 200, {'status': 'success'}


def _job(ran, lane='bus:a', device='d1', channel='ch', command='set', value=1, kind='command',
         run=None, **kw):
    params = {'value': value, **kw.pop('params', {})}

    def _run(job):
        ran.append((job.device, job.command, dict(job.params)))
        return 200, {'status': 'success', 'params': dict(job.params)}
    return Job(lane=lane, device=device, channel=channel, command=command, params=params,
               run=run or _run, kind=kind, **kw)


@pytest.fixture
def disp():
    d = CommandDispatcher(clock=_Clock(), idle_s=0.2)
    yield d
    d.stop()


def _block(disp, lane='bus:a', device='blocker', channel='x'):
    """Occupy a lane so that what follows queues behind it."""
    g = _Gate()
    disp.submit(Job(lane=lane, device=device, channel=channel, command='hold', params={}, run=g))
    assert g.started.wait(3)
    return g


# ── coalescing: only the same intent ────────────────────────────────────────

def test_coalescing_only_the_same_intent(disp):
    ran = []
    g = _block(disp)
    a = disp.submit(_job(ran, value=500))
    b = disp.submit(_job(ran, value=100))                    # a newer value of the same setting
    # VFD: set_frequency then run — two intents on one channel, both run, in order
    c = disp.submit(_job(ran, command='set_frequency', value=30))
    d = disp.submit(_job(ran, command='run', value=1))
    # another non-value parameter is another intent
    e = disp.submit(_job(ran, command='set_frequency', value=40, params={'ramp': 5}))
    g.release.set()
    for j in (b, c, d, e):
        assert j.wait(3) is not None
    assert a.result[1]['status'] == 'superseded' and a.result[1]['superseded'] is True
    assert ran == [('d1', 'set', {'value': 100}), ('d1', 'set_frequency', {'value': 30}),
                   ('d1', 'run', {'value': 1}), ('d1', 'set_frequency', {'value': 40, 'ramp': 5})]


def test_coalesce_false_is_never_merged(disp):
    ran = []
    g = _block(disp)
    a = disp.submit(_job(ran, command='reset_fault', value=1, coalesce=False))
    b = disp.submit(_job(ran, command='reset_fault', value=1, coalesce=False))
    g.release.set()
    assert a.wait(3) and b.wait(3)
    assert [r[1] for r in ran] == ['reset_fault', 'reset_fault']


@pytest.mark.parametrize('kind', ['sequence', 'refresh'])
def test_sequence_and_refresh_are_never_merged_nor_merge(disp, kind):
    ran = []
    g = _block(disp)
    first = disp.submit(_job(ran, value=1, kind=kind))
    a = disp.submit(_job(ran, value=2))                     # never merges into a sequence step
    g.release.set()
    assert first.wait(3) and a.wait(3)
    assert [r[2]['value'] for r in ran] == [1, 2]


def test_a_refresh_is_admitted_only_into_an_empty_slot(disp):
    ran = []
    g = _block(disp)
    a = disp.submit(_job(ran, value=5))
    r = disp.submit(_job(ran, value=4, kind='refresh'))
    # put off at once, retryable — never `superseded`: what waits may expire (CR2-2)
    assert r.done.is_set() and r.result[0] == 409 and not r.result[1].get('superseded')
    g.release.set()
    assert a.wait(3)
    r2 = disp.submit(_job(ran, value=5, kind='refresh'))
    assert r2.wait(3)[1]['status'] == 'success'
    assert [x[2]['value'] for x in ran] == [5, 5]


def test_the_superseded_caller_is_told(disp):
    told = []
    g = _block(disp)
    disp.submit(_job([], value=1, on_done=lambda code, res: told.append(res['status'])))
    disp.submit(_job([], value=2))
    assert told == ['superseded']
    g.release.set()


def test_a_running_command_is_never_interrupted(disp):
    ran = []
    g = _Gate()
    running = disp.submit(_job(ran, value=1, run=g))
    assert g.started.wait(3)
    newer = disp.submit(_job(ran, value=2))
    assert not running.done.is_set()                         # still running, not superseded
    g.release.set()
    assert running.wait(3)[1]['status'] == 'success' and newer.wait(3)[1]['status'] == 'success'


# ── the revert ───────────────────────────────────────────────────────────────

def test_a_revert_goes_to_the_head_and_is_never_superseded(disp):
    ran = []
    g = _block(disp)
    disp.submit(_job(ran, device='d2', value=7))
    disp.submit(_job(ran, device='d1', value=9))
    current = [True]

    def revert(job):
        # its own is_current() is asked again here, at the head of the lane
        if not current[0]:
            return 409, {'status': 'cancelled'}
        ran.append(('d1', 'revert', dict(job.params)))
        return 200, {'status': 'success'}
    rv = disp.submit(_job(ran, device='d1', value=0, kind='revert', run=revert))
    after = disp.submit(_job(ran, device='d1', value=3))     # does not merge with the revert
    g.release.set()
    assert rv.wait(3) and after.wait(3)
    assert ran[0] == ('d1', 'revert', {'value': 0})
    assert ('d1', 'set', {'value': 3}) in ran


def test_a_revert_has_no_deadline(disp):
    ran = []
    g = _block(disp)
    rv = disp.submit(_job(ran, value=0, kind='revert'))
    disp.clock.t += 3600
    g.release.set()
    assert rv.wait(3)[1]['status'] == 'success'


# ── expiry and the slot cap ──────────────────────────────────────────────────

def test_expired_at_the_head_writes_nothing(disp):
    ran = []
    g = _block(disp)
    j = disp.submit(_job(ran, value=1, max_age_s=30))
    disp.clock.t += 31
    g.release.set()
    code, res = j.wait(3)
    assert res['status'] == 'expired' and ran == []
    assert disp.counters('d1', 'set')['expired'] == 1


def test_slot_cap_rejects_the_ninth(disp):
    ran = []
    g = _block(disp)
    jobs = [disp.submit(_job(ran, command='pulse', value=i, coalesce=False)) for i in range(9)]
    code, res = jobs[8].result
    assert code == 429 and res['status'] == 'rejected' and 'queue full' in res['reason']
    g.release.set()
    assert all(j.wait(3) for j in jobs[:8])
    assert len(ran) == 8


def test_queued_ms_is_in_every_result(disp):
    g = _block(disp)
    j = disp.submit(_job([], value=1))
    disp.clock.t += 0.25
    g.release.set()
    assert j.wait(3)[1]['queued_ms'] == 250.0


# ── lanes ────────────────────────────────────────────────────────────────────

def test_lanes_run_in_parallel_one_lane_is_serial(disp):
    ran = []
    g = _block(disp, lane='bus:slow')
    other = disp.submit(_job(ran, lane='bus:fast', value=1))
    assert other.wait(3)[1]['status'] == 'success'           # not behind the slow one
    same = disp.submit(_job(ran, lane='bus:slow', device='d9', value=1))
    time.sleep(0.05)
    assert not same.done.is_set()                            # the same wire waits
    g.release.set()
    assert same.wait(3)


def test_round_robin_across_devices_fifo_within_one(disp):
    ran = []
    g = _block(disp)
    for i in range(3):
        disp.submit(_job(ran, device='a', command=f'c{i}', value=i))
    last = None
    for i in range(2):
        last = disp.submit(_job(ran, device='b', command=f'c{i}', value=i))
    g.release.set()
    assert _wait(lambda: len(ran) == 5)
    assert [(d, c) for d, c, _p in ran] == [('a', 'c0'), ('b', 'c0'), ('a', 'c1'), ('b', 'c1'), ('a', 'c2')]


def test_a_lane_thread_exits_when_idle(disp):
    j = disp.submit(_job([], lane='bus:idle', value=1))
    assert j.wait(3)
    assert 'bus:idle' in disp.lanes()
    assert _wait(lambda: 'bus:idle' not in disp.lanes(), timeout=3)
    # and comes back on demand
    assert disp.submit(_job([], lane='bus:idle', value=2)).wait(3)[1]['status'] == 'success'


def test_submit_and_wait_from_inside_a_lane_raises(disp):
    seen = []

    def nested(job):
        try:
            disp.submit_wait(_job([], lane='bus:b', value=1))
        except RuntimeError as e:
            seen.append(str(e))
        return 200, {'status': 'success'}
    assert disp.submit(_job([], run=nested)).wait(3)
    assert seen and 'lane' in seen[0]


def test_a_crash_in_a_command_is_an_error_result_and_the_lane_goes_on(disp):
    def boom(job):
        raise ValueError('bad')
    assert disp.submit(_job([], run=boom)).wait(3)[1]['status'] == 'error'
    assert disp.submit(_job([], value=2)).wait(3)[1]['status'] == 'success'


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_cancel_on_standby_or_delete(disp):
    ran = []
    g = _block(disp)
    a = disp.submit(_job(ran, device='gone', value=1))
    b = disp.submit(_job(ran, device='stays', value=1))
    assert disp.cancel(device='gone', reason='device deleted') == 1
    assert a.result[1]['status'] == 'cancelled' and 'deleted' in a.result[1]['reason']
    c = disp.submit(_job(ran, device='stays', command='other', value=1))
    assert disp.cancel(reason='standby') == 2                # every pending one
    assert b.result[1]['status'] == 'cancelled' and c.result[1]['status'] == 'cancelled'
    g.release.set()
    time.sleep(0.05)
    assert ran == []


def test_a_waiter_that_gives_up_withdraws_a_pending_command(disp):
    g = _block(disp)
    code, res = disp.submit_wait(_job([], value=1), timeout=0.05)
    assert res['status'] == 'cancelled' and 'waited' in res['reason']
    g.release.set()


def test_the_oldest_pending_age_per_lane(disp):
    g = _block(disp)
    disp.submit(_job([], value=1, max_age_s=30))
    disp.clock.t += 20
    lane = disp.lanes()['bus:a']
    assert lane['pending'] == 1 and lane['oldest_s'] == 20 and lane['max_age_s'] == 30
    g.release.set()


# ── review 2 (CR2) ───────────────────────────────────────────────────────────

def test_a_refresh_put_off_by_a_waiting_command_is_not_ended():
    """CR2-2 (proof ported from the review): the refresh met a queued command
    and was told `superseded` — its timer then ended the refresh for good —
    and the queued command expired, writing nothing: the device forgot the
    setting. Now it is a retryable 409 the timer tries again."""
    t = [0.0]
    d = CommandDispatcher(clock=lambda: t[0])
    try:
        started, release = threading.Event(), threading.Event()

        def block(job):
            started.set()
            release.wait(5)
            return 200, {'status': 'success'}
        d.submit(Job(lane='bus:x', device='other', channel='c', command='hold', params={}, run=block))
        assert started.wait(2)
        wrote = []
        cmd = d.submit(Job(lane='bus:x', device='hp', channel='compressor_limit', command='compressor_limit',
                           params={'value': 80}, max_age_s=30,
                           run=lambda j: (wrote.append(80), (200, {'status': 'success'}))[1]))
        ref = d.submit(Job(lane='bus:x', device='hp', channel='compressor_limit', command='compressor_limit',
                           params={'value': 60}, kind='refresh', coalesce=False,
                           run=lambda j: (wrote.append(60), (200, {'status': 'success'}))[1]))
        assert ref.done.is_set() and ref.result[0] == 409
        assert not ref.result[1].get('superseded') and ref.result[1].get('retry') is True
        t[0] = 31.0
        release.set()
        assert d.wait_for(cmd, 5)[1]['status'] == 'expired' and wrote == []
        # the timer's next try finds the slot empty: the setting is written again
        again = Job(lane='bus:x', device='hp', channel='compressor_limit', command='compressor_limit',
                    params={'value': 60}, kind='refresh',
                    run=lambda j: (wrote.append(60), (200, {'status': 'success'}))[1])
        assert d.submit_wait(again, 5)[1]['status'] == 'success' and wrote == [60]
    finally:
        d.stop()


@pytest.mark.parametrize('garbage', [None, 'x', (200,), (200, 'not a dict')])
def test_a_lane_survives_a_run_that_returns_garbage(disp, garbage):
    """CR2-7: the result handling sat outside the try — a run returning no
    (status, dict) killed the lane thread and stranded what waited."""
    g = _block(disp)
    bad = disp.submit(_job([], value=1, run=lambda j: garbage))
    after = disp.submit(_job([], device='d2', value=2))
    g.release.set()
    assert bad.wait(3)[1]['status'] == 'error'
    assert after.wait(3)[1]['status'] == 'success'


def test_closing_keeps_the_reverts_and_cancels_the_rest(disp):
    """CR2-8: at shutdown nothing new is taken but the dead-man's reverts,
    which main.stop() runs through the lanes after the API has closed."""
    ran = []
    g = _block(disp)
    waiting = disp.submit(_job(ran, value=1))
    disp.close()
    assert waiting.result[1]['status'] == 'cancelled'
    assert disp.submit(_job(ran, value=2)).result[1]['status'] == 'cancelled'
    rv = disp.submit(_job(ran, value=0, kind='revert'))
    g.release.set()
    assert rv.wait(3)[1]['status'] == 'success' and ran == [('d1', 'set', {'value': 0})]


def test_a_deleted_devices_counters_go(disp):
    disp.count('gone', 'set', 'held')
    disp.count('stays', 'set', 'held')
    disp.forget('gone')
    assert disp.counters('gone', 'set')['held'] == 0 and disp.counters('stays', 'set')['held'] == 1
