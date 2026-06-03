from agedum.sources import find_project_root, load_global_source, load_source


def _make_project(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# instructions\n")
    skill = tmp_path / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n")


def test_load_source_finds_agents_and_skills(tmp_path):
    _make_project(tmp_path)
    src = load_source(tmp_path)
    assert src.agents_md == src.root / "AGENTS.md"
    assert src.skills_dir == src.root / ".agents" / "skills"


def test_find_project_root_walks_up_to_agents_md(tmp_path):
    _make_project(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path.resolve()


def test_missing_source_resolves_to_none(tmp_path):
    (tmp_path / ".git").mkdir()
    src = load_source(tmp_path)
    assert src.agents_md is None
    assert src.skills_dir is None


def test_load_global_source_uses_xdg_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    (xdg / "agents").mkdir(parents=True)
    (xdg / "agents" / "AGENTS.md").write_text("global instructions\n")
    (xdg / "agents" / "skills" / "g").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    src = load_global_source()
    assert src.agents_md == xdg / "agents" / "AGENTS.md"
    assert src.skills_dir == xdg / "agents" / "skills"


def test_load_global_source_defaults_to_config_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "agents" / "skills" / "g").mkdir(parents=True)
    (home / ".config" / "agents" / "AGENTS.md").write_text("global instructions\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    src = load_global_source()
    assert src.agents_md == home / ".config" / "agents" / "AGENTS.md"
    assert src.skills_dir == home / ".config" / "agents" / "skills"
