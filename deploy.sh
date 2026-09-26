#!/bin/bash
# 部署流水线（服务器无 git，代码走 rsync）：
#   本地闸门 → 远端预检 → 备份 → rsync → RELEASE → 装依赖 → 冒烟 → 重启 → 健康检查
#
# 用法：./deploy.sh [子命令] [选项]
#   deploy（默认）    完整部署；任何一步失败立刻停止并说明服务器现状
#   dry-run           只跑本地闸门 + rsync -n 列出将传输的文件 + 打印将执行的远端步骤，不改服务器
#   rollback [备份名]  回滚到某份备份（默认最新）：默认只恢复代码，不动 .env / config/ / data/ / logs/ / .venv/
#   status            远端服务状态 / RELEASE / /health / 磁盘 / 备份列表
#   lock              把远端 pip freeze 写回本地 requirements.lock
#   install-units     把 deploy/*.service 安装到 /etc/systemd/system/（先 diff，输入 yes 后 daemon-reload，不重启）
#   help              本说明
# 选项：
#   --allow-dirty     工作区有未提交改动时仍继续（deploy 默认中止；dry-run 只警告）
#   --skip-tests      跳过本地 pytest 闸门
#   --yes / -y        rollback / install-units / --delete 预览不再交互确认
#   --with-config     仅 rollback：连 .env 与 config/（settings.yaml、accounts.enc）一起恢复到备份时刻
#                     （灾难恢复用；默认只恢复代码，以免抹掉部署之后在控制台里改过的账号密码与设置）
# 环境变量：
#   DEPLOY_ENV        配置文件路径，默认 deploy/deploy.env（不存在时用脚本内默认值，模板见 deploy/deploy.env.example）
#   DEPLOY_DELETE=1   rsync 加 --delete：先 rsync -n 打印将删除的远端文件，输入 yes 才真正执行
#   DEPLOY_SSH_CMD    替换 ssh 命令（默认 ssh，可带参数；测试用假 ssh 注入）
#
# 只同步代码，不覆盖服务器上的 .env / config/*.yaml / accounts.enc / data/ / logs/ / .venv/。

set -euo pipefail

LOCAL="$(cd "$(dirname "$0")" && pwd)"

# ---- 配置：默认值 = 旧脚本的字面量；deploy/deploy.env 可覆盖 ----
SERVER="root@104.249.172.32"
REMOTE="/opt/zjip-monitor"
BACKUP_DIR="/opt/zjip-monitor-backups"
KEEP_BACKUPS=3
MIN_FREE_MB=200
DEPLOY_ENV="${DEPLOY_ENV:-$LOCAL/deploy/deploy.env}"
if [ -f "$DEPLOY_ENV" ]; then
  # shellcheck disable=SC1090
  . "$DEPLOY_ENV"
fi
SSH_CMD="${DEPLOY_SSH_CMD:-ssh}"

# ---- 输出样式（沿用 ▶ ✓ ✗；非终端时不上色）----
if [ -t 1 ]; then
  C_STEP=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_RESET=""
fi
step() { printf '%s▶ %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '%s✓ %s%s\n' "$C_OK" "$*" "$C_RESET"; }
warn() { printf '%s! %s%s\n' "$C_WARN" "$*" "$C_RESET" >&2; }
fail() { printf '%s✗ %s%s\n' "$C_ERR" "$*" "$C_RESET" >&2; exit 1; }

# KEEP_BACKUPS must be >= 1. The trim step deletes everything past line KEEP+1
# of a newest-first listing, so 0 would wipe the backup that was just taken —
# exactly the one a rollback needs — leaving no way back.
case "$KEEP_BACKUPS" in
  ''|*[!0-9]*) fail "KEEP_BACKUPS 必须是正整数（≥1），当前：$KEEP_BACKUPS" ;;
esac
[ "$KEEP_BACKUPS" -ge 1 ] || fail "KEEP_BACKUPS 必须是正整数（≥1），当前：${KEEP_BACKUPS}（0 会把刚做的备份一起删掉）"

