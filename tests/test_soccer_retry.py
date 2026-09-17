"""Offline regression checks for first dispatch, retry timing and reconciliation."""
import copy
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import soccer as r


def check():
    calendar = json.loads((Path(__file__).parent / 'data/soccer-calendar.json').read_text())['d']
    day = date(2026, 9, 17)
    for mode in ('busy', 'cap', 'once', 'lost', 'recovery'):
        client, verifier = Mock(), Mock()
        calls, ends, records = [], {}, []
        guard = threading.Lock()
        def mine():
            with guard:
                if mode == 'once':
                    return copy.deepcopy([record for due, record in records if time.monotonic() >= due])
                return []
        client.mine.side_effect = verifier.mine.side_effect = mine
        def submit(payload):
            target = json.loads(payload['data'])[0]['period']
            assert len(target) == 1
            target = target[0]['period_id']
            started = time.monotonic()
            with guard:
                assert target not in ends or ends[target] <= started
                calls.append((target, started))
                if mode == 'once':
                    records.append((started + .18, {'id': target, 'group_id': 1029, 'status': 3,
                        'detail': {str(day): [{'period_id': target, 'is_cancel': 0}]}}))
            time.sleep(.06)
            ends[target] = time.monotonic()
            if mode == 'lost':
                raise r.requests.Timeout('response lost')
            if mode == 'recovery':
                raise r.BrowserUnavailable('response lost with browser')
            return {'e': 'OK'} if mode == 'once' else {
                'e': 'ERROR',
                'm': '点击太频繁了' if mode == 'busy' else '系统繁忙',
            }
        client.fork.side_effect = lambda: Mock(submit=Mock(side_effect=submit))
        opening = r.now() + r.timedelta(seconds=.04)
        window = .65
        r.RUN_EVENTS.clear()
        with patch.object(r, 'log'), patch.object(r, 'SUBMIT_INTERVAL_SECONDS', .15), \
                patch.object(r, 'RETRY_WINDOW_SECONDS', window), \
                patch.object(r, 'MAX_SUBMISSION_ATTEMPTS', 2 if mode == 'cap' else 15), \
                patch.object(r, 'VERIFY_INTERVAL_SECONDS', .04):
            result = r.poll(client, day, opening + r.timedelta(seconds=1.3),
                            calendar=calendar, own=[], opening=opening, once=mode == 'once',
                            verifier_factory=lambda c: verifier)
        assert {p for p, _ in calls} == {3892, 3893}, (mode, calls)
        # All periods share the account-level retry interval.
        assert all(.14 <= b[1]-a[1] < .45 for a, b in zip(calls, calls[1:])), calls
        for target in (3892, 3893):
            starts = [t for p, t in calls if p == target]
            if mode in ('once', 'lost', 'recovery'):
                assert len(starts) == 1, (mode, calls)
            elif mode == 'cap':
                assert len(starts) == 2, calls
            else:
                assert len(starts) >= 2, calls
        if mode == 'once':
            assert set(result) == {3892, 3893}, result
        if mode in ('lost', 'recovery'):
            assert not result
            summary = next(e for e in r.RUN_EVENTS if e['event'] == 'run_summary')
            assert summary['uncertain'] == [3892, 3893], summary
        if mode == 'busy':
            assert calls[-1][1] - calls[0][1] < window + .03, calls
            assert any(e['event'] == 'submission_exhausted' for e in r.RUN_EVENTS)
        assert verifier.http.close.called
    print('PASS: account-level submit interval, fair first dispatch, window/cap, one-shot delayed confirmation, lost-response no resubmit')


if __name__ == '__main__':
    check()
