from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8")) or {}


def _skill_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} is missing YAML frontmatter"
    return yaml.safe_load(match.group(1)) or {}


def _skill_files() -> dict[str, Path]:
    return {
        path.parent.name: path
        for path in sorted(SKILLS_ROOT.glob("*/SKILL.md"))
    }


def test_package_versions_are_synchronized():
    manifest = _manifest()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = importlib.import_module("hermes_decision_inbox")
    cli = importlib.import_module("hermes_decision_inbox.cli")

    versions = {
        str(manifest["version"]),
        str(pyproject["project"]["version"]),
        str(package.__version__),
        str(cli.__version__),
    }
    assert len(versions) == 1
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions.pop())


def test_plugin_registration_matches_manifest_and_skill_tree():
    from hermes_decision_inbox import plugin

    class RecordingContext:
        def __init__(self):
            self.tools = {}
            self.skills = {}

        def register_tool(self, *, name, **kwargs):
            self.tools[name] = kwargs

        def register_system_prompt_section(self, **kwargs):
            pass

        def register_cli_command(self, **kwargs):
            pass

        def register_skill(self, *, name, path, **kwargs):
            self.skills[name] = Path(path)

    context = RecordingContext()
    plugin.register(context)
    manifest = _manifest()
    skill_files = _skill_files()

    assert set(context.tools) == set(manifest["provides_tools"])
    assert set(context.skills) == set(skill_files)
    for name, path in context.skills.items():
        assert path == skill_files[name]
        assert path.is_file()


def test_each_skill_declares_a_current_hermes_contract():
    manifest = _manifest()
    skill_files = _skill_files()
    assert skill_files

    expected = {
        "decision-inbox-routing": {
            "requires_toolsets": {"decision_inbox"},
            "references": ("publish_weekly_wiki_review",),
        },
        "decision-inbox-session-decisions": {
            "requires_toolsets": {"decision_inbox"},
            "references": ("publish_decision",),
        },
        "decision-inbox-read-decisions": {
            "requires_toolsets": {"decision_inbox"},
            "references": ("read_decisions",),
        },
        "decision-inbox-apply-resume-smoke": {
            "requires_toolsets": {"decision_inbox"},
            "references": ("publish_decision", "PUBLISHED", "Decision Inbox smoke test passed"),
        },
        "memory-wiki-decision-applier": {
            "requires_toolsets": {"terminal"},
            "references": ("decision-inbox apply-manifest",),
        },
    }
    assert set(expected) == set(skill_files)

    for name, path in skill_files.items():
        metadata = _skill_frontmatter(path)
        hermes_metadata = (metadata.get("metadata") or {}).get("hermes") or {}
        assert metadata["name"] == name
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(metadata["version"]))
        assert set(hermes_metadata["requires_toolsets"]) == expected[name]["requires_toolsets"]
        body = path.read_text(encoding="utf-8")
        for reference in expected[name]["references"]:
            assert reference in body

    assert set(manifest["provides_tools"]) == {
        "publish_weekly_wiki_review",
        "publish_decision",
        "read_decisions",
    }


def test_real_hermes_loads_tools_and_all_skills(tmp_path, monkeypatch):
    pytest.importorskip("hermes_cli")
    from hermes_cli import plugins as plugins_mod
    from tools.registry import registry

    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "hermes-decision-inbox").symlink_to(ROOT, target_is_directory=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {
            "enabled": ["hermes-decision-inbox"],
            "entries": {
                "hermes-decision-inbox": {
                    "settings": {"service_url": "http://127.0.0.1:8080"}
                }
            },
        }
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("DECISION_INBOX_PUBLISH_TOKEN", "test-token")

    manager = plugins_mod.PluginManager(scope_key=str(home.resolve()))
    manager.discover_and_load()
    loaded = manager._plugins.get("hermes-decision-inbox")

    assert loaded is not None
    assert loaded.enabled
    assert loaded.error is None
    assert set(loaded.tools_registered) == set(_manifest()["provides_tools"])
    assert set(manager.list_plugin_skills("hermes-decision-inbox")) == set(_skill_files())
    assert manager.find_plugin_skill(
        "hermes-decision-inbox:decision-inbox-session-decisions"
    ) == SKILLS_ROOT / "decision-inbox-session-decisions/SKILL.md"

    for tool_name in _manifest()["provides_tools"]:
        entry = registry.get_entry(tool_name, scope=str(home.resolve()))
        assert entry is not None
        assert entry.toolset == "decision_inbox"
        assert entry.schema["name"] == tool_name
