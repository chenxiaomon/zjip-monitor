"""deploy.sh is driven end-to-end without a server.

DEPLOY_SSH_CMD points at a fake ssh that
  * logs every remote script it is handed (stdin) so the test can assert which
    steps ran and in what order,
  * execs rsync's own `rsync --server ...` locally, so `rsync -n`/rsync really
    walk the tree and the "remote" is just a temp directory,
  * on request (FAKE_SSH_EXEC_STEPS) really runs a remote step's script against
    that temp "remote" — the steps that only need tar/df/ls, which is enough for
    a genuine deploy → backup → rollback round-trip,
  * answers a few marked steps with canned output (backup list, pip freeze).

Nothing here needs a network, a real ssh, systemd or the production box. The
"repository" being deployed is a tiny fake tree, so `.venv/`-sized directories
are never walked or copied.
"""

import os
import re
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO / "deploy.sh"
# macOS ships bash 3.2 at /bin/bash; the script must stay compatible with it,
# so prefer that interpreter over a newer one that may be earlier on PATH.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")

FAKE_SSH = r"""#!/bin/sh
# Fake ssh for tests: log argv + stdin, exec rsync's server side locally,
# optionally run a step for real, answer marked steps with canned output.
log="${FAKE_SSH_LOG:?}"
while [ $# -gt 0 ]; do
  case "$1" in
    -l|-p|-i|-o|-F) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host="$1"; shift
if [ "$1" = "rsync" ]; then
  printf 'RSYNC %s\n' "$*" >> "$log"
  exec "$@"
fi
script=$(cat)
{ printf '=== CMD %s\n%s\n=== END\n' "$*" "$script"; } >> "$log"
step=$(printf '%s\n' "$script" | sed -n 's/^# step: //p' | head -n 1)
[ -n "$step" ] || exit 0
case " ${FAKE_SSH_FAIL_STEP:-} " in
  *" $step "*) echo "fake failure in $step"; exit 1 ;;
esac
case " ${FAKE_SSH_EXEC_STEPS:-} " in
  *" $step "*)
    # Behave like the real thing: ssh flattens argv into one string, the login
    # shell re-parses it, then `bash -s -- args` reads the script from stdin.
    eval "set -- $*"
    shift 3
    printf '%s\n' "$script" | bash -s -- "$@"
    exit $? ;;
esac
case "$step" in
  # Newest first. The older name predates the "-pre-" convention: `ls code-*.tgz`
  # still finds it, so an archive made by an earlier deploy.sh stays rollable.
  list-backups) printf '%s\n' code-20260102-000000-pre-bbbbbbb.tgz code-20260101-000000-aaaaaaa.tgz ;;
  pip-freeze)   printf '%s\n' "Python 3.12.3" "foo==1.0" "bar==2.0" ;;
esac
exit 0
"""

# The health step is the one remote step that needs more than tar/df/ls, so it
# gets its own stand-ins on PATH: scripted HTTP codes, scripted service states,
# and a no-op sleep so the "15 x 2s" retry loop costs nothing in a test.
FAKE_CURL = r"""#!/bin/sh
# Fake curl: log the whole argv (so --max-time can be asserted), then answer by
# URL. The strict health URL walks FAKE_CURL_HEALTH_CODES, repeating the last
# entry once the script runs out.
printf '%s\n' "$*" >> "${FAKE_CURL_LOG:?}"
url=""
for a in "$@"; do url="$a"; done
case "$url" in
  *"/health?strict=1")
    n=1
    if [ -f "$FAKE_CURL_COUNT" ]; then n=$(($(cat "$FAKE_CURL_COUNT") + 1)); fi
    printf '%s' "$n" > "$FAKE_CURL_COUNT"
    set -- ${FAKE_CURL_HEALTH_CODES:-200}
    while [ "$n" -gt 1 ] && [ $# -gt 1 ]; do shift; n=$((n - 1)); done
    # Real curl prints nothing and exits non-zero when it cannot connect.
    [ "$1" = "000" ] && exit 7
    printf '%s' "$1" ;;
  *"/login")  printf '%s' "${FAKE_CURL_LOGIN_CODE:-200}" ;;
  *"/health") printf '%s\n' "${FAKE_CURL_HEALTH_BODY:-}" ;;
esac
exit 0
"""

FAKE_SYSTEMCTL = r"""#!/bin/sh
# Fake systemctl: the health step only ever asks `is-active --quiet <service>`.
svc=""
for a in "$@"; do
  case "$a" in -*|is-active) ;; *) svc="$a" ;; esac
done
case " ${FAKE_ACTIVE_SERVICES-zjip-web zjip-daemon} " in
  *" $svc "*) exit 0 ;;
esac
exit 3
"""

FAKE_JOURNALCTL = r"""#!/bin/sh
echo "JOURNALCTL $*"
"""

