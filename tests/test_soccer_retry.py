"""Offline checks for response-driven B opening and alternating retries."""
import json
import sys
import time
import threading
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import soccer as r


def check():
    calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text())['d']
    day = date(2026, 9, 22)
    for mode in ('early','late','slow','timeout','connection','ok','quota','full','service','single','once','retry_after'):
        client, verifier = Mock(), Mock()
        calls, ends, records, active, counts = [], {}, [], set(), {}
        guard = threading.Lock()
        def mine():
            # Deliberately slow reconciliation must not block timeout retries.
            if mode == 'timeout' and calls:
                time.sleep(.3)
            return list(records)
        client.mine.side_effect = verifier.mine.side_effect = mine
        def submit(payload):
            target = json.loads(payload['data'])[0]['period'][0]['period_id']
            with guard:
                assert target not in active
                active.add(target)
                counts[target] = counts.get(target,0)+1
                number = counts[target]
                calls.append((target,time.monotonic()))
            try:
                if mode == 'timeout':
                    time.sleep(.25)
                    raise r.requests.Timeout('lost')
                if mode == 'connection':
                    raise r.requests.ConnectionError('lost')
                if target == 3892 and number == 1:
                    time.sleep({'late':.12,'slow':.25}.get(mode,.02))
                    if mode == 'quota':
                        return {'e':'ERROR','m':'有效期内爽约次数与已预约未开始时段数量合计达到上限（3）'}
                    if mode == 'full':
                        return {'e':'ERROR','m':'已约满'}
                    if mode == 'ok':
                        records.append({'id':target,'group_id':1029,'status':3,
                                        'detail':{str(day):[{'period_id':target}]}})
                        return {'e':'OK'}
                if mode == 'retry_after':
                    raise r.TransientError('HTTP 503',503,.2)
                return {'e':'ERROR','m':'不在服务时间' if mode == 'service' else '点击太频繁了'}
            finally:
                with guard:
                    active.remove(target)
                    ends[(target,number)] = time.monotonic()
        client.fork.side_effect = lambda: Mock(submit=Mock(side_effect=submit))
        opening = r.now()+r.timedelta(seconds=.02)
        r.RUN_EVENTS.clear()
        with patch.object(r,'log'), patch.object(r,'SUBMIT_INTERVAL_SECONDS',.08), \
             patch.object(r,'FIRST_PAIR_GAP_SECONDS',.16), patch.object(r,'RETRY_DELAY_SECONDS',.08), \
             patch.object(r,'MAX_SUBMISSION_ATTEMPTS',6), patch.object(r,'VERIFY_INTERVAL_SECONDS',.02):
            result = r.poll(client,day,opening+r.timedelta(seconds=1.2),calendar=calendar,own=[],
                            opening=opening,once=mode=='once',target_periods={3893} if mode=='single' else None,
                            verifier_factory=lambda c:verifier)
        assert len(calls)<=6, (mode,calls)
        assert all(b[1]-a[1]>=.075 for a,b in zip(calls,calls[1:])),(mode,calls)
        summary = next(e for e in r.RUN_EVENTS if e['event']=='run_summary')
        if mode == 'quota':
            assert len(calls)==1 and all('合计达到上限' in summary['stopped'][str(t)] if str(t) in summary['stopped'] else '合计达到上限' in summary['stopped'][t] for t in (3892,3893)),summary
            continue
        if mode == 'single':
            assert all(t==3893 for t,_ in calls)
            continue
        assert [t for t,_ in calls[:2]] == [3892,3893],(mode,calls)
        gap=calls[1][1]-calls[0][1]
        if mode in ('late','slow','timeout','connection','retry_after'):
            assert .15<=gap<.30,(mode,gap)
        else:
            assert .09<=gap<.25,(mode,gap)
        if mode in ('early','service'):
            assert [t for t,_ in calls] == [3892,3893,3892,3893,3892,3893],calls
        if mode=='timeout':
            assert calls[2][0]==3892 and calls[2][1]-ends[(3892,1)]<.06,calls
        if mode in ('ok','full'):
            assert counts[3892]==1,calls
        if mode=='ok':
            assert 3892 in result
        if mode=='once':
            assert len(calls)==2,calls
        if mode=='retry_after':
            assert calls[2][1]-ends[(3892,1)]>=.195,calls
    bridge=Mock()
    bridge.call.return_value={'status':200,'url':r.API+'/site/resource/launch',
                             'headers':{'content-type':'application/json'},'text':'{"e":"OK"}'}
    client=r.Client(user='test',password='unused',browser=bridge)
    with patch.object(r,'log'):
        client.submit({'data':'[]'})
    assert bridge.call.call_args.kwargs['timeoutMs']==5000
    print('PASS: early/late/slow opening, B reservation before A retry, timeout parallel verification, alternating fairness, OK/full/quota, single target, once, Retry-After, service-time retry, 5s timeout')

if __name__=='__main__':
    check()
