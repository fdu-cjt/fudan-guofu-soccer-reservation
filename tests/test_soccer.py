"""Offline checks for the new booking.fudan.edu.cn implementation."""
import contextlib
import copy
import io
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import soccer as r

REAL_LOG = r.log          # 测试里多数场景会 patch 掉 log()，日志专项测试需要真的那个


def test_git_version():
    for status, dirty in [('## main...origin/main\n', False),
                          ('## concurrent-test\n M automations/soccer.py\n', True)]:
        with patch('fudan_spider.utils.subprocess.check_output', side_effect=['abc123\n', status]):
            assert r.git_version() == {'branch': status.splitlines()[0][3:].split('...')[0],
                                       'commit': 'abc123', 'dirty': dirty}
    with patch('fudan_spider.utils.subprocess.check_output', side_effect=FileNotFoundError):
        assert r.git_version() == {'branch': 'unknown', 'commit': 'unknown', 'dirty': None}


def test_credentials(tmp):
    """凭据优先级：.env 文件 < 环境变量；缺失时报错而不是静默用兜底值。"""
    original = r.USER, r.PASSWORD, r.PHONE
    env_file = Path(tmp) / 'credentials.env'
    env_file.write_text('# 注释\nFUDAN_USER=from-file\nFUDAN_PASSWORD=f\nFUDAN_PHONE=f\n', encoding='utf-8')
    try:
        with patch.object(r, 'CREDENTIALS', env_file):
            r.USER, r.PASSWORD, r.PHONE = 'builtin', 'builtin', 'builtin'
            r.load_credentials()
            assert (r.USER, r.PASSWORD, r.PHONE) == ('from-file', 'f', 'f'), (r.USER, r.PASSWORD, r.PHONE)
            with patch.dict(os.environ, {'FUDAN_USER': 'from-env', 'FUDAN_PASSWORD': 'p', 'FUDAN_PHONE': '1'}):
                r.load_credentials()
                assert r.USER == 'from-env', r.USER
            assert set(r.SECRETS) == {r.PASSWORD, r.USER, r.PHONE}
            with patch.object(r, 'CREDENTIALS', Path(tmp) / 'missing.env'):
                r.USER, r.PASSWORD, r.PHONE = '', '', ''
                try:
                    r.load_credentials()
                    assert False, '缺少凭据时必须报错'
                except RuntimeError:
                    pass
    finally:
        r.USER, r.PASSWORD, r.PHONE = original
        r.load_credentials.__globals__['SECRETS'] = (r.PASSWORD, r.USER, r.PHONE)


def test_logging(tmp):
    """日志必须真的落盘：与终端同一份内容、凭据被脱敏、收尾关闭句柄、写不了时降级不崩。"""
    target = Path(tmp) / 'nested' / 'soccer.log'
    captured = io.StringIO()
    path, reason = r.setup_logging(target)
    assert reason == '' and path == target, (path, reason)
    try:
        assert target.parent.is_dir(), '父目录应被自动创建'
        with contextlib.redirect_stdout(captured):
            r.log('日志自检 hello-log')
            with patch.object(r, 'SECRETS', ('p456', 'u123', '')):
                r.log('泄露检查 u123/p456')
        text = target.read_text(encoding='utf-8')
        written = [line.split(' ', 1)[1] for line in text.splitlines()]
        printed = [line.split(' ', 1)[1] for line in captured.getvalue().splitlines() if line.strip()]
        assert written == printed, (written, printed)
        assert written[0].endswith('日志自检 hello-log'), written
        assert 'u123' not in text and 'p456' not in text, text
        assert written[1].endswith('[redacted]/[redacted]'), written
    finally:
        r.close_logging()
    assert r.LOG_STREAM is None, '收尾必须关闭日志句柄'
    # 日志文件指向一个不可能存在的目录（父路径是文件）时，必须降级成「只打终端」而不是崩。
    (Path(tmp) / 'plain.txt').write_text('x', encoding='utf-8')
    try:
        path, reason = r.setup_logging(Path(tmp) / 'plain.txt' / 'soccer.log')
        assert reason and r.LOG_STREAM is None, (path, reason)
        fallback = io.StringIO()
        with contextlib.redirect_stdout(fallback):
            r.log('降级后仍要能打印')
        assert '降级后仍要能打印' in fallback.getvalue(), fallback.getvalue()
    finally:
        r.close_logging()


