# deploy/ — 部署流水线的配套文件

服务器上没有 git，代码靠 `../deploy.sh` 用 rsync 推送；这个目录放它需要的模板。

| 文件 | 用途 |
| --- | --- |
| `deploy.env.example` | `deploy.sh` 的配置模板（服务器地址、代码目录、备份目录、保留份数）。复制为 `deploy.env` 后生效；`deploy.env` 已 gitignore，也不会被 rsync 到服务器 |
| `zjip-web.service` | 生产机 `/etc/systemd/system/zjip-web.service` 的模板（uvicorn 加载 `web.app:app`，监听 127.0.0.1:8000） |
| `zjip-daemon.service` | 生产机 `/etc/systemd/system/zjip-daemon.service` 的模板（`python run_daemon.py`） |

两份 unit 与生产机现有文件语义一致，只多了注释；改动后用 `./deploy.sh install-units` 安装：
它会先把本地文件和服务器上的 diff 出来、要求输入 `yes`，再 `install` + `daemon-reload`，
**不会**自动重启服务（之后跑一次 `./deploy.sh` 或手动 `systemctl restart zjip-web zjip-daemon`）。

## 首次使用

```bash
cp deploy/deploy.env.example deploy/deploy.env   # 按需改 SERVER / REMOTE
./deploy.sh status                                # 看服务、RELEASE、/health、磁盘、备份
./deploy.sh dry-run                               # 本地测试 + 列出将传输的文件，不改服务器
./deploy.sh                                       # 正式部署
```

## 子命令

| 命令 | 作用 |
| --- | --- |
| `deploy`（默认） | 本地 pytest → 远端预检（磁盘 ≥ 200MB）→ 备份代码目录 → rsync → 写 RELEASE → `pip install -r requirements.lock` → 导入冒烟 → 重启两个服务 → `/health?strict=1` 健康检查。任何一步失败立刻停止并说明服务器现状 |
| `dry-run` | 只跑本地闸门 + `rsync -n`（列出将传输的文件）+ 打印将执行的远端步骤 |
| `rollback [备份名]` | 回滚到某份备份（默认最新）：解包覆盖代码目录（不动 `data/ logs/ .venv/`），装依赖、冒烟、重启、健康检查。执行前要求 `yes` |
| `status` | 远端 `systemctl is-active`、RELEASE、`/health` JSON、`df -h /`、备份列表 |
| `lock` | 把远端 `pip freeze` 写回本地 `requirements.lock` |
| `install-units` | 把 `deploy/*.service` 安装到 `/etc/systemd/system/`（先 diff，再确认，`daemon-reload`，不重启） |
| `help` | 用法 |

选项：`--allow-dirty`（工作区有未提交改动仍部署）、`--skip-tests`（跳过本地 pytest）、`--yes`（跳过交互确认）。
环境变量：`DEPLOY_DELETE=1`（rsync 加 `--delete`，先预览要删的远端文件并确认；排除清单里的 `.env`、`config/*.yaml`、`data/` 等受保护不会被删）、`DEPLOY_SSH_CMD`、`DEPLOY_ENV`。

## 备份与回滚

- 每次 `deploy` 先在服务器上 `tar czf BACKUP_DIR/code-<UTC时间>-<git短sha>.tgz`，内容是代码目录里除 `data/ logs/ .venv/ .pytest_cache/ __pycache__` 之外的一切——包括 `.env`、`config/`（`settings.yaml`、`accounts.enc`）、`requirements.lock`、`RELEASE`，都是回滚所需且很小。
- 只保留最近 `KEEP_BACKUPS` 份（默认 3）。
- `rollback` 是解包覆盖：部署新增的文件不会被删掉（旧代码不引用它们，无害）；备份里的 `.env` / `config/` 也会一并恢复到备份时刻，回滚前脚本会提示这一点。

## requirements.lock

生产 venv 的 `pip freeze`（Linux / Python 3.12）。`deploy.sh` 在服务器上执行 `pip install -r requirements.lock`，
保证每次部署装的都是同一组版本；本地开发装 `requirements-dev.txt`，不要在 macOS / Python 3.10 上装 lock。
服务器上升级过依赖后跑 `./deploy.sh lock` 重新生成并提交。
