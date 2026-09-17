# 复旦大学国福路足球场预约

通过复旦 UIS 登录预约平台，预约国福路足球场的 **18:00–19:00** 和 **19:00–20:00** 时段。启动时先尝试已开放的空位，然后持续等待每天早上 07:00 放号；预约成功后继续运行，浏览器保持打开，方便人工查看结果。

当前版本：**v1.2.0**。当前代码只包含足球场预约；讨论室预约已移除，旧版仍保存在 Git 历史和版本标签中。

## 运行环境

- Windows 11 / PowerShell；当前浏览器启动和进程管理按 Windows 实现。
- Python 3.12 或更新版本，以及 uv。
- Node.js / npm；浏览器依赖由 package-lock.json 锁定。
- 本机已安装 Google Chrome 或 Microsoft Edge，无需另行下载 Playwright 浏览器。
- 可使用学校预约平台的 UIS 账号，以及预约所需手机号。

## 安装与配置

```powershell
git clone https://github.com/fdusjtu/fudan-guofu-soccer-reservation.git
cd fudan-guofu-soccer-reservation
uv sync --locked
npm ci --ignore-scripts --no-audit --no-fund

New-Item -ItemType Directory -Force .local | Out-Null
if (!(Test-Path .local/credentials.env)) {
    Copy-Item credentials.example.env .local/credentials.env
}
notepad .local/credentials.env
```

填写三个值，保存后关闭编辑器：

```dotenv
FUDAN_USER=你的UIS账号
FUDAN_PASSWORD=你的UIS密码
FUDAN_PHONE=你的手机号
```

程序自动读取 `.local/credentials.env`；同名环境变量优先。真实凭据、浏览器会话和日志位于被 Git 忽略的 `.local/`，仓库只提供空白模板。不要把真实信息填进 `credentials.example.env`，也不要强制提交本地凭据和会话。

首次使用建议先检查登录，**此命令不创建预约**：

```powershell
uv run --locked python -m automations.soccer --check --log-file .local/soccer-login-check.log
```

检查仍需要登录并访问学校平台，额外认证可能需要人工完成。检查结束后默认保留浏览器，回到终端按 Enter 关闭。

## 启动预约

```powershell
# 默认两个时段
uv run --locked python -m automations.soccer

# 只预约 18:00–19:00
uv run --locked python -m automations.soccer --slots 1

# 只预约 19:00–20:00
uv run --locked python -m automations.soccer --slots 2

# 显式指定两个时段
uv run --locked python -m automations.soccer --slots 1 2
```

编号代表时间段，不代表两个固定实体场地；每笔请求只预约一个时段，由平台分配子场地。正式启动可能立即预约现有空位。

保持终端打开、电脑不休眠、网络可用，并同步系统时间。正常运行不会因为本轮成功、部分成功或核实结果不确定而退出。按 **Ctrl+C** 停止自动预约；默认随后保留浏览器等待人工处理，按 Enter 或再次 Ctrl+C 关闭。若需停止时直接关闭浏览器：

```powershell
uv run --locked python -m automations.soccer --close-browser
```

一次只运行一个足球预约进程。修改代码或配置后需停止旧进程并重新启动。

## 每天如何预约

时间均为北京时间，调度依赖本机时钟。早上放号目标为 **放号日 +2 天**：例如 9 月 17 日早上预约 9 月 19 日。

| 阶段 | 行为 |
| --- | --- |
| 启动时 | 扫描今天至今天 +2 天的已开放空位；按日期尝试所选时段，跳过已开始或本人已有有效预约的时段。 |
| 放号前 | 提前确定目标日期；06:55 检查会话，06:57 再读取规则、日历和已有预约。会话失效时尝试恢复。 |
| 07:00:00.100–07:02:00.100 | 正式新增提交窗口，默认 120 秒。两个时段共用串行队列，失败按规则重试。 |
| 提交后 | 查询本人预约核实结果；恢复与核实循环的截止时间为 07:05，在途请求及最终收尾可能延后结束。 |
| 本轮完成后 | 保留浏览器和会话，等待下一天 06:55 准备下一轮。 |

启动空位扫描只执行一次，不会整夜监控退订。每个候选日期开始前重查余量，启动扫描每个目标时段只尝试提交一次，随后有限核实；临近放号时受截止时间限制。迟启动、登录异常或网络故障会影响整点就绪。

