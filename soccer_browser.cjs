// One persistent browser, concurrent JSON-line commands; stdout is protocol only.
const { chromium } = require('playwright');
const readline = require('node:readline');
const { createHash } = require('node:crypto');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const BASE = 'https://booking.fudan.edu.cn';
const PAGE = BASE + '/reservation/fe/site/reservationInfo?id=1029';
let context, page, browser, readOnly = true, documentStatus, closing = false, shutdownPromise;
const failures = [];
const progress = fields => process.stdout.write(JSON.stringify({ event: 'progress', ...fields }) + '\n');

// Preserve the separately launched Chrome + CDP path verified by the login logs.
const CHROME_CANDIDATES = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe'),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];

function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (candidate && fs.existsSync(candidate)) return path.resolve(candidate);
  }
  throw new Error('找不到本机 Chrome/Edge 可执行文件');
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitForDebugger(port, deadline) {
  let launcherExitedAt = null;
  while (Date.now() < deadline) {
    if (closing) throw new Error('Browser is closing');
    if (chromeProcess?.spawnFailure)
      throw new Error('Browser failed to start: ' + chromeProcess.spawnFailure.message);
    if (chromeProcess && chromeProcess.exitCode !== null && launcherExitedAt === null) {
      launcherExitedAt = Date.now();
      progress({ stage: 'browser_launcher_exited', code: chromeProcess.exitCode });
    }
    // Chromium may forward this launch to an existing process for the same
    // profile and let the new launcher exit. Keep probing the requested port
    // briefly instead of treating that normal handoff as an immediate failure.
    if (launcherExitedAt !== null && Date.now() - launcherExitedAt >= 15000)
      throw new Error('Browser launcher exited before its debugger became ready');
    try {
      const response = await fetch('http://127.0.0.1:' + port + '/json/version',
        { signal: AbortSignal.timeout(Math.max(1, Math.min(1000, deadline - Date.now()))) });
      if (response.ok) return await response.json();
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('Browser 调试端口未就绪；该配置目录可能已被另一个浏览器进程占用');
}

let chromeProcess = null;

async function withTimeout(promise, ms) {
  let timer;
  try { return await Promise.race([Promise.resolve(promise).catch(() => {}),
    new Promise(resolve => { timer = setTimeout(resolve, ms); })]); }
  finally { clearTimeout(timer); }
}

// 退出时必须回收整个 Chrome 进程树：只关闭 CDP 连接不会结束浏览器，
// 残留进程会一直占着 --user-data-dir，导致下一次运行起不来调试端口。
function terminateChromeChild(child) {
  if (!child || !child.pid || child.exitCode !== null) {
    return Promise.resolve();
  }

  // Windows: 保留原来的 taskkill
  if (process.platform === 'win32') {
    return withTimeout(new Promise(resolve => {
      const killer = spawn(
        'taskkill',
        ['/pid', String(child.pid), '/T', '/F'],
        { stdio: 'ignore', windowsHide: true }
      );
      killer.on('exit', resolve);
      killer.on('error', resolve);
    }), 5000);
  }

  // macOS / Linux: detached:true 时 Chrome 是独立进程组
  return withTimeout(new Promise(resolve => {
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {
      try {
        process.kill(child.pid, 'SIGTERM');
      } catch {}
    }

    setTimeout(() => {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch {
        try {
          process.kill(child.pid, 'SIGKILL');
        } catch {}
      }

      resolve();
    }, 500);
  }), 2000);
}

function killChromeTree() {
  const child = chromeProcess;
  chromeProcess = null;
  return terminateChromeChild(child);
}

// 上一次运行崩溃后可能留下占着同一配置目录的 Chrome，回收它以自愈。
async function killStaleForProfile(profile) {
  if (process.platform === 'win32') {
    // The owner record can be stale or overwritten after a failed launch. The
    // dedicated profile is the reliable boundary on Windows: never touch a
    // browser that does not carry this exact --user-data-dir argument.
    const script = `
      $escaped = [regex]::Escape([string]$env:SOCCER_PROFILE)
      $pattern = '(?:^|\\s)(?:"--user-data-dir=' + $escaped + '"|--user-data-dir="' + $escaped + '"|--user-data-dir=' + $escaped + ')(?=\\s|$)'
      Get-CimInstance Win32_Process |
        Where-Object { $_.Name -in @('chrome.exe', 'msedge.exe') -and $_.CommandLine -and $_.CommandLine -match $pattern } |
        ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F *> $null }`;
    return withTimeout(new Promise(resolve => {
      const killer = spawn('powershell', ['-NoProfile', '-NonInteractive', '-Command', script],
        { stdio: 'ignore', windowsHide: true, env: { ...process.env, SOCCER_PROFILE: profile } });
      killer.on('exit', resolve);
      killer.on('error', resolve);
    }), 5000);
  }

  const ownerPath = path.join(profile, '.soccer-owner.json');
  if (!fs.existsSync(ownerPath)) return;
  let owner;
  try { owner = JSON.parse(fs.readFileSync(ownerPath, 'utf8')); }
  catch { return; }
  if (owner.profile !== profile || !owner.pid) return;

  // macOS/Linux: verify the recorded PID still belongs to the exact browser,
  // profile and debugging port before sending a signal.
  const command = await withTimeout(
    new Promise(resolve => {
      const ps = spawn(
        'ps',
        ['-p', String(owner.pid), '-o', 'command='],
        { stdio: ['ignore', 'pipe', 'ignore'] }
      );

      let output = '';

      ps.stdout.on('data', chunk => {
        output += chunk.toString();
      });

      ps.on('error', () => resolve(''));
      ps.on('close', () => resolve(output.trim()));
    }),
    2000
  );

  if (!command) {
    return;
  }

  const sameExecutable =
    owner.executable && command.includes(owner.executable);

  const sameProfile =
    owner.profile && command.includes(owner.profile);

  const samePort =
    command.includes('--remote-debugging-port=' + owner.port);

  if (!sameExecutable || !sameProfile || !samePort) {
    return;
  }

  let target = -Number(owner.pid);
  try {
    process.kill(target, 'SIGTERM');
  } catch {
    target = Number(owner.pid);
    try { process.kill(target, 'SIGTERM'); }
    catch { return; }
  }

  // Chrome must release this profile before the next launcher uses it.
  async function waitForExit(ms) {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      try { process.kill(target, 0); }
      catch (error) {
        if (error.code === 'ESRCH') return true;
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return false;
  }
  if (await waitForExit(2000)) return;
  try { process.kill(target, 'SIGKILL'); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
  if (!await waitForExit(1000)) throw new Error('Stale browser did not release its profile');
}

// Keep browser diagnostics out of the JSON protocol stream.
function spawnChrome(profile, port) {
  const executable = findChrome();
  const startedMs = Date.now();
  const child = spawn(executable, [
    '--remote-debugging-port=' + port,
    '--user-data-dir=' + profile,
    '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
    '--window-size=1280,900',
    'about:blank',
  ], { detached: true, stdio: 'ignore' });
  child.on('error', error => { child.spawnFailure = error; });
  child.once('spawn', () => fs.writeFileSync(path.join(profile, '.soccer-owner.json'),
    JSON.stringify({ pid: child.pid, profile, executable, port, startedMs })));
  child.unref();
  return child;
}

async function launchChrome(profile, deadline) {
  fs.mkdirSync(profile, { recursive: true });
  await killStaleForProfile(profile);
  if (closing || Date.now() >= deadline) throw new Error('Browser start deadline reached');
  const port = await freePort();
  chromeProcess = spawnChrome(profile, port);
  let info;
  try {
    info = await waitForDebugger(port, deadline);
  } catch (error) {
    await killChromeTree();
    await killStaleForProfile(profile);
    throw error;
  }
  if (closing) throw new Error('Browser is closing');
  const connected = await chromium.connectOverCDP('http://127.0.0.1:' + port,
    { timeout: Math.max(1, Math.min(5000, deadline - Date.now())) });
  const persistent = connected.contexts()[0];
  if (!persistent) throw new Error('已连上 Chrome，但没有可用的浏览器上下文');
  return { context: persistent, browser: connected, version: info.Browser };
}

async function browserFetch({ url, method, headers, body, timeoutMs, openingMs, deadlineMs, plannedAt }) {
  const sentAt = Date.now();
  const timing = { sentAt, plannedAt, dispatchDelayMs: plannedAt == null ? null : sentAt - plannedAt };
  const launch = method === 'POST' && new URL(url).pathname.endsWith('/site/resource/launch');
  if (launch && (!Number.isFinite(openingMs) || !Number.isFinite(deadlineMs)
      || sentAt < openingMs || sentAt >= deadlineMs || plannedAt != null && plannedAt >= deadlineMs))
    return { skipped: true, ...timing };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const started = performance.now();
  try {
    const response = await fetch(url, {
      method, headers, body, credentials: 'same-origin', redirect: 'error',
      cache: 'no-store', signal: controller.signal,
    });
    return { status: response.status, url: response.url,
      headers: Object.fromEntries(response.headers), text: await response.text(),
      ...timing, elapsedMs: performance.now() - started };
  } catch (error) { return { failure: error.name, ...timing, elapsedMs: performance.now() - started }; }
  finally { clearTimeout(timer); }
}

async function fetchInPage(command) {
  const url = new URL(command.url);
  if (url.origin !== BASE || !url.pathname.startsWith('/reservation/site/'))
    throw new Error('API destination not allowed');
  if (readOnly && command.method !== 'GET') throw new Error('Read-only browser rejects writes');
  const current = page && !page.isClosed() ? new URL(page.url()) : null;
  if (!browser?.isConnected() || !current || current.origin !== BASE
      || current.pathname !== '/reservation/fe/site/reservationInfo' || current.searchParams.get('id') !== '1029')
    throw Object.assign(new Error('Browser disconnected or page left booking site'), { code: 'BROWSER_UNAVAILABLE' });
  try { return await page.evaluate(browserFetch, command); }
  catch (error) { throw Object.assign(error, { code: 'BROWSER_UNAVAILABLE' }); }
}

async function login({ user, password, deadlineMs }) {
  const deadline = Math.min(Date.now() + 45000, deadlineMs ?? Infinity);
  const remaining = limit => {
    if (closing || Date.now() >= deadline) throw new Error('Browser login deadline reached');
    return Math.max(1, Math.min(limit, deadline - Date.now()));
  };
  await page.goto(PAGE, { waitUntil: 'domcontentloaded', timeout: remaining(15000) });
  let submitted = false, lastStatus, previousLocation, rejected = 0, reloaded = false;
  while (!closing && Date.now() < deadline) {
    if (documentStatus === 400) {
      if (reloaded) throw new Error('Browser login rejected: document HTTP 400; credentials submitted=' + submitted);
      reloaded = true;
      progress({ stage: 'document_retry', http: 400 });
      await new Promise(resolve => setTimeout(resolve, 3000));
      await page.reload({ waitUntil: 'domcontentloaded', timeout: remaining(10000) });
    }
    const locations = page.frames().map(frame => {
      const url = new URL(frame.url() || 'about:blank');
      return url.origin + url.pathname;
    });
    if (JSON.stringify(locations) !== previousLocation) {
      previousLocation = JSON.stringify(locations);
      progress({ stage: 'login_navigation', locations });
    }
    for (const frame of page.frames()) {
      const origin = new URL(frame.url()).origin;
      if (!['https://id.fudan.edu.cn', 'https://id.fudan.ac', 'https://uis.fudan.edu.cn'].includes(origin)) continue;
      // 统一身份认证是 el-input 表单，页面里第一个 input[type=text] 是 readOnly 的
      // “请选择”下拉框。按 :visible 取 .first() 会命中它，fill() 会一直等到 30 秒超时。
      // 必须按 name/id 精确定位真正的账号框和密码框。
      const userField = frame.locator('input[name="username"], #login-username').first();
      const passwordField = frame.locator('input[name="password"], #login-password').first();
      if (submitted || !await userField.count() || !await passwordField.count()) continue;
      await userField.fill(user, { timeout: remaining(10000) });
      await passwordField.fill(password, { timeout: remaining(10000) });
      await frame.locator('button.content_submit, button:has-text("登录")').first().click({ timeout: remaining(10000) });
      submitted = true;
      progress({ stage: 'credentials_submitted' });
    }
    if (new URL(page.url()).origin === BASE) {
      // Let the official page initialise before a session check.
      const loginButton = page.getByText(/^登\s*录$/).first();
      if (!submitted && await loginButton.isVisible()) {
        await loginButton.click({ timeout: remaining(10000) });
      } else if (await page.getByText('国福路田径场-足球场', { exact: true }).first().isVisible()) {
        try {
          const response = await fetchInPage({ url: BASE + '/reservation/site/user/baseinfo',
            method: 'GET', headers: {}, timeoutMs: remaining(10000) });
          lastStatus = response.status;
          progress({ stage: 'login_check', http: lastStatus });
          rejected = [400, 412].includes(lastStatus) ? rejected + 1 : 0;
          if (rejected >= 3) throw new Error('Browser login rejected: HTTP ' + lastStatus + ' before business access');
          if (response.status === 200) {
            const body = JSON.parse(response.text);
            if (body.e === 'OK') {
              if (String(body.d.number) !== String(user)) throw new Error('Account mismatch');
              return { accountVerified: true, http: 200 };
            }
            if (body.e === 'UN_AUTH') {
              await page.goto(BASE + '/reservation/api/login/main?redirect_url=' + encodeURIComponent(PAGE),
                { waitUntil: 'domcontentloaded', timeout: remaining(10000) });
            }
          }
        } catch (error) {
          if (error.message === 'Account mismatch' || error.message.startsWith('Browser login rejected')) throw error;
        }
      }
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  throw new Error('Browser login not completed; last HTTP=' + (lastStatus ?? 'none') + '; credentials submitted=' + submitted);
}

async function confirmNotice(currentPage, deadlineMs) {
  const remaining = () => Math.max(1, Math.min(65000, (deadlineMs ?? Date.now() + 65000) - Date.now()));
  const notice = currentPage.locator('.introduce_ref1:visible').first();
  await notice.waitFor({ timeout: Math.min(10000, remaining()) });
  const content = (await notice.innerText()).trim();
  if (!content) throw new Error('预约须知内容为空，未自动确认');
  const confirm = currentPage.getByRole('button', { name: /^(?:\d+s后确定|确定)$/ }).first();
  const responsePromise = currentPage.waitForResponse(response =>
    new URL(response.url()).pathname.endsWith('/site/resource/read-description') &&
    response.request().method() === 'POST', { timeout: remaining() });
  const [response] = await Promise.all([responsePromise, confirm.click({ timeout: remaining() })]);
  const result = await response.json();
  if (response.status() !== 200 || result.e !== 'OK')
    throw new Error('官网未确认预约须知');
  return { confirmed: true, characters: content.length };
}

async function handle(command) {
  switch (command.op) {
    case 'start': {
      readOnly = command.readOnly !== false;
      const launched = await launchChrome(command.profile, Math.min(Date.now() + 30000, command.deadlineMs ?? Infinity));
      context = launched.context;
      browser = launched.browser;
      page = context.pages()[0] || await context.newPage();
      page.on('pageerror', error => {
        failures.push({ type: 'pageerror', message: error.message.slice(0, 200) });
        if (failures.length > 12) failures.shift();
      });
      context.on('response', async response => {
        const url = new URL(response.url());
        if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) {
          documentStatus = response.status();
          progress({ stage: 'document', http: documentStatus, location: url.origin + url.pathname });
        }
        if (url.origin !== BASE || response.status() < 400) return;
        const headers = response.headers();
        const item = { path: url.pathname, http: response.status(),
          contentType: headers['content-type'], server: headers.server };
        failures.push(item);
        if (failures.length > 12) failures.shift();
        try {
          const requestHeaders = await response.request().allHeaders();
          item.cookieBytes = (requestHeaders.cookie || '').length;
          if (response.status() === 400) {
            const text = await response.text();
            item.bodyBytes = Buffer.byteLength(text);
            item.bodyHash = createHash('sha256').update(text).digest('hex');
            item.headerTooLarge = /header.*too large|cookie.*too large/i.test(text);
          }
        } catch {}
      });
      return { ready: true, version: browser.version(), readOnly };
    }
    case 'login': return login(command);
    case 'confirmNotice': {
      const current = page && !page.isClosed() ? new URL(page.url()) : null;
      if (!browser?.isConnected() || !current || current.origin !== BASE ||
          current.pathname !== '/reservation/fe/site/reservationInfo' || current.searchParams.get('id') !== '1029')
        throw Object.assign(new Error('Browser left booking page'), { code: 'BROWSER_UNAVAILABLE' });
      return confirmNotice(page, command.deadlineMs);
    }
    case 'fetch': return fetchInPage(command);
    case 'diagnostics': {
      const cookies = await context.cookies(BASE);
      const html = await page.content();
      const url = new URL(page.url());
      return { location: url.origin + url.pathname, title: await page.title(),
        failures, documentStatus, bodyCharacters: (await page.locator('body').innerText()).length,
        frames: page.frames().map(frame => { const u = new URL(frame.url() || 'about:blank'); return u.origin + u.pathname; }),
        pageHash: createHash('sha256').update(html).digest('hex'),
        challenge: html.includes('$_ts'),
        cookies: cookies.map(c => ({ name: c.name, domain: c.domain, path: c.path,
          size: c.value.length, httpOnly: c.httpOnly })) };
    }
    case 'close':
      await shutdown();
      return { closed: true };
    default: throw new Error('Unknown browser operation');
  }
}

function shutdown() {
  closing = true;
  if (!shutdownPromise) shutdownPromise = (async () => {
    await withTimeout(context ? context.close() : null, 2000);
    await killChromeTree();
    await withTimeout(browser ? browser.close() : null, 1000);
  })();
  return shutdownPromise;
}

if (require.main === module) {
const input = readline.createInterface({ input: process.stdin });
input.on('line', line => {
  let command;
  try { command = JSON.parse(line); }
  catch { process.stdout.write(JSON.stringify({ id: null, error: 'Invalid JSON command' }) + '\n'); return; }
  // Do not await one command before reading the next: slow fetches must overlap.
  handle(command).then(result => {
    process.stdout.write(JSON.stringify({ id: command.id, result }) + '\n');
    if (command.op === 'close') input.close();
  }, error => {
    // Playwright errors may include locators/values; return only a bounded diagnosis.
    const message = command.op === 'login' && !error.message.startsWith('Browser login') && error.message !== 'Account mismatch'
      ? 'Browser login failed: ' + error.name : String(error.message).split('\n')[0];
    process.stdout.write(JSON.stringify({ id: command.id, error: message, code: error.code }) + '\n');
  });
});
input.on('close', shutdown);
}
module.exports = { browserFetch, confirmNotice, withTimeout, findChrome, killStaleForProfile, terminateChromeChild };
