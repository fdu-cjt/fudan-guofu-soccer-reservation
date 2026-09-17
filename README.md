# 复旦自动预约

本项目按 `fudan_spider` 核心库和 `automations` 自动化入口组织：
`fudan_spider/apis/room.py` 负责讨论室平台接口，`fudan_spider/apis/soccer.py` 负责足球平台接口，
`automations/room.py` 和 `automations/soccer.py` 负责各自的调度。
旧的 `reserve.py`、`reserve_soccer.py` 已移除，统一从 `automations` 模块启动。

## 讨论室预约（默认 A103）

### 启动

```powershell
cd D:\a103
uv run --locked python -m automations.room
```

通过 `--rooms` 选择房间：

```powershell
uv run --locked python -m automations.room --rooms A103
uv run --locked python -m automations.room --rooms B101
uv run --locked python -m automations.room --rooms A103 B101
```

不传参数默认 A103；重复名称自动去重，无效名称在启动时拒绝。多个房间并行预约，各自核实和停止，目标是两个都约，一个成功不会停止另一个。每个房间沿用下面的提交节奏和各自的服务器规则。共享会话的登录与缓存写入串行执行，避免文件冲突。一次只运行一个讨论室进程，多房间请放在同一条命令中。

启动后等待下一个有效整点，提前 15 秒登录、读取开放规则并准备请求。只抢该整点刚开放的一个小时，不查询空位，不扫描其他时段。

默认目标为明天 08:00–22:00，按结束时间进入未来 24 小时的规则提交：

- 今天 09:00 → 明天 08:00–09:00。
- 今天 19:00 → 明天 18:00–19:00。
- 今天 22:00 → 明天 21:00–22:00。
- 22:00 后等待次日 09:00；正常启动不会立即创建预约。

仍会检查服务器开放日期、星期、关闭时段、最小/最大提前时间及时间格规则。不符合规则就跳过。预约用途固定为“面试”。

### 提交节奏

- 每个窗口 30 秒，每 100 毫秒发起一次，最多 300 次；用线程并发，不等待上一笔响应后才发下一笔。
- 超时、无法识别的响应、明确的暂时时段冲突，以及“系统繁忙！由于当前使用人数较多，请稍后再试！”不打断发起节奏；提交异常会记录异常类型。
- 明确接收成功后停止新增请求；已经发出的请求允许完成，仍可能产生重复提交或重复记录。
- 课程冲突确认提示、权限/次数限制等不可重试拒绝、登录失效或 HTTP 429 限流会停止新增请求。
- 每个请求连接超时 3.05 秒、读取超时 8 秒。读取超时并非严格的请求总时限，客户端超时也不取消服务端处理。
- 提交期间使用独立会话并发查询自己的预约，目标查询起始间隔为 500 毫秒；上一轮未返回时不叠加查询，不阻塞 100 毫秒的提交节拍。查到目标时间的有效记录就停止新增提交；取消、拒绝或其他时段的记录不触发停止。查询网络异常时提交继续。
- 窗口结束并等待已发请求收尾后，再查一次自己的预约，打印实际记录数量和状态；查不到只报告未确认。
- 100 毫秒为发起的目标间隔，受 Windows 调度和网络影响，不保证每次精确命中；错过的节拍跳过，不集中补发。
- 不再读取或写入 pending 预约标记；旧 pending 文件不影响运行。

### 配置

在 `automations/room.py` 顶部修改后重启生效：

- `START_HOUR = 8`、`END_HOUR = 22`：希望预约的每日时间范围，每次一小时。
- `RETRY_WINDOW_SECONDS = 30`：整点后的提交窗口。
- `SUBMIT_INTERVAL_SECONDS = 0.1`：固定发起间隔，单位秒。
- `VERIFY_INTERVAL_SECONDS = 0.5`：并发核实的最短起始间隔，包含查询耗时，单位秒。
- `SUBMISSION_TIMEOUT_SECONDS = 8`：单次提交的读取超时，单位秒。
- `VERBOSE_HTTP = False`：需要逐请求耗时时可打开。

保持终端打开；Ctrl+C 停止。停止后已经发出的请求可能仍需等待网络返回。电脑休眠、断网和本机时钟误差会影响运行。账号、密码和手机号读取 `.local/room-credentials.json`（格式见 `room-credentials.example.json`），环境变量 `FUDAN_USER`、`FUDAN_PASSWORD`、`FUDAN_PHONE` 优先；验证码或额外认证仍需要人工处理。

### 检查与文件

运行模拟接口测试，不产生真实预约：

```powershell
uv run --locked python -B D:\a103\tests\test_room.py
uv run --locked python -B D:\a103\tests\test_soccer.py
```

- `.local/session.json`：登录会话缓存。
- `.local/runs.log`：运行日志。
- `uv run --locked python -m automations.room --import-state <会话JSON路径>`：导入浏览器会话后退出。

2026-09-15 非整点真实接口小样本测量：对已占用的明天 21:00–22:00，500/200/100 毫秒各发起最多两次，六次均返回时段冲突；响应耗时约 245–279 毫秒，未观察到限流，随后未查到该时段的有效预约记录。这个样本不能证明某个整点提交频率最优。详细结果在 `.local/submission-timing-20260915.json`。

## 国福路足球场预约

使用项目内的 uv 环境（Python 3.12），浏览器依赖由 npm 锁文件固定：

```powershell
uv sync --locked
npm ci --ignore-scripts --no-audit --no-fund
uv run --locked python -m automations.soccer
```

默认预约 18:00–19:00 和 19:00–20:00 两个时段；指定时段时使用编号：