# ---- 参数 ----
CMD=""; ARG=""; ALLOW_DIRTY=0; SKIP_TESTS=0; ASSUME_YES=0; WITH_CONFIG=0
for a in "$@"; do
  case "$a" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --skip-tests)  SKIP_TESTS=1 ;;
    --yes|-y)      ASSUME_YES=1 ;;
    --with-config) WITH_CONFIG=1 ;;
    -h|--help)     CMD="help" ;;
    -*)            fail "未知选项：${a}（./deploy.sh help 查看用法）" ;;
    *)
      if [ -z "$CMD" ]; then CMD="$a"
      elif [ -z "$ARG" ]; then ARG="$a"
      else fail "多余参数：$a"
      fi ;;
  esac
done
CMD="${CMD:-deploy}"

# ---- 远端执行 ----
# Every ssh round-trip goes through here. The remote script arrives on stdin (a
# heredoc with a quoted delimiter, so nothing expands on the laptop) and the
# values it needs travel as positional parameters. ssh flattens argv into one
# string that the remote login shell re-parses, hence the %q quoting.
remote() {
  local quoted=() arg
  for arg in "$@"; do quoted+=("$(printf '%q' "$arg")"); done
  # shellcheck disable=SC2086  # SSH_CMD may legitimately carry options
  $SSH_CMD "$SERVER" bash -s -- ${quoted[@]+"${quoted[@]}"}
}

confirm() {  # confirm "提示" → 0 继续；--yes 时直接通过
  if [ "$ASSUME_YES" = 1 ]; then echo "  （--yes：已自动确认）"; return 0; fi
  local answer=""
  printf '%s 输入 yes 继续，其他输入取消：' "$1"
  read -r answer || true
  # A piped answer echoes no newline, so the next line would join the prompt.
  [ -t 0 ] || echo
  [ "$answer" = "yes" ]
}

# ---- rsync ----
# Historical exclude list plus: deploy/deploy.env (server address, local-only),
# caches, .claude/ (worktrees live there — huge), and RELEASE (written by the
# server side; must survive --delete). `.git` without the slash also covers a
# worktree checkout, where .git is a file.
# .env.bak and config/accounts.enc.bak exist only on the server (save_accounts
# keeps one generation; the laptop never has them). Without an exclude, --delete
# would remove precisely the files a bad settings edit needs to recover from.
RSYNC_EXCLUDES=(
  --exclude='.venv/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.env'
  --exclude='.env.bak'
  --exclude='config/accounts.enc'
  --exclude='config/accounts.enc.bak'
  --exclude='config/accounts.yaml'
  --exclude='config/settings.yaml'
  --exclude='miniprogram/config.js'
  --exclude='miniprogram/project.private.config.json'
  --exclude='data/'
  --exclude='logs/'
  --exclude='.git'
  --exclude='deploy.sh'
  --exclude='deploy/deploy.env'
  --exclude='archive/archive.env'
  --exclude='.pytest_cache/'
  --exclude='.claude/'
  --exclude='RELEASE'
)

run_rsync() {  # $@ = 额外 rsync 参数（-n、--delete）
  rsync -az -v -e "$SSH_CMD" "${RSYNC_EXCLUDES[@]}" "$@" "$LOCAL/" "$SERVER:$REMOTE/"
}

# ---- 本地闸门 ----
GIT_SHA="nogit"; GIT_SHA_FULL="nogit"; GIT_BRANCH="unknown"

