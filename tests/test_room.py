"""Offline checks: python -B tests/test_room.py; never sends a live reservation."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
import time
from unittest.mock import Mock, patch
import sys

import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import room as r

DUE = r.stamp("2026-09-15 19:00")
MACHINE = {"timeLineInterval": "1", "minInterval": "1", "maxInterval": "5"}
RULE = {"status": "1", "startDate": "2026-09-01", "endDate": "2027-01-01",
        "startTime": "08:00:00", "endTime": "22:00:00", "weekdays": "1,2,3,4,5,6,7",
        "minAheadTime": "0", "maxAheadTime": "24"}


class Clock(datetime):
    current = DUE - timedelta(seconds=15)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz)


def advance(due):
    Clock.current = max(Clock.current, due)


@contextmanager
def scenario(window=0.31, interval=0.1):
    Clock.current = DUE - timedelta(seconds=15)
    with patch.object(r, "datetime", Clock), patch.object(r, "wait_until", side_effect=advance), patch.object(r, "RETRY_WINDOW_SECONDS", window), patch.object(r, "SUBMIT_INTERVAL_SECONDS", interval), patch.object(r, "Client") as factory, patch.object(r, "log") as logs:
        client = factory.return_value
        client.settings.return_value = ({"uid": "config"}, MACHINE, [{"fieldName": "预约用途", "necessary": "1"}], [RULE], [])
        client.mine.return_value = []
        client.concurrent_mine.return_value = []
        def prepare(payload):
            assert Clock.current < DUE
            return requests.Request("POST", r.BASE + r.APP + "saveAppInfo", json=[payload]).prepare(), {}
        client.prepare_submission.side_effect = prepare
        workers = []
        def fork():
            worker = Mock()
            worker.request.side_effect = lambda *args, **kwargs: client.request(*args, **kwargs)
            worker.mine.side_effect = lambda: client.concurrent_mine()
            workers.append(worker)
            return worker
        client.fork.side_effect = fork
        yield client, logs
        assert not factory.called or client.http.close.called
        assert all(worker.http.close.called for worker in workers)


def check():
    assert r.next_release(r.stamp("2026-09-15 07:00")) == r.stamp("2026-09-15 09:00")
    assert r.next_release(r.stamp("2026-09-15 21:59")) == r.stamp("2026-09-15 22:00")
    assert r.next_release(r.stamp("2026-09-15 22:00")) == r.stamp("2026-09-16 09:00")
    assert r.target_slot([RULE], [], MACHINE, DUE) == (r.stamp("2026-09-16 18:00"), r.stamp("2026-09-16 19:00"))
    assert r.target_slot([RULE], [], MACHINE, DUE.replace(hour=7)) is None
    assert r.target_slot([RULE], [], MACHINE, DUE.replace(hour=9))[0].hour == 8
    assert r.target_slot([RULE], [], MACHINE, DUE.replace(hour=22))[1].hour == 22
    assert r.target_slot([RULE], [], MACHINE, DUE.replace(hour=23)) is None
    assert r.target_slot([dict(RULE, maxAheadTime="23")], [], MACHINE, DUE) is None
    assert r.target_slot([dict(RULE, status="0")], [], MACHINE, DUE) is None
    closed = dict(RULE, startTime="18:30", endTime="19:30")
    assert r.target_slot([RULE], [closed], MACHINE, DUE) is None
    assert r.target_slot([RULE], [], dict(MACHINE, minInterval="2"), DUE) is None
    assert r.target_slot([RULE], [], dict(MACHINE, timeLineInterval="2"), DUE) is None
    assert r.target_slot([dict(RULE, weekdays="1")], [], MACHINE, DUE) is None
    assert r.target_slot([RULE], [], dict(MACHINE, timeLineInterval="0.5"), DUE)

    # Real threads prove at least three POSTs start without waiting for prior responses.
    with scenario(window=0.32) as (client, logs):
        ready = Barrier(3)
        lock = Lock()
        sent = []
        requests_seen = []
        def submit(*args, **kwargs):
            with lock:
                sent.append(time.perf_counter())
                requests_seen.append(kwargs["prepared"][0])
                index = len(sent)
            assert Clock.current >= DUE and client.mine.call_count == 0
            if index <= 3:
                ready.wait(timeout=2)
            return {"code": -1, "msg": "预约时间块无效/冲突，请选择其它时间段"}
        client.request.side_effect = submit
        def mine():
            assert Clock.current >= DUE + timedelta(seconds=0.32)
            return []
        client.mine.side_effect = mine
        r.run(scheduled_for=DUE)
        assert 3 <= len(sent) <= 4
        assert all(b - a >= 0.085 for a, b in zip(sent, sent[1:]))
        assert len({id(req) for req in requests_seen}) == len(sent)
        assert all(json.loads(req.body)[0]["appStartTime"] == "2026-09-16 18:00:00" for req in requests_seen)
        client.mine.assert_called_once()
        print("TIMING simulated POST starts (ms):", [round((t-sent[0])*1000, 1) for t in sent])

    # Timeout/malformed/temporary rejection never gate later ticks or trigger a mid-window query.
    with scenario(window=0.3, interval=0.03) as (client, logs):
        client.request.side_effect = [requests.Timeout(), {}, {"code": -1, "msg": "该时间段不可用"}, {"code": 0}]
        r.run(scheduled_for=DUE)
        assert client.request.call_count == 4
        client.mine.assert_called_once()

    # The observed busy response must keep dispatching until success or the window ends.
    busy = {"code": -1, "msg": "系统繁忙！由于当前使用人数较多，请稍后再试！"}
    with scenario(window=0.2, interval=0.04) as (client, logs):
        client.request.side_effect = [busy, busy, {"code": 0}]
        r.run(scheduled_for=DUE)
        assert client.request.call_count == 3
    with scenario(window=0.16, interval=0.04) as (client, logs):
        client.request.return_value = busy
        r.run(scheduled_for=DUE)
        assert client.request.call_count >= 3
        assert not any("accepted" in call.args[0] for call in logs.call_args_list)

    # A record appearing while POSTs are still pending stops new submissions immediately.
    with scenario(window=0.8, interval=0.04) as (client, logs):
        querying = Event()
        verified = Event()
        starts = []
        found_at = []
        target = {"appDate": "2026-09-16", "appStartTime": "18:00:00", "appEndTime": "19:00:00",
                  "appStatus": "PASS", "appStatusInfo": "审核通过"}
        def pending_post(*args, **kwargs):
            starts.append(time.perf_counter())
            assert verified.wait(2)
            return {}  # No API acceptance: the query must be what stops the dispatcher.
        def concurrent_mine():
            querying.set()
            time.sleep(0.18)
            found_at.append(time.perf_counter())
            verified.set()
            return [target]
        client.request.side_effect = pending_post
        client.concurrent_mine.side_effect = concurrent_mine
        client.mine.return_value = [target]
        r.run(scheduled_for=DUE)
        assert querying.is_set() and 3 <= len(starts) < 10
        assert all(t < found_at[0] for t in starts)
        assert any("并发核实已查到" in call.args[0] for call in logs.call_args_list)
        client.mine.assert_called_once()

    # Query failures don't halt submissions; cancelled or different records never signal success.
    with scenario(window=0.32, interval=0.04) as (client, logs), patch.object(r, "VERIFY_INTERVAL_SECONDS", 0.05):
        client.request.return_value = {}
        client.concurrent_mine.side_effect = [requests.Timeout(),
            [dict(target, appStatus="CANCEL")], [dict(target, appStartTime="17:00:00")], [], [], [], [], []]
        r.run(scheduled_for=DUE)
        assert client.request.call_count >= 5
        assert client.concurrent_mine.call_count >= 3
        assert not any("并发核实已查到" in call.args[0] for call in logs.call_args_list)

    # Slow queries never overlap one another and don't block the submission cadence.
    with scenario(window=0.32, interval=0.04) as (client, logs), patch.object(r, "VERIFY_INTERVAL_SECONDS", 0.05):
        running = Event()
        query_starts = []
        def slow_query():
            assert not running.is_set()
            running.set()
            query_starts.append(time.perf_counter())
            time.sleep(0.12)
            running.clear()
            return []
        client.concurrent_mine.side_effect = slow_query
        client.request.return_value = {}
        r.run(scheduled_for=DUE)
        assert client.request.call_count >= 5 and len(query_starts) >= 2
        assert all(b - a >= 0.115 for a, b in zip(query_starts, query_starts[1:]))

    # Explicit acceptance, confirmation prompts, permanent refusal, expiry and throttling stop new sends.
    for response in ({"code": 0}, {"code": 0, "data": "预约时段与课程安排冲突，确定预约？"},
                     {"code": 9, "msg": "预约次数已达上限"}, r.SessionExpired("expired"), r.RateLimited("HTTP 429")):
        with scenario() as (client, logs):
            client.request.side_effect = [response]
            r.run(scheduled_for=DUE)
            client.request.assert_called_once()
            client.mine.assert_called_once()

    # In-flight submissions may finish late; no new POST starts after the dispatch deadline.
    with scenario(window=0.12) as (client, logs):
        sent = []
        def slow_submit(*args, **kwargs):
            sent.append(time.perf_counter())
            time.sleep(0.25)
            return {"code": 0}
        client.request.side_effect = slow_submit
        r.run(scheduled_for=DUE)
        assert len(sent) == 2 and sent[1] - sent[0] < 0.12
        client.mine.assert_called_once()
    with scenario() as (client, logs):
        Clock.current = DUE + timedelta(seconds=31)
        r.run(scheduled_for=DUE)
        client.request.assert_not_called()

    # Final verification reports records and their real status, including pending approval.
    with scenario() as (client, logs):
        client.request.return_value = {"code": 0}
        client.mine.return_value = [{"appDate": "2026-09-16", "appStartTime": "18:00:00", "appEndTime": "19:00:00", "appStatus": "UNAUDIT", "appStatusInfo": "待审核"}]
        r.run(scheduled_for=DUE)
        assert any("1 条有效记录，状态：待审核" in call.args[0] for call in logs.call_args_list)
    with scenario() as (client, logs):
        client.settings.return_value = ({"uid": "config"}, MACHINE, [], [], [])
        r.run(scheduled_for=DUE)
        client.request.assert_not_called()

    # Retain offline checks of login fallback, cookie isolation and prepared-request transport settings.
    with TemporaryDirectory() as tmp, patch.object(r, "STATE", Path(tmp) / "session.json"), patch.object(r, "log"):
        client = r.Client()
        with patch.object(client, "request", side_effect=[r.SessionExpired("expired"), {"code": 0, "data": {"accessToken": "test"}}, {"username": r.USER}]), patch.object(client, "password_login") as login, patch.object(client, "data", return_value={"isAccess": True}):
            client.login()
            login.assert_called_once()
        worker = client.fork()
        assert worker.http is not client.http and worker.http.cookies is not client.http.cookies
        assert worker.user == client.user
        prepared = worker.prepare_submission({"test": 1})
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = b'{"code":0}'
        with patch.object(worker.http, "send", return_value=response) as send:
            worker.request("POST", r.APP + "saveAppInfo", prepared=prepared, timeout=(3.05, 8))
            assert send.call_args.kwargs["timeout"] == (3.05, 8)
            assert send.call_args.kwargs["allow_redirects"] is False
        worker.http.close()
        client.http.close()

    # Starting the program schedules preparation for the next valid release; no immediate booking.
    Clock.current = DUE - timedelta(seconds=30)
    with patch.object(r, "datetime", Clock), patch.object(r, "single_instance"), patch.object(r, "log"), patch.object(r, "wait_until", side_effect=advance) as wait, patch.object(r, "run", side_effect=KeyboardInterrupt) as run, patch.object(sys, "argv", ["room.py"]):
        try:
            r.main()
        except KeyboardInterrupt:
            pass
        wait.assert_called_once_with(DUE - timedelta(seconds=15))
        run.assert_called_once_with(scheduled_for=DUE, room="551")
    barrier = Barrier(2)
    def parallel_run(**kwargs):
        assert kwargs['scheduled_for'] == DUE
        barrier.wait(timeout=2)
    def one_window(moment):
        if moment == DUE:
            raise KeyboardInterrupt
        advance(moment)
    Clock.current = DUE - timedelta(seconds=30)
    with patch.object(r, "datetime", Clock), patch.object(r, "single_instance"), patch.object(r, "log"), patch.object(r, "wait_until", side_effect=one_window), patch.object(r, "run", side_effect=parallel_run) as run, patch.object(sys, "argv", ["room.py", "--rooms", "A103", "B101", "A103"]):
        try:
            r.main()
        except KeyboardInterrupt:
            pass
        assert sorted(c.kwargs['room'] for c in run.call_args_list) == ['551', '591']
    with scenario() as (client, logs):
        client.request.return_value = {'code': 0}
        r.run(scheduled_for=DUE, room='591')
        r.Client.assert_called_once_with('591')
        assert any('[B101]' in c.args[0] for c in logs.call_args_list)
        assert not any('[A103]' in c.args[0] for c in logs.call_args_list)
    print("PASS: hourly target, opening rules, 100ms concurrent dispatch, retry, success stop, 30s boundary and final verification")


if __name__ == "__main__":
    check()
