"""Thread-safe Python -> persistent Node Playwright bridge; credentials stay on stdin."""
import json
import subprocess
import threading
import time
from concurrent.futures import Future, TimeoutError
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


class BrowserUnavailable(requests.ConnectionError):
    """The browser/page/IPC must be restored before further submissions."""


class SubmissionSkipped(requests.RequestException):
    """The browser rejected a request outside its submission window."""


class BrowserBridge:
    def __init__(self, read_only=True, profile=None, on_event=None, deadline=None):
        self.lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.closed = False
        self.on_event = on_event
        self.read_only = read_only
        self.profile = str(profile or ROOT / '.local' / 'soccer-chrome')
        self.process = subprocess.Popen(
            ['node', str(ROOT / 'soccer_browser.cjs')], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = threading.Thread(target=self._read, name='soccer-browser-results', daemon=True)
        self.reader.start()
        try:
            self.info = self.call('start', timeout=40, readOnly=read_only, profile=self.profile,
                                  deadlineMs=None if deadline is None else deadline * 1000)
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if 'event' in message:
                    if self.on_event is not None:
                        self.on_event({k: v for k, v in message.items() if k != 'event'})
                    continue
                with self.lock:
                    future = self.pending.pop(message['id'], None)
                if future is not None:
                    if 'error' in message:
                        kind = BrowserUnavailable if message.get('code') == 'BROWSER_UNAVAILABLE' else RuntimeError
                        future.set_exception(kind(message['error']))
                    else:
                        future.set_result(message['result'])
        finally:
            with self.lock:
                pending, self.pending = self.pending, {}
            for future in pending.values():
                future.set_exception(BrowserUnavailable('Browser bridge exited'))

    def restart(self, deadline):
        self.close()
        if time.time() >= deadline:
            raise requests.Timeout('Recovery window ended')
        self.__init__(self.read_only, self.profile, self.on_event, deadline=deadline)

    def call(self, operation, timeout=40, **fields):
        future = Future()
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise BrowserUnavailable('Browser bridge is closed')
            self.sequence += 1
            request_id = self.sequence
            self.pending[request_id] = future
            try:
                self.process.stdin.write(json.dumps({'id': request_id, 'op': operation, **fields}) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.pending.pop(request_id, None)
                raise BrowserUnavailable('Cannot write to browser bridge') from None
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            with self.lock:
                self.pending.pop(request_id, None)
            # The page's own network timeout expires first; an IPC timeout means
            # the browser/bridge is unresponsive and needs coordinated recovery.
            raise BrowserUnavailable('Browser operation timed out; outcome may be unknown') from None

    def close(self):
        if self.closed:
            return
        try:
            if self.process.poll() is None:
                self.call('close', timeout=15)
        except (RuntimeError, requests.RequestException):
            pass
        finally:
            self.closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.reader.join(timeout=2)
            self.process.stdout.close()


class BrowserSession(requests.Session):
    """Keep existing request/form preparation; send exclusively through page fetch."""
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.window = None
        self.planned_at = None

    def send(self, request, **kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        if body is not None and not isinstance(body, str):
            raise ValueError('Browser transport requires a text request body')
        timeout = kwargs.get('timeout') or 30
        if isinstance(timeout, tuple):
            timeout = sum(timeout)
        try:
            result = self.bridge.call('fetch', timeout=timeout + 5, url=request.url,
                method=request.method, body=body, timeoutMs=int(timeout * 1000),
                openingMs=self.window[0] if self.window else None,
                deadlineMs=self.window[1] if self.window else None, plannedAt=self.planned_at,
                headers={k: v for k, v in request.headers.items() if k.lower() in {'content-type', 'accept'}})
        except RuntimeError as exc:
            # Preserve the existing solver/worker network-error handling on page or pipe loss.
            raise requests.ConnectionError(str(exc)) from None
        if self.bridge.on_event is not None and 'sentAt' in result:
            self.bridge.on_event({'stage': 'fetch_timing', 'path': request.path_url.split('?')[0],
                'method': request.method, **{k: result.get(k) for k in
                ('sentAt', 'plannedAt', 'dispatchDelayMs', 'elapsedMs', 'skipped', 'failure')}})
        if result.get('skipped'):
            raise SubmissionSkipped('Browser submission window closed')
        if result.get('failure'):
            kind = requests.Timeout if result['failure'] == 'AbortError' else requests.ConnectionError
            raise kind('Browser fetch failed: ' + result['failure'])
        response = requests.Response()
        response.status_code = result['status']
        response.url = result['url']
        response.headers.update(result['headers'])
        response._content = result['text'].encode('utf-8')
        response.encoding = 'utf-8'
        response.request = request
        return response