local_gate() {  # $1 = strict|warn：脏工作区时中止，还是只警告
  step "本地闸门 ..."
  if git -C "$LOCAL" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_SHA_FULL="$(git -C "$LOCAL" rev-parse HEAD)"
    GIT_SHA="$(git -C "$LOCAL" rev-parse --short HEAD)"
    GIT_BRANCH="$(git -C "$LOCAL" rev-parse --abbrev-ref HEAD)"
    local dirty
    dirty="$(git -C "$LOCAL" status --porcelain)"
    if [ -n "$dirty" ]; then
      warn "工作区有未提交改动（部署的是磁盘上的文件，RELEASE 记录的却是 HEAD）："
      printf '%s\n' "$dirty" | head -n 20 || true
      if [ "$1" = "strict" ] && [ "$ALLOW_DIRTY" != 1 ]; then
        fail "已中止（服务器未改动）。先提交，或加 --allow-dirty 继续"
      fi
    fi
    echo "  版本 ${GIT_SHA}（${GIT_BRANCH}）"
  else
    warn "$LOCAL 不是 git 仓库：跳过脏检查，版本记为 nogit"
  fi

  if [ "$SKIP_TESTS" = 1 ]; then
    warn "已跳过本地测试（--skip-tests）"
  else
    local py="$LOCAL/.venv/bin/python"
    [ -x "$py" ] || py="python3"
    echo "  本地测试：$py -m pytest -q"
    (cd "$LOCAL" && "$py" -m pytest -q) || fail "本地测试未通过，已中止（服务器未改动）"
  fi
}

# ---- 部署 / 回滚共用的远端步骤 ----
step_pip_install() {  # $1 = 失败时的现状说明
  step "远端安装依赖 ..."
  remote "$REMOTE" <<'EOF' || fail "依赖安装失败：$1"
# step: pip-install
set -euo pipefail
cd "$1"
if [ -f requirements.lock ]; then
  echo "  pip install -r requirements.lock（锁定版本）"
  .venv/bin/pip install -q -r requirements.lock
else
  echo "! 未找到 requirements.lock，退回 requirements.txt（版本未锁定；建议 ./deploy.sh lock 生成）"
  .venv/bin/pip install -q -r requirements.txt
fi
EOF
}

step_smoke() {  # $1 = 失败时的现状说明
  step "导入冒烟测试（失败则不重启）..."
  remote "$REMOTE" <<'EOF' || fail "冒烟测试失败：$1"
# step: smoke
set -euo pipefail
cd "$1"
.venv/bin/python -c 'import sys; sys.path.insert(0, "."); import web.app; from src.main import run_all; import run_daemon'
echo "  导入通过：web.app / src.main.run_all / run_daemon"
EOF
}

step_restart() {
  step "重启 zjip-daemon + zjip-web ..."
  remote <<'EOF' || fail "重启失败：请立刻到服务器上看 systemctl status zjip-web zjip-daemon，或 ./deploy.sh rollback"
# step: restart
set -euo pipefail
systemctl restart zjip-daemon
systemctl restart zjip-web
echo "  已重启 zjip-daemon、zjip-web"
EOF
}

HEALTH_DEGRADED=0

step_health() {  # $1 = 失败时的提示
  step "健康检查（/health?strict=1，最多 15 次 × 2s；两服务 is-active）..."
  # The remote script separates "broken" from "degraded": exit 3 means the new
  # code is up and answering but rates its own business state unhealthy, which
  # a rollback cannot fix — so it must not read as a failed deploy.
  local rc=0
  remote <<'EOF' || rc=$?
# step: health
set -uo pipefail
HEALTH_URL='http://127.0.0.1:8000/health?strict=1'
LOGIN_URL='http://127.0.0.1:8000/login'
healthy=0; degraded=0; code="000"; i=0
while [ "$i" -lt 15 ]; do
  i=$((i + 1))
  # --max-time keeps the advertised "15 × 2s" ceiling true even when the server
  # accepts the connection and then never answers.
  code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL") || code="000"
  case "$code" in
    200) echo "  /health?strict=1 → 200（第 $i 次）"; healthy=1; break ;;
    503) degraded=1; break ;;
    404|302|401|403)
      # No /health route in this build: 404 when it really 404s, 302/401/403
      # when the auth middleware answers the unknown path before routing ever
      # sees it. Settle for the login page answering.
      login=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$LOGIN_URL") || login="000"
      if [ "$login" = "200" ]; then
        echo "  /health?strict=1 → ${code}（这个版本还没有 /health 路由），退回检查 /login → 200，视为健康"
        healthy=1; break
      fi ;;
  esac
  sleep 2