### 两个时段的提交顺序

同一账号最多只有一笔预约请求在途。第二笔需同时满足：**第一笔已经返回**，且**距离第一笔发起至少 2 秒**。

例如第一笔在 07:00:00.100 发出、07:00:05 才返回，第二笔可紧接着发送，不额外等待 2 秒。尚未尝试的时段优先于已经失败时段的重试。

### 重试与结果核实

- 每个时段每轮最多提交 **60 次**，首次计入上限；**120 秒**窗口与次数上限哪个先达到，就停止新增提交。两个时段共用队列，不保证各提交 60 次。
- 系统繁忙、5xx 和“点击太频繁了”可在限制内重试，并遵循服务端退避时间。
- 已确认成功的时段停止提交。已受理或网络超时导致结果不明时，先查询本人预约，不立即重复提交；有限核实后仍未找到记录则报告“结果不确定”。
- 429、访问防护、权限限制、冲突或已有待处理预约等情况会暂停相关目标或本轮请求；准备阶段连续恢复失败等致命错误会停止程序并默认保留浏览器。
- 登录失效或浏览器断开时有限恢复。短信或其他额外认证可能需要人工处理。保留浏览器不意味着页面一定停在每笔请求的确认画面。

预约结果以平台实际记录为准；本地测试不能保证真实放号时必然预约成功。

## 调整参数

编辑 [automations/soccer.py](automations/soccer.py) 顶部参数后重启程序：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| RETRY_WINDOW_SECONDS | 120 | 正式放号新增提交窗口，单位秒。 |
| MAX_SUBMISSION_ATTEMPTS | 60 | 每个时段每轮最多提交次数，包含首次。 |
| SUBMIT_INTERVAL_SECONDS | 2.0 | 账号两次请求发起之间的最短间隔，仍需等待上一笔返回。 |
| OPENING_GUARD_MS | 100 | 本地 07:00 后延迟 100 毫秒开始提交。 |
| MAX_VERIFICATION_ATTEMPTS | 6 | 有限核实的轮数上限。 |
| MAX_RECOVERY_ATTEMPTS | 3 | 恢复尝试上限。 |

## 日志与排查

- 默认日志为 `.local/soccer.log`，**每次重新启动会覆盖**，连续运行跨天不会重新清空。需留档时先复制日志，或通过 `--log-file` 指定新路径。
- 独立浏览器配置目录为 `.local/soccer-chrome`，保存登录状态。
- 日志中的 `fetch_timing` 记录页面调用 fetch 的时间和响应耗时，并非服务器收到请求的时间。
- 无法进入页面时，先运行 `--check` 并查看终端和检查日志。配置项非空不代表密码正确或会话有效。
- 当前路径为 Python → Node Playwright → 本机浏览器 → 页面内请求；`--transport http` 保留作对照，不能继承浏览器动态会话。

持续五分钟检查会话、不提交预约：

```powershell
uv run --locked python -m automations.soccer --check --watch-seconds 300 --log-file .local/soccer-check.log
```

## 代码结构与测试

```text
automations/soccer.py             预约调度、重试、核实与跨天循环
fudan_spider/apis/soccer_api.py   足球预约平台接口
fudan_spider/uis.py               UIS 登录响应解析
fudan_spider/utils.py             配置读取与会话文件保存
soccer_browser.py                Python 浏览器桥接
soccer_browser.cjs               浏览器启动、登录与页面请求
captcha_slide.py                 滑块处理
credentials.example.env         空白凭据模板
tests/                          模拟接口和本地浏览器测试
```

以下测试不创建学校预约。浏览器测试会调用本机浏览器和本地模拟服务：

```powershell
uv run --locked python -B tests/test_soccer.py
uv run --locked python -B tests/test_soccer_retry.py
uv run --locked python -B tests/test_soccer_browser.py
```

## 版本

- **v1.0.0**：首次保留的基线。
- **v1.1.0**：独立本地凭据、时段选择、串行提交、结果核实修正、跨天常驻及两分钟提交窗口。
- **v1.2.0**：足球场专用版本，移除讨论室预约入口、接口、测试及专用解析函数，整理公开仓库使用说明。
