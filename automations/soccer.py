"""Soccer reservation automation."""
import argparse
import math
import queue
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import time
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from pathlib import Path
from typing import Optional, TextIO

import requests
from captcha_slide import CaptchaError
from soccer_browser import BrowserBridge, BrowserSession, BrowserUnavailable, SubmissionSkipped

from fudan_spider.apis.soccer_api import (
    API, BASE, CREDENTIALS, DEFAULT_LOG_PATH, IDP, LOCAL, PAGE, PASSWORD,
    PHONE, RESOURCE, SSO, STATE, USER, VENUE,
    AccessBlocked, Client as _SoccerClient, NoticeUnread, SessionExpired, TransientError,
    calendar_periods, make_payload, own_slot, slide_solver,
)
from fudan_spider.utils import SHANGHAI, git_version, load_env_file

CAPTCHA_POOL_SIZE = 2
SUBMIT_INTERVAL_SECONDS = 1.0
FIRST_PAIR_GAP_SECONDS = 2.0
RETRY_WINDOW_SECONDS = 120
MAX_SUBMISSION_ATTEMPTS = 10
RETRY_DELAY_SECONDS = 1.0
MAX_VERIFICATION_ATTEMPTS = 3
MAX_RECOVERY_ATTEMPTS = 3
VERIFY_INTERVAL_SECONDS = 0.5
OPENING_GUARD_MS = 100       # Avoid submitting before the server's opening boundary.
RECOVERY_RETRY_SECONDS = 2
CAPTCHA_MAX_AGE_SECONDS = 10
RUN_EVENTS = []
LOG_LOCK = threading.Lock()
SECRETS = ()
LOG_PATH = DEFAULT_LOG_PATH
LOG_STREAM: Optional[TextIO] = None
LOG_REASON = ""


def log(message):
    """脱敏后同时写终端和日志文件；写盘失败只提示一次，不影响预约主流程。"""
    global LOG_REASON
    text = str(message)
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "[redacted]")
    line = f"{datetime.now(SHANGHAI):%Y-%m-%d %H:%M:%S.%f}"[:-3] + " " + text
    print(line, flush=True)
    stream = LOG_STREAM
    if stream is None:
        if not LOG_REASON:
            LOG_REASON = "日志文件未打开"
        return
    try:
        stream.write(line + "\n")
        stream.flush()
    except OSError as exc:
        LOG_REASON = f"{type(exc).__name__}"
        print("WARNING: 日志文件写入失败，后续只打印到终端", flush=True)


def setup_logging(path=None):
    """按日期保留日志；显式路径也追加写入，并返回 (路径, 失败原因)。"""
    global LOG_PATH, LOG_STREAM, LOG_REASON
    candidate = Path(path) if path else Path(DEFAULT_LOG_PATH).with_name(
        f"{Path(DEFAULT_LOG_PATH).stem}-{now():%Y-%m-%d}{Path(DEFAULT_LOG_PATH).suffix}"
    )
    LOG_PATH, LOG_STREAM, LOG_REASON = candidate, None, ""
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        LOG_STREAM = candidate.open("a", encoding="utf-8")
    except OSError as exc:
        LOG_STREAM, LOG_REASON = None, f"{type(exc).__name__}: {exc}"
    return LOG_PATH, LOG_REASON


def close_logging():
    """收尾关文件；由 main() 的 finally 保证调到，异常路径也不会漏。"""
    global LOG_STREAM, LOG_REASON
    stream, LOG_STREAM = LOG_STREAM, None
    if stream is not None:
        try:
            stream.close()
        except OSError as exc:
            LOG_REASON = f"{type(exc).__name__}"


def audit(event, **fields):
    entry = {"time": now().isoformat(), "event": event, **fields}
    with LOG_LOCK:
        RUN_EVENTS.append(entry)
        log(json.dumps(entry, ensure_ascii=False))


def load_credentials():
    """按「.env 文件 < 环境变量」的顺序读取账号密码手机号，并填好日志脱敏名单。"""
    global USER, PASSWORD, PHONE, SECRETS
    try:
        file_values = load_env_file(CREDENTIALS)
    except OSError as exc:
        log(f"凭据文件不可读（{type(exc).__name__}），改用环境变量。")
        file_values = {}
    origin = {}
    targets = {"FUDAN_USER": "USER", "FUDAN_PASSWORD": "PASSWORD", "FUDAN_PHONE": "PHONE"}
    for name, global_name in targets.items():
        value = os.environ.get(name) or file_values.get(name) or globals()[global_name]
        origin[name] = "环境变量" if os.environ.get(name) else ("凭据文件" if file_values.get(name) else "源码兜底值")
        globals()[global_name] = value
    SECRETS = (PASSWORD, USER, PHONE)
    if not all((USER, PASSWORD, PHONE)):
        raise RuntimeError(f"账号密码手机号不完整，请填写 {CREDENTIALS} 或设置 FUDAN_USER/FUDAN_PASSWORD/FUDAN_PHONE。")
    if "源码兜底值" in origin.values():
        fallback = ", ".join(n for n, o in origin.items() if o == "源码兜底值")
        log(f"WARNING: {fallback} 仍在使用源码里的兜底值，建议移到 {CREDENTIALS}（已被 .gitignore 忽略）。")
    else:
        log("凭据来源：" + "，".join(f"{n}={o}" for n, o in origin.items()))



@contextmanager
def single_instance(port=55104):
    # ponytail: 一个本地账号只跑一个抢场进程；多机部署要换成共享锁。
    with socket.socket() as lock:
        if os.name == "nt":
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            lock.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"另一个抢场进程还在运行，或端口 {port} 被占用。") from None
        yield




def now():
    return datetime.now(SHANGHAI)