```powershell
uv run --locked python -m automations.soccer --slots 1       # 18:00–19:00
uv run --locked python -m automations.soccer --slots 2       # 19:00–20:00
uv run --locked python -m automations.soccer --slots 1 2    # 两个时段
```

实际路径为 Python → 常驻 Node Playwright → 本机 Chrome → 页面内 fetch。
Chrome 使用独立配置 `.local/soccer-chrome`，会话保存在该目录；无需另外下载浏览器。
`.local/credentials.env` 提供 `FUDAN_USER`、`FUDAN_PASSWORD`、`FUDAN_PHONE`，环境变量优先。

### 业务与时间

- 默认预约两个时段：18–19、19–20 点。可用 `--slots 1` 或 `--slots 2` 指定单个时段；每个时段各发一笔单时段请求，由系统分配子场地。早上放号目标是放号日 +2 天。
- 启动先登录，扫描今天至今天 +2 天的日历（当前规则允许的范围），只尝试明确可预约且剩余量大于零的时段；跳过已经开始和本人已有有效预约的时段。所有有空位的日期都按日期顺序尝试，每个日期开始前重查余量。
- 启动预约每个日期只做一轮单时段提交，也不超过目标时段开始时间；临近早上准备/放号时让出时间。只提交该日期查到空位的目标时段，随后做一次有限核实。扫描是启动时的一次检查，不是整夜监控退订空位。
- 各日期尝试结束后，只要确认了新增预约就结束运行；如果没有空位或全部尝试仍未确认成功，则等待下一次 07:00–07:05 放号窗口。窗口内启动直接进入原抢约流程，避免扫描拖慢整点提交。查询失败会恢复重试，不当作“无空位”。
- 等待放号时，06:55 检查已登录会话，06:57 做用户信息、规则、日历和本人预约检查；会话失效才恢复登录。健康会话不在 06:59 重新导航登录。
- 准备完成后，整点前每 15 秒查询本人预约检查会话；发现失效提前恢复。日志记录实际准备完成时间及距整点的秒数；迟启动或故障不保证能在 06:57 就绪。
- 正式放号首次提交从本地 07:00 后延 100ms 开始。两个时段共用一个账号级提交队列：同一时间只允许一笔在途，距上次实际发起至少 2 秒；新增提交仅限起点后的 30 秒，每个时段最多 15 次，并满足服务端退避要求后才能重试。不再按固定轮次跳过发送机会。
- 繁忙、5xx 和“点击太频繁了”在次数上限内按账号级间隔重试；429、防护响应、已有待处理预约和权限/表单/冲突类拒绝会暂停全部或对应时段。普通请求超时、已受理但未见记录时先核实，不立即叠加 POST。
- 放号前按不超过 15 秒间隔检查会话；放号后仅在提交返回 OK 或出现不确定网络结果时唤醒核实，最多核实 6 轮。核实请求不重叠，仍未查到时记录“结果不确定”并停止该时段，避免无限提交。
- 提交阶段浏览器断开或登录失效最多恢复 3 次；准备阶段连续失败达到 3 次也会停止。403/412、防护 HTML 和 429 不再当作登录失效循环恢复。恢复登录并复查规则后，结果不明的已发请求仍先进行有限核实，查不到不自动重发；尚未提交的目标可继续。
- 早上放号新增预约受 30 秒提交窗口约束，恢复和有限核实最晚到 07:05；启动空位预约使用该次尝试的截止时间。浏览器在调用 fetch 前再次校验时间窗口；已经发出的请求不能保证在服务器停止处理，窗口结束后还会等待在途请求并最终核实。
- `anti_bot=0` 不取验证码；需要滑块时在后台补充，一张凭据只用一次。整点前 10 秒开始预取，最多保留 2 张、超过 10 秒的本地缓存保守丢弃（不代表已证明服务端有效期为 10 秒）。池空跳过当前发送机会，不让图像求解阻塞调度；恢复浏览器后作废旧池。短信/额外认证仍可能需要人工处理。

### 检查、日志与测试

```powershell
# 只读；桥接层禁止非 GET 请求，不提交预约或验证码校验。
uv run --locked python -m automations.soccer --check --watch-seconds 300 --log-file .local\soccer-latest-check.log

# 本地测试，不创建学校预约。
uv run --locked python -B tests/test_soccer.py
uv run --locked python -B tests/test_soccer_browser.py
uv run --locked python -B tests/test_soccer_retry.py

# 本机 HTTP 模拟服务 + 真 Chrome，测量 20 秒调度抖动；不会向学校压测。
uv run --locked python -B tests/test_soccer_browser.py --benchmark
```

- 默认每次运行重写 `.local/soccer.log`。先取得单实例锁再打开日志，拒绝第二个实例不会清空正在运行的日志。
- `fetch_timing` 记录计划时间、页面调用 fetch 的时间和响应耗时。页面调用时间不等于数据上网线或学校服务器收到的时间。
- `.local/soccer-chrome/.soccer-owner.json` 仅用于核对本脚本遗留的浏览器进程；清理须匹配 PID、启动时间、可执行文件、完整配置参数及调试端口，不按目录前缀批量关闭 Chrome。
- `--transport http` 保留作对照；它不能继承 Chrome 动态会话。`--browser-profile` 可指定独立配置目录。
- 2026-09-16 检查 Windows 时间服务报告未同步，`w32tm /resync` 返回拒绝访问。需在 Windows“日期和时间”设置中完成同步；本地节拍测试不能证明本机整点与服务器时间一致。
- 保持电脑唤醒和网络可用；当前脚本只运行一个窗口，不自动配置每日任务。
