"""前端资源本地化：没有公网 CDN，图标 sprite 与状态配色和模板保持一致，静态资源带缓存头。"""

import re
from pathlib import Path

from src.scraper import AUDIT_STATUS
from tests.conftest import ADMIN, GROUP_A_USER

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "web" / "templates"
STATIC = REPO / "web" / "static"


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_template_loads_anything_from_the_public_internet():
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'(src|href)="https?://(?!www\.zjip)', text), f"{path.name} 引用了外部资源"
        assert "tailwindcss" not in text and "unpkg" not in text, path.name


def test_every_icon_a_template_uses_exists_in_the_sprite():
    sprite = (STATIC / "icons.svg").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="i-([a-z0-9-]+)"', sprite))
    used: set[str] = set()
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"icon\(\s*'([a-z0-9-]+)'", text))
        # the nav lists pass icon names inside (href, icon, label) tuples
        used.update(re.findall(r"\('/[a-z]*',\s*'([a-z0-9-]+)',", text))
    assert used, "no icon() calls found in the templates"
    missing = used - ids
    assert not missing, f"web/static/icons.svg 缺少图标: {sorted(missing)}"


def test_static_assets_are_local_and_cacheable(web_client):
    for name in ("ui.css", "ui.js", "icons.svg", "htmx.min.js", "htmx-sse.js", "favicon.svg"):
        resp = web_client.get(f"/static/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["cache-control"] == "public, max-age=3600", name
    versioned = web_client.get("/static/ui.css?v=deadbeef")
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "cache-control" not in web_client.get("/static/nope.css").headers


def test_pages_reference_versioned_local_assets_and_the_status_css(web_client, login_as):
    html = login_as(web_client, *ADMIN).get("/settings").text
    assert re.search(r'href="/static/ui\.css\?v=[0-9a-f]{8}"', html)
    assert re.search(r'src="/static/htmx\.min\.js\?v=[0-9a-f]{8}"', html)
    assert re.search(r'<use href="/static/icons\.svg\?v=[0-9a-f]{8}#i-settings"', html)  # nav icon via the sprite
    for code in AUDIT_STATUS:
        assert f".s-pill-{code}{{" in html and f"html.dark .s-pill-{code}{{" in html
    assert ".s-pill-x{" in html  # unknown-code fallback
    assert 'class="nav-toggle' in html or "nav-toggle" in html  # mobile menu button


def test_login_page_needs_nothing_from_the_internet(web_client):
    html = web_client.get("/login").text
    assert "/static/ui.css?v=" in html and "https://" not in html


def test_group_user_gets_no_admin_navigation(fake_accounts, web_client, login_as):
    html = login_as(web_client, *GROUP_A_USER).get("/").text
    assert 'href="/settings"' not in html and 'href="/notify"' not in html and 'href="/reports"' not in html
    assert 'href="/records"' in html and "退出登录" in html


def test_home_screen_manifest_and_icons(web_client):
    import json
    import struct

    resp = web_client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(resp.text)
    assert manifest["start_url"] == "/" and manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        data = web_client.get(icon["src"]).content
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert f"{w}x{h}" == icon["sizes"]
    touch = web_client.get("/static/apple-touch-icon.png").content
    assert struct.unpack(">II", touch[16:24]) == (180, 180)

    for page in (web_client.get("/login").text,):
        assert 'rel="manifest" href="/static/manifest.webmanifest"' in page
        assert 'rel="apple-touch-icon"' in page and 'name="theme-color"' in page


def test_add_account_error_keeps_the_chosen_group(fake_accounts, isolated_settings, web_client, login_as):
    client = login_as(web_client, *ADMIN)
    html = client.post("/accounts", data={"company": "新公司", "username": "ua1", "password": "x", "group": "group_b"}).text
    assert "账号 ua1 已存在" in html
    assert '<option value="group_b" selected>' in html
    assert '<option value="group_a" >' in html or '<option value="group_a">' in html