def test_cas_login(tmp):
    client = r.Client(Path(tmp) / 'login-test.json')
    try:
        with patch.object(client.http, 'get', return_value=Mock(status_code=412)), patch.object(client.http, 'post') as post:
            try:
                client.cas_login()
                assert False, '入口 HTTP 错误必须明确报告'
            except RuntimeError as exc:
                assert 'HTTP 412' in str(exc)
                assert '跳转结构变化' not in str(exc)
            post.assert_not_called()
        entry = Mock(status_code=200, url=r.IDP + '/#/login?lck=test&entityId=test')
        methods = {'code': 200, 'entityId': 'test', 'requestType': 'test', 'lck': 'test',
                   'data': [{'moduleCode': 'userAndPwd', 'authChainCode': 'test'}]}
        responses = [Mock(json=Mock(return_value=data)) for data in (
            methods, {'result': False}, {'code': 200, 'data': 'dGVzdA=='},
            {'code': 200, 'loginToken': 'test'})]
        responses.append(Mock(url=r.IDP + '/authCenter/authnEngine', text='<form action="https://booking.fudan.edu.cn/callback" method="post"><input name="ticket" value="test"></form>'))
        key = Mock()
        key.encrypt.return_value = b'encrypted'
        with patch.object(client.http, 'get', return_value=entry), \
             patch.object(client.http, 'post', side_effect=responses) as post, \
             patch('fudan_spider.apis.soccer_api.load_der_public_key', return_value=key), \
             patch.object(client, 'finish_login') as finish:
            client.cas_login()
            assert post.call_args_list[3].kwargs['json']['authPara']['password'] == 'ZW5jcnlwdGVk'
            finish.assert_called_once()
    finally:
        client.http.close()


def test_request_policy(tmp):
    """防护响应不触发登录循环，429/5xx 暴露可用的退避信息。"""
    client = r.Client(Path(tmp) / 'request-policy.json')
    try:
        def response(status, content_type='application/json', headers=None):
            item = Mock(status_code=status, headers={'content-type': content_type, **(headers or {})}, content=b'')
            return item
        for status in (403, 412):
            with patch.object(client.http, 'request', return_value=response(status)):
                try:
                    client.request('/site/test')
                    assert False, status
                except r.AccessBlocked:
                    pass
        with patch.object(client.http, 'request', return_value=response(429, headers={'Retry-After': '3'})):
            try:
                client.request('/site/test')
                assert False, '429 must be transient'
            except r.TransientError as exc:
                assert exc.status_code == 429 and exc.retry_after == 3
        with patch.object(client.http, 'request', return_value=response(503)):
            try:
                client.request('/site/test')
                assert False, '5xx must be transient'
            except r.TransientError as exc:
                assert exc.status_code == 503
        with patch.object(client.http, 'request', return_value=response(200, 'text/html')):
            try:
                client.request('/site/test')
                assert False, 'HTML protection response must stop automation'
            except r.AccessBlocked:
                pass
    finally:
        client.http.close()