FAKE_SLEEP = r"""#!/bin/sh
exit 0
"""

# Steps whose remote script only needs tar/df/ls/printf and can run for real
# against the temp "remote".
REAL_STEPS = "preflight backup write-release list-backups rollback-restore"

# A representative slice of the real tree: things that must ship, things that
# must never leave the laptop, and things that are just noise.
MINI_REPO_FILES = {
    "src/main.py": "print('new')\n",
    "web/app.py": "print('web')\n",
    "run_daemon.py": "pass\n",
    "requirements.txt": "fastapi\n",
    "requirements.lock": "fastapi==0.1\n",
    "deploy/deploy.env.example": "SERVER=x\n",
    "config/settings.yaml.example": "base_url: x\n",
    # server-side / secret — must be excluded
    ".env": "SECRET=1\n",
    "config/settings.yaml": "secret: 1\n",
    "config/accounts.enc": "SECRET",
    "miniprogram/config.js": "SECRET",
    "miniprogram/project.private.config.json": "{\"appid\": \"SECRET\"}",
    "deploy/deploy.env": "SERVER=secret@host\n",
    "archive/archive.env": "SERVER=secret@host\n",
    # runtime / caches — must be excluded
    "data/control/state.json": "{}",
    "logs/app.log": "x",
    ".venv/lib/python3.12/site.py": "x",
    ".pytest_cache/v/cache/nodeids": "x",
    ".claude/worktrees/wt/big.bin": "x",
    "src/__pycache__/main.cpython-312.pyc": "x",
}

# What the production box looks like before a deploy: old code plus the
# server-only files that must survive every deploy and be inside every backup.
REMOTE_SEED = {
    "src/main.py": "print('old')\n",
    "web/app.py": "print('old web')\n",
    "stale_old.py": "old\n",
    ".env": "SERVER_SECRET\n",
    # Written on the server only (save_accounts / .env edits keep one generation);
    # the laptop never has them, so --delete must be taught to leave them alone.
    ".env.bak": "SERVER_SECRET_OLD\n",
    "config/settings.yaml": "server: 1\n",
    "config/accounts.enc": "ENC",
    "config/accounts.enc.bak": "ENC_OLD",
    "requirements.lock": "fastapi==0.0\n",
    "RELEASE": "0000000 main 2026-01-01T00:00:00Z oldhost\n",
    "data/snapshots/snap.json": "{}",
    "logs/app.log": "log\n",
    ".venv/lib/python3.12/site.py": "venv\n",
    "src/__pycache__/main.cpython-312.pyc": "pyc",
}


def _write_exec(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def make_mini_repo(root: Path) -> Path:
    root.mkdir()
    _write_tree(root, MINI_REPO_FILES)
    for unit in (REPO / "deploy").glob("*.service"):
        shutil.copy(unit, root / "deploy" / unit.name)
    shutil.copy(REPO / ".gitignore", root / ".gitignore")
    shutil.copy(DEPLOY_SH, root / "deploy.sh")
    (root / "deploy.sh").chmod(0o755)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def seed_remote(remote: Path) -> None:
    _write_tree(remote, REMOTE_SEED)
    (remote / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    _write_exec(remote / ".venv" / "bin" / "python", "#!/bin/sh\nexit 0\n")
    # A real server's files were written at an earlier deploy; rsync's quick
    # check (size + mtime) must not mistake same-second test files for unchanged.
    old = time.time() - 86400
    for p in remote.rglob("*"):
        os.utime(p, (old, old), follow_symlinks=False)


@pytest.fixture
def harness(tmp_path):
    fake_ssh = _write_exec(tmp_path / "fake_ssh", FAKE_SSH)
    log = tmp_path / "ssh.log"
    remote = tmp_path / "remote"
    remote.mkdir()
    backups = tmp_path / "backups"
    env_file = tmp_path / "deploy.env"
    env_file.write_text(
        f"SERVER=fake@localhost\nREMOTE={remote}\nBACKUP_DIR={backups}\nKEEP_BACKUPS=2\n",
        encoding="utf-8",
    )

    def run(*args, script=DEPLOY_SH, stdin="", env=None, real_steps=False):
        full_env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("DEPLOY_", "FAKE_SSH_"))}
        full_env.update({
            "DEPLOY_SSH_CMD": str(fake_ssh),
            "DEPLOY_ENV": str(env_file),
            "FAKE_SSH_LOG": str(log),
            # macOS bsdtar: no AppleDouble `._x` shadows in the test archives
            "COPYFILE_DISABLE": "1",
        })
        if real_steps:
            full_env["FAKE_SSH_EXEC_STEPS"] = REAL_STEPS
        if env:
            full_env.update(env)
        return subprocess.run(
            [BASH, str(script), *args],
            cwd=str(script.parent), env=full_env, input=stdin,
            capture_output=True, text=True, errors="replace", timeout=120,
        )

    def steps():
        """Remote steps in execution order, 'RSYNC' for rsync transports."""
        if not log.exists():
            return []
        out = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.startswith("RSYNC "):
                out.append("RSYNC")
            elif line.startswith("# step: "):
                out.append(line[len("# step: "):].strip())
        return out

    def scripts():
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def script_of(step):
        """The logged block for one step: its argument line plus the script."""
        for block in scripts().split("=== CMD ")[1:]:
            if f"\n# step: {step}\n" in block:
                return block.split("=== END")[0]
        raise AssertionError(f"step {step!r} never ran; log:\n{scripts()}")

    def reset_log():
        if log.exists():
            log.unlink()

    return SimpleNamespace(run=run, steps=steps, scripts=scripts, script_of=script_of,
                           reset_log=reset_log, log=log, remote=remote, backups=backups,
                           tmp=tmp_path, env_file=env_file)


