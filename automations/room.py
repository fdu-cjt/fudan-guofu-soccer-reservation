"""Discussion-room reservation automation."""
import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time as daytime, timedelta

import math
import os
from pathlib import Path
import re
import socket
import time
import threading

import requests

from fudan_spider.apis import room as room_api
from fudan_spider.apis.room import (
    APP, BASE, PASSWORD, PHONE, ROOM, ROOMS, STATE, USER,
    Client as _RoomClient, RateLimited, SessionExpired,
)
from fudan_spider.utils import SHANGHAI

LOGIN_LOCK = threading.Lock()
START_HOUR = 8
END_HOUR = 22
RETRY_WINDOW_SECONDS = 30
SUBMIT_INTERVAL_SECONDS = 0.1
VERIFY_INTERVAL_SECONDS = 0.5
SUBMISSION_TIMEOUT_SECONDS = 8
VERBOSE_HTTP = False
LOCAL = STATE.parent


def log(message):
    line = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + message
    print(line, flush=True)
    LOCAL.mkdir(exist_ok=True)
    with (LOCAL / "runs.log").open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def import_state(source):
    room_api.import_state(source, state_path=STATE, logger=log)


@contextmanager
def single_instance():
    # ponytail: one local account/room worker; use a shared lock if running on multiple hosts.
    with socket.socket() as lock:
        if os.name == "nt":
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            lock.bind(("127.0.0.1", 55103))
        except OSError:
            raise RuntimeError("Another room reservation worker is running, or port 55103 is occupied.") from None
        yield


class Client(_RoomClient):
    def __init__(self, room=ROOM, state_path=None, logger=None):
        super().__init__(room, state_path=STATE if state_path is None else state_path,
                         logger=logger or log)


def stamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def hours(value):
    result = timedelta(hours=float(value))
    if result < timedelta(0):
        raise ValueError("Negative time setting.")
    return result


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def applies(rule, day):
    return (date.fromisoformat(rule["startDate"]) <= day <= date.fromisoformat(rule["endDate"])
            and str(day.isoweekday()) in rule["weekdays"].split(","))


def at(day, value):
    return datetime.combine(day, daytime.fromisoformat(value), SHANGHAI)


def next_release(now):
    if not 0 <= START_HOUR < END_HOUR <= 24:
        raise ValueError("Expected 0 <= START_HOUR < END_HOUR <= 24.")
    for offset in (0, 1):
        midnight = at(now.date() + timedelta(days=offset), "00:00:00")
        for hour in range(START_HOUR + 1, END_HOUR + 1):
            due = midnight + timedelta(hours=hour)
            if due > now:
                return due


def target_slot(opening, closed, machine, due):
    """At 19:00 today, target tomorrow 18:00-19:00, subject to server rules."""
    end = due + timedelta(days=1)
    start = end - timedelta(hours=1)
    day = start.date()
    if not (at(day, "00:00:00") + timedelta(hours=START_HOUR) <= start
            and end <= at(day, "00:00:00") + timedelta(hours=END_HOUR)):
        return None
    rule = next((r for r in opening if r["status"] == "1" and applies(r, day)), None)
    if rule is None:
        return None
    step = hours(machine["timeLineInterval"])
    minimum, maximum = hours(machine["minInterval"]), hours(machine["maxInterval"])
    if step <= timedelta(0) or minimum <= timedelta(0) or maximum < minimum:
        raise ValueError("Invalid reservation time grid or length limits.")
    opened, shut = at(day, rule["startTime"]), at(day, rule["endTime"])
    if (not opened <= start < end <= shut or not minimum <= end - start <= maximum
            or (start - opened) % step or (end - start) % step
            or start <= due or start < due + hours(rule["minAheadTime"])
            or end > due + hours(rule["maxAheadTime"])
            or any(applies(c, day) and overlaps((start, end), (at(day, c["startTime"]), at(day, c["endTime"]))) for c in closed)):
        return None
    return start, end


def reservation_span(record):
    return (stamp(record["appDate"] + " " + record["appStartTime"]),
            stamp(record["appDate"] + " " + record["appEndTime"]))


def inactive(record):
    return (record["appStatus"] in {"CANCEL", "CANCEL_BY_ADMIN", "REJECT"}
            or (str(record.get("statusFlag")) == "1" and record["appStatus"] == "PASS"))