done
if [ "$degraded" = 1 ]; then
  echo "! /health?strict=1 → 503：服务已启动，但系统自评为 degraded（业务状态不健康，回滚修不好）"
  echo "---- /health 完整 JSON ----"
  curl -s --max-time 5 'http://127.0.0.1:8000/health' | head -c 2000; echo
elif [ "$healthy" != 1 ]; then
  echo "✗ /health?strict=1 最后返回 ${code}（000 = 连不上；5xx = 服务起不来）"
  curl -s --max-time 5 'http://127.0.0.1:8000/health' | head -c 2000; echo
fi
active=1
if systemctl is-active --quiet zjip-web; then echo "  zjip-web: active"; else echo "✗ zjip-web 未运行"; active=0; fi
if systemctl is-active --quiet zjip-daemon; then echo "  zjip-daemon: active"; else echo "✗ zjip-daemon 未运行"; active=0; fi
if { [ "$healthy" != 1 ] && [ "$degraded" != 1 ]; } || [ "$active" != 1 ]; then
  echo "---- journalctl -u zjip-web -n 30 ----"
  journalctl -u zjip-web -n 30 --no-pager
  echo "---- journalctl -u zjip-daemon -n 30 ----"
  journalctl -u zjip-daemon -n 30 --no-pager
  exit 1
fi
[ "$degraded" = 1 ] && exit 3
exit 0
EOF
  case "$rc" in
    0) ;;
    3) HEALTH_DEGRADED=1
       warn "服务已启动，但系统自评为 degraded，请到控制台查看（部署本身算成功：回滚改不了业务状态）" ;;
    *) fail "健康检查未通过。$1" ;;
  esac
}

# ---- 子命令 ----
cmd_deploy() {
  local_gate strict
  echo "  目标 $SERVER:$REMOTE"

  step "远端预检（目录 / venv / 磁盘 ≥ ${MIN_FREE_MB}MB / 备份目录）..."
  remote "$REMOTE" "$BACKUP_DIR" "$MIN_FREE_MB" <<'EOF' || fail "远端预检未通过，已中止（服务器未改动，仍在运行旧版本）"
# step: preflight
set -euo pipefail
REMOTE="$1"; BACKUP_DIR="$2"; MIN_FREE_MB="$3"
[ -d "$REMOTE" ] || { echo "✗ 远端目录不存在：$REMOTE"; exit 1; }
[ -x "$REMOTE/.venv/bin/python" ] || { echo "✗ 远端 venv 不存在：$REMOTE/.venv"; exit 1; }
avail_mb=$(df -Pm "$REMOTE" | awk 'NR==2 {print $4}')
echo "  磁盘剩余 ${avail_mb}MB（下限 ${MIN_FREE_MB}MB）"
[ "$avail_mb" -ge "$MIN_FREE_MB" ] || { echo "✗ 磁盘剩余不足，拒绝部署（先清理再来）"; exit 1; }
# Backups contain .env in the clear (FERNET_KEY / WEB_PASS / API_KEY), so the
# directory must be owner-only even if an earlier run left it at 0755.
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
echo "  备份目录 $BACKUP_DIR 就绪（权限 700）"
EOF

  local backup_name
  # "pre-<新 sha>" spells out what the archive holds: the code that was on the
  # server *before* GIT_SHA went out. A name carrying only the new sha reads as
  # "this archive is <sha>" and invites rolling back into the very build that is
  # being replaced.
  backup_name="code-$(date -u +%Y%m%d-%H%M%S)-pre-${GIT_SHA}.tgz"
  step "备份远端代码（部署 ${GIT_SHA} 之前服务器上的代码）→ $BACKUP_DIR/$backup_name ..."
  remote "$REMOTE" "$BACKUP_DIR" "$backup_name" "$KEEP_BACKUPS" <<'EOF' || fail "备份失败，已中止（服务器未改动，仍在运行旧版本）"
# step: backup
set -euo pipefail
REMOTE="$1"; BACKUP_DIR="$2"; NAME="$3"; KEEP="$4"
cd "$REMOTE"
# The archive carries .env in the clear, so it must not land at the default
# 0644 inside a 0755 directory: umask covers the tar and the chmods repair a
# directory or file created before this rule existed.
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
# Top-level entries minus runtime data, caches and the venv. Everything else —
# code, .env, config/ (settings.yaml, accounts.enc), requirements.lock, RELEASE —
# is small and is exactly what a rollback has to put back.
set --
for entry in .[!.]* ..?* *; do
  [ -e "$entry" ] || continue
  case "$entry" in data|logs|.venv|.pytest_cache) ;; *) set -- "$@" "$entry" ;; esac