def health_env(harness, health_codes=("200",), login_code="200", body="",
               active=("zjip-web", "zjip-daemon")) -> dict:
    """Env that makes the real `health` step run locally against scripted answers.

    curl / systemctl / journalctl / sleep are shadowed on PATH; every other
    remote step stays canned, so a deploy reaches the health check for free.
    """
    bindir = harness.tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / "curl", FAKE_CURL)
    _write_exec(bindir / "systemctl", FAKE_SYSTEMCTL)
    _write_exec(bindir / "journalctl", FAKE_JOURNALCTL)
    _write_exec(bindir / "sleep", FAKE_SLEEP)
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_SSH_EXEC_STEPS": "health",
        "FAKE_CURL_LOG": str(harness.tmp / "curl.log"),
        "FAKE_CURL_COUNT": str(harness.tmp / "curl.count"),
        "FAKE_CURL_HEALTH_CODES": " ".join(health_codes),
        "FAKE_CURL_LOGIN_CODE": login_code,
        "FAKE_CURL_HEALTH_BODY": body,
        "FAKE_ACTIVE_SERVICES": " ".join(active),
    }


def curl_calls(harness) -> list:
    log = harness.tmp / "curl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _listed_paths(output: str) -> set:
    """Paths from an `rsync -n -v` listing (one per line, dirs end with '/')."""
    paths = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or " " in line or line.startswith(("sent ", "total ", "Transfer ")):
            continue
        paths.add(line)
    return paths


def _tar_name(member_name: str) -> str:
    name = member_name[2:] if member_name.startswith("./") else member_name
    return name.rstrip("/")


def _tar_names(path: Path) -> set:
    with tarfile.open(path) as tf:
        # macOS bsdtar may add AppleDouble `._x` shadows; they are noise here.
        return {_tar_name(m.name) for m in tf.getmembers()
                if not Path(m.name).name.startswith("._")}


# ---------------------------------------------------------------------------
# static checks
# ---------------------------------------------------------------------------

