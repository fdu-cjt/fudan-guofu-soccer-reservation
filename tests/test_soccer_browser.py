"""Run with python -B tests/test_soccer_browser.py (no school requests)."""
import json
import subprocess
import sys
import time
import contextlib
import io
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
from unittest.mock import patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automations import soccer as r
from soccer_browser import BrowserBridge, BrowserSession, BrowserUnavailable

# Only a loopback HTTP server is reachable through this fixture. It executes the
# same browserFetch function used in production, including the deadline check.
LOCAL_BROWSER = r'''
const {chromium} = require('playwright');
const {browserFetch, confirmNotice} = require('./soccer_browser.cjs');
const http = require('node:http');
const readline = require('node:readline');
let browser, page, server, origin, active=0, peak=0, posts=0;
const output = value => process.stdout.write(JSON.stringify(value)+'\n');
async function handle(c) {
  if(c.op==='start') {
    server=http.createServer((req,res)=>{
      if(req.url==='/') {res.end('<html><body>Local timing check</body></html>'); return;}
      if(req.url==='/site/readDescription' && req.method==='POST') {
        res.setHeader('content-type','application/json');res.end('{"e":"OK"}');return;
      }
      const post=req.method==='POST';
      if(post) {posts++; active++; peak=Math.max(peak,active); output({event:'progress',stage:'local_arrival',atMs:Date.now()});}
      req.resume();
      setTimeout(()=>{res.setHeader('content-type','application/json');
        res.end(JSON.stringify(post?{e:'ERROR',m:'系统繁忙'}:{e:'OK',d:{total:0,data:[]}}));
        if(post)active--;
      },req.url.endsWith('/hang')?5500:(post?350:5));
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    origin='http://127.0.0.1:'+server.address().port;
    browser=await chromium.launch({channel:'chrome',headless:false});
    page=await browser.newPage(); await page.goto(origin);
    return {ready:true};
  }
  if(c.op==='fetch') {
    if(c.evaluationDelayMs) await new Promise(resolve=>setTimeout(resolve,c.evaluationDelayMs));
    c.url=origin+new URL(c.url).pathname;
    return page.evaluate(browserFetch,c);
  }
  if(c.op==='noticeFixture') {
    await page.setContent('<div class="introduce_ref1">场地预约须知测试内容</div><button id="confirm" disabled>1s后确定</button><script>setTimeout(()=>{const b=document.querySelector("#confirm");b.disabled=false;b.textContent="确定"},150);document.querySelector("#confirm").onclick=()=>fetch("/site/readDescription",{method:"POST"})</script>');
    const started=Date.now();
    const result=await confirmNotice(page,Date.now()+5000);
    return {...result,elapsedMs:Date.now()-started};
  }
  if(c.op==='stats')return {posts,peak};
  if(c.op==='close'){await browser.close();server.close();return {closed:true};}
  throw Error('unsupported fixture operation');
}
const input=readline.createInterface({input:process.stdin});
input.on('line',line=>{const c=JSON.parse(line);handle(c).then(result=>{
  output({id:c.id,result});if(c.op==='close')input.close();
},error=>output({id:c.id,error:error.message}));});
'''


