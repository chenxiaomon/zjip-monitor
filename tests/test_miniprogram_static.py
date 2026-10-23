"""Static checks of the WeChat mini program.

It cannot run here (WeChat DevTools needs WeChat's servers), and it is
uploaded by hand from another computer — so these are the checks that run
before anyone previews it on a phone: every page and component is complete,
the JSON parses, the JS parses, every handler a template binds exists, the
templates are balanced, every /api path it calls exists on the server, it
sticks to syntax every DevTools compile setting accepts, and it carries no key.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MP = Path(__file__).parent.parent / "miniprogram"
# Local files, gitignored: never uploaded from the repo
LOCAL_ONLY = {"config.js", "project.private.config.json"}
PARTS = (".js", ".json", ".wxml", ".wxss")


def _files(suffix: str) -> list[Path]:
    return sorted(p for p in MP.rglob(f"*{suffix}") if p.name not in LOCAL_ONLY)


def _app_json() -> dict:
    return json.loads((MP / "app.json").read_text(encoding="utf-8"))


def _units() -> list[Path]:
    """Every page and component, as its path without a suffix."""
    pages = [MP / p for p in _app_json()["pages"]]
    components = sorted({p.with_suffix("") for p in (MP / "components").rglob("*.json")})
    return pages + components


@pytest.mark.parametrize("path", _files(".json"), ids=lambda p: str(p.relative_to(MP)))
def test_the_json_parses(path):
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("unit", _units(), ids=lambda p: str(p.relative_to(MP)))
def test_every_page_and_component_is_complete(unit):
    missing = [s for s in PARTS if not unit.with_suffix(s).exists()]
    assert missing == []


def test_the_tab_pages_are_pages():
    app = _app_json()
    tabs = [t["pagePath"] for t in app["tabBar"]["list"]]
    assert set(tabs) <= set(app["pages"]) and app["pages"][0] == tabs[0]


@pytest.mark.parametrize("unit", _units(), ids=lambda p: str(p.relative_to(MP)))
def test_used_components_exist(unit):
    using = json.loads(unit.with_suffix(".json").read_text(encoding="utf-8")).get("usingComponents", {})
    for path in using.values():
        assert (MP / path.lstrip("/")).with_suffix(".js").exists(), path


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", _files(".js"), ids=lambda p: str(p.relative_to(MP)))
def test_the_js_parses(path):
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", _files(".js"), ids=lambda p: str(p.relative_to(MP)))
def test_only_syntax_every_compile_setting_accepts(path):
    """The other computer's DevTools may compile with other settings."""
    code = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
    for token in ("?.", "??", "async ", "await "):
        assert token not in code, token


_HANDLER = re.compile(r"\b(?:capture-)?(?:bind|catch)(?::)?[a-z]+=\"([A-Za-z_]\w*)\"")
_TAG = re.compile(r"<(/?)([A-Za-z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>")


@pytest.mark.parametrize("unit", _units(), ids=lambda p: str(p.relative_to(MP)))
def test_every_bound_handler_exists(unit):
    wxml = unit.with_suffix(".wxml").read_text(encoding="utf-8")
    js = unit.with_suffix(".js").read_text(encoding="utf-8")
    missing = [h for h in set(_HANDLER.findall(wxml)) if not re.search(rf"\b{h}\s*\(", js)]
    assert missing == []


@pytest.mark.parametrize("path", _files(".wxml"), ids=lambda p: str(p.relative_to(MP)))
def test_the_templates_are_balanced(path):
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    assert text.count("{{") == text.count("}}")
    stack = []
    for closing, name, _attrs, self_closing in _TAG.findall(text):
        if self_closing:
            continue
        if closing:
            assert stack and stack[-1] == name, f"</{name}> closes {stack[-1:]}"
            stack.pop()
        else:
            stack.append(name)
    assert stack == []


def _server_paths() -> set[str]:
    from web.app import app

    def walk(routes):
        for r in routes:
            if hasattr(r, "original_router"):  # newer FastAPI wraps included routers
                yield from walk(r.original_router.routes)
            else:
                yield r

    return {r.path for r in walk(app.routes) if getattr(r, "path", "").startswith("/api/")}


def test_every_api_path_it_calls_exists():
    called = {m for p in _files(".js") for m in re.findall(r"['\"](/api/[\w/-]+)['\"]", p.read_text(encoding="utf-8"))}
    assert called and called <= _server_paths(), called - _server_paths()


def test_it_carries_no_key_and_no_token_in_a_url():
    for path in _files(".js") + _files(".wxml"):
        code = path.read_text(encoding="utf-8")
        assert not re.search(r"API_KEY\s*[:=]|X-API-Key", code), path
        assert "token=" not in code, path
