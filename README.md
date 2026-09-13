# zjip-monitor

浙江省数据知识产权登记平台监控工具。项目可以用多个企业账号登录 `zjip.org.cn`，定时拉取登记申请状态，生成快照、变更事件和 HTML 报表，并通过钉钉、企业微信或邮件推送通知。

当前还包含一个轻量 Web 控制台，使用 FastAPI、Jinja2、HTMX 和 SSE 展示仪表盘、账号、记录、变更、通知、报表与设置页面。

## 功能

- HTTP 直登（POST /v1/user/login，密码 md5）并缓存 token；网络或响应异常时自动回退到 Playwright 浏览器登录
- 分页拉取登记记录并按状态码映射展示；翻页不信任服务端 totalSize（以空页/短页/无新记录为准），中途页面出错保留已抓部分并与上份快照合并，不产生假「记录消失」
- 对比历史快照，输出新增、变更、移除事件
- 生成 HTML 汇总报表
- 支持命令行一次运行、定时守护进程和 Web 控制台
- Web 控制台带 HTTP Basic Auth，默认监听 `127.0.0.1:8000`；「登记记录」页支持按数据名称 / 登记号 / 公司关键字搜索
- 巡检/登录结果持久化到 `data/control/accounts_status.json`：定时巡检失败、部分抓取、平台总数与本地不一致都会在界面上标出

## 账号管理与改密码

- 控制台「账号」页每行有 **编辑** 按钮：可直接修改密码（留空 = 不改）、联系人与分组；公司名称与账号与历史数据绑定，不可修改
- 新增/编辑弹窗内的 **测试登录** 按钮会用表单中填写的账号密码向平台试登录一次（不保存、不写 token 缓存），先验证再保存
- 改密码后会自动清除旧 token 缓存并后台验证一次，结果直接显示在账号行
- **防锁号**：同一账号连续 N 次（`settings.yaml → login.max_consecutive_failures`，默认 3）被平台拒绝登录后，定时巡检自动跳过该账号并推送通知，界面标黄「已暂停 · 需更新密码」；在界面改密码或手动点「巡检」成功后自动恢复
- 收缩卫兵拒绝保存时（记录数骤减超 50%），若确认属实，可 `touch data/control/force_save/<账号>.flag` 放行下一次巡检

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

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` 已设置 `testpaths=tests` 与 `pythonpath=.`，所以裸 `pytest` 即可运行，
`scripts/test_login.py`（手动排障脚本，不是测试）不会被收集。测试依赖只在
`requirements-dev.txt` 中，`deploy.sh` 只安装 `requirements.txt`，不会进生产环境。

测试用例自带隔离：账号状态文件、控制目录、settings.yaml、快照目录都指向临时路径，
Web 用例通过 `tests/conftest.py` 固定的测试账号登录，不会读写真实数据。

## 不进仓库的文件

以下文件包含本机配置、账号、token 或运行数据，已在 `.gitignore` 中排除：

- `.env`
- `config/accounts.yaml`
- `config/accounts.enc`
- `config/settings.yaml`
- `data/*`
- `logs/`

GitHub 仓库只保留代码、模板和示例配置。
