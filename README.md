# 闲鱼运营工作台

一个本地运行的闲鱼运营辅助工作台，用来把商品采集、历史趋势、自动发布队列、自动回复、自动发货和风控审计放到同一个界面里管理。

项目默认使用 Mock 适配器，不请求真实平台接口；真实采集通过 Playwright 适配器接入。所有登录态、数据库和浏览器缓存都只保存在本机 `data/` 目录，并已被 `.gitignore` 排除，不应该上传到仓库。

## 功能概览

- 商品采集：按关键词采集商品池，保存价格、卖家、地区、想要数、浏览数、销量字段和搜索排名。
- 历史趋势：每次采集写入 `market_snapshots`，可按关键词、商品 ID、时间范围查看价格和热度变化。
- 自动发布：保留草稿、确认模式、全自动模式、任务队列、发布上限和审计日志。
- 自动回复：关键词规则、模拟来信、自动回复日志。
- 自动发货：发货规则、模拟付款触发、自动确认开关、发货任务队列。
- 风控审计：全局暂停、账号暂停、风险事件、审计日志。
- 登录向导：使用 Playwright 打开专用浏览器窗口，手动登录后把 storage state 加密保存到本地账号。

## 目录结构

```text
.
├── app.py                    # 标准库 HTTP 服务、SQLite、队列和 API
├── start.ps1                 # Windows 快速启动脚本
├── adapters/
│   ├── base.py               # Adapter 接口和异常
│   ├── mock_goofish.py       # 本地模拟适配器
│   └── real_goofish.py       # Playwright 真实采集适配器
├── services/
│   └── collector.py          # 商品字段归一化
├── static/
│   ├── index.html            # 前端页面
│   ├── app.js                # 前端交互
│   └── styles.css            # 前端样式
├── .gitignore                # 排除 data、数据库、缓存、环境变量
├── LICENSE
└── README.md
```

运行后会生成：

```text
data/
├── workbench.sqlite3         # 本地数据库
├── local_secret.key          # 本地加密 key
└── login-capture/            # 登录向导专用浏览器 profile
```

这些文件包含本地状态或敏感信息，不要提交到 GitHub。

## 环境要求

- Windows / PowerShell
- Python 3.11 或更高版本
- 真实采集需要 Playwright
- 推荐安装 Chrome、Edge 或 115 浏览器

安装 Playwright：

```powershell
python -m pip install playwright
python -m playwright install chromium
```

如果已经安装 Chrome、Edge 或 115 浏览器，工具会优先尝试复用系统浏览器。也可以显式指定：

```powershell
$env:GOOFISH_BROWSER_PATH="E:\tools\115Chrome\Application\115chrome.exe"
```

## 启动

进入项目目录：

```powershell
cd C:\path\to\xianyu-ops-workbench
```

Mock 模式：

```powershell
$env:GOOFISH_ADAPTER="mock"
python app.py
```

真实采集模式：

```powershell
$env:GOOFISH_ADAPTER="real"
python app.py
```

打开：

```text
http://127.0.0.1:8765
```

## 快速试用

1. 启动 Mock 模式。
2. 打开 `http://127.0.0.1:8765`。
3. 点击“生成演示数据”。
4. 到“采集统计”输入关键词，点击“运行采集”。
5. 到“趋势分析”选择关键词和时间范围，查看价格、想要、浏览和销量趋势。

## 真实采集

真实采集集中在：

```text
adapters/real_goofish.py::search_items
```

当前实现会使用 Playwright 打开：

```text
https://www.goofish.com/search?q=关键词
```

并监听搜索相关 JSON response，解析商品数据后写入本地数据库。

### 登录态导入方式

推荐用页面里的“登录向导”：

1. 在“账号”页创建或选择一个账号。
2. 点击账号行里的“登录向导”。
3. 工具会打开一个专用浏览器窗口。
4. 你在这个专用窗口里手动登录闲鱼。
5. 登录完成后点击“保存登录态”。
6. 工作台会把该专用窗口的 Playwright `storage_state` 加密保存到本地账号。

也可以手动把 `goofish.com` Cookie 字符串或 Playwright storage state JSON 粘贴到“登录态 / Cookie”。

### 真实采集边界

工具不会自动绕过：

- 验证码
- 滑块
- 手机验证
- 登录失效
- 平台风控页

检测到这些情况时，真实采集会停止并返回错误。需要用户自己处理登录或验证后再继续。

## 历史趋势数据

趋势页使用本地连续采集形成的历史快照，不是平台直接提供的长期历史库。要看某一段时间内的变化，需要对同一关键词或同一商品持续运行采集。

每条快照会保存：

- 关键词、商品 ID、标题、地区、卖家
- 当前价格、原价
- 想要数、浏览数
- 成交/销量字段
- 搜索排名和采集来源
- 采集时间

销量字段只使用接口返回里能识别到的字段，例如：

```text
soldCount
saleCount
tradeCount
salesVolume
soldNum
saleNum
```

如果真实接口没有暴露精确销量，页面不会伪造销量；此时主要看想要数、浏览数、排名和价格变化作为热度趋势代理。

## 自动发布、回复和发货

这些高风险功能保留，但都通过任务队列和审计执行：

- 自动发布走 `publish_jobs`
- 自动发货走 `delivery_jobs`
- 自动回复写入 `messages`
- 风险事件写入 `risk_events`
- 关键动作写入 `audit_logs`

当前真实 adapter 中发布、回复、发货仍是协议接入点，默认会抛出 `NotConfiguredError`。这表示 UI、队列和审计链路已就绪，但真实平台协议尚未接入。

## API 摘要

常用接口：

```text
GET  /api/summary
GET  /api/accounts
POST /api/accounts
PATCH /api/accounts/{id}
POST /api/accounts/{id}/login-capture/start
POST /api/login-capture/{session_id}/status
POST /api/login-capture/{session_id}/save
POST /api/login-capture/{session_id}/close
POST /api/collector/run
GET  /api/items
GET  /api/trends
POST /api/publish-drafts
POST /api/publish-jobs
POST /api/messages/simulate
POST /api/delivery-jobs/simulate
GET  /api/risk-events
GET  /api/audit-logs
```

趋势接口示例：

```text
/api/trends?keyword=相机&days=30&bucket=day
```

## 安全说明

- 本项目是本地单机工具，不提供公网部署方案。
- 不要把 `data/` 目录提交到 GitHub。
- 不要把 Cookie、storage state、数据库或 `local_secret.key` 发给他人。
- 不实现验证码、滑块、手机验证或平台风控绕过。
- 使用真实平台接口前，请自行确认账号、平台规则和业务合规边界。

## 开发说明

语法检查：

```powershell
python -m py_compile app.py adapters\__init__.py adapters\base.py adapters\mock_goofish.py adapters\real_goofish.py services\collector.py
node --check static\app.js
```

停止后重新启动：

```powershell
$env:GOOFISH_ADAPTER="real"
python app.py
```

## 许可证

MIT