def wait_until(moment):
    while now() < moment:
        time.sleep(min(30, max(0, (moment - now()).total_seconds())))



class Client(_SoccerClient):
    def __init__(self, state_path=None, user=None, password=None, phone=None,
                 browser=None, logger=None, auditor=None):
        super().__init__(
            state_path=STATE if state_path is None else state_path,
            user=USER if user is None else user,
            password=PASSWORD if password is None else password,
            phone=PHONE if phone is None else phone,
            browser=browser,
            logger=logger or log,
            auditor=auditor or audit,
        )


def selected_period_ids(calendar, slot_numbers):
    """Map user-facing slot numbers to the validated platform period IDs."""
    if slot_numbers is None:
        return None
    numbers = {int(value) for value in slot_numbers}
    if not numbers or not numbers <= {1, 2}:
        raise ValueError("预约时段只能选择 1（18:00-19:00）或 2（19:00-20:00）。")
    periods = calendar_periods(calendar)
    return {periods[number - 1]["id"] for number in numbers}


def available_slots(calendar, own, current, slot_numbers=None):
    """Only explicit availability in the validated booking range may start an immediate attempt."""
    periods = calendar_periods(calendar)
    selected = selected_period_ids(calendar, slot_numbers)
    if selected is not None:
        periods = [period for period in periods if period["id"] in selected]
    available = {}
    for day_text, fields in sorted(calendar["data"].items()):
        day = date.fromisoformat(day_text)
        if not current.date() <= day <= target_day(current.date()):
            continue
        for period in periods:
            start = datetime.fromisoformat(f"{day_text}T{period['start_time']}").replace(tzinfo=SHANGHAI)
            if start <= current or own_slot(own, day_text, period["id"]):
                continue
            cells = [fields[str(field["id"])][str(period["id"])] for field in calendar["resource"]]
            if any(cell["status"] == 0 and int(cell["num"]) > 0 for cell in cells):
                available.setdefault(day, set()).add(period["id"])
    return available


def read_bookings(client, audit_records=False):
    """A broken read-only booking list must not prevent a timed submission."""
    if time.monotonic() < client.__dict__.get("_booking_list_retry_at", 0):
        return None
    try:
        return client.mine(audit_records=True) if audit_records else client.mine()
    except (SessionExpired, BrowserUnavailable, AccessBlocked):
        raise
    except (TransientError, requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, TransientError) and exc.status_code == 429:
            client._booking_list_retry_at = time.monotonic() + max(60, exc.retry_after or 0)
        audit("booking_list_unavailable", error=type(exc).__name__, reason=str(exc)[:200])
        return None


def report_daily_bookings(client, day, calendar, target_periods=None):
    """At 07:05, report the selected periods found in the user's bookings."""
    records = read_bookings(client, audit_records=True)
    periods = calendar_periods(calendar)
    if target_periods is not None:
        periods = [period for period in periods if period["id"] in target_periods]
    report = {}
    for period in periods:
        unknown = []
        record = own_slot(records, str(day), period["id"], unknown=unknown)
        report[period["str_time"]] = (str(record.get("status_name") or "已预约") if record else
                                      "无法核实：预约列表读取失败" if records is None else
                                      "无法核实：预约记录格式不识别" if unknown else "未查到预约")
    summary = "；".join(f"{period}：{status}" for period, status in report.items()) or "无目标时段"
    log(f"{day} 07:05预约汇报：{summary}")
    audit("daily_report", day=str(day), periods=report)
    return report


def _submission_kind(response):
    """Classify the platform reply before deciding whether another POST is safe."""
    code = str(response.get("e") or "").upper()
    message = str(response.get("m") or code)
    text = f"{code} {message}".lower()
    if code == "OK":
        return "accepted", message
    if code == "APPLY" or any(word in text for word in ("未提交的预约", "未结束的预约", "待审核预约", "已有预约申请")):
        return "account_stop", message
    if code in {"VERIFY", "CAPTCHA", "SLIDE"} or any(
            word in text for word in ("captcha", "验证码", "滑块", "点选", "安全校验")):
        return "captcha", message
    if code in {"429", "RATE_LIMIT", "TOO_FREQUENT"} or any(
            word in text for word in ("429", "too many", "访问频繁", "请求过于频繁", "拒绝访问",
                                      "防护", "blocked")):
        return "blocked", message
    if any(word in text for word in ("点击太频繁", "操作太频繁", "提交太频繁", "预约太频繁",
                                     "请求太频繁", "间隔太短", "提交间隔", "请求间隔")):
        return "retry", message
    if any(word in text for word in ("预约次数", "最多预约", "没有权限", "无权限", "表单", "手机号",
                                     "规则发生变化", "规则已变更", "不允许预约", "合计达到上限")):
        return "account_stop", message
    if any(word in text for word in ("时段冲突", "不可预约", "已约满")):
        return "target_stop", message
    if any(word in text for word in ("系统繁忙", "稍后再试", "服务异常", "暂不可用", "未开放", "网络", "系统超时", "不在服务时间")):
        return "retry", message
    return "target_stop", message


def _rate_limited(exc):
    text = str(exc).lower()
    return isinstance(exc, TransientError) and exc.status_code == 429 or any(
        marker in text for marker in ("http 429", "http 403", "http 412", "too many",
                                     "访问频繁", "请求过于频繁", "防护", "blocked"))


def _soft_submission_limit(message):
    text = str(message).lower()
    return any(marker in text for marker in (
        "429", "too many", "too frequent", "too_frequent", "访问频繁", "请求过于频繁",
        "预约太频繁", "请求太频繁", "点击太频繁", "操作太频繁", "提交太频繁",
        "间隔太短", "提交间隔", "请求间隔")) and not any(
        marker in text for marker in ("拒绝访问", "防护", "blocked"))