def submission_status(result):
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        return "unknown"
    if result["code"] == 0:
        # Official labAppointment.js treats this code=0 branch as an unsubmitted confirmation.
        if result.get("data") == "预约时段与课程安排冲突，确定预约？":
            return "confirm"
        return "accepted"  # API acknowledgement; verify actual records together after submissions.
    if result.get("msg") in {"该时间段不可用", "预约时间块无效/冲突，请选择其它时间段",
                             "系统繁忙！由于当前使用人数较多，请稍后再试！"}:
        return "retry"
    return "rejected"


def wait_until(due):
    timer = None
    try:
        while True:
            remaining = (due - datetime.now(SHANGHAI)).total_seconds()
            if remaining <= 0:
                return
            if remaining <= 1 and os.name == "nt" and timer is None:
                candidate = ctypes.WinDLL("winmm")
                if candidate.timeBeginPeriod(1) == 0:
                    timer = candidate
            delay = min(30, remaining - 1) if remaining > 1 else remaining
            time.sleep(delay)
    finally:
        if timer is not None:
            timer.timeEndPeriod(1)


def run(scheduled_for=None, room=ROOM):
    name = ROOMS[room]
    due = scheduled_for or next_release(datetime.now(SHANGHAI))
    deadline = due + timedelta(seconds=RETRY_WINDOW_SECONDS)
    if SUBMIT_INTERVAL_SECONDS <= 0 or RETRY_WINDOW_SECONDS <= 0 or VERIFY_INTERVAL_SECONDS <= 0:
        raise ValueError("Submission interval and window must be positive.")
    if datetime.now(SHANGHAI) >= deadline:
        log(f"[{name}] 已过提交窗口，跳过")
        return
    client = None
    worker_clients = []
    worker_lock = threading.Lock()
    local = threading.local()
    finished = threading.Event()
    futures = []
    try:
        # Serialize login and writes to the shared session cache.
        with LOGIN_LOCK:
            client = Client(room)
            client.login()
        basic, machine, fields, opening, closed = client.settings()
        span = target_slot(opening, closed, machine, due)
        if span is None:
            log(f"[{name}] {due:%m-%d %H:%M} 对应的目标不符合开放规则，跳过")
            return
        start, end = span
        if not re.fullmatch(r"1[3-9]\d{9}", PHONE):
            raise ValueError("Invalid phone.")
        purpose_key = fields[0]["fieldName"] + ("*" if str(fields[0]["necessary"]) == "1" else "")
        payload = {"appStartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                   "appEndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
                   "appUser": USER, "appUserPhone": PHONE, "configUid": basic["uid"],
                   "extendsFieldValue": [{purpose_key: "面试"}], "appType": "MACHINE_APP",
                   "usage": None, "peopleNumber": None, "appPoint": []}
        prepared = client.prepare_submission(payload)
        verifier = client.fork()
        worker_clients.append(verifier)
        rule = next(r for r in opening if r["status"] == "1" and applies(r, start.date()))
        log(f"[{name}] 就绪：{due:%H:%M:%S} 开抢 {start:%m-%d %H:%M}–{end:%H:%M}，每 {SUBMIT_INTERVAL_SECONDS * 1000:g}ms 发起一次，持续 {RETRY_WINDOW_SECONDS}s")

        def submit(stop_at):
            if finished.is_set() or time.perf_counter() >= stop_at:
                return None
            if not hasattr(local, "client"):
                local.client = client.fork()
                with worker_lock:
                    worker_clients.append(local.client)
            # PreparedRequest is mutable; each simultaneous send receives its own copy.
            request, settings = prepared
            worker_prepared = (request.copy(), settings.copy())
            if finished.is_set() or time.perf_counter() >= stop_at:
                return None
            current = datetime.now(SHANGHAI)
            if start <= current or start < current + hours(rule["minAheadTime"]):
                finished.set()
                return None
            begun = time.perf_counter()
            try:
                result = local.client.request("POST", APP + "saveAppInfo", prepared=worker_prepared,
                                              timeout=(3.05, SUBMISSION_TIMEOUT_SECONDS))
            except (SessionExpired, RateLimited) as exc:
                result = {"code": -1, "msg": str(exc)}
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                log(f"[{name}] 提交结果不明：{type(exc).__name__}，发出 {current:%H:%M:%S.%f}")
                result = None
            status = submission_status(result)
            if status in {"accepted", "confirm", "rejected"}:
                finished.set()
            return status, result, current, time.perf_counter() - begun

        def verify(stop_at):
            warned = False
            while not finished.is_set() and time.perf_counter() < stop_at:
                begun = time.perf_counter()
                try:
                    records = verifier.mine()
                    matches = [r for r in records if not inactive(r) and reservation_span(r) == span]
                    if matches:
                        finished.set()
                        states = ", ".join(str(r.get("appStatusInfo", r["appStatus"])) for r in matches)
                        log(f"[{name}] 并发核实已查到目标预约，停止新增提交；状态：{states}")
                        return
                    warned = False
                except (SessionExpired, RateLimited) as exc:
                    finished.set()
                    log(f"[{name}] 核实查询停止本轮：{exc}")
                    return
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    if not warned:
                        log(f"[{name}] 核实查询异常（{type(exc).__name__}），提交继续")
                        warned = True
                next_check = min(stop_at, begun + VERIFY_INTERVAL_SECONDS)
                finished.wait(max(0, next_check - time.perf_counter()))

        # One dedicated query worker; submission ticks never wait for its network response.
        ticks = math.ceil(RETRY_WINDOW_SECONDS / SUBMIT_INTERVAL_SECONDS)
        verification = None
        with ThreadPoolExecutor(max_workers=ticks + 1) as pool:
            wait_until(due)
            origin = time.perf_counter() - max(0, (datetime.now(SHANGHAI) - due).total_seconds())
            stop_at = origin + RETRY_WINDOW_SECONDS
            try:
                for tick in range(ticks):
                    tick_at = origin + tick * SUBMIT_INTERVAL_SECONDS
                    if finished.wait(max(0, tick_at - time.perf_counter())):
                        break
                    current = time.perf_counter()
                    if current >= stop_at:
                        break
                    if current - tick_at >= SUBMIT_INTERVAL_SECONDS:
                        continue  # Skip missed ticks rather than releasing a catch-up burst.
                    futures.append(pool.submit(submit, stop_at))
                    if verification is None:
                        verification = pool.submit(verify, stop_at)
            except BaseException:
                finished.set()
                raise
        if verification is not None:
            verification.result()
        results = [result for future in futures if (result := future.result()) is not None]
        accepted = sum(status == "accepted" for status, *_ in results)
        unknown = sum(status == "unknown" for status, *_ in results)
        log(f"[{name}] 提交结束：发出 {len(results)} 次，接口接收 {accepted} 次，结果不明 {unknown} 次")
        for status, result, sent_at, elapsed in results:
            if status in {"accepted", "confirm", "rejected"}:
                message = str(result.get("msg", result.get("data", "")))[:160].replace("\n", " ").replace("\r", " ")
                log(f"[{name}] {status}，发出 {sent_at:%H:%M:%S.%f}，耗时 {elapsed:.2f}s，{message}")
                break
        # Queries never interrupt the timed submissions. In-flight requests may finish after 30s.
        wait_until(deadline)
        records = client.mine()
        matches = [r for r in records if not inactive(r) and reservation_span(r) == span]
        if matches:
            states = ", ".join(str(r.get("appStatusInfo", r["appStatus"])) for r in matches)
            log(f"[{name}] 已查到 {start:%m-%d %H:%M}–{end:%H:%M}：{len(matches)} 条有效记录，状态：{states}")
        else:
            log(f"[{name}] 暂未查到 {start:%m-%d %H:%M}–{end:%H:%M} 的有效预约记录，未确认成功")
    finally:
        for worker in worker_clients:
            worker.http.close()
        if client is not None:
            client.http.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-state", type=Path, help="Import a Playwright storage-state JSON, then exit.")
    parser.add_argument("--rooms", nargs="+", choices=list(ROOMS.values()), default=[ROOMS[ROOM]],
                        help="Rooms to reserve independently (default: A103).")
    args = parser.parse_args()
    selected = [rid for rid, name in ROOMS.items() if name in args.rooms]
    with single_instance():
        if args.import_state:
            import_state(args.import_state)
            return
        while True:
            due = next_release(datetime.now(SHANGHAI))
            prepare_at = due - timedelta(seconds=15)
            log(f"下次准备 {prepare_at:%m-%d %H:%M:%S}，提交 {due:%m-%d %H:%M:%S}（上海时间）")
            wait_until(prepare_at)
            try:
                if len(selected) == 1:
                    run(scheduled_for=due, room=selected[0])
                else:
                    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                        jobs = {rid: pool.submit(run, scheduled_for=due, room=rid)
                                for rid in selected}
                        for rid, job in jobs.items():
                            try:
                                job.result()
                            except (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                                detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                                log(f"[{ROOMS[rid]}] STOPPED: {detail}")
            except (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                log("STOPPED: " + detail)
            wait_until(due)  # Preparation failure must not loop before the same hour.


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