done
tar czf "$BACKUP_DIR/$NAME" --exclude='__pycache__' --exclude='*.pyc' "$@"
chmod 600 "$BACKUP_DIR/$NAME"
echo "  已备份 ${NAME}（$(du -h "$BACKUP_DIR/$NAME" | cut -f1)，权限 600）——内容是本次部署之前服务器上的代码"
# Names carry a UTC stamp, so a reverse sort is newest-first.
ls -1 "$BACKUP_DIR"/code-*.tgz | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "  已清理旧备份 $(basename "$old")"
done
EOF

  local rsync_delete=()
  if [ "${DEPLOY_DELETE:-0}" = 1 ]; then
    step "预览 --delete 将删除的远端文件（排除清单里的 .env / config / data 等受保护，不会被删）..."
    local preview doomed
    preview="$(run_rsync -n --delete)" || fail "rsync 预览失败（无法连接服务器？）；服务器未改动，仍在运行旧版本"
    doomed="$(printf '%s\n' "$preview" | grep '^deleting ' || true)"
    if [ -n "$doomed" ]; then
      printf '%s\n' "$doomed"
      confirm "以上远端文件将被删除。" || fail "已取消（服务器代码未改动；刚生成的备份 $backup_name 可忽略）"
    else
      echo "  没有需要删除的远端文件"
    fi
    rsync_delete=(--delete)
  fi

  step "rsync 代码 → $SERVER:$REMOTE ..."
  run_rsync ${rsync_delete[@]+"${rsync_delete[@]}"} \
    || fail "rsync 失败：服务未重启（进程仍是旧版本），但磁盘上的代码可能已部分更新；可 ./deploy.sh rollback $backup_name 恢复"

  local on_disk="磁盘代码已更新但服务未重启（进程仍是旧版本）；可 ./deploy.sh rollback $backup_name 恢复"
  local release_line
  release_line="$GIT_SHA_FULL $GIT_BRANCH $(date -u +%Y-%m-%dT%H:%M:%SZ) $(hostname)"
  step "写入 RELEASE ..."
  remote "$REMOTE" "$release_line" <<'EOF' || fail "写 RELEASE 失败：$on_disk"
# step: write-release
set -euo pipefail
printf '%s\n' "$2" > "$1/RELEASE"
echo "  RELEASE: $2"
EOF

  step_pip_install "$on_disk"
  step_smoke "$on_disk"
  step_restart
  step_health "新版本已重启但状态异常；可 ./deploy.sh rollback $backup_name 回滚"

  if [ "$HEALTH_DEGRADED" = 1 ]; then
    ok "部署完成（带警告：系统自评 degraded）：${GIT_SHA}（${GIT_BRANCH}）→ $SERVER:$REMOTE"
  else
    ok "部署完成：${GIT_SHA}（${GIT_BRANCH}）→ $SERVER:$REMOTE"
  fi
  echo "  这份备份是部署 ${GIT_SHA} 之前服务器上的代码：$backup_name"
  echo "  回滚：./deploy.sh rollback ${backup_name}（默认只恢复代码；加 --with-config 才连 .env 与 config/ 一起恢复）"
}