def _soft_rate_limited(exc):
    text = str(exc).lower()
    return ((isinstance(exc, TransientError) and exc.status_code == 429
             and not any(marker in text for marker in ("拒绝访问", "防护", "blocked")))
            or _soft_submission_limit(exc))


def poll(client, day, deadline, commit=True, once=False, calendar=None, own=None, opening=None, anti_bot=0,
         verifier_factory=None, target_periods=None):
    calendar = client.calendar(day) if calendar is None else calendar
    own = read_bookings(client) if own is None else own
    periods = calendar_periods(calendar)
    fields = calendar["resource"]
    if target_periods is not None:
        if not target_periods or not set(target_periods) <= {p["id"] for p in periods}:
            raise ValueError("预约目标必须是已校验日历中的非空时段集合。")
        periods = [p for p in periods if p["id"] in target_periods]
    if SUBMIT_INTERVAL_SECONDS <= 0:
        raise ValueError("提交间隔必须大于零。")
    opening = opening or now()
    submission_deadline = min(deadline, opening + timedelta(seconds=RETRY_WINDOW_SECONDS))
    attempt_limit = len(periods) if once else MAX_SUBMISSION_ATTEMPTS
    confirmed, halted, uncertain = {}, {}, set()
    workers, futures = [], []
    lock = threading.Lock()
    stop, ready, recovery_needed, verify_wake, solving = (threading.Event() for _ in range(5))
    access_blocked = threading.Event()
    ready.set()
    if anti_bot == 1:
        solving.set()
    attempts, generation, recovery_attempts = 0, 0, 0
    attempts_by_target = {period["id"]: 0 for period in periods}
    in_flight, awaiting_verification, unknown_targets = set(), set(), set()
    retry_order = [period["id"] for period in periods]
    retry_started = False
    format_warned = False
    pause_reason = None
    last_submit_at, retry_after = -float("inf"), {}
    submit_gate, next_submit_at = threading.Lock(), -float("inf")
    pool = queue.Queue(maxsize=CAPTCHA_POOL_SIZE)
    if isinstance(client.http, BrowserSession):
        client.http.window = (opening.timestamp()*1000, submission_deadline.timestamp()*1000)

    def maybe_stop_locked():
        if all(period["id"] in confirmed or period["id"] in halted for period in periods) \
                and not awaiting_verification:
            stop.set()
            verify_wake.set()

    def update(records):
        nonlocal format_warned
        with lock:
            for period in periods:
                unknown = []
                record = own_slot(records, str(day), period["id"], unknown=unknown)
                if records is not None and unknown and not format_warned:
                    audit("booking_format_unknown", record_ids=unknown)
                    format_warned = True
                if record and period["id"] not in confirmed:
                    confirmed[period["id"]] = str(record.get("status_name") or "已预约")
                    awaiting_verification.discard(period["id"])
                    unknown_targets.discard(period["id"])
                    uncertain.discard(period["id"])
                    halted.pop(period["id"], None)
                    audit("confirmed", targets=[period["id"]], status=confirmed[period["id"]])
            maybe_stop_locked()

    def halt_target(target, reason, is_uncertain=False):
        with lock:
            if target not in confirmed:
                halted[target] = reason
                awaiting_verification.discard(target)
                unknown_targets.discard(target)
                if is_uncertain:
                    uncertain.add(target)
            audit("target_stopped", targets=[target], reason=reason, uncertain=is_uncertain)
            maybe_stop_locked()

    def pause_all(reason, access=False, **fields):
        nonlocal pause_reason
        pause_reason = str(fields.get("message") or fields.get("error") or reason)
        if access:
            access_blocked.set()
        stop.set()
        verify_wake.set()
        audit("submission_paused", reason=reason, **fields)

    def schedule_retry(target, reason, delay=None, timeout=False):
        nonlocal retry_started
        delay = RETRY_DELAY_SECONDS if delay is None else delay
        with lock:
            if not retry_started:
                retry_started = True
                # Both initial requests timing out keep the default A/B order.
                if not timeout:
                    index = retry_order.index(target)
                    retry_order[:] = retry_order[index:] + retry_order[:index]
                audit("retry_started", reason=reason, first_target=retry_order[0])
            retry_after[target] = time.monotonic() + delay
            audit("retry_scheduled", targets=[target], reason=reason, delay=delay)

    def exhaust_locked():
        targets = []
        for period in periods:
            target = period["id"]
            if target in confirmed or target in halted or (target in awaiting_verification and target not in unknown_targets):
                continue
            if target in awaiting_verification or target in unknown_targets:
                uncertain.add(target)
            halted[target] = "已达到提交次数上限"
            awaiting_verification.discard(target)
            unknown_targets.discard(target)
            targets.append(target)
        if targets:
            audit("submission_exhausted", targets=targets, attempts=attempts,
                  total_limit=attempt_limit)
        maybe_stop_locked()

    update(own)
    if not commit:
        for period in periods:
            log(f"{day} {period['str_time']}：" + confirmed.get(period["id"], "未查到有效预约（仅检查，不提交）"))
        return confirmed

    def need_recovery(exc):
        ready.clear()
        recovery_needed.set()
        verify_wake.set()
        audit("recovery_required", error=type(exc).__name__)

    verifier = (verifier_factory or (lambda c: c.fork()))(client)
    verify_wake.set()  # Perform one readiness check; after opening, verify only on demand.

    def verify():
        retry_at = None
        verification_rounds = {}
        verification_errors = 0
        while not stop.is_set() and now() < deadline:
            current = now()
            remaining = max(0, (deadline-current).total_seconds())
            if retry_at is not None:
                timeout = min(remaining, max(0, retry_at-time.monotonic()))
            elif current < opening:
                timeout = min(remaining, 15, max(0, (opening-current).total_seconds()))
            else:
                timeout = remaining
            woke = verify_wake.wait(timeout)
            if stop.is_set() or now() >= deadline:
                break
            # Before opening, a timeout is the periodic readiness check. After opening,
            # an idle verifier sleeps until a submit/recovery path explicitly wakes it.
            if not woke and now() >= opening and retry_at is None:
                continue
            verify_wake.clear()  # Events raised during the query remain for the next pass.
            retry_at = None
            epoch = generation
            if ready.is_set():
                try:
                    update(read_bookings(verifier))
                    verification_errors = 0
                except (SessionExpired, BrowserUnavailable) as exc:
                    if epoch == generation:
                        need_recovery(exc)
                except AccessBlocked as exc:
                    pause_all("verification_access_blocked", access=True, error=str(exc)[:200])
                except TransientError as exc:
                    if _rate_limited(exc):
                        pause_all("verification_rate_limited", access=True, error=str(exc)[:200],
                                  retry_after=exc.retry_after)
                    else:
                        verification_errors += 1
                        if verification_errors >= MAX_VERIFICATION_ATTEMPTS:
                            pause_all("verification_exhausted", error=type(exc).__name__)
                        else:
                            delay = max(VERIFY_INTERVAL_SECONDS, exc.retry_after or 0)
                            audit("verification_retry", error=type(exc).__name__,
                                  attempt=verification_errors, retry_after=delay)
                            retry_at = time.monotonic() + delay
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    if _rate_limited(exc):
                        pause_all("verification_rate_limited", access=True, error=str(exc)[:200])
                    else:
                        verification_errors += 1
                    if not stop.is_set() and verification_errors >= MAX_VERIFICATION_ATTEMPTS:
                        pause_all("verification_exhausted", error=type(exc).__name__)
                    elif not stop.is_set():
                        audit("verification_error", error=type(exc).__name__, attempt=verification_errors)
                        retry_at = time.monotonic() + VERIFY_INTERVAL_SECONDS
                with lock:
                    pending = set(awaiting_verification)
                if pending and not stop.is_set() and ready.is_set():
                    with lock:
                        for target in pending:
                            verification_rounds[target] = verification_rounds.get(target, 0) + 1
                            keep_retrying = (not once
                                              and target in unknown_targets
                                              and attempts_by_target[target] < attempt_limit)
                            if verification_rounds[target] >= MAX_VERIFICATION_ATTEMPTS and not keep_retrying:
                                halted[target] = "核实超时，结果不确定"
                                uncertain.add(target)
                                awaiting_verification.discard(target)
                                audit("verification_uncertain", targets=[target])
                        maybe_stop_locked()
                    retry_at = max(retry_at or 0, time.monotonic() + VERIFY_INTERVAL_SECONDS)

    def produce_captcha():
        solver = slide_solver(client)
        while not stop.is_set() and now() < deadline:
            # Prefetch shortly before opening; never block the dispatch thread on solving.
            if not solving.is_set() or not ready.is_set() or pool.full() or now() < opening-timedelta(seconds=10):
                stop.wait(.05)
                continue
            epoch = generation
            try:
                solved, _, _ = solver.solve()
                if epoch == generation and ready.is_set() and not stop.is_set():
                    pool.put_nowait((solved, time.monotonic(), epoch))
            except (SessionExpired, BrowserUnavailable) as exc:
                if epoch == generation:
                    need_recovery(exc)
            except AccessBlocked as exc:
                pause_all("captcha_access_blocked", access=True, error=str(exc)[:200])
            except TransientError as exc:
                if _rate_limited(exc):
                    pause_all("captcha_rate_limited", access=True, error=str(exc)[:200],
                              retry_after=exc.retry_after)
                else:
                    audit("captcha_error", error=type(exc).__name__)
                    stop.wait(max(.2, exc.retry_after or 0))
            except (CaptchaError, requests.RequestException, RuntimeError, ValueError, KeyError, TypeError, queue.Full) as exc:
                if _rate_limited(exc):
                    pause_all("captcha_rate_limited", access=True, error=str(exc)[:200])
                else:
                    audit("captcha_error", error=type(exc).__name__)
                    stop.wait(.2)

    local = threading.local()
    # One worker per selected period; retries normally share the account cadence.
    executor = ThreadPoolExecutor(max_workers=max(1, len(periods)))

    def submit(target, payload, planned_at):
        nonlocal next_submit_at
        epoch = generation
        try:
            if stop.is_set() or not ready.is_set() or now() >= submission_deadline:
                return
            if not hasattr(local, "client"):
                local.client = client.fork()
                with lock:
                    workers.append(local.client)
            with submit_gate:
                delay = next_submit_at - time.monotonic()
                if delay > 0:
                    stop.wait(min(delay, max(0, (submission_deadline-now()).total_seconds())))
                if stop.is_set() or now() >= submission_deadline:
                    return
                with lock:
                    if stop.is_set() or target in confirmed or target in halted:
                        return
                if isinstance(local.client.http, BrowserSession):
                    local.client.http.planned_at = planned_at
                sent_at = time.monotonic()
                next_submit_at = sent_at + SUBMIT_INTERVAL_SECONDS
                with lock:
                    if target == first_lead and attempts_by_target[target] == 1 and first_follow is not None:
                        first_due[first_follow] = sent_at + FIRST_PAIR_GAP_SECONDS
            response = local.client.submit(payload)
            if not isinstance(response, dict) or "e" not in response:
                raise ValueError("预约响应结构错误")
            with lock:
                if target == first_lead and attempts_by_target[target] == 1 and first_follow is not None:
                    first_due[first_follow] = min(first_due[first_follow],
                                                 time.monotonic() + SUBMIT_INTERVAL_SECONDS)
        except SubmissionSkipped:
            halt_target(target, "浏览器跳过了提交窗口")
            return
        except SessionExpired as exc:
            # An explicit auth rejection is not an accepted booking awaiting confirmation.
            with lock:
                awaiting_verification.discard(target)
                unknown_targets.discard(target)
            if epoch == generation:
                need_recovery(exc)
            schedule_retry(target, "session_expired")
            return
        except BrowserUnavailable as exc:
            with lock:
                awaiting_verification.add(target)
            if epoch == generation:
                need_recovery(exc)
            return
        except AccessBlocked as exc:
            pause_all("submission_access_blocked", access=True, targets=[target], error=str(exc)[:200])
            return
        except TransientError as exc:
            if _rate_limited(exc) and not _soft_rate_limited(exc):
                pause_all("submission_rate_limited", access=True, targets=[target], error=str(exc)[:200])
            else:
                schedule_retry(target, type(exc).__name__, max(RETRY_DELAY_SECONDS, exc.retry_after or 0))
            return
        except requests.RequestException as exc:
            if _rate_limited(exc) and not _soft_rate_limited(exc):
                pause_all("submission_rate_limited", access=True, targets=[target], error=str(exc)[:200])
                return
            with lock:
                awaiting_verification.add(target)
                unknown_targets.add(target)
            timed_out = isinstance(exc, requests.Timeout)
            schedule_retry(target, type(exc).__name__, 0 if timed_out else RETRY_DELAY_SECONDS, timeout=timed_out)
            audit("submission_unknown", error=type(exc).__name__, targets=[target], retrying=not once)
            verify_wake.set()
            return
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            if _soft_rate_limited(exc):
                schedule_retry(target, str(exc)[:200])
            elif _rate_limited(exc):
                pause_all("submission_rate_limited", access=True, targets=[target], error=str(exc)[:200])
            elif isinstance(exc, (ValueError, KeyError, TypeError)):
                halt_target(target, f"提交响应结构错误：{type(exc).__name__}")
            else:
                halt_target(target, f"未识别的提交错误：{type(exc).__name__}", is_uncertain=True)
            return
        else:
            with lock:
                awaiting_verification.discard(target)
                unknown_targets.discard(target)
            kind, message = _submission_kind(response)
            if kind == "accepted":
                with lock:
                    awaiting_verification.add(target)
                audit("accepted_unconfirmed", targets=[target])
                verify_wake.set()
            elif kind == "captcha":
                solving.set()
                audit("verification_required", stage="submission", targets=[target])
            elif kind == "blocked":
                if _soft_submission_limit(message):
                    schedule_retry(target, message)
                else:
                    pause_all("submission_response_blocked", access=True, targets=[target], message=message)
            elif kind == "account_stop":
                pause_all("account_rejected", targets=[target], message=message)
            elif kind == "target_stop":
                halt_target(target, message)
            else:
                schedule_retry(target, message)
        finally:
            with lock:
                in_flight.discard(target)

    threads = [threading.Thread(target=verify, name="soccer-verifier"),
               threading.Thread(target=produce_captcha, name="soccer-captcha")]
    anchor = time.monotonic() + (opening-now()).total_seconds()
    first_due = {p['id']: anchor for p in periods}
    initial_targets = [p['id'] for p in periods if p['id'] not in confirmed]
    first_lead = initial_targets[0] if initial_targets else None
    first_follow = initial_targets[1] if len(initial_targets) == 2 else None
    if first_follow is not None:
        first_due[first_follow] = float("inf")
    try:
        for thread in threads:
            thread.start()
        while not stop.is_set() and now() < deadline:
            if recovery_needed.is_set():
                ready.clear()
                generation += 1
                for future in futures:
                    future.result()  # Drain old submissions before replacing the shared browser.
                futures.clear()
                while not pool.empty():
                    pool.get_nowait()
                recovered = False
                while not stop.is_set() and now() < deadline:
                    if recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
                        pause_all("recovery_exhausted")
                        break
                    recovery_attempts += 1
                    try:
                        audit("recovery_attempt", attempt=recovery_attempts)
                        client.recover(deadline)
                        if isinstance(client, Client):
                            info = client.settings()
                            if info["config"]["anti_bot"] == 1:
                                solving.set()
                        recovered = True
                        break
                    except AccessBlocked as exc:
                        pause_all("recovery_access_blocked", access=True, error=str(exc)[:200])
                        break
                    except TransientError as exc:
                        if _rate_limited(exc):
                            pause_all("recovery_rate_limited", access=True, error=str(exc)[:200],
                                      retry_after=exc.retry_after)
                            break
                        delay = max(RECOVERY_RETRY_SECONDS, exc.retry_after or 0)
                        audit("recovery_retry", error=type(exc).__name__, retry_after=delay)
                        stop.wait(min(delay, max(0, (deadline-now()).total_seconds())))
                    except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                        if _rate_limited(exc):
                            pause_all("recovery_rate_limited", access=True, error=str(exc)[:200])
                            break
                        audit("recovery_retry", error=type(exc).__name__, attempt=recovery_attempts)
                        stop.wait(min(RECOVERY_RETRY_SECONDS, max(0, (deadline-now()).total_seconds())))
                if not recovered:
                    break
                recovery_needed.clear()
                ready.set()
                verify_wake.set()
                audit("recovery_complete", confirmed=sorted(confirmed))
                continue
            if now() < opening:
                stop.wait(min(.1, max(0, (opening-now()).total_seconds())))
                continue
            if not once and attempts >= attempt_limit and not in_flight:
                with lock:
                    exhaust_locked()
                stop.wait(.01)
                continue
            pending = []
            for future in futures:
                if future.done():
                    future.result()
                else:
                    pending.append(future)
            futures = pending
            # Give untouched periods their first opportunity before retrying a
            # period that already received a response; otherwise a fast busy
            # response from the first period can starve the second one.
            with lock:
                order = list(retry_order)
            ordered_periods = sorted(
                periods,
                key=lambda period: (
                    attempts_by_target[period["id"]] > 0,
                    order.index(period["id"]),
                ),
            )
            for period in ordered_periods:
                if stop.is_set() or recovery_needed.is_set():
                    break
                with lock:
                    target = period["id"]
                    current_mono = time.monotonic()
                    retryable_unknown = (not once
                                         and target in unknown_targets
                                         and attempts_by_target[target] < attempt_limit)
                    if (target in in_flight or target in confirmed or target in halted
                            or (target in awaiting_verification and not retryable_unknown)):
                        continue
                    if now() >= submission_deadline:
                        halted[target] = "提交窗口结束"
                        audit("submission_exhausted", targets=[target], attempts=attempts_by_target[target])
                        maybe_stop_locked()
                        continue
                    if not once and attempts >= attempt_limit and not in_flight:
                        exhaust_locked()
                        break
                    if once and attempts_by_target[target] > 0:
                        if target not in awaiting_verification:
                            halted[target] = "单次尝试已完成"
                            maybe_stop_locked()
                        continue
                    if attempts >= attempt_limit:
                        continue
                    if (attempts_by_target[target] > 0 and first_follow is not None
                            and attempts_by_target[first_follow] == 0
                            and first_follow not in confirmed and first_follow not in halted):
                        continue  # Reserve B's first opportunity before retrying A.
                    if (target == first_follow and first_due[target] == float("inf")
                            and (first_lead in confirmed or first_lead in halted)):
                        first_due[target] = current_mono
                    if attempts_by_target[target] == 0:
                        due = max(first_due[target], last_submit_at + SUBMIT_INTERVAL_SECONDS)
                    else:
                        due = max(first_due[target], retry_after.get(target, 0),
                                  last_submit_at + SUBMIT_INTERVAL_SECONDS)
                    if current_mono < due:
                        continue
                    planned_at = (opening.timestamp() + due - anchor) * 1000
                captcha = None
                if solving.is_set():
                    while True:
                        try:
                            value, created, epoch = pool.get_nowait()
                        except queue.Empty:
                            break
                        if epoch == generation and time.monotonic()-created < CAPTCHA_MAX_AGE_SECONDS:
                            captcha = value
                            break
                    if captcha is None:
                        audit("captcha_pending", targets=[period["id"]])
                        continue
                with lock:
                    retryable_unknown = (not once
                                         and target in unknown_targets
                                         and attempts_by_target[target] < attempt_limit)
                    if (stop.is_set() or target in confirmed or target in halted
                            or (target in awaiting_verification and not retryable_unknown)):
                        continue
                    if attempts >= attempt_limit:
                        continue
                    if attempts_by_target[target] > 0:
                        index = retry_order.index(target)
                        retry_order[:] = retry_order[index+1:] + retry_order[:index+1]
                    in_flight.add(target)
                    last_submit_at = current_mono
                    attempts += 1
                    attempts_by_target[target] += 1
                    audit("submit", attempts=attempts, target_attempt=attempts_by_target[target],
                          targets=[target], planned_at_ms=planned_at)
                payload = make_payload(day, fields, period, captcha, phone=PHONE)
                try:
                    futures.append(executor.submit(submit, target, payload, planned_at))
                except (RuntimeError, ValueError) as exc:
                    with lock:
                        in_flight.discard(target)
                    halt_target(target, f"提交任务创建失败：{type(exc).__name__}")
            stop.wait(.01)  # Local scheduling only; no network polling here.
    finally:
        stop.set()
        verify_wake.set()
        executor.shutdown(wait=True, cancel_futures=True)
        for thread in threads:
            if thread.ident is not None:
                thread.join()
        for worker in workers:
            worker.http.close()
        verifier.http.close()
        if awaiting_verification:
            with lock:
                for target in awaiting_verification:
                    uncertain.add(target)
                    halted[target] = "核实未完成，结果不确定"
                awaiting_verification.clear()
        if not access_blocked.is_set():
            try:
                update(read_bookings(client))
            except AccessBlocked as exc:
                audit("final_verification_error", error=type(exc).__name__, reason=str(exc)[:200])
            except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
                audit("final_verification_error", error=type(exc).__name__)
        if pause_reason:
            for period in periods:
                target = period["id"]
                if target not in confirmed:
                    halted[target] = f"本轮停止：{pause_reason}" + ("；预约结果不确定" if target in uncertain else "")
        for period in periods:
            target = period["id"]
            status = confirmed.get(target) or halted.get(target) or ("结果不确定" if target in uncertain else "未确认成功")
            log(f"{day} {period['str_time']}：{status}")
        audit("run_summary", attempts=attempts, confirmed=sorted(confirmed),
              stopped=halted, uncertain=sorted(uncertain), targets=[p["id"] for p in periods],
              retry_started=retry_started, pause_reason=pause_reason)
    return confirmed


