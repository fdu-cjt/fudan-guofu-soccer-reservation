# 复旦大学国福路足球场预约

## 安装

适用于 Windows / PowerShell。请先安装 Git、uv、Node.js（含 npm）和 Chrome 或 Edge。

```powershell
git clone https://github.com/fdusjtu/fudan-guofu-soccer-reservation.git
cd fudan-guofu-soccer-reservation
uv sync --locked
npm ci --ignore-scripts --no-audit --no-fund
```

## 配置账号

请在 `.local/credentials.env` 中填写 UIS 账号、密码和手机号：

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
notepad .local/credentials.env
```

在打开的 `.local/credentials.env` 中填写并保存：

```dotenv
FUDAN_USER=你的UIS账号
FUDAN_PASSWORD=你的UIS密码
FUDAN_PHONE=你的手机号
```

## uv 启动

```powershell
# 检查登录，不提交预约
uv run --locked python -m automations.soccer --check

# 正式启动，默认预约 18–19 点和 19–20 点
uv run --locked python -m automations.soccer

# 只预约一个时段：1 = 18–19 点，2 = 19–20 点
uv run --locked python -m automations.soccer --time 1
```

启动后先尝试已有空位，再每天早上 07:00–07:02 尝试预约放号日后两天的场地。

运行日志按日期追加写入 `logs/soccer-YYYY-MM-DD.log`。
