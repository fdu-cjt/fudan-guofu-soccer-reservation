# 复旦大学国福路足球场预约

## 安装

支持 Windows 和 macOS。请先安装 Git、uv、Node.js（含 npm）以及 Chrome 或 Edge。

### Windows / PowerShell

```powershell
git clone https://github.com/fdusjtu/fudan-guofu-soccer-reservation.git
cd fudan-guofu-soccer-reservation
uv sync --locked
npm ci --ignore-scripts --no-audit --no-fund
```

### macOS / Terminal

```bash
git clone https://github.com/fdusjtu/fudan-guofu-soccer-reservation.git
cd fudan-guofu-soccer-reservation
uv sync --locked
npm ci --ignore-scripts --no-audit --no-fund
```

## 配置账号

请在 `.local/credentials.env` 中填写 UIS 账号、密码和手机号：

Windows / PowerShell：

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
notepad .local/credentials.env
```

macOS / Terminal：

```bash
mkdir -p .local
chmod 700 .local
touch .local/credentials.env
chmod 600 .local/credentials.env
open -e .local/credentials.env
```

在打开的 `.local/credentials.env` 中填写并保存：

```dotenv
FUDAN_USER=你的UIS账号
FUDAN_PASSWORD=你的UIS密码
FUDAN_PHONE=你的手机号
```

## 启动

每次启动会在日志中记录 `runtime_version`，包含分支、提交编号和是否有未提交修改；无需手动记住提交编号。

Windows PowerShell 和 macOS Terminal 均使用以下命令：

```bash
# 检查登录，不提交预约
uv run --locked python -m automations.soccer --check

# 正式启动，默认同时预约 18–19 点和 19–20 点
uv run --locked python -m automations.soccer

# 只预约 18–19 点
uv run --locked python -m automations.soccer --time 1

# 只预约 19–20 点
uv run --locked python -m automations.soccer --time 2
```

首次使用场地时，默认的浏览器模式会按官网页面的倒计时阅读并确认预约须知，然后重新检查状态。`--check` 不提交预约，但首次运行可能会在官网确认须知。使用 `--transport http` 时需先在官网手动完成确认。

macOS 上需要防止电脑休眠时，可以使用：

```bash
caffeinate -dimsu uv run --locked python -m automations.soccer
```

终端和程序需持续运行到放号时间。关闭终端、退出程序或让 Mac 进入休眠都会使自动预约停止。

启动后先尝试已有空位，再每天早上 07:00–07:02 尝试预约放号日后两天的场地。

当前 main 首轮先发 A（18–19 点）；B（19–20 点）在“A 收到有效业务响应后 1 秒”和“A 首发后 2 秒”中较早的时刻发送。A 的提前重试不会挤占 B 的首发。A 仍在请求中也不阻塞到期的 B；仅选一个时段时直接首发该时段。

每笔预约请求最多等待 5 秒。两笔首发之后按失败顺序进入交替重试，账号发送至少间隔 1 秒；普通可重试失败冷却 1 秒，网络超时不额外冷却，查询与重试同时安排。网络连接异常冷却 1 秒且不提前触发 B 首发，Retry-After 要求更长等待时遵守它。仍在请求中、等待 OK 核实、已成功或已停止的时段跳过.

“点击太频繁”“系统繁忙”“系统超时”“不在服务时间”等提示在有限窗口内允许重试。账号额度上限等账号级错误停止整轮并在最终汇报保留原因；已约满等仅停止相应时段。OK 最多核实三轮，仍未确认则标记不确定；客户端网络超时并不代表服务器没有成功。

每轮合计最多 20 次提交调度，约 07:02 停止新提交，07:05 只读复查，随后等待下一天。启动空位检查保持每个候选时段只尝试一次。首发保留 100ms 开场保护，120 秒提交窗口从计划首发起计算。