def next_window(current):
    opening = current.replace(hour=7, minute=0, second=0, microsecond=0)
    if current >= opening + timedelta(minutes=5):
        opening += timedelta(days=1)
    return opening, opening + timedelta(minutes=5)


def target_day(anchor, offset_days=2):
    """目标日期 = 放号日 + 2 天。--check 和正式运行共用同一个口径，避免两套算法漂移。"""
    return anchor + timedelta(days=offset_days)


def book_available(client, cutoff, anti_bot, slot_numbers=None):
    current = now()
    end_day = target_day(current.date())
    selected = "、".join({1: "18:00-19:00", 2: "19:00-20:00"}[number]
                        for number in sorted(slot_numbers if slot_numbers is not None else {1, 2}))
    log(f"开始检查已有空位：{current.date()} 至 {end_day}，时段 {selected}。")
    calendar = client.calendar(current.date(), end_day)
    own = read_bookings(client)
    candidates = available_slots(calendar, own, now(), slot_numbers=slot_numbers)
    audit("startup_availability", dates={str(day): sorted(ids) for day, ids in candidates.items()})
    if not candidates:
        log("空位检查结果：未发现可立即尝试的空位（已排除已开始、本人已预约及无可用名额的时段）。")
    labels = {period["id"]: period["str_time"] for period in calendar["time"]}
    for day, targets in candidates.items():
        log(f"空位检查结果：{day}，时段 {'、'.join(labels[target] for target in sorted(targets))} 有候选空位，提交前将再次核实。")
    confirmed = False
    for day in candidates:
        if now() >= cutoff:
            break
        # Recheck each date: earlier attempts may take minutes, and quota can change meanwhile.
        calendar, own = client.calendar(day), read_bookings(client)
        targets = available_slots(calendar, own, now(), slot_numbers=slot_numbers).get(day)
        if not targets:
            continue
        start = min(datetime.fromisoformat(f"{day}T{p['start_time']}").replace(tzinfo=SHANGHAI)
                    for p in calendar["time"] if p["id"] in targets)
        deadline = min(now()+timedelta(minutes=5), cutoff, start)
        if now() >= deadline:
            continue
        log(f"启动发现空位：{day}，时段 {'、'.join(labels[target] for target in sorted(targets))}；立即尝试至 {deadline:%H:%M:%S}。")
        result = poll(client, day, deadline, calendar=calendar, own=own,
                      anti_bot=anti_bot, once=True, target_periods=targets)
        confirmed = bool(result) or confirmed
    return confirmed


