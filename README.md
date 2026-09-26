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
- 控制台顶部**运维横幅**：失败账号过多、长时间无成功巡检、磁盘告急、附件超量、巡检进程卡死、定时巡检暂停、公司名脱敏后重名
- **磁盘守护**：剩余空间低于阈值时停止下载附件；每轮巡检结束清理过期快照/报表并写一份用量快照
- **`GET /health`** 健康检查端点（免鉴权，`?strict=1` 在系统 degraded 时返回 503），供部署脚本与外部探针使用
- 三个进程统一的日志配置，控制台进程也落盘到 `logs/web_YYYY-MM-DD.log`

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

## 运维横幅与健康检查

三个通知渠道（钉钉/企业微信/邮件）默认全部关闭，所以运维告警的出口是**控制台顶部横幅**
和日志。七项检查：

| 检查 | 触发条件 | 阈值来源 |
|---|---|---|
| 失败账号过多 | 本轮 status 不是 ok/partial 的账号数达到阈值 | `ops_alerts.min_failed_accounts` |
| 长时间无成功巡检 | 所有账号最近一次成功距今超过阈值，或从未成功过 | `ops_alerts.stale_hours` |
| 磁盘告急 | 剩余空间低于阈值（附件下载已暂停） | `disk.min_free_mb` |
| 附件目录超量 | `data/attachments/` 总量超过阈值 | `attachments.max_total_mb` |
| 巡检进程卡死 | 持锁进程还活着但心跳已停 | — |
| 定时巡检已暂停 | 控制台里主动暂停了 | — |
| 公司名重名 | 两家公司脱敏后同名，快照与附件会互相覆盖 | — |

- 告警去重记在 `data/control/ops_alerts.json`：新激活时打一条 critical/warning 日志，
  恢复时打一条 info，持续存在的不再刷屏。日志里搜 `运维告警[` 可查全部激活/恢复记录。
  这个文件可以安全删除，删掉后下一轮会重新激活并重打日志。
- 分组账号的横幅只统计自己能看到的企业；磁盘、卡死、暂停这类系统级告警对所有人可见。
- 之后如果启用了任一通知渠道，同样的告警会自动推送，不需要额外配置。

`GET /health` 返回 JSON：版本（来自服务器上的 `RELEASE`）、控制台启动时间、最近一轮巡检、
上次成功巡检距今小时数、各状态计数、磁盘用量，以及 `checks[]` 逐项结论。

- **它是免鉴权的公开端点**（服务器只有 IP、nginx 只开 80 明文），所以响应里只有计数、时间、
  状态码和阈值，**绝不能往里加企业名、用户名、错误原文或文件路径**——有测试专门拦这件事。
- 不带参数永远返回 200（方便直接 curl 读 JSON）；`?strict=1` 才会在任一检查不通过时返回 503。
- degraded 不等于部署失败：`deploy.sh` 收到 503 会打印 JSON 并警告，仍算部署成功。
  排查看 `checks[]` 里 `ok: false` 那一项的 `detail`。
- 全新安装在第一轮巡检成功之前，`?strict=1` 会返回 503（`recent_success` 为红），属预期。

## 日志

三个进程共用 `src/logging_setup.py`：

| 进程 | 文件 | 保留 |
|---|---|---|
| `run_daemon.py` / `run_once.py` | `logs/run_YYYY-MM-DD.log` | `retention.logs_keep_days`（默认 30 天） |
| 控制台（`uvicorn web.app:app`） | `logs/web_YYYY-MM-DD.log` | `retention.web_logs_keep_days`（默认 14 天） |

文件日志是 DEBUG 级、每天 0 点轮转；stderr 是 INFO 级，只有连着终端时才上色（journald 里
不再有转义码）。uvicorn 与 APScheduler 的日志也汇进同一份文件。
`ZJIP_LOG_TO_FILE=0` 只输出到 stderr，`ZJIP_LOG_DIR` 改目录。

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

## 本机归档（archive）

服务器上的 `data/` 只是工作缓存：快照每家只留 30 份、事件日志满 5MB 就轮转，被新一轮替换掉的旧通知书平台上也不再提供。
`archive/` 是**只在本机运行**的归档工具，把这些都拉回本地长期保存，并建成一个可查询的 SQLite 库。

```bash
cp archive/archive.env.example archive/archive.env   # 可选：改服务器地址或归档目录（默认 ~/zjip-archive）
pip install -r requirements-dev.txt                   # 需要 pypdf（只装在本机）
python -m archive pull     # rsync 拉取附件、快照、事件日志
python -m archive index    # 增量更新 ~/zjip-archive/archive.sqlite
```

- **只增不减**：`pull` 永远不带 `--delete`。服务器删掉的快照、轮转掉的日志，本地都留着；服务器原地改写过的文件，
  rsync 会先把本地旧版挪到 `.overwritten/<时间>/`。归档目录是正本，请纳入 Time Machine 等备份。
- `archive.sqlite` 里有：`records`（每条登记申请的首次出现时间与最新状态）、`status_events`（全部状态变化）、
  `documents`（每个文件的哈希，以及从正文解析出的落款日、补正截止日、补正理由、公告里的证书有效期、证书编号等）、
  `documents_fts`（正文全文检索，3 个字以上用 `MATCH`，更短的词对 `documents.text` 用 `LIKE`）。
- 例：`sqlite3 ~/zjip-archive/archive.sqlite "select reg_no, deadline, reasons from documents where doc_kind='补正' order by deadline desc limit 5"`

**释放服务器空间**（本机有了经过哈希核对的副本后，把旧文件从服务器删掉）：

```bash
python -m archive pull && python -m archive index   # 先同步，保证本机副本是最新的
python -m archive release                          # 预演：列出可释放的文件与合计，服务器上什么都不删
python -m archive release --company 某企业目录 --yes # 先小范围正式释放
python -m archive release --yes                    # 全量
python -m archive restore 企业目录 登记号           # 客户要旧证书时：把这条申请的文件送回服务器
```

- 哪些可以释放：已办结（登记成功/不予登记/撤回等）且平台 `archive.release_after_terminal_days`（默认 90）天没动过的申请的
  全部文件，以及被新一轮替换下来的旧通知书。还在办理中的申请一个文件都不动。
- 服务器端 `scripts/archive_release.py` 不信任本机发来的任何东西：持巡检锁、只接受 `data/attachments/` 下的文件、
  逐个重算 sha256 与本机副本比对、按**服务器自己**的最新快照和 settings.yaml 复核释放条件，全部通过才删。
- 删除前先在该记录的 `.sync.json` 里记下 `archived`，所以之后的巡检不会把它重新下载回来；控制台登记记录页显示为
  灰色「已归档」标记（带归档日期），不再是下载链接。

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
