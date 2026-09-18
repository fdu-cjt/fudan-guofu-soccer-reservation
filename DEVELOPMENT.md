# 开发记录

> 个人使用。当前整理时间：2026-09-18。

## 当前使用版本

仓库：<https://github.com/fdusjtu/fudan-guofu-soccer-reservation>

| 名称 | 类型 | 指向 | 用途 |
| --- | --- | --- | --- |
| `main` | 本地/远端分支 | 当前 HEAD | 当前唯一使用版本；本地 `main` 与 GitHub `origin/main` 一致 |
| `pre-retry` | 本地/远端标签 | `7c1d9e5` | 加入“超时后额外重发一次”之前的回滚点 |

功能代码的最近三个提交是 `8706338`、`2ba8d61`、`26aa6b7`；它们是 `main` 的连续提交，不是三个需要单独使用的版本：

1. `8706338`：加入超时后最多重发一次。
2. `2ba8d61`：先把超时核实改为 3 轮。
3. `26aa6b7`：把所有结果核实统一为 3 轮，当前版本。

## 本地实验分支

| 分支 | 提交 | 用途 | 是否当前使用 |
| --- | --- | --- | --- |
| `concurrent-test` | `f52f6f9` | 旧的并行预约测试逻辑 | 否，仅本地保留 |
| `room` | `210749b` | 讨论室预约代码 | 否，仅本地保留 |

GitHub 当前只发布 `main`；`concurrent-test` 和 `room` 没有对应的远端分支。不要在这两个分支上启动日常预约。

## 历史标签

以下标签只用于保留过去的发布快照，不是当前运行版本：

| 标签 | 提交 | 含义 |
| --- | --- | --- |
| `stable-2026-09-17` | `9830163` | 之前发布的稳定快照 |
| `2min-2026-09-17` | `a367080` | 旧的 2 分钟提交窗口版本 |
| `baseline-2026-09-17` | `e158709` | 最早保留的基线 |

## 当前足球预约逻辑

- 不加 `--time`：预约时段 1 和 2，当前代码对应 `18:00–19:00`、`19:00–20:00`。
- 加 `--time 1` 或 `--time 2`：只预约指定时段。
- 两个时段共用一个账号级提交队列；两次 POST 开始之间至少间隔 2 秒。
- 同一时段已有请求在等待确认时，不会并发提交同一时段的第二笔请求。
- 每次提交后最多核实本人预约记录 3 轮。
- 如果发生真正的请求超时：先核实；核实不到时，同一时段最多额外重发 1 次，仍不确定就停止该时段。
- 服务器明确返回可重试的业务错误时，按至少 2 秒延迟重试；限流、登录失效或访问阻断会进入暂停/恢复流程。
- 日志按日期追加到 `logs/soccer-YYYY-MM-DD.log`，不会覆盖前一天日志。

## 常用命令

```powershell
# 使用当前版本
git switch main
git pull --ff-only origin main
uv sync --locked

# 只检查登录、表单和时段，不提交
uv run --locked python -m automations.soccer --check

# 默认预约两个时段
uv run --locked python -m automations.soccer

# 只预约一个时段
uv run --locked python -m automations.soccer --time 1

# 运行测试
uv run --locked python -B tests\test_soccer.py
```

## 修改和回滚规则

1. 修改前先确认：`git status --short --branch`。
2. 账号、密码、手机号、浏览器配置和日志只放在 `.local/`、本地浏览器目录或 `logs/`，不要 `git add`。
3. 普通修改直接在 `main` 完成；确认测试通过后提交并推送：

   ```powershell
   git add -- <明确的代码文件>
   git commit -m "简短说明"
   git push origin main
   ```

4. 需要查看旧逻辑时使用 `pre-retry`，不要重置或覆盖 `main`：

   ```powershell
   git switch --detach pre-retry
   # 查看或测试后回到当前版本
   git switch main
   ```

5. 小修改不再单独创建新分支或新标签；只有需要长期回滚的稳定状态才创建标签。