def watch_access(client, day, seconds):
    """Low-rate, read-only live check; keep observing errors without hiding them by relogin."""
    start = time.monotonic()
    deadline = start + seconds
    rounds, failures = 0, 0
    checks = [("user", lambda: client.data("/site/user/baseinfo")),
              ("resource", client.settings), ("calendar", lambda: client.calendar(day)),
              ("mine", client.mine)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        while True:
            rounds += 1
            futures = [(name, executor.submit(check)) for name, check in checks]
            for name, future in futures:
                try:
                    future.result()
                    audit("access_check", round=rounds, endpoint=name, ok=True)
                except AccessBlocked:
                    raise
                except TransientError as exc:
                    if _rate_limited(exc):
                        raise
                    failures += 1
                    audit("access_check", round=rounds, endpoint=name, ok=False, error=str(exc)[:200])
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    failures += 1
                    audit("access_check", round=rounds, endpoint=name, ok=False, error=str(exc)[:200])
            if time.monotonic() >= deadline:
                break
            time.sleep(min(15, max(0, deadline - time.monotonic())))
    audit("access_summary", rounds=rounds, requests=rounds * len(checks), failures=failures,
          elapsed_seconds=round(time.monotonic() - start, 1))
    if failures:
        raise RuntimeError(f"持续访问检查中有 {failures} 次失败，尚不能认为会话稳定。")


def hold_browser(close_browser=False):
    """Keep the visible Chrome page available for manual follow-up."""
    if close_browser:
        return
    log("预约程序已停止，浏览器窗口保留；完成验证码或确认操作后按 Enter 关闭浏览器。")
    try:
        input("按 Enter 关闭浏览器（Ctrl+C 也会关闭）：")
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="立即验证登录、表单和时段，不提交。")
    parser.add_argument("--dry-run", action="store_true", help="与 --check 相同。")
    parser.add_argument("--transport", choices=("browser", "http"), default="browser", help="默认使用本机 Chrome；http 保留用于对照。")
    parser.add_argument("--browser-profile", type=Path, default=LOCAL / "soccer-chrome", help="独立 Chrome 配置目录，用于保留登录状态。")
    parser.add_argument("--time", dest="slot_numbers", nargs="+", type=int,
                        choices=(1, 2), metavar="N",
                        help="预约时段：1=18:00-19:00，2=19:00-20:00；默认两个都预约。")
    parser.add_argument("--close-browser", action="store_true",
                        help="停止程序后直接关闭 Chrome；默认保留窗口等待手动处理。")
    parser.add_argument("--watch-seconds", type=float, default=0, help="仅用于 --check：每 15 秒并行检查四个只读接口。")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="日志文件路径；默认按日期写入 logs/soccer-YYYY-MM-DD.log。")
    args = parser.parse_args()
    if not math.isfinite(args.watch_seconds) or args.watch_seconds < 0 or (args.watch_seconds and not (args.check or args.dry_run)):
        parser.error("--watch-seconds 必须非负且与 --check/--dry-run 一起使用。")
    # Acquire before opening a log: a rejected second instance must not truncate it.
    with single_instance():
        run(args)