def test_bash_syntax_ok():
    res = subprocess.run([BASH, "-n", str(DEPLOY_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_help_lists_every_subcommand(harness):
    res = harness.run("help")
    assert res.returncode == 0, res.stderr
    for name in ("deploy", "dry-run", "rollback", "status", "lock", "install-units"):
        assert name in res.stdout
    for opt in ("--allow-dirty", "--skip-tests", "--yes", "--with-config", "DEPLOY_DELETE"):
        assert opt in res.stdout
    assert harness.steps() == []


def test_unknown_subcommand_is_rejected(harness):
    res = harness.run("frobnicate")
    assert res.returncode != 0
    assert "frobnicate" in res.stdout + res.stderr
    assert harness.steps() == []


def test_gitignore_hides_deploy_env():
    entries = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/deploy.env" in entries


def test_requirements_lock_is_pinned_and_documented():
    text = (REPO / "requirements.lock").read_text(encoding="utf-8")
    lines = text.splitlines()
    header = [ln for ln in lines if ln.startswith("#")]
    pins = [ln for ln in lines if ln and not ln.startswith("#")]
    assert header, "lock file needs a header explaining its provenance"
    assert re.search(r"taken \d{4}-\d{2}-\d{2} \(UTC\)", text), "header must say when the freeze was taken"
    assert "requirements-dev.txt" in text
    assert "3.12" in text
    assert pins and all("==" in ln for ln in pins), pins
    for pkg in ("fastapi==", "uvicorn==", "playwright==", "APScheduler=="):
        assert any(ln.startswith(pkg) for ln in pins), pkg
    # Non-ASCII comments could trip pip's requirement-file decoding under a
    # C/POSIX locale over ssh; the header must stay plain ASCII.
    text.encode("ascii")


def test_service_templates_match_production_units():
    def significant(name):
        lines = (REPO / "deploy" / name).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    assert significant("zjip-web.service") == [
        "[Unit]",
        "Description=Zjip Monitor Web Dashboard",
        "After=network.target",
        "[Service]",
        "User=root",
        "WorkingDirectory=/opt/zjip-monitor",
        "EnvironmentFile=/opt/zjip-monitor/.env",
        "ExecStart=/opt/zjip-monitor/.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8000",
        "Restart=on-failure",
        "RestartSec=5",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    assert significant("zjip-daemon.service") == [
        "[Unit]",
        "Description=Zjip Monitor Daemon (Scheduled Scraper)",
        "After=network.target",
        "[Service]",
        "User=root",
        "WorkingDirectory=/opt/zjip-monitor",
        "EnvironmentFile=/opt/zjip-monitor/.env",
        "ExecStart=/opt/zjip-monitor/.venv/bin/python run_daemon.py",
        "Restart=on-failure",
        "RestartSec=10",
        "[Install]",
        "WantedBy=multi-user.target",
    ]


def test_deploy_env_example_covers_every_knob():
    text = (REPO / "deploy" / "deploy.env.example").read_text(encoding="utf-8")
    for key in ("SERVER=", "REMOTE=", "BACKUP_DIR=", "KEEP_BACKUPS="):
        assert key in text


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------

def test_dry_run_lists_transfer_and_honours_excludes(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("dry-run", "--skip-tests", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    listed = _listed_paths(res.stdout)

    for must in ("src/main.py", "web/app.py", "run_daemon.py", "requirements.lock",
                 "deploy/zjip-web.service", "deploy/zjip-daemon.service",
                 "deploy/deploy.env.example", "config/settings.yaml.example"):
        assert must in listed, f"{must} missing from transfer list:\n{res.stdout}"

    for never in (".env", "config/settings.yaml", "config/accounts.enc",
                  "miniprogram/config.js", "miniprogram/project.private.config.json",
                  "deploy/deploy.env", "archive/archive.env", "deploy.sh", ".git"):
        assert never not in listed, f"{never} must not be transferred"
    for prefix in ("data/", "logs/", ".venv/", ".pytest_cache/", ".claude/", ".git/"):
        assert not any(p.startswith(prefix) for p in listed), prefix
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in listed)

    # Only rsync's transport touched "the server"; nothing was written or run there.
    assert harness.steps() == ["RSYNC"]
    assert list(harness.remote.iterdir()) == []
    # The plan of remote steps is spelled out for the operator.
    for word in ("预检", "备份", "RELEASE", "requirements.lock", "冒烟", "重启", "健康检查"):
        assert word in res.stdout
    assert "-pre-" in res.stdout  # the backup naming is part of the plan


def test_dry_run_on_real_repo_never_lists_secrets(harness):
    res = harness.run("dry-run", "--skip-tests", "--allow-dirty")
    assert res.returncode == 0, res.stdout + res.stderr
    listed = _listed_paths(res.stdout)
    for must in ("src/main.py", "web/app.py", "requirements.lock", "deploy/zjip-web.service"):
        assert must in listed, must
    for never in (".env", "config/settings.yaml", "config/accounts.enc", "deploy/deploy.env"):
        assert never not in listed, never
    for prefix in ("data/", "logs/", ".venv/", ".pytest_cache/", ".claude/", ".git/"):
        assert not any(p.startswith(prefix) for p in listed), prefix
    assert harness.steps() == ["RSYNC"]
    assert list(harness.remote.iterdir()) == []


def test_dry_run_only_warns_about_dirty_tree(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    (repo / "src" / "wip.py").write_text("x\n")
    res = harness.run("dry-run", "--skip-tests", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "未提交" in res.stdout + res.stderr
    assert "src/wip.py" in res.stdout


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

def test_deploy_refuses_dirty_tree_before_any_remote_call(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    (repo / "src" / "wip.py").write_text("x\n")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh")
    assert res.returncode != 0
    assert "--allow-dirty" in res.stdout + res.stderr
    assert harness.steps() == []
    assert list(harness.remote.iterdir()) == []


def test_deploy_pipeline_runs_steps_in_order(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    sha = _git(repo, "rev-parse", "HEAD")
    seed_remote(harness.remote)

    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == [
        "preflight", "backup", "RSYNC", "write-release",
        "pip-install", "smoke", "restart", "health",
    ]

    # --- backup really happened, holds code + .env + config/, not runtime dirs
    archives = sorted(harness.backups.glob("code-*.tgz"))
    assert len(archives) == 1
    # The name says what is inside: the code from *before* this deploy's sha.
    assert archives[0].name.endswith(f"-pre-{sha[:7]}.tgz"), archives[0].name
    names = _tar_names(archives[0])
    for must in ("src/main.py", ".env", ".env.bak", "config/settings.yaml",
                 "config/accounts.enc", "config/accounts.enc.bak",
                 "requirements.lock", "RELEASE", "stale_old.py"):
        assert must in names, (must, names)
    for never in ("data", "logs", ".venv", ".pytest_cache"):
        assert never not in names and not any(n.startswith(never + "/") for n in names), never
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    with tarfile.open(archives[0]) as tf:
        old_main = tf.extractfile([m for m in tf.getmembers()
                                   if _tar_name(m.name) == "src/main.py"][0]).read()
    assert old_main == b"print('old')\n"

    # --- rsync landed the new code, only the code, and did not delete anything
    assert (harness.remote / "src" / "main.py").read_text() == "print('new')\n"
    assert (harness.remote / "web" / "app.py").read_text() == "print('web')\n"
    assert (harness.remote / "requirements.lock").read_text() == "fastapi==0.1\n"
    assert (harness.remote / "deploy" / "zjip-web.service").exists()
    assert (harness.remote / "stale_old.py").exists()  # no --delete by default
    for untouched in (".env", ".env.bak", "config/settings.yaml", "config/accounts.enc",
                      "config/accounts.enc.bak", "data/snapshots/snap.json",
                      "logs/app.log", ".venv/lib/python3.12/site.py"):
        assert (harness.remote / untouched).read_text() == REMOTE_SEED[untouched], untouched
    for never in ("deploy/deploy.env", "archive/archive.env", "miniprogram/config.js", ".git",
                  ".claude", ".pytest_cache", "deploy.sh"):
        assert not (harness.remote / never).exists(), never
    rsync_line = [ln for ln in harness.scripts().splitlines() if ln.startswith("RSYNC ")][0]
    assert "--delete" not in rsync_line

    # --- RELEASE: <sha> <branch> <utc time> <deployer host>
    release = (harness.remote / "RELEASE").read_text().split()
    assert len(release) == 4
    assert release[0] == sha and release[1] == "main"
    assert release[2].endswith("Z") and "T" in release[2]

    # --- remaining steps carry the expected commands
    assert "requirements.lock" in harness.script_of("pip-install")
    smoke = harness.script_of("smoke")
    for imp in ("import web.app", "from src.main import run_all", "import run_daemon"):
        assert imp in smoke
    health = harness.script_of("health")
    for word in ("/health?strict=1", "/login", "journalctl", "zjip-web", "zjip-daemon"):
        assert word in health
    # An unbounded curl would blow the advertised "15 x 2s" ceiling on a hung box.
    curls = [ln for ln in health.splitlines() if "curl " in ln]
    assert len(curls) >= 3
    assert all("--max-time" in ln for ln in curls), curls
    assert "部署完成" in res.stdout
    assert "rollback" in res.stdout
    # The operator is told which build the backup predates, not just its name.
    assert "之前" in res.stdout


def test_deploy_then_rollback_round_trip(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (harness.remote / "src" / "main.py").read_text() == "print('new')\n"
    assert (harness.remote / "RELEASE").read_text() != REMOTE_SEED["RELEASE"]
    backup = sorted(harness.backups.glob("code-*.tgz"))[0].name
    assert backup in res.stdout  # operator is told how to roll back

    harness.reset_log()
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == ["list-backups", "rollback-restore", "pip-install",
                               "smoke", "restart", "health"]
    assert backup in res.stdout
    assert (harness.remote / "src" / "main.py").read_text() == "print('old')\n"
    assert (harness.remote / "RELEASE").read_text() == REMOTE_SEED["RELEASE"]
    assert (harness.remote / "requirements.lock").read_text() == "fastapi==0.0\n"
    # runtime state was never part of the archive, so it is exactly as before
    for untouched in ("data/snapshots/snap.json", "logs/app.log", ".venv/lib/python3.12/site.py"):
        assert (harness.remote / untouched).read_text() == REMOTE_SEED[untouched]


def test_backups_are_trimmed_to_keep_backups(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    for i in range(3):
        if i:
            time.sleep(1.05)  # archive names are second-resolution
        res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
        assert res.returncode == 0, res.stdout + res.stderr
    names = sorted(p.name for p in harness.backups.glob("code-*.tgz"))
    assert len(names) == 2, names  # KEEP_BACKUPS=2 in the harness env file
    assert "已清理旧备份" in res.stdout


def test_keep_backups_one_keeps_the_backup_just_taken(harness):
    """The lower bound of the trim: `sort -r | tail -n +2` must spare row one."""
    harness.env_file.write_text(
        f"SERVER=fake@localhost\nREMOTE={harness.remote}\n"
        f"BACKUP_DIR={harness.backups}\nKEEP_BACKUPS=1\n", encoding="utf-8")
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(list(harness.backups.glob("code-*.tgz"))) == 1


@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_keep_backups_must_be_a_positive_integer(harness, value):
    """KEEP=0 passed the old "non-negative" check and then made the trim delete
    from row one of a newest-first listing — wiping the backup just taken, i.e.
    the only way back from the deploy that created it."""
    harness.env_file.write_text(f"KEEP_BACKUPS={value}\n", encoding="utf-8")
    res = harness.run("help")
    assert res.returncode != 0, res.stdout
    assert "KEEP_BACKUPS" in res.stdout + res.stderr
    assert harness.steps() == []


def test_backup_dir_and_archive_are_owner_only(harness):
    """The archive holds .env verbatim — FERNET_KEY, WEB_PASS, API_KEY."""
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert stat.S_IMODE(harness.backups.stat().st_mode) == 0o700
    archive = sorted(harness.backups.glob("code-*.tgz"))[0]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert ".env" in _tar_names(archive)  # still backed up, just not world-readable


def test_backup_name_marks_it_as_pre_deploy(harness):
    """`-pre-<sha>` reads as "the code from before <sha>". A bare `-<sha>` reads
    as "this archive is <sha>" and invites rolling back into the build that is
    being replaced — the opposite of what the archive contains."""
    repo = make_mini_repo(harness.tmp / "repo")
    sha = _git(repo, "rev-parse", "--short", "HEAD")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    name = sorted(harness.backups.glob("code-*.tgz"))[0].name
    assert re.match(r"^code-\d{8}-\d{6}-pre-%s\.tgz$" % re.escape(sha), name), name
    assert f"部署 {sha} 之前" in res.stdout
    assert f"rollback {name}" in res.stdout
    # and the archive really is the pre-deploy code, not the new one
    with tarfile.open(sorted(harness.backups.glob("code-*.tgz"))[0]) as tf:
        member = [m for m in tf.getmembers() if _tar_name(m.name) == "src/main.py"][0]
        assert tf.extractfile(member).read() == b"print('old')\n"


def test_deploy_stops_when_preflight_fails(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env={"FAKE_SSH_FAIL_STEP": "preflight"})
    assert res.returncode != 0
    assert harness.steps() == ["preflight"]
    assert "旧版本" in res.stdout + res.stderr
    assert list(harness.remote.iterdir()) == []


def test_deploy_preflight_really_rejects_missing_remote(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    shutil.rmtree(harness.remote)  # no code dir / venv on the "server"
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode != 0
    assert harness.steps() == ["preflight"]
    assert "远端目录不存在" in res.stdout
    assert not harness.remote.exists()


def test_deploy_stops_when_smoke_fails_without_restart(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env={"FAKE_SSH_FAIL_STEP": "smoke"})
    assert res.returncode != 0
    assert harness.steps()[-1] == "smoke"
    assert "restart" not in harness.steps()
    assert "旧版本" in res.stdout + res.stderr
    assert "rollback" in res.stdout + res.stderr


def test_deploy_health_failure_points_at_rollback(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env={"FAKE_SSH_FAIL_STEP": "health"})
    assert res.returncode != 0
    assert harness.steps()[-1] == "health"
    assert "rollback" in res.stdout + res.stderr


# ---------------------------------------------------------------------------
# health check: healthy / degraded / broken are three different outcomes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["404", "302", "401", "403"])
def test_health_without_health_route_falls_back_to_login(harness, code):
    """A build with no /health route answers the probe in several ways: a plain
    404, or 302/401/403 when the auth middleware intercepts the unknown path
    before routing sees it (which is what the current web/app.py does). All of
    them mean "old build", not "broken deploy" — /login answering settles it."""
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=(code,), login_code="200"))
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert code in out and "/login" in out
    assert "部署完成" in out
    assert "JOURNALCTL" not in out  # nothing was treated as a failure
    # one probe each: the fallback settles it, no 15-round retry loop
    assert len(curl_calls(harness)) == 2


def test_health_falls_back_only_while_login_answers(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("302",), login_code="500"))
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "JOURNALCTL -u zjip-web" in out and "JOURNALCTL -u zjip-daemon" in out
    assert "rollback" in out
    # 15 rounds, each pairing a /health probe with a /login probe
    assert len([c for c in curl_calls(harness) if "strict=1" in c]) == 15


def test_health_503_is_a_warning_not_a_failed_deploy(harness):
    """503 means the new code is up and rates its own business state degraded
    (a wrong account password, say). A rollback cannot fix that and would only
    take a working release away, so the deploy stands — loudly."""
    repo = make_mini_repo(harness.tmp / "repo")
    body = '{"status": "degraded", "accounts": [{"name": "a1", "ok": false}]}'
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("503",), body=body))
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "服务已启动，但系统自评为 degraded，请到控制台查看" in out
    assert body in out  # the whole /health JSON, not just the status code
    assert "部署完成" in out
    assert "JOURNALCTL" not in out
    # 503 is conclusive: no point burning 15 retries on it
    assert len([c for c in curl_calls(harness) if "strict=1" in c]) == 1


def test_health_503_still_fails_when_a_service_is_down(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("503",), active=("zjip-web",)))
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "zjip-daemon 未运行" in out
    assert "JOURNALCTL" in out
    assert "rollback" in out


@pytest.mark.parametrize("code", ["500", "502"])
def test_health_non_503_server_error_is_a_failure(harness, code):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=(code,)))
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert code in out
    assert "JOURNALCTL" in out
    assert "rollback" in out


def test_health_unreachable_is_a_failure(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("000",)))
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "000" in out
    assert "JOURNALCTL" in out
    assert "rollback" in out


def test_health_retries_until_the_service_comes_up(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("000", "000", "200")))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "第 3 次" in res.stdout
    assert len([c for c in curl_calls(harness) if "strict=1" in c]) == 3


def test_health_curls_are_bounded_by_max_time(harness):
    """Without --max-time, "15 x 2s" holds only while the server answers; a box
    that accepts the connection and then hangs stalls the deploy indefinitely."""
    repo = make_mini_repo(harness.tmp / "repo")
    # 403 + a dead /login exercises all three call sites: probe, fallback, JSON dump
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      env=health_env(harness, health_codes=("403",), login_code="404"))
    assert res.returncode != 0
    calls = curl_calls(harness)
    assert len(calls) == 15 * 2 + 1, calls
    assert all("--max-time 5" in c for c in calls), calls


def test_deploy_delete_previews_and_requires_yes(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)

    # declined → nothing transferred, nothing deleted
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      stdin="no\n", env={"DEPLOY_DELETE": "1"})
    assert res.returncode != 0, res.stdout
    assert "deleting stale_old.py" in res.stdout
    assert (harness.remote / "stale_old.py").exists()
    assert (harness.remote / "src" / "main.py").read_text() == "print('old')\n"
    assert "write-release" not in harness.steps()

    # confirmed → stale code removed, excluded server-side files untouched
    harness.reset_log()
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh",
                      stdin="yes\n", env={"DEPLOY_DELETE": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (harness.remote / "stale_old.py").exists()
    assert (harness.remote / "src" / "main.py").read_text() == "print('new')\n"
    for untouched in (".env", ".env.bak", "config/settings.yaml", "config/accounts.enc",
                      "config/accounts.enc.bak", "RELEASE", "data/snapshots/snap.json",
                      "logs/app.log", ".venv/lib/python3.12/site.py"):
        assert (harness.remote / untouched).read_text() == REMOTE_SEED[untouched], untouched
    # The server-only recovery copies are never even proposed for deletion.
    assert "deleting .env.bak" not in res.stdout
    assert "accounts.enc.bak" not in res.stdout
    rsync_lines = [ln for ln in harness.scripts().splitlines() if ln.startswith("RSYNC ")]
    assert len(rsync_lines) == 2  # preview + real
    assert all("--delete" in ln for ln in rsync_lines)


def test_deploy_yes_flag_skips_delete_prompt(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    (harness.remote / "stale_old.py").write_text("old\n")
    res = harness.run("deploy", "--skip-tests", "--yes", script=repo / "deploy.sh",
                      stdin="", env={"DEPLOY_DELETE": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (harness.remote / "stale_old.py").exists()


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def test_rollback_defaults_to_latest_and_needs_confirmation(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("rollback", script=repo / "deploy.sh", stdin="")
    assert res.returncode != 0
    assert "code-20260102-000000-pre-bbbbbbb.tgz" in res.stdout
    assert harness.steps() == ["list-backups"]

    harness.reset_log()
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == ["list-backups", "rollback-restore", "pip-install",
                               "smoke", "restart", "health"]
    restore = harness.script_of("rollback-restore")
    assert "code-20260102-000000-pre-bbbbbbb.tgz" in restore
    assert "code-20260101" not in restore


def test_rollback_accepts_explicit_backup_and_rejects_unknown(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    # Also proves an archive named by an older deploy.sh is still selectable.
    res = harness.run("rollback", "code-20260101-000000-aaaaaaa.tgz", "--yes",
                      script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "code-20260101-000000-aaaaaaa.tgz" in harness.script_of("rollback-restore")

    harness.reset_log()
    res = harness.run("rollback", "code-nope.tgz", "--yes", script=repo / "deploy.sh")
    assert res.returncode != 0
    assert harness.steps() == ["list-backups"]
    assert "code-20260102-000000-pre-bbbbbbb.tgz" in res.stdout + res.stderr


def test_rollback_keeps_live_env_and_config_by_default(harness):
    """Code rolls back; secrets and settings do not. The console rewrites .env
    and config/accounts.enc while a release is live, so restoring the backup's
    copies would silently undo account-password and settings edits made since —
    data loss dressed up as a rollback."""
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr

    # after the deploy, an operator fixes an account password in the console
    (harness.remote / ".env").write_text("SERVER_SECRET_ROTATED\n")
    (harness.remote / ".env.bak").write_text("SERVER_SECRET\n")
    (harness.remote / "config" / "accounts.enc").write_text("ENC_NEW_PASSWORD")
    (harness.remote / "config" / "accounts.enc.bak").write_text("ENC")
    (harness.remote / "config" / "settings.yaml").write_text("server: 2\n")

    harness.reset_log()
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    # code and RELEASE went back ...
    assert (harness.remote / "src" / "main.py").read_text() == "print('old')\n"
    assert (harness.remote / "requirements.lock").read_text() == "fastapi==0.0\n"
    assert (harness.remote / "RELEASE").read_text() == REMOTE_SEED["RELEASE"]
    # ... secrets and settings stayed exactly as the console left them
    assert (harness.remote / ".env").read_text() == "SERVER_SECRET_ROTATED\n"
    assert (harness.remote / ".env.bak").read_text() == "SERVER_SECRET\n"
    assert (harness.remote / "config" / "accounts.enc").read_text() == "ENC_NEW_PASSWORD"
    assert (harness.remote / "config" / "accounts.enc.bak").read_text() == "ENC"
    assert (harness.remote / "config" / "settings.yaml").read_text() == "server: 2\n"
    # the operator is told both what was skipped and how to ask for it
    assert "--with-config" in res.stdout + res.stderr


def test_rollback_with_config_restores_env_and_config(harness):
    """The disaster-recovery variant: everything goes back, and the prompt says
    so before anything is unpacked."""
    repo = make_mini_repo(harness.tmp / "repo")
    seed_remote(harness.remote)
    res = harness.run("deploy", "--skip-tests", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    (harness.remote / ".env").write_text("SERVER_SECRET_ROTATED\n")
    (harness.remote / "config" / "accounts.enc").write_text("ENC_NEW_PASSWORD")

    harness.reset_log()
    res = harness.run("rollback", "--with-config", "--yes",
                      script=repo / "deploy.sh", real_steps=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (harness.remote / "src" / "main.py").read_text() == "print('old')\n"
    assert (harness.remote / ".env").read_text() == REMOTE_SEED[".env"]
    assert (harness.remote / "config" / "accounts.enc").read_text() == REMOTE_SEED["config/accounts.enc"]
    assert (harness.remote / "config" / "settings.yaml").read_text() == REMOTE_SEED["config/settings.yaml"]
    assert "丢失" in res.stdout + res.stderr  # the warning names the cost


def test_rollback_listing_explains_the_pre_sha_naming(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "pre-<sha>" in res.stdout
    assert "之前" in res.stdout


def test_rollback_with_no_backups_aborts(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh", real_steps=True)
    assert res.returncode != 0
    assert harness.steps() == ["list-backups"]
    assert "没有任何备份" in res.stdout + res.stderr


# ---------------------------------------------------------------------------
# status / lock / install-units
# ---------------------------------------------------------------------------

def test_status_is_read_only(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("status", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == ["status"]
    status = harness.script_of("status")
    for word in ("systemctl is-active", "RELEASE", "/health", "df -h", "json.tool"):
        assert word in status
    assert "restart" not in status


def test_lock_writes_requirements_lock_from_remote_freeze(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("lock", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == ["pip-freeze"]
    text = (repo / "requirements.lock").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("#")
    assert "Python 3.12.3" in text
    assert "requirements-dev.txt" in text
    assert "fake@localhost" in text
    pins = [ln for ln in lines if ln and not ln.startswith("#")]
    assert pins == ["foo==1.0", "bar==2.0"]
    text.encode("ascii")


def test_install_units_diffs_then_installs_only_after_yes(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("install-units", script=repo / "deploy.sh", stdin="no\n")
    assert res.returncode != 0
    steps = harness.steps()
    assert steps.count("unit-diff") == 2
    assert "unit-install" not in steps
    scripts = harness.scripts()
    assert "zjip-web.service" in scripts and "zjip-daemon.service" in scripts
    # the unit text itself travels inside the remote script
    assert "ExecStart=/opt/zjip-monitor/.venv/bin/uvicorn web.app:app" in scripts
    assert "ExecStart=/opt/zjip-monitor/.venv/bin/python run_daemon.py" in scripts

    harness.reset_log()
    res = harness.run("install-units", "--yes", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps()[-1] == "unit-install"
    install = harness.script_of("unit-install")
    assert "daemon-reload" in install
    assert "systemctl restart" not in install
    assert "restart" in res.stdout  # operator is told to restart by hand / via deploy