def benchmark():
    events = []
    def on_event(event):
        events.append(event)
        r.audit('browser_progress', **event)
    popen = subprocess.Popen
    def local_node(command, **kwargs):
        return popen(['node', '-e', LOCAL_BROWSER], **kwargs)
    with patch('soccer_browser.subprocess.Popen', side_effect=local_node):
        bridge = BrowserBridge(read_only=False, on_event=on_event)
        client = r.Client(user='test', password='unused', browser=bridge)
        try:
            notice = bridge.call('noticeFixture', timeout=10)
            assert notice['confirmed'] and notice['characters'] > 0
            assert notice['elapsedMs'] >= 100, notice
            # A request valid in Python but delayed past its cutoff must never reach the server.
            result = bridge.call('fetch', url=r.API+'/site/resource/launch', method='POST',
                body='test', headers={}, timeoutMs=1000, openingMs=time.time()*1000-1000,
                deadlineMs=time.time()*1000+30, evaluationDelayMs=80)
            assert result['skipped'] and bridge.call('stats')['posts'] == 0
            result = bridge.call('fetch', url=r.API+'/site/resource/launch', method='POST',
                body='test', headers={}, timeoutMs=1000, openingMs=time.time()*1000+1000,
                deadlineMs=time.time()*1000+2000)
            assert result['skipped'] and bridge.call('stats')['posts'] == 0
            started = time.monotonic()
            result = bridge.call('fetch', url=r.API+'/site/hang', method='GET',
                                 headers={}, timeoutMs=5000)
            assert result['failure'] == 'AbortError', result
            assert 4.8 <= time.monotonic()-started < 6.5, result
            calendar = json.loads((Path(__file__).parent/'data/soccer-calendar.json').read_text(encoding='utf-8'))['d']
            opening = r.now()+r.timedelta(seconds=.2)
            deadline = opening+r.timedelta(seconds=20)
            with TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), patch.object(r, 'MAX_SUBMISSION_ATTEMPTS', 6):
                r.setup_logging(Path(directory)/'timing.log')
                try:
                    r.poll(client, opening.date()+r.timedelta(days=2), deadline,
                           calendar=calendar, own=[], opening=opening, anti_bot=0)
                finally:
                    r.close_logging()
            timing = sorted([e for e in events if e.get('stage') == 'fetch_timing'
                             and e.get('method') == 'POST' and not e.get('skipped')], key=lambda e:e['sentAt'])
            arrivals = [e['atMs'] for e in events if e.get('stage') == 'local_arrival']
            assert len(timing) == 6 and len(arrivals) == len(timing), {
                'timing': len(timing), 'arrivals': len(arrivals), 'server': bridge.call('stats'),
                'errors': [e for e in r.RUN_EVENTS if e.get('event') in
                           ('submission_unknown', 'submission_retry', 'submission_skipped', 'run_summary')][-8:]}
            assert all(opening.timestamp()*1000 <= e['sentAt'] < deadline.timestamp()*1000 for e in timing)
            stats = bridge.call('stats')
            assert stats['peak'] == 1, '350ms responses should finish before the next scheduled send'
            assert 1250 <= timing[1]['sentAt']-timing[0]['sentAt'] < 2000, timing
            assert all(b['sentAt']-a['sentAt'] >= 980 for a,b in zip(timing[1:],timing[2:])), timing
            def distribution(values):
                values=sorted(values)
                return {label:round(values[round((len(values)-1)*q)],2) for label,q in [('p50',.5),('p95',.95),('max',1)]}
            report = {'seconds':20, 'requests':len(timing), 'peak_pending':stats['peak'],
                'planned_to_fetch_ms':distribution([e['dispatchDelayMs'] for e in timing]),
                'adjacent_fetch_interval_ms':distribution([b['sentAt']-a['sentAt'] for a,b in zip(timing,timing[1:])]),
                'local_server_interval_ms':distribution([b-a for a,b in zip(arrivals,arrivals[1:])]),
                'fetch_response_ms':distribution([e['elapsedMs'] for e in timing])}
            print('LOCAL_BROWSER_TIMING',json.dumps(report))
        finally:
            client.http.close()
            bridge.close()

# Real pipes and deliberately out-of-order replies exercise the bridge, not a mocked call().
SERVER = r'''
import json, sys, threading, time
lock = threading.Lock()
def reply(c):
    if c['op'] == 'fetch':
        time.sleep(.3 if 'slow' in c['url'] else .01)
        result = {'status': 400 if 'bad' in c['url'] else 200, 'url': c['url'],
                  'headers': {'content-type':'application/json'},
                  'text': json.dumps({'e':'OK','d': c})}
    else:
        result = {'ready': True}
    with lock:
        print(json.dumps({'id':c['id'],'result':result}), flush=True)
for line in sys.stdin:
    c = json.loads(line)
    if c['op'] == 'close':
        reply(c)
        break
    threading.Thread(target=reply, args=(c,)).start()
'''


MACOS_BROWSER_CHECK = r'''
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const childProcess = require('node:child_process');
const path = require('node:path');

(async () => {
Object.defineProperty(process, 'platform', {value: 'darwin'});
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const profile = '/tmp/soccer-profile';
const owner = {pid: 4321, profile, executable: chrome, port: 9222};
const spawnCalls = [];
const signals = [];
let staleChecks = 0;

childProcess.spawn = (command, args, options) => {
  spawnCalls.push({command, args, options});
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  process.nextTick(() => {
    if (command === 'ps') {
      child.stdout.emit('data', chrome + ' --user-data-dir=' + profile + ' --remote-debugging-port=9222');
      child.emit('close', 0);
    } else {
      child.emit('error', new Error('unexpected command: ' + command));
    }
  });
  return child;
};
process.kill = (pid, signal) => {
  if (signal === 0) {
    if (pid === -4321 && ++staleChecks < 3) return;
    throw Object.assign(new Error('process exited'), {code: 'ESRCH'});
  }
  signals.push([pid, signal]);
};

const browser = require('./soccer_browser.cjs');
fs.existsSync = value => value === chrome || value === path.join(profile, '.soccer-owner.json');
fs.readFileSync = () => JSON.stringify(owner);
assert.equal(browser.findChrome(), path.resolve(chrome));
await browser.killStaleForProfile(profile);
assert.equal(spawnCalls[0].command, 'ps');
assert.equal(staleChecks, 3);
assert.deepEqual(signals, [[-4321, 'SIGTERM']]);

await browser.terminateChromeChild({pid: 9876, exitCode: null});
assert.deepEqual(signals.slice(1), [[-9876, 'SIGTERM'], [-9876, 'SIGKILL']]);
console.log('PASS: macOS Chrome discovery and process cleanup');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
'''