def test_parallel_dispatch():
    """同一账号的两个时段至少间隔提交，但不被慢请求串行阻塞。"""
    day = date(2026, 9, 17)
    calendar = json.loads((Path(__file__).parent / 'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    client, verifier = Mock(), Mock()
    client.mine.return_value = verifier.mine.return_value = []
    workers, calls, ends = [], [], []
    guard = threading.Lock()
    opening = r.now() + r.timedelta(seconds=.03)
    deadline = opening + r.timedelta(seconds=1.05)
    def submit(payload):
        periods = json.loads(payload['data'])[0]['period']
        assert len(periods) == 1
        with guard:
            calls.append((periods[0]['period_id'], time.monotonic(), r.now()))
        time.sleep(.35)
        with guard:
            ends.append(time.monotonic())
        return {'e': 'ERROR', 'm': '系统繁忙，请稍后再试'}
    def fork():
        worker = Mock()
        worker.submit.side_effect = submit
        workers.append(worker)
        return worker
    client.fork.side_effect = fork
    with patch.object(r, 'log'), patch.object(r, 'SUBMIT_INTERVAL_SECONDS', .2), patch.object(r, 'FIRST_PAIR_GAP_SECONDS', .2), \
         patch.object(r, 'VERIFY_INTERVAL_SECONDS', .01):
        r.poll(client, day, deadline, calendar=calendar, own=[], anti_bot=0,
               opening=opening, verifier_factory=lambda c: verifier)
    assert len(calls) >= 2, calls
    assert {p for p, _, _ in calls[:2]} == {3892, 3893}
    assert .18 <= calls[1][1] - calls[0][1] < .3, calls
    assert calls[1][1] < ends[0], '第二个时段不应等待第一个慢请求结束'
    assert all(opening <= t < deadline for _, _, t in calls), calls
    assert all(w.http.close.called for w in workers) and verifier.http.close.called
    assert verifier.mine.call_count == 1, verifier.mine.call_count
    print('TIMING soccer POST starts (ms):', [(p, round((t-calls[0][1])*1000)) for p,t,_ in calls])


def test_recovery_and_ok_wakeup():
    calendar = json.loads((Path(__file__).parent / 'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    day = date(2026, 9, 17)
    def record(period):
        return {'id': period, 'group_id': 1029, 'status': 3,
                'appointment_time': {'groups': [{'dates': [{'date': str(day),
                    'periods': [{'period_id': period}]}]}]}}
    for mode in ('ok', 'recover', 'deadline'):
        client, verifier = Mock(), Mock()
        records, sent, accepted, observed, recovery_times = [], [], {}, {}, []
        restored = threading.Event()
        recovery_query_started = threading.Event()
        recovery_query, resumed_sends = [], []
        def mine():
            for item in records:
                observed.setdefault(item['id'], time.monotonic())
            return copy.deepcopy(records)
        def verify_mine():
            if mode == 'recover' and restored.is_set() and not recovery_query:
                recovery_query.append(time.monotonic())
                recovery_query_started.set()
                time.sleep(.35)  # A slow query must not hold up resumed submissions.
                recovery_query.append(time.monotonic())
            return mine()
        client.mine.side_effect = mine
        verifier.mine.side_effect = verify_mine
        def submit(payload):
            target = json.loads(payload['data'])[0]['period'][0]['period_id']
            sent.append((target, restored.is_set()))
            if restored.is_set():
                if mode == 'recover':
                    assert recovery_query_started.wait(.5), 'Recovery query did not start'
                resumed_sends.append(time.monotonic())
            if mode != 'ok' and not restored.is_set():
                raise r.BrowserUnavailable('browser disconnected')
            records.append(record(target))
            accepted[target] = time.monotonic()
            return {'e': 'OK'}
        client.fork.side_effect = lambda: Mock(submit=Mock(side_effect=submit))
        def recover(deadline):
            recovery_times.append(r.now())
            if mode == 'deadline' or len(recovery_times) <= 2:
                raise r.BrowserUnavailable('still offline')
            restored.set()
            records.append(record(3892))  # Lost response, but the first booking actually succeeded.
        client.recover.side_effect = recover
        opening = r.now() + r.timedelta(seconds=.03)
        deadline = opening + r.timedelta(seconds=.35 if mode == 'deadline' else 1.5)
        with patch.object(r, 'log'), patch.object(r, 'SUBMIT_INTERVAL_SECONDS', .04), \
                patch.object(r, 'VERIFY_INTERVAL_SECONDS', 5), \
                patch.object(r, 'RECOVERY_RETRY_SECONDS', .01):
            result = r.poll(client, day, deadline, opening=opening, anti_bot=0,
                            calendar=calendar, own=[], verifier_factory=lambda c: verifier)
        if mode == 'deadline':
            assert not result and len(recovery_times) == r.MAX_RECOVERY_ATTEMPTS
            assert all(t < deadline for t in recovery_times)
        else:
            assert set(result) == {3892, 3893}
            if mode == 'recover':
                assert len(recovery_times) == 3, recovery_times
                client.mine.assert_called_once()  # Final reconciliation only, never a recovery barrier.
                assert len(recovery_query) == 2, recovery_query
                assert any(recovery_query[0] <= t < recovery_query[1] for t in resumed_sends), \
                    'Submissions must continue while recovery verification is in flight'
            else:
                assert all(0 <= observed[p]-accepted[p] < .2 for p in accepted), (observed, accepted)


def test_second_instance_keeps_log():
    with TemporaryDirectory() as directory:
        target = Path(directory) / 'running.log'
        target.write_text('KEEP_EXISTING_LOG', encoding='utf-8')
        with patch('sys.argv', ['soccer.py', '--check', '--log-file', str(target)]), \
                patch.object(r, 'single_instance', side_effect=RuntimeError('already running')):
            try:
                r.main()
                assert False
            except RuntimeError:
                pass
        assert target.read_text(encoding='utf-8') == 'KEEP_EXISTING_LOG'


def test_browser_hold():
    with patch('builtins.input', return_value='') as prompt, patch.object(r, 'log'):
        r.hold_browser()
        prompt.assert_called_once()
    with patch('builtins.input') as prompt:
        r.hold_browser(close_browser=True)
        prompt.assert_not_called()


def test_early_preparation(mine_error=None):
    clock = [datetime(2026, 9, 17, 6, 54, tzinfo=r.SHANGHAI)]
    waits = []
    client = Mock(user='test')
    client.settings.return_value = {'config': {'anti_bot': 0}}
    client.data.return_value = {'number': 'test'}
    if mine_error:
        client.mine.side_effect = mine_error
    def wait(moment):
        waits.append(moment.strftime('%H:%M'))
        clock[0] = moment
    with TemporaryDirectory() as directory, patch.object(r, 'load_credentials'), patch.object(r, 'log'), \
            patch.object(r, 'now', side_effect=lambda: clock[0]), patch.object(r, 'wait_until', side_effect=wait), \
            patch.object(r, 'Client', return_value=client), patch.object(r, 'poll') as poll, \
            patch.object(r, 'book_available', return_value=False) as startup:
        args = Mock(check=False, dry_run=False, transport='http', watch_seconds=0,
                    log_file=Path(directory)/'prepare.log')
        r.run(args, continuous=False)
        assert waits == ['06:55', '06:57'], waits
        client.login.assert_called_once()
        client.recover.assert_not_called()
        assert client.data.call_count == 2
        startup.assert_called_once()
        assert poll.call_args.kwargs['opening'].strftime('%H:%M') == '07:00'
        assert poll.call_args.kwargs['opening'] == datetime(2026,9,17,7,0,0,100000,tzinfo=r.SHANGHAI)
        if mine_error:
            assert poll.call_args.kwargs['own'] is None


def test_notice_confirm_without_relogin():
    client = Mock(user='test')
    client.browser.info = {'ready': True}
    client.browser.call.return_value = {'confirmed': True, 'characters': 42}
    client.data.return_value = {'number': 'test'}
    client.settings.side_effect = [r.NoticeUnread('请先阅读预约须知'),
                                   {'config': {'anti_bot': 0}}]
    with TemporaryDirectory() as directory, patch.object(r, 'load_credentials'), \
            patch.object(r, 'log'), patch.object(r, 'Client', return_value=client), \
            patch.object(r, 'BrowserBridge', return_value=client.browser), patch.object(r, 'poll'):
        args = Mock(check=True, dry_run=False, transport='browser', watch_seconds=0,
                    close_browser=True, log_file=Path(directory) / 'notice.log')
        r.run(args)
    client.login.assert_called_once()
    client.recover.assert_not_called()
    assert client.data.call_count == 1
    assert client.settings.call_count == 2
    client.browser.call.assert_called_once()
    assert client.browser.call.call_args.args == ('confirmNotice',)


def test_startup_availability():
    calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    current = datetime(2026, 9, 16, 18, 30, tzinfo=r.SHANGHAI)
    cells = calendar['data']['2026-09-17']
    calendar['data'] = {f'2026-09-{day}': copy.deepcopy(cells) for day in (15, 16, 17, 18, 19)}
    for fields in calendar['data'].values():
        for periods in fields.values():
            for cell in periods.values():
                cell.update(status=0, num='1')
    own = [{'id': 1, 'group_id': 1029, 'status': 3,
            'appointment_time': {'groups': [{'dates': [{'date': '2026-09-17',
                'periods': [{'period_id': 3892}]}]}]}}]
    calendar['data']['2026-09-18']['1030']['3893']['status'] = 3
    calendar['data']['2026-09-18']['1031']['3893']['num'] = '0'
    expected = {date(2026,9,16): {3893}, date(2026,9,17): {3893}, date(2026,9,18): {3892}}
    assert r.available_slots(calendar, own, current) == expected
    assert r.available_slots(calendar, own, current, slot_numbers={1}) == {
        date(2026, 9, 18): {3892}
    }
    client = Mock()
    client.calendar.return_value = calendar
    client.mine.return_value = own
    cutoff = current.replace(day=17, hour=6, minute=55)
    with patch.object(r, 'now', return_value=current), patch.object(r, 'log') as log, \
            patch.object(r, 'poll', side_effect=[{}, {3893: '已预约'}, {}]) as poll:
        assert r.book_available(client, cutoff, 0)
        assert [c.args[1] for c in poll.call_args_list] == list(expected)
        assert [c.kwargs['target_periods'] for c in poll.call_args_list] == list(expected.values())
        assert all(current < c.args[2] <= current+r.timedelta(minutes=5) for c in poll.call_args_list)
        assert client.calendar.call_args_list[0].args == (current.date(), date(2026,9,18))
        messages = [call.args[0] for call in log.call_args_list]
        assert '2026-09-16 至 2026-09-18' in messages[0], messages
        assert any('2026-09-16，时段 19:00-20:00 有候选空位' in text for text in messages), messages
    # Recheck closes the race between the initial scan and attempting a later date.
    full = copy.deepcopy(calendar)
    for fields in full['data'].values():
        for periods in fields.values():
            for cell in periods.values():
                cell['num'] = '0'
    client.calendar.side_effect = [full]
    with patch.object(r, 'now', return_value=current), patch.object(r, 'log') as log, \
            patch.object(r, 'poll') as poll:
        assert not r.book_available(client, cutoff, 0, slot_numbers={1})
        poll.assert_not_called()
        messages = [call.args[0] for call in log.call_args_list]
        assert '时段 18:00-19:00。' in messages[0] and '19:00-20:00' not in messages[0], messages
        assert any('未发现可立即尝试的空位' in text for text in messages), messages
    client.calendar.side_effect = [calendar, full, full, full]
    with patch.object(r, 'now', return_value=current), patch.object(r, 'poll') as poll:
        assert not r.book_available(client, cutoff, 0)
        poll.assert_not_called()
    # Narrowed targets use the real dispatcher: no POST for a full/expired/owned period.
    worker, verifier = Mock(), Mock()
    worker.submit.return_value = {'e': 'BUSY'}
    client.fork.return_value = worker
    client.mine.return_value = verifier.mine.return_value = []
    with patch.object(r, 'log'):
        r.poll(client, date(2026,9,17), r.now()+r.timedelta(seconds=.6), calendar=calendar, own=[],
               target_periods={3893}, verifier_factory=lambda c: verifier)
    assert worker.submit.called
    assert all(json.loads(c.args[0]['data'])[0]['period'][0]['period_id'] == 3893
               for c in worker.submit.call_args_list)
    # A successful startup pass still reaches the official opening window.
    client = Mock(user='test')
    client.settings.return_value = {'config': {'anti_bot': 0}}
    client.data.return_value = {'number': 'test'}
    clock = [current]
    def advance(moment):
        clock[0] = moment
    with TemporaryDirectory() as directory, patch.object(r, 'load_credentials'), patch.object(r, 'log') as log, \
            patch.object(r, 'now', side_effect=lambda: clock[0]), patch.object(r, 'wait_until', side_effect=advance) as wait, \
            patch.object(r, 'Client', return_value=client), patch.object(r, 'poll') as poll, \
            patch.object(r, 'book_available', side_effect=[r.SessionExpired('expired'), True]) as startup, \
            patch.object(r.time, 'sleep'):
        args = Mock(check=False, dry_run=False, transport='http', watch_seconds=0,
                    log_file=Path(directory)/'startup.log')
        r.run(args, continuous=False)
        assert startup.call_count == 2  # A failed scan is retried, never treated as an empty calendar.
        client.recover.assert_called_once()
        assert [call.args[0] for call in wait.call_args_list] == [
            current.replace(day=17, hour=6, minute=55),
            current.replace(day=17, hour=6, minute=57),
        ]
        poll.assert_called_once()
        assert any('下一次准备时间：2026-09-17 06:55' in call.args[0] for call in log.call_args_list)
        client.http.close.assert_called_once()


def test_continuous_windows(report_error=None):
    """Normal runs keep the session and enter the next day's release window."""
    clock = [datetime(2026, 9, 17, 6, 54, tzinfo=r.SHANGHAI)]
    waits, poll_days, report_times = [], [], []
    calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    client = Mock(user='test')
    client.settings.return_value = {'config': {'anti_bot': 0}}
    client.data.return_value = {'number': 'test'}
    client.calendar.return_value = calendar
    client.mine.return_value = []
    opening = clock[0].replace(hour=7, minute=0, second=0, microsecond=0)
    def wait(moment):
        waits.append(moment)
        clock[0] = moment
    def poll(*args, **kwargs):
        poll_days.append(args[1])
        if len(poll_days) == 1:
            clock[0] = opening + timedelta(minutes=2)
            return {}
        raise KeyboardInterrupt
    original_report = r.report_daily_bookings
    def report(*args, **kwargs):
        report_times.append(clock[0])
        if report_error:
            raise report_error
        return original_report(*args, **kwargs)
    with TemporaryDirectory() as directory, patch.object(r, 'load_credentials'), patch.object(r, 'log'), \
            patch.object(r, 'now', side_effect=lambda: clock[0]), patch.object(r, 'wait_until', side_effect=wait), \
            patch.object(r, 'Client', return_value=client), patch.object(r, 'book_available', return_value=True), \
            patch.object(r, 'poll', side_effect=poll), \
            patch.object(r, 'report_daily_bookings', side_effect=report) as daily_report:
        args = Mock(check=False, dry_run=False, transport='http', watch_seconds=0,
                    log_file=Path(directory)/'continuous.log')
        try:
            r.run(args)
            assert False, 'test must stop the continuous worker after the second window'
        except KeyboardInterrupt:
            pass
    assert poll_days == [date(2026, 9, 19), date(2026, 9, 20)], poll_days
    assert waits == [
        datetime(2026, 9, 17, 6, 55, tzinfo=r.SHANGHAI),
        datetime(2026, 9, 17, 6, 57, tzinfo=r.SHANGHAI),
        datetime(2026, 9, 17, 7, 5, tzinfo=r.SHANGHAI),
        datetime(2026, 9, 18, 6, 55, tzinfo=r.SHANGHAI),
        datetime(2026, 9, 18, 6, 57, tzinfo=r.SHANGHAI),
    ], waits
    assert report_times == [opening + timedelta(minutes=5)], report_times
    daily_report.assert_called_once_with(client, date(2026, 9, 19), calendar,
                                         target_periods={3892, 3893})
    if isinstance(report_error, (r.SessionExpired, r.BrowserUnavailable)):
        client.recover.assert_called_once()
    else:
        client.recover.assert_not_called()
    client.submit.assert_not_called()
    client.http.close.assert_called_once()


def test_daily_report():
    calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    day = date(2026, 9, 17)
    record = {'id': 3892, 'group_id': 1029, 'status': 3, 'status_name': '已预约',
              'appointment_time': {'groups': [{'dates': [{'date': str(day),
                  'periods': [{'period_id': 3892}]}]}]}}
    client = Mock()
    client.mine.return_value = [record]
    with patch.object(r, 'log') as log, patch.object(r, 'audit') as audit:
        report = r.report_daily_bookings(client, day, calendar, target_periods={3892, 3893})
        assert report == {'18:00-19:00': '已预约', '19:00-20:00': '未查到预约'}, report
        assert '07:05预约汇报' in log.call_args.args[0]
        audit.assert_called_once_with('daily_report', day=str(day), periods=report)
        assert r.report_daily_bookings(client, day, calendar, target_periods={3893}) == {
            '19:00-20:00': '未查到预约'}
        client.mine.return_value = [{'id':42, 'group_id':1029, 'status':3}]
        assert r.report_daily_bookings(client, day, calendar, target_periods={3892}) == {
            '18:00-19:00': '无法核实：预约记录格式不识别'}
        client.mine.side_effect = ValueError('分页结构变化')
        assert r.report_daily_bookings(client, day, calendar, target_periods={3892}) == {
            '18:00-19:00': '无法核实：预约列表读取失败'}
    client.submit.assert_not_called()


def test_booking_list_rate_limit_cooldown():
    client = Mock()
    client.mine.side_effect = [r.TransientError('HTTP 429', 429), []]
    with patch.object(r.time, 'monotonic', return_value=100), patch.object(r, 'audit'):
        assert r.read_bookings(client) is None
    with patch.object(r.time, 'monotonic', return_value=120):
        assert r.read_bookings(client) is None
    assert client.mine.call_count == 1
    with patch.object(r.time, 'monotonic', return_value=161):
        assert r.read_bookings(client) == []
    assert client.mine.call_count == 2


def check():
    assert r.MAX_SUBMISSION_ATTEMPTS == 10
    test_git_version()
    day = date(2026,9,17)
    begin = datetime(2026,9,15,7,tzinfo=r.SHANGHAI)
    assert r.next_window(begin)[0] == begin
    assert r.next_window(begin+r.timedelta(minutes=5))[0] == begin+r.timedelta(days=1)
    assert r.target_day(begin.date()) == date(2026,9,17), '--check 与正式运行必须共用同一口径'
    calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
    for cells in calendar['data'][str(day)].values():
        for cell in cells.values():
            cell.update(status=0,num='1')
    assert r.selected_period_ids(calendar, {1}) == {3892}
    assert r.selected_period_ids(calendar, {2}) == {3893}
    def record(period, rid=1030):
        return {'id':period,'group_id':1029,'resource_id':rid,'status':3,'status_name':'已预约',
                'appointment_time':{'groups':[{'dates':[{'date':str(day),
                    'periods':[{'period_id':period}]}]}]}}
    payload=r.make_payload(day,calendar['resource'],calendar['time'][0])
    data=json.loads(payload['data'])
    assert data == [{'group_id':1029,'resource_ids':[1030,1031],
                     'period':[{'date':str(day),'period_id':3892}],'number':1}]
    assert payload['collective']==0 and json.loads(payload['data_colle'])[0]['name']=='手机号'
    assert payload['captcha']=='{}'
    with_captcha = r.make_payload(day, calendar['resource'], calendar['time'][0], {'token':'t1','pointJson':'p1'})
    assert json.loads(with_captcha['captcha']) == {'token':'t1','pointJson':'p1'}
    active = copy.deepcopy(record(3892))
    active['is_cancel'] = 1
    active['appointment_time']['groups'][0]['dates'][0]['periods'][0]['can_cancel'] = 1
    assert r.own_slot([active],str(day),3892) is active
    assert r.own_slot([dict(record(3892),status=0)],str(day),3892) is None
    assert r.own_slot([dict(record(3892),group_id=999)],str(day),3892) is None
    assert r.own_slot([active],str(day),3893) is None
    legacy = {'id':43, 'group_id':1029, 'status':3, 'detail':{str(day):[{'period_id':3892}]}}
    unknown = []
    assert r.own_slot([legacy],str(day),3892,unknown=unknown) is None
    assert unknown == [43]
    unknown = []
    assert r.own_slot([{'id':42,'group_id':1029,'status':3}],str(day),3892,unknown=unknown) is None
    assert unknown == [42]
    assert r._submission_kind({'e': 'ERROR', 'm': '点击太频繁了'})[0] == 'retry'

    test_parallel_dispatch()
    test_recovery_and_ok_wakeup()
    test_second_instance_keeps_log()
    test_browser_hold()
    test_early_preparation()
    test_early_preparation(ValueError('预约列表分页结构变化'))
    test_early_preparation(r.TransientError('HTTP 429', 429))
    test_notice_confirm_without_relogin()
    test_startup_availability()
    test_continuous_windows()
    test_continuous_windows(r.SessionExpired('expired during daily report'))
    test_continuous_windows(r.TransientError('HTTP 503'))
    test_daily_report()
    test_booking_list_rate_limit_cooldown()

    with TemporaryDirectory() as tmp, patch.object(r, 'LOCAL', Path(tmp)), \
            patch.object(r, 'DEFAULT_LOG_PATH', Path(tmp) / 'default.log'), patch.object(r, 'log'):
        test_credentials(tmp)
        test_cas_login(tmp)
        test_request_policy(tmp)
        # 日志测试要用真的 log()，写在临时目录里，别碰工作区的 .local。
        r.setup_logging(Path(tmp) / 'default.log')
        with patch.object(r, 'log', REAL_LOG):
            try:
                test_logging(tmp)
            finally:
                r.close_logging()
        c = r.Client(Path(tmp)/'session.json')
        prepared, settings = c.prepare_submission(payload)
        assert prepared.method == 'POST' and prepared.url == r.API+'/site/resource/launch'
        assert 'multipart/form-data' in prepared.headers['Content-Type']
        assert payload['data'].encode() in prepared.body
        c.http.close()
        try:
            r.make_payload(day, calendar['resource'], calendar['time'])
            assert False, '每笔预约不能包含多个时段'
        except ValueError as exc:
            assert '一个时段' in str(exc)
        class FakeSolver:
            def __init__(self):
                self.count = 0
            def solve(self):
                self.count += 1
                return {'token': f't{self.count}', 'pointJson': 'p'}, 100, 0.3
        def run(kind, anti_bot=1, verifier_error=None, persistent_error=False):
            """跑一轮 poll，返回 (提交内容列表, 确认结果)；同时断言核实会话一定被关闭。"""
            fake, verifier = Mock(), Mock()
            solver = FakeSolver()
            own, calls, workers = [], [], []
            call_lock = threading.Lock()
            closed = threading.Event()
            verifier.http = Mock()
            verifier.http.close.side_effect = closed.set
            raised = threading.Event()
            opening = r.now() + r.timedelta(seconds=.03)
            def mine():
                if verifier_error and (persistent_error or not raised.is_set()):
                    raised.set()      # 只报一次，之后恢复正常，用来验证「不停止」
                    raise verifier_error
                return copy.deepcopy(own)
            verifier.mine.side_effect = mine
            fake.mine.side_effect = lambda: copy.deepcopy(own)
            def submit(payload, prepared=None):
                assert r.now() >= opening, '无论是否需要验证码，都必须等待开抢时间'
                periods = json.loads(payload['data'])[0]['period']
                assert len(periods) == 1, 'SinglePeriodNumRule=1，每笔只能提交一个时段'
                with call_lock:
                    calls.append(payload)
                    attempt = len(calls)
                if kind == 'timeout' and attempt == 1:
                    raise requests.Timeout()
                if kind == 'server_error':
                    raise r.TransientError('HTTP 503')
                if kind in ('captcha', 'captcha_forever') and (kind == 'captcha_forever' or attempt == 1):
                    return {'e': 'VERIFY', 'm': '需要验证码'}
                if kind == 'busy':
                    return {'e': 'ERROR', 'm': '系统繁忙，请稍后再试'}
                if kind == 'partial' and periods[0]['period_id'] == 3893:
                    target_calls = sum(json.loads(p['data'])[0]['period'][0]['period_id'] == 3893 for p in calls)
                    if target_calls <= 2:
                        return {'e': 'ERROR', 'm': '系统繁忙，请稍后再试'}
                if kind == 'unknown':
                    return {'e': 'ERROR', 'm': '业务规则已变更'}
                if kind == 'quota':
                    return {'e': 'LIMIT', 'm': '超过预约次数'}
                if kind == 'http429':
                    raise RuntimeError('HTTP 429')
                if kind == 'expired':
                    raise r.SessionExpired('expired')
                if kind == 'malformed':
                    return {}
                if kind != 'unconfirmed':
                    own.append(record(periods[0]['period_id']))
                    # Let independent verifier stop submissions while POST remains pending.
                    if kind == 'concurrent':
                        time.sleep(.03)
                        raise requests.Timeout()
                return {'e': 'OK'}
            def fork():
                worker = Mock()
                worker.submit.side_effect = submit
                workers.append(worker)
                return worker
            fake.fork.side_effect = fork
            # Keep compressed functional tests above Windows' scheduler granularity.
            with patch.object(r, 'SUBMIT_INTERVAL_SECONDS', .04), patch.object(r, 'RETRY_DELAY_SECONDS', .04), patch.object(r, 'VERIFY_INTERVAL_SECONDS', .005), \
                 patch.object(r, 'slide_solver', lambda client: solver):
                result = r.poll(fake, day, opening+r.timedelta(seconds=.7), calendar=calendar, own=[], anti_bot=anti_bot,
                                opening=opening,
                                verifier_factory=lambda client: verifier)
            assert closed.is_set(), 'poll 必须关闭 fork 出来的核实会话'
            assert all(w.http.close.called for w in workers)
            assert not list(Path(tmp).glob('soccer_pending*'))
            tokens = [json.loads(payload['captcha']) for payload in calls]
            if anti_bot == 1:
                assert len({t['token'] for t in tokens if t}) == len([t for t in tokens if t]), '一次性验证码不能被复用'
            elif kind == 'captcha':
                assert tokens[0] == {} and any(t.get('token') for t in tokens), tokens
                assert solver.count >= sum(bool(t) for t in tokens), tokens
            else:
                assert all(t == {} for t in tokens) and solver.count == 0, 'anti_bot=0 时不该求解验证码'
            return calls, result
        for kind in ('normal', 'timeout', 'concurrent', 'captcha', 'quota', 'unconfirmed'):
            calls, result = run(kind)
            assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS, (kind, len(calls))
            expected = 0 if kind in ('quota', 'unconfirmed') else 2
            assert len(result) == expected, (kind, len(result))
        calls, result = run('captcha_forever')
        assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS and len(result) == 0, (len(calls), len(result))
        # anti_bot=0：不带验证码也不求解；若服务端中途要验证码，自动切换成携带滑块重试。
        calls, result = run('normal', anti_bot=0)
        assert len(calls) == 2 and len(result) == 2, (len(calls), len(result))
        assert sorted(json.loads(p['data'])[0]['period'][0]['period_id'] for p in calls) == [3892, 3893]
        calls, result = run('captcha', anti_bot=0)
        assert len(calls) == 3 and len(result) == 2, (len(calls), len(result))
        assert any(json.loads(p['captcha']).get('token') == 't1' for p in calls)
        calls, result = run('busy', anti_bot=0)
        assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS and not result
        calls, result = run('partial', anti_bot=0)
        assert len(result) == 2
        submitted = [json.loads(p['data'])[0]['period'][0]['period_id'] for p in calls]
        assert submitted.count(3892) == 1 and submitted.count(3893) == 3, submitted
        calls, result = run('unknown', anti_bot=0)
        assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS and not result
        r.RUN_EVENTS.clear()
        calls, result = run('unconfirmed', anti_bot=0,
                            verifier_error=ValueError('预约列表结构变化'), persistent_error=True)
        assert len(calls) == 2 and not result, '提交 OK 后列表读不懂，不应重复提交'
        assert 'booking_list_unavailable' in {e['event'] for e in r.RUN_EVENTS}
        assert 'booking_format_unknown' not in {e['event'] for e in r.RUN_EVENTS}
        for kind in ('http429', 'expired', 'malformed'):
            calls, result = run(kind, anti_bot=0)
            assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS and not result, (kind, len(calls))
        # 提交侧临时故障（5xx）：只退避重试，不能判定本轮失败。
        calls, result = run('server_error')
        assert 1 <= len(calls) <= r.MAX_SUBMISSION_ATTEMPTS and len(result) == 0, (len(calls), len(result))
        # 核实通道遇到 5xx/连接错误：只读列表不可用也不能阻止继续预约。
        for transient in (r.TransientError('HTTP 503'), requests.ConnectionError('boom')):
            r.RUN_EVENTS.clear()
            calls, result = run('normal', verifier_error=transient)
            events = {e['event'] for e in r.RUN_EVENTS}
            assert len(result) == 2, (transient, len(result))
            assert 'verification_stopped' not in events, (transient, events)
            assert 'booking_list_unavailable' in events, (transient, events)
        # 核实异常不再停止提交；恢复查询后仍能确认成功。
        r.RUN_EVENTS.clear()
        calls, result = run('normal', verifier_error=RuntimeError('结构变化'))
        assert len(result) == 2
        assert 'booking_list_unavailable' in {e['event'] for e in r.RUN_EVENTS}
        assert 'verification_stopped' not in {e['event'] for e in r.RUN_EVENTS}
        fake=Mock()
        r.poll(fake, day, r.now()-r.timedelta(seconds=1), calendar=calendar, own=[])
        fake.submit.assert_not_called()
        # 不再有每轮 JSON 报告：结构化事件只进 .local/soccer.log 和 RUN_EVENTS。
        assert not list(Path(tmp).glob('soccer-run-*.json')), '不该再写每轮 JSON 报告'
        assert not list(Path(tmp).glob('soccer-run-*.tmp'))
    print('PASS: multipart, single-period dispatch, opening wait, per-target in-flight guard and bounded retries, '
          'acceptance/timeout verification, anti_bot 0/1 CAPTCHA pool, TransientError backoff, verifier close, '
          'deadline, credentials, log file, no run report, limited recovery, parallel verification after recovery, '
          '06:57 preparation, second-instance log protection, all-date startup availability, per-date recheck, '
          'subset dispatch, continuous release windows, one-shot startup attempt, startup recovery/fallback')


if __name__=='__main__':
    with patch.object(r, 'FIRST_PAIR_GAP_SECONDS', .08):
        check()
