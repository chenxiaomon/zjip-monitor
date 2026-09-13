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
  list-backups) printf '%s\n' code-20260102-000000-bbbbbbb.tgz code-20260101-000000-aaaaaaa.tgz ;;
  pip-freeze)   printf '%s\n' "Python 3.12.3" "foo==1.0" "bar==2.0" ;;
esac
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
    "deploy/deploy.env": "SERVER=secret@host\n",
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
    "config/settings.yaml": "server: 1\n",
    "config/accounts.enc": "ENC",
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
    for opt in ("--allow-dirty", "--skip-tests", "--yes", "DEPLOY_DELETE"):
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
    assert "2026-09-13" in text
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
                  "miniprogram/config.js", "deploy/deploy.env", "deploy.sh", ".git"):
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
    assert archives[0].name.endswith(f"-{sha[:7]}.tgz")
    names = _tar_names(archives[0])
    for must in ("src/main.py", ".env", "config/settings.yaml", "config/accounts.enc",
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
    for untouched in (".env", "config/settings.yaml", "config/accounts.enc",
                      "data/snapshots/snap.json", "logs/app.log", ".venv/lib/python3.12/site.py"):
        assert (harness.remote / untouched).read_text() == REMOTE_SEED[untouched], untouched
    for never in ("deploy/deploy.env", "miniprogram/config.js", ".git", ".claude",
                  ".pytest_cache", "deploy.sh"):
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
    assert "部署完成" in res.stdout
    assert "rollback" in res.stdout


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
    for untouched in (".env", "config/settings.yaml", "config/accounts.enc", "RELEASE",
                      "data/snapshots/snap.json", "logs/app.log", ".venv/lib/python3.12/site.py"):
        assert (harness.remote / untouched).read_text() == REMOTE_SEED[untouched], untouched
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
    assert "code-20260102-000000-bbbbbbb.tgz" in res.stdout
    assert harness.steps() == ["list-backups"]

    harness.reset_log()
    res = harness.run("rollback", "--yes", script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert harness.steps() == ["list-backups", "rollback-restore", "pip-install",
                               "smoke", "restart", "health"]
    restore = harness.script_of("rollback-restore")
    assert "code-20260102-000000-bbbbbbb.tgz" in restore
    assert "code-20260101" not in restore


def test_rollback_accepts_explicit_backup_and_rejects_unknown(harness):
    repo = make_mini_repo(harness.tmp / "repo")
    res = harness.run("rollback", "code-20260101-000000-aaaaaaa.tgz", "--yes",
                      script=repo / "deploy.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "code-20260101-000000-aaaaaaa.tgz" in harness.script_of("rollback-restore")

    harness.reset_log()
    res = harness.run("rollback", "code-nope.tgz", "--yes", script=repo / "deploy.sh")
    assert res.returncode != 0
    assert harness.steps() == ["list-backups"]
    assert "code-20260102-000000-bbbbbbb.tgz" in res.stdout + res.stderr


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