def check_non_json_browser_output():
    bridge = BrowserBridge.__new__(BrowserBridge)
    bridge.lock = threading.Lock()
    future = Future()
    bridge.pending = {1: future}
    bridge.on_event = None
    bridge.process = SimpleNamespace(stdout=io.StringIO(
        'Chrome diagnostic on stderr redirected by a launcher\n'
        '{"id":1,"result":{"ready":true}}\n'))
    bridge._read()
    assert future.result()['ready'] is True


def check_macos_browser_lifecycle():
    subprocess.run(['node', '-e', MACOS_BROWSER_CHECK], cwd=Path(__file__).resolve().parents[1],
                   check=True, text=True)


def check():
    check_non_json_browser_output()
    check_macos_browser_lifecycle()
    popen = subprocess.Popen
    def fake_node(command, **kwargs):
        return popen([sys.executable, '-u', '-c', SERVER], **kwargs)
    with patch('soccer_browser.subprocess.Popen', side_effect=fake_node):
        bridge = BrowserBridge()
        try:
            client = r.Client(user='test', password='unused', browser=bridge)
            child = client.fork()
            assert child.browser is bridge
            with ThreadPoolExecutor(max_workers=2) as pool:
                slow = pool.submit(client.data, '/site/slow')
                time.sleep(.03)
                fast = pool.submit(child.data, '/site/fast')
                assert fast.result()['url'].endswith('/site/fast')
                assert not slow.done(), 'Waiting for one reply must not serialize the pipe'
                assert slow.result()['url'].endswith('/site/slow')
            prepared = client.prepare_submission({'data': '中文', 'number': 1})
            response = client.submit({'data': '中文', 'number': 1}, prepared=prepared)
            command = response['d']
            assert '中文' in command['body'] and 'multipart/form-data' in command['headers']['Content-Type']
            assert not {'Cookie', 'User-Agent', 'Origin'} & command['headers'].keys()
            child.http.close()
            assert client.data('/site/fast')['method'] == 'GET', 'Closing a fork must not close shared Chrome'
            try:
                client.data('/site/bad')
                raise AssertionError('HTTP 400 must not become success')
            except RuntimeError as exc:
                assert 'HTTP 400' in str(exc)
            client.http.close()
            with patch.object(bridge, 'call', side_effect=RuntimeError('Page context lost')):
                try:
                    BrowserSession(bridge).get(r.API + '/site/user/baseinfo')
                    raise AssertionError('Page loss must remain a network failure')
                except requests.ConnectionError:
                    pass
        finally:
            bridge.close()
        assert bridge.process.poll() == 0 and not bridge.reader.is_alive()

    # Actual Chrome, no navigation: write guard runs before any page fetch.
    with TemporaryDirectory() as profile:
        bridge = BrowserBridge(read_only=True, profile=profile)
        try:
            try:
                BrowserSession(bridge).get(r.API + '/site/user/baseinfo')
                raise AssertionError('Blank page must trigger recovery before a school request')
            except BrowserUnavailable:
                pass
            previous_pid = bridge.process.pid
            bridge.restart(time.time()+40)
            assert bridge.process.pid != previous_pid, 'Recovery must restart the shared bridge'
            try:
                BrowserSession(bridge).post(r.API + '/site/resource/launch', data={'test': 'blocked'})
                raise AssertionError('Read-only check allowed a booking request')
            except requests.ConnectionError as exc:
                assert 'Read-only browser rejects writes' in str(exc)
        finally:
            bridge.close()
    print('PASS: real pipe concurrency, reply IDs, multipart UTF-8, shared-session lifetime, HTTP errors, real Chrome read-only guard')


if __name__ == '__main__':
    check()
    if '--benchmark' in sys.argv:
        benchmark()