cmd_dry_run() {
  local_gate warn
  local extra=()
  if [ "${DEPLOY_DELETE:-0}" = 1 ]; then extra=(--delete); fi
  step "rsync -n：将传输的文件（→ $SERVER:${REMOTE}，未真正传输）..."
  run_rsync -n ${extra[@]+"${extra[@]}"} || fail "rsync -n 失败（无法连接服务器？）"
  echo
  echo "将执行的远端步骤（dry-run 未执行任何一步，服务器未改动）："
  echo "  1. 预检：$REMOTE 与 .venv 存在；df 剩余 ≥ ${MIN_FREE_MB}MB；mkdir -p ${BACKUP_DIR}（chmod 700）"
  echo "  2. 备份：tar czf $BACKUP_DIR/code-<UTC时间>-pre-${GIT_SHA}.tgz（chmod 600；排除 data/ logs/ .venv/ .pytest_cache/ __pycache__），保留最近 $KEEP_BACKUPS 份"
  echo "     —— 名字里的 pre-${GIT_SHA} 表示「部署 ${GIT_SHA} 之前服务器上的代码」，回滚时选它就回到本次部署前"
  if [ "${DEPLOY_DELETE:-0}" = 1 ]; then
    echo "  3. rsync --delete 上述清单（先确认 deleting 列表）"
  else
    echo "  3. rsync 上述清单（不带 --delete；DEPLOY_DELETE=1 可开）"
  fi
  echo "  4. 写 RELEASE：$GIT_SHA_FULL $GIT_BRANCH <UTC时间> $(hostname)"
  echo "  5. pip install -r requirements.lock（不存在则退回 requirements.txt 并警告）"
  echo "  6. 导入冒烟：web.app / src.main.run_all / run_daemon"
  echo "  7. 重启：systemctl restart zjip-daemon zjip-web"
  echo "  8. 健康检查：/health?strict=1 → 200（15 次 × 2s，每次 --max-time 5；404/302/401/403 退回 /login → 200 也算健康；503 = degraded，只警告不算失败）+ 两服务 is-active"
  ok "dry-run 完成"
}

cmd_rollback() {
  step "列出远端备份（${BACKUP_DIR}）..."
  local backups
  backups="$(remote "$BACKUP_DIR" <<'EOF'
# step: list-backups
set -uo pipefail
cd "$1" 2>/dev/null || exit 0
ls -1 code-*.tgz 2>/dev/null | sort -r || true
EOF
)" || fail "无法列出远端备份"
  [ -n "$backups" ] || fail "远端没有任何备份（${BACKUP_DIR}），无法回滚"
  printf '%s\n' "$backups" | sed 's/^/  /'
  echo "  （名字里的 pre-<sha> = 部署 <sha> 之前服务器上的代码，选它就回到那次部署前的状态）"

  local target
  if [ -n "$ARG" ]; then
    target="$(basename "$ARG")"
    printf '%s\n' "$backups" | grep -qxF "$target" || fail "备份不存在：${target}（可用备份见上）"
  else
    target="$(printf '%s\n' "$backups" | head -n 1)"
  fi
  echo
  echo "  将回滚到：$target"
  if [ "$WITH_CONFIG" = 1 ]; then
    warn "--with-config：除代码外，备份里的 .env 与 config/（settings.yaml、accounts.enc）也会覆盖回备份时刻——本次部署之后在控制台里改过的账号、密码、设置会全部丢失"
  else
    echo "  只恢复代码：.env 与 config/（settings.yaml、accounts.enc）保持服务器现状不动"
    echo "  （data/ logs/ .venv/ 同样不动；确实要连配置一起回退到备份时刻，用 --with-config）"
  fi
  confirm "确认回滚？" || fail "已取消，服务器未改动"

  step "解包备份 → $REMOTE ..."
  remote "$REMOTE" "$BACKUP_DIR" "$target" "$WITH_CONFIG" <<'EOF' || fail "解包失败：服务器代码可能处于半恢复状态，请登录检查"
