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

## 账号文件与加密密钥

运行时读的是加密文件 `config/accounts.enc`，密钥是 `.env` 里的 `FERNET_KEY`。
生产的 systemd 服务通过 `EnvironmentFile=/opt/zjip-monitor/.env` 拿到同一把密钥。

```bash
python scripts/encrypt_accounts.py              # accounts.yaml → accounts.enc
python scripts/decrypt_accounts.py              # 查看现有账号（密码打码）
python scripts/decrypt_accounts.py --out config/accounts.yaml   # 导出明文以便编辑（0600）
```

几条必须遵守的规则：

- **跑加密脚本前先 `unset FERNET_KEY`**。`load_dotenv` 不会覆盖 shell 里已有的变量，
  所以残留的 `FERNET_KEY`（哪怕是空值）会让脚本看到的密钥和生产进程不一致。现在脚本
  会直接报错退出；`.env` 里已有密钥时它绝不会改写。
- **不要拿一份过期的 `accounts.yaml` 重跑加密脚本**。控制台里新增或改密的账号只存在于
  `accounts.enc`。正确做法是先 `decrypt_accounts.py --out config/accounts.yaml` 导出当前
  内容再编辑。脚本在覆盖前会解密旧文件并打印新增/移除/密码有变化的摘要（不回显密码），
  终端里需要输入 `yes` 才继续；旧文件保留为 `accounts.enc.bak`。
- **不要把 `decrypt_accounts.py` 的默认输出当成 accounts.yaml 回灌**——里面的密码是
  `****xx` 掩码。脚本会识别并拒绝，但别依赖这道保险。
- 导出的明文文件用完立刻 `rm`。`--out` 拒绝写入符号链接。
- YAML 里纯数字的账号或密码要加引号（`password: "01234567"`），否则会被解析成数字而拒收。

## 配置

`config/settings.yaml` 是唯一的运行配置，完整说明见 `config/settings.yaml.example`
（每个键都有中文注释）。不写的键一律用默认值，默认值就是改成可配置之前代码里的字面量。

- 大部分键保存后**下一次巡检 / 下一次刷新页面即生效**，不用重启服务。
  两个例外：`schedule.crons`（调度表只在 daemon 启动时读一次）和日志保留天数。
- 数值键填错（字符串、带引号的数字、负数、`true`）会回退到默认值并在日志里提示一次，
  不会让服务起不来。
- **cron 表达式按北京时间解释**，与服务器系统时区无关；服务器系统时区是 UTC，
  所以日志时间戳比 cron 里写的早 8 小时。
- 在控制台「设置」页保存任何一项，都会用程序重写这个文件，里面的注释会全部消失
  （值不受影响）。想看说明请对照 `settings.yaml.example`。

## 磁盘与数据保留

生产服务器根分区只有 9.6G，`data/attachments/` 是唯一无上限增长的目录。策略是
**测量 + 告警 + 停止新增，绝不自动删除任何通知书或证书**：

- 剩余空间低于 `disk.min_free_mb`（默认 500MB）时跳过附件下载，已跳过的记录不写
  manifest，下一轮会重试。
- 每轮巡检结束时清理：每家企业保留最近 `retention.snapshots_keep_per_company` 份快照、
  报表保留 `retention.reports_keep_days` 天、`events.jsonl` 超过 `retention.events_rotate_mb`
  就轮转并只留最近几份、清扫超过 1 小时的 `.part` 下载残留。
- 清理在巡检的**每一条出口**执行（正常完成、被取消、出错、单账号巡检），因为磁盘最紧张的
  时候恰恰是巡检失败的时候；清理全程持有全局运行锁，不会和另一个进程的写入打架。
- 每轮结束写一份 `data/control/disk_stats.json`（各目录用量与剩余空间），控制台横幅与
  `/health` 都读它。

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

## 部署到服务器

服务器上没有 git，代码靠 `./deploy.sh` 用 rsync 推送。详细说明见 `deploy/README.md`。

```bash
./deploy.sh status     # 看服务状态、当前版本、/health、磁盘、备份列表
./deploy.sh dry-run    # 跑本地测试 + 列出将传输的文件，不改服务器
./deploy.sh            # 正式部署
./deploy.sh rollback   # 回滚到最近一份备份（默认只恢复代码）
```

`deploy` 的每一步：本地工作区检查与全量 pytest（不通过就中止）→ 远端预检（目录、venv、
剩余磁盘 ≥ 200MB）→ 把服务器上的代码打包成 `code-<时间>-pre-<新sha>.tgz` 备份（只留最近
3 份，权限 600）→ rsync → 写 `RELEASE` 版本文件 → `pip install -r requirements.lock` →
导入冒烟 → 重启两个服务 → `/health?strict=1` 健康检查。任何一步失败都会停下并说明服务器
当前处于什么状态、怎么回滚。

几个要点：

- **备份名里的 sha 是即将部署的新版本，内容是被它替换掉的旧代码**。
- **回滚默认只覆盖代码**，不动服务器上的 `.env` 与 `config/`（否则会把部署之后在控制台改过的
  账号密码和设置一起退回去）。真正的灾难恢复用 `./deploy.sh rollback --with-config`。
- 备份里**含 `.env` 明文口令**，所以备份目录 700、文件 600。
- 默认**不带 `--delete`**。要用就 `DEPLOY_DELETE=1 ./deploy.sh`，它会先列出将被删除的远端
  文件并要求输入 `yes`。
- 健康检查：200 = 正常；`/health` 不存在（404/302/401/403）时退回检查登录页；**503 = 服务
  起来了但系统自评 degraded**，只警告不算部署失败；连不上或其它 5xx 才算失败。
- `requirements.lock` 是生产 venv（Linux / Python 3.12）的 pip freeze，**不要在本地
  macOS / Python 3.10 上装**。服务器上升级过依赖后跑 `./deploy.sh lock` 重新生成并提交。
- systemd unit 模板在 `deploy/`，改动后用 `./deploy.sh install-units`（先 diff、再确认、
  不自动重启）。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` 已设置 `testpaths=tests` 与 `pythonpath=.`，所以裸 `pytest` 即可运行，
`scripts/test_login.py`（手动排障脚本，不是测试）不会被收集。测试依赖只在
`requirements-dev.txt` 中，`deploy.sh` 在服务器上安装的是 `requirements.lock`
（生产 venv 的 pip freeze），测试依赖不会进生产环境。

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
