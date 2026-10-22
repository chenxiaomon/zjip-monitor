"""分组 management and 专属分组 — the boundary that keeps colleagues apart."""

import json

import pytest

from tests.conftest import ADMIN
from web.services import groups as group_svc
from web.services import users


def _audit(d):
    return [json.loads(ln) for f in sorted(d.glob("*.jsonl"))
            for ln in f.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def admin(web_client, login_as, isolated_settings, fake_accounts):
    return login_as(web_client, *ADMIN)


def _new_user(client, **extra):
    data = {"username": "zhangsan", "display_name": "张三", "role": "operator",
            "password": "first-password", **extra}
    return client.post("/users", data=data)


def test_a_new_colleague_gets_a_group_of_their_own(admin, isolated_settings, _isolated_audit):
    resp = _new_user(admin, own_group="on")
    assert resp.headers.get("HX-Trigger") == "userSaved", resp.text[:300]
    assert isolated_settings.read()["groups"]["u_zhangsan"] == "张三的企业"
    assert users.get("zhangsan")["groups"] == ["u_zhangsan"]
    actions = [e["action"] for e in _audit(_isolated_audit)]
    assert actions[-2:] == ["group.create", "user.create"]


def test_their_own_group_can_be_combined_with_others(admin, isolated_settings):
    _new_user(admin, own_group="on", groups=["group_a"])
    assert users.get("zhangsan")["groups"] == ["group_a", "u_zhangsan"]


def test_a_refused_user_leaves_no_group_behind(admin, isolated_settings, _isolated_audit):
    resp = _new_user(admin, own_group="on", password="short")
    assert "至少" in resp.text
    assert "u_zhangsan" not in isolated_settings.read()["groups"]
    assert users.get("zhangsan") is None
    assert not [e for e in _audit(_isolated_audit) if e["action"].startswith("group.")]


def test_admins_get_no_personal_group(admin, isolated_settings):
    _new_user(admin, own_group="on", role="admin")
    assert "u_zhangsan" not in isolated_settings.read()["groups"]


def test_without_the_box_a_group_must_be_picked(admin):
    assert "分组" in _new_user(admin).text  # own_group unchecked, no groups
    assert users.get("zhangsan") is None


def test_create_rename_delete(admin, isolated_settings, _isolated_audit):
    card = admin.post("/users/groups", data={"label": "李四的企业"}).text
    assert "李四的企业" in card
    key = next(k for k, v in isolated_settings.read()["groups"].items() if v == "李四的企业")
    admin.post(f"/users/groups/{key}/rename", data={"label": "李四"})
    assert isolated_settings.read()["groups"][key] == "李四"
    admin.post(f"/users/groups/{key}/delete")
    assert key not in isolated_settings.read()["groups"]
    assert [e["action"] for e in _audit(_isolated_audit)][-3:] == [
        "group.create", "group.rename", "group.delete"]


@pytest.mark.parametrize("key,why", [("group_a", "家企业"), ("group_b", "家企业")])
def test_a_group_in_use_cannot_be_deleted(admin, isolated_settings, key, why):
    card = admin.post(f"/users/groups/{key}/delete").text
    assert why in card
    assert key in isolated_settings.read()["groups"]


def test_labels_must_be_unique_and_present(admin, isolated_settings):
    assert "已有名为" in admin.post("/users/groups", data={"label": "来源一"}).text
    assert "不能为空" in admin.post("/users/groups", data={"label": "  "}).text


def test_a_broken_settings_file_is_never_overwritten(admin, isolated_settings):
    isolated_settings.path.write_text("groups: [broken", encoding="utf-8")
    before = isolated_settings.path.read_bytes()
    card = admin.post("/users/groups", data={"label": "新组"}).text
    assert "没有保存" in card or "格式错误" in card
    assert isolated_settings.path.read_bytes() == before


def test_the_users_page_shows_what_each_user_can_see(admin):
    body = admin.get("/users").text
    ga_row = body.split('id="user-ga"', 1)[1].split("</tr>", 1)[0]
    assert '<td class="num">2</td>' in ga_row  # ua1, ua2 are group_a
    assert "未分组" in body and "只有管理员看得到" in body


def test_personal_keys_are_safe():
    assert group_svc.personal_key("Zhang.San-1") == "u_zhang_san_1"