# step: rollback-restore
set -euo pipefail
REMOTE="$1"; ARCHIVE="$2/$3"; WITH_CONFIG="$4"
[ -f "$ARCHIVE" ] || { echo "✗ 备份不存在：$ARCHIVE"; exit 1; }
if [ "$WITH_CONFIG" = 1 ]; then
  tar xzf "$ARCHIVE" -C "$REMOTE"
  echo "  已解包 $3 → ${REMOTE}（含 .env 与 config/；data/ logs/ .venv/ 未动）"
else
  # Code only. The console rewrites .env and config/ (accounts.enc, settings.yaml)
  # while the release is live, so restoring them would silently undo password and
  # settings edits made since the backup — data loss, not a rollback. tar honours
  # --exclude while extracting; archive entries carry relative names (".env",
  # "config/settings.yaml"), and the "config/*" pattern is what skips the members
  # under an excluded directory.
  tar xzf "$ARCHIVE" -C "$REMOTE" \
    --exclude='.env' --exclude='./.env' \
    --exclude='.env.bak' --exclude='./.env.bak' \
    --exclude='config' --exclude='./config' \
    --exclude='config/*' --exclude='./config/*'
  echo "  已解包 $3 → ${REMOTE}（只恢复代码：.env 与 config/ 保持现状；data/ logs/ .venv/ 未动）"
fi
if [ -f "$REMOTE/RELEASE" ]; then echo "  RELEASE: $(cat "$REMOTE/RELEASE")"; fi
EOF

  local restored="备份已解包但服务未重启（进程仍是回滚前版本）"
  step_pip_install "$restored"
  step_smoke "$restored"
  step_restart
  step_health "回滚后的版本仍不健康：请登录服务器排查 journalctl"
  if [ "$WITH_CONFIG" = 1 ]; then
    ok "回滚完成：${target}（代码 + .env + config/ 均已恢复到备份时刻）"
  else
    ok "回滚完成：${target}（只恢复了代码；.env 与 config/ 仍是服务器上的现状）"
  fi
}

cmd_status() {
  step "远端状态（${SERVER}）..."
  remote "$REMOTE" "$BACKUP_DIR" <<'EOF' || fail "获取状态失败"
# step: status
set -uo pipefail
REMOTE="$1"; BACKUP_DIR="$2"
echo "== 服务"
for svc in zjip-web zjip-daemon; do
  printf '  %-12s %s\n' "$svc" "$(systemctl is-active "$svc" 2>&1)"
done
echo "== RELEASE"
if [ -f "$REMOTE/RELEASE" ]; then echo "  $(cat "$REMOTE/RELEASE")"; else echo "  （无 RELEASE 文件：尚未用本版 deploy.sh 部署过）"; fi
echo "== /health"
body=$(curl -s --max-time 5 http://127.0.0.1:8000/health) || body=""
if [ -z "$body" ]; then
  echo "  （无响应）"
else
  py="$REMOTE/.venv/bin/python"; [ -x "$py" ] || py=python3
  printf '%s\n' "$body" | "$py" -m json.tool 2>/dev/null | sed 's/^/  /' || echo "  $body"
fi
echo "== 磁盘"
df -h / | sed 's/^/  /'
echo "== 备份（${BACKUP_DIR}）"
if ls "$BACKUP_DIR"/code-*.tgz >/dev/null 2>&1; then
  ls -lh "$BACKUP_DIR"/code-*.tgz | sed 's/^/  /'
else
  echo "  （无）"
fi
EOF
}

write_lock_file() {  # path pyver pkgs source date — header stays ASCII: pip decodes
                     # requirement files with the locale codec, which is C over ssh
  {
    echo "# Pinned production dependencies: pip freeze from $4 ($2), taken $5 (UTC)."
    echo "# For the production Linux / Python 3.12 venv only -- deploy.sh runs"
    echo "#   pip install -r requirements.lock"
    echo "# on the server. Local development installs requirements-dev.txt (which pulls"
    echo "# in requirements.txt); do not install this file on macOS / Python 3.10."
    echo "# Regenerate with: ./deploy.sh lock"
    printf '%s\n' "$3"
  } > "$1"
}

cmd_lock() {
  step "读取远端 pip freeze（$SERVER:$REMOTE/.venv）..."
  local freeze pyver pkgs
  freeze="$(remote "$REMOTE" <<'EOF'
# step: pip-freeze
set -euo pipefail
cd "$1"
.venv/bin/python -V
.venv/bin/pip freeze
EOF
)" || fail "pip freeze 失败"
  pyver="$(printf '%s\n' "$freeze" | head -n 1)"
  pkgs="$(printf '%s\n' "$freeze" | tail -n +2)"
  printf '%s\n' "$pkgs" | grep -q '==' || fail "pip freeze 输出异常：$freeze"
  write_lock_file "$LOCAL/requirements.lock" "$pyver" "$pkgs" "$SERVER:$REMOTE/.venv" "$(date -u +%Y-%m-%d)"
  ok "已写入 requirements.lock（$(printf '%s\n' "$pkgs" | grep -c '==') 个包，${pyver}）；核对后提交"
}

