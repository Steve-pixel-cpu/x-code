"""Skills 设置 API 集成测试: GET 清单 / POST 安装（本地真 git 仓库端到端,
覆盖三种仓库布局与重名/覆盖）/ DELETE 卸载（含项目级拒删）/ 热生效。

安装走真实 git clone（git 是 x-code 硬依赖, CI 与本机都有）, 仓库用
tmp_path 里 git init + commit 出来的本地路径——不打真网。"""

import subprocess

import pytest

import server
from skills import discover_skills


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c


@pytest.fixture()
def user_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(server, "USER_DIR", home)
    return home


def _git(repo: subprocess.CompletedProcess, *args: str) -> None:
    """在 repo 目录跑 git 命令, 失败即断言（测试环境问题尽早暴露）。"""
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _init_repo(tmp_path, layout: str = "multi") -> str:
    """造一个社区技能仓库。返回可 clone 的本地路径。

    layout:
      multi   — skills/<a, b>/SKILL.md 一仓多技能
      root    — 根目录就是单个技能
      nested  — subpath 指向的单个技能目录
    """
    repo = tmp_path / "community-repo"
    repo.mkdir()
    if layout == "multi":
        for name, desc in (("alpha", "Alpha does A"),
                           ("beta", "Beta does B")):
            d = repo / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n"
                "Do it well.\n", encoding="utf-8")
    elif layout == "root":
        (repo / "SKILL.md").write_text(
            "---\nname: rootskill\ndescription: Root layout skill\n---\n"
            "body\n", encoding="utf-8")
    else:  # nested
        d = repo / "packages" / "nested-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: nested\ndescription: Nested skill\n---\nbody\n",
            encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init")
    return str(repo)


def test_get_empty(user_dir, client):
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert r.json() == {"skills": []}


def test_install_multi_skill_repo(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "multi")
    r = client.post("/api/skills/install", json={"repo": repo})
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["installed"]}
    assert names == {"alpha", "beta"}
    # 落盘: 进了用户技能目录, source=user
    got = client.get("/api/skills").json()["skills"]
    assert {s["name"] for s in got} == {"alpha", "beta"}
    assert all(s["source"] == "user" for s in got)
    # 工具同步: skill_read 进了共享 TOOLS
    from main import TOOLS
    assert any(t["name"] == "skill_read" for t in TOOLS)


def test_install_root_layout(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "root")
    r = client.post("/api/skills/install", json={"repo": repo})
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["installed"]] == ["rootskill"]


def test_install_with_subpath(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "nested")
    r = client.post("/api/skills/install",
                    json={"repo": repo, "subpath": "packages/nested-skill"})
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["installed"]] == ["nested"]


def test_install_duplicate_requires_overwrite(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "root")
    assert client.post("/api/skills/install",
                       json={"repo": repo}).status_code == 200
    r = client.post("/api/skills/install", json={"repo": repo})
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]
    # overwrite=True 放行
    r2 = client.post("/api/skills/install",
                     json={"repo": repo, "overwrite": True})
    assert r2.status_code == 200


def test_install_bad_repo_400(user_dir, client):
    r = client.post("/api/skills/install",
                    json={"repo": "https://invalid.example/nope.git"})
    assert r.status_code == 400
    assert "clone" in r.json()["detail"]


def test_install_missing_subpath_400(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "root")
    r = client.post("/api/skills/install",
                    json={"repo": repo, "subpath": "no/such/dir"})
    assert r.status_code == 400
    assert "子目录不存在" in r.json()["detail"]


def test_delete_user_skill(user_dir, client, tmp_path):
    repo = _init_repo(tmp_path, "root")
    client.post("/api/skills/install", json={"repo": repo})
    r = client.delete("/api/skills/rootskill")
    assert r.status_code == 200
    assert client.get("/api/skills").json()["skills"] == []
    # 卸载后 skill_read 从 TOOLS 摘掉（无技能时不再注册）
    from main import TOOLS
    assert not any(t["name"] == "skill_read" for t in TOOLS)


def test_delete_missing_400(user_dir, client):
    r = client.delete("/api/skills/ghost")
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_delete_project_level_rejected(user_dir, client, tmp_path,
                                       monkeypatch):
    """项目级技能不在 USER_DIR 下, 卸载被拒且提示去项目仓库管理。"""
    proj = tmp_path / "proj"
    d = proj / ".claude" / "skills" / "projskill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: projskill\ndescription: p\n---\nx", encoding="utf-8")
    monkeypatch.chdir(proj)

    assert client.get("/api/skills").json()["skills"][0]["source"] == "project"
    r = client.delete("/api/skills/projskill")
    assert r.status_code == 400
    assert "项目级" in r.json()["detail"]
    assert d.is_dir()   # 没被误删


def test_resync_updates_active_runtime(user_dir, client, tmp_path):
    """热生效: 安装后已开会话的系统提示里出现该技能, 卸载后消失。
    替身必须是 ConversationRuntime 子类——resync 只认真 runtime,
    测试留下的鸭子类型替身一律跳过（同生产容错口径）。"""
    from runtime import ConversationRuntime

    class _FakeRuntime(ConversationRuntime):
        def __init__(self):   # 不调父类构造: 只需要 set_system_prompt 可观察
            self.sections = None
        def set_system_prompt(self, sections):
            self.sections = list(sections)

    ws = server.get_or_create_web_session("ws-skills-test")
    ws.runtime = _FakeRuntime()
    ws.workdir = str(tmp_path)
    try:
        repo = _init_repo(tmp_path, "root")
        client.post("/api/skills/install", json={"repo": repo})
        joined = "\n".join(ws.runtime.sections)
        assert "# Skills" in joined
        assert "rootskill" in joined

        client.delete("/api/skills/rootskill")
        joined = "\n".join(ws.runtime.sections)
        assert "# Skills" not in joined
    finally:
        server._sessions.pop("ws-skills-test", None)


def test_discovery_after_install_via_shared_helper(user_dir, client, tmp_path):
    """CLI 同一套 discover_skills 也能看到 Web 端装好的技能（共目录）。"""
    repo = _init_repo(tmp_path, "multi")
    client.post("/api/skills/install", json={"repo": repo})
    found = {s.name for s in discover_skills(tmp_path, user_dir)}
    assert found == {"alpha", "beta"}