def run(args, continuous=True):
    path, reason = setup_logging(args.log_file)
    log(f"日志文件：{path}" + (f"（不可写：{reason}）" if reason else "（追加写入）"))
    client, browser = None, None
    try:
        audit("runtime_version", **git_version())
        load_credentials()
        log("预约调度：先发A，B在A响应后1秒或首发后2秒较早时发送；请求5秒超时，超时立即参与重试，普通失败冷却1秒；交替发送间隔1秒，合计最多10次。")
        slot_numbers = getattr(args, "slot_numbers", None)
        if not isinstance(slot_numbers, (list, tuple, set)):
            slot_numbers = {1, 2}
        else:
            slot_numbers = set(slot_numbers)
        log("预约时段：" + ", ".join(
            f"{number}（{('18:00-19:00' if number == 1 else '19:00-20:00')}）"
            for number in sorted(slot_numbers)))
        preview = args.check or args.dry_run
        first_cycle = True
        must_login = True
        while True:
            opening, deadline = next_window(now())
            if preview:
                deadline = now() + timedelta(seconds=90)
            elif first_cycle:
                log(f"先检查已开放空位；即使已有预约也继续等待 {opening:%Y-%m-%d %H:%M}–{deadline:%H:%M} 放号窗口，07:00 进入提交。")
            else:
                log(f"等待下一次放号准备：{opening:%Y-%m-%d %H:%M}–{deadline:%H:%M}，07:00 进入提交；已预约时段会自动跳过。")
            day = target_day(now().date() if preview else opening.date())
            startup = first_cycle and not preview
            prepared = False
            preparation_failures = 0
            notice_attempted = False
            while now() < deadline:
                try:
                    new_client = client is None
                    if new_client:
                        if args.transport == "browser":
                            browser = BrowserBridge(read_only=preview, profile=args.browser_profile.resolve(),
                                on_event=lambda fields: audit("browser_progress", **fields), deadline=deadline.timestamp())
                            audit("browser", **browser.info)
                        client = Client(browser=browser)
                    if must_login:
                        client.login(deadline) if new_client else client.recover(deadline)
                        must_login = False
                    else:
                        user = client.data("/site/user/baseinfo")
                        if str(user["number"]) != client.user:
                            raise SessionExpired("浏览器账号发生变化，需要重新核对登录。")
                    info = client.settings()
                    if startup:
                        preparation = opening-timedelta(minutes=5)
                        book_available(client, preparation if now() < preparation else opening,
                                       info["config"]["anti_bot"], slot_numbers=slot_numbers)
                        first_cycle = False
                        startup = False
                        if now() < preparation:
                            log(f"启动空位检查完成；等待期间不持续扫描空位。下一次准备时间：{preparation:%Y-%m-%d %H:%M}，放号时间：{opening:%Y-%m-%d %H:%M}。")
                        else:
                            log(f"启动空位检查完成；已进入本轮放号准备阶段，放号时间：{opening:%Y-%m-%d %H:%M}。")
                        wait_until(preparation)
                        continue
                    calendar, own = client.calendar(day), read_bookings(client)
                    if not preview and now() < opening-timedelta(minutes=3):
                        audit("preparation", stage="logged_in")
                        wait_until(opening-timedelta(minutes=3))
                        continue
                    prepared = True
                    break
                except NoticeUnread as exc:
                    if browser is None or notice_attempted:
                        raise
                    notice_attempted = True
                    log("检测到未阅读预约须知，使用官网页面完成阅读和确认。")
                    result = browser.call("confirmNotice", timeout=70,
                                          deadlineMs=deadline.timestamp() * 1000)
                    audit("notice_confirmed", characters=result["characters"])
                except AccessBlocked:
                    raise
                except TransientError as exc:
                    preparation_failures += 1
                    if _rate_limited(exc) or preparation_failures >= MAX_RECOVERY_ATTEMPTS:
                        raise
                    audit("preparation_retry", error=type(exc).__name__, reason=str(exc)[:200])
                    must_login = True
                    time.sleep(min(max(RECOVERY_RETRY_SECONDS, exc.retry_after or 0),
                                   max(0, (deadline-now()).total_seconds())))
                except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                    preparation_failures += 1
                    if preparation_failures >= MAX_RECOVERY_ATTEMPTS:
                        raise
                    audit("preparation_retry", error=type(exc).__name__, reason=str(exc)[:200])
                    must_login = True
                    time.sleep(min(RECOVERY_RETRY_SECONDS, max(0, (deadline-now()).total_seconds())))
            if not prepared:
                raise RuntimeError("准备阶段未能恢复登录或完成规则检查，已到本次截止时间。")
            target_periods = (selected_period_ids(calendar, slot_numbers)
                              if isinstance(calendar, dict) else None)
            audit("preparation", stage="ready", seconds_before_opening=round((opening-now()).total_seconds(), 3))
            dispatch_opening = (opening + timedelta(milliseconds=OPENING_GUARD_MS)
                                if not preview else None)
            poll(client, day, deadline, commit=not preview, once=preview, anti_bot=info["config"]["anti_bot"],
                 calendar=calendar, own=own, opening=dispatch_opening, target_periods=target_periods)
            if args.watch_seconds:
                watch_access(client, day, args.watch_seconds)
            if preview or not continuous:
                return
            wait_until(opening + timedelta(minutes=5))
            must_login = False
            try:
                report_daily_bookings(client, day, calendar, target_periods=target_periods)
            except (SessionExpired, BrowserUnavailable) as exc:
                must_login = True
                audit("daily_report_error", error=type(exc).__name__, recovery=True)
            except (AccessBlocked, TransientError, requests.RequestException,
                    RuntimeError, ValueError, KeyError, TypeError) as exc:
                audit("daily_report_error", error=type(exc).__name__, reason=str(exc)[:200])
            first_cycle = False
            next_opening = next_window(opening + timedelta(minutes=5))[0]
            audit("window_complete", next_opening=next_opening.isoformat())
            next_preparation = next_opening - timedelta(minutes=5)
            log(f"本轮预约完成；明天 {next_preparation:%Y-%m-%d %H:%M} 开始准备，{next_opening:%H:%M} 继续提交预约。")
            wait_until(next_preparation)
    except BaseException as exc:
        audit("run_error", error=type(exc).__name__)
        raise
    finally:
        if client is not None:
            client.http.close()
        if browser is not None:
            hold_browser(close_browser=getattr(args, "close_browser", False) is True)
            browser.close()
        audit("log_file", path=str(LOG_PATH), status="ok" if LOG_STREAM is not None else "unavailable",
              reason=LOG_REASON or None, events=len(RUN_EVENTS))
        close_logging()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已停止。")
    except (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        log("STOPPED: " + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1) from None
