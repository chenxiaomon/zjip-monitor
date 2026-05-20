# zjip-monitor

浙江省数据知识产权登记平台监控工具。项目可以用多个企业账号登录 `zjip.org.cn`，定时拉取登记申请状态，生成快照、变更事件和 HTML 报表，并通过钉钉、企业微信或邮件推送通知。

当前还包含一个轻量 Web 控制台，使用 FastAPI、Jinja2、HTMX 和 SSE 展示仪表盘、账号、记录、变更、通知、报表与设置页面。

## 功能

- Playwright 自动登录并缓存 token
- 分页拉取登记记录并按状态码映射展示
- 对比历史快照，输出新增、变更、移除事件
- 生成 HTML 汇总报表
- 支持命令行一次运行、定时守护进程和 Web 控制台
- Web 控制台带 HTTP Basic Auth，默认监听 `127.0.0.1:8000`

## 本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

复制配置模板：

```bash
cp .env.example .env
cp config/settings.yaml.example config/settings.yaml
cp config/accounts.yaml.example config/accounts.yaml
```

填写 `.env`、`config/settings.yaml` 和 `config/accounts.yaml` 后，加密账号配置：

```bash
python scripts/encrypt_accounts.py
```

加密完成后可以删除明文账号文件：

```bash
rm config/accounts.yaml
```

运行一次巡检：

```bash
python run_once.py
```

启动定时巡检：

```bash
python run_daemon.py
```

启动 Web 控制台：

```bash
python run_web.py
```

浏览器访问 `http://127.0.0.1:8000`，默认 Basic Auth 账号密码来自 `.env` 的 `WEB_USER` / `WEB_PASS`。

## 不进仓库的文件

以下文件包含本机配置、账号、token 或运行数据，已在 `.gitignore` 中排除：

- `.env`
- `config/accounts.yaml`
- `config/accounts.enc`
- `config/settings.yaml`
- `data/*`
- `logs/`

GitHub 仓库只保留代码、模板和示例配置。