cmd_install_units() {
  local units=("$LOCAL"/deploy/*.service)
  [ -f "${units[0]}" ] || fail "没有找到 deploy/*.service"
  local stage
  stage="/tmp/zjip-units-$(date -u +%Y%m%d-%H%M%S)-$$"
  step "对比 systemd unit（服务器 /etc/systemd/system/ ↔ 本地 deploy/）..."
  local unit name
  for unit in "${units[@]}"; do
    name="$(basename "$unit")"
    echo "── $name"
    # The unit text rides inside the remote script (quoted heredoc): ssh flattens
    # argv into one string, so file contents cannot travel as an argument.
    remote "$stage" "$name" <<EOF || fail "上传 $name 失败，服务器未改动"
# step: unit-diff
set -uo pipefail
mkdir -p "\$1"
cat > "\$1/\$2" <<'__UNIT__'
$(cat "$unit")
__UNIT__
if [ -f "/etc/systemd/system/\$2" ]; then
  if diff -u "/etc/systemd/system/\$2" "\$1/\$2"; then echo "  无差异"; fi
else
  echo "  服务器上不存在，将新建："
  sed 's/^/    /' "\$1/\$2"
fi
exit 0
EOF
  done
  if ! confirm "把以上 unit 写入 /etc/systemd/system/ 并 daemon-reload？"; then
    remote "$stage" <<'EOF' || true
# step: unit-cleanup
rm -rf "$1"
EOF
    fail "已取消，服务器未改动"
  fi
  step "安装 unit + daemon-reload ..."
  remote "$stage" <<'EOF' || fail "安装 unit 失败：请登录服务器检查 /etc/systemd/system/zjip-*.service"
# step: unit-install
set -euo pipefail
for f in "$1"/*.service; do
  install -m 644 "$f" /etc/systemd/system/
  echo "  已安装 $(basename "$f")"
done
rm -rf "$1"
systemctl daemon-reload
echo "  daemon-reload 完成"
EOF
  ok "unit 已安装。未自动重启服务：跑一次 ./deploy.sh，或手动 systemctl restart zjip-web zjip-daemon"
}

cmd_help() {
  # The usage block is the comment header at the top of this file.
  sed -n '2,/^$/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^$/d'
}

case "$CMD" in
  deploy)        cmd_deploy ;;
  dry-run)       cmd_dry_run ;;
  rollback)      cmd_rollback ;;
  status)        cmd_status ;;
  lock)          cmd_lock ;;
  install-units) cmd_install_units ;;
  help)          cmd_help ;;
  *)             fail "未知子命令：${CMD}（./deploy.sh help 查看用法）" ;;
esac
