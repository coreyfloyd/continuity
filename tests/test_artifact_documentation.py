"""Published examples round-trip through the commands that own their formats."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs/examples"
HANDOFF = ROOT / "scripts/lib/handoff.py"
CHECKLIST = ROOT / "scripts/lib/checklist.py"


def sandbox_env(tmp_path):
    env = dict(os.environ)
    for key in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CONTINUITY_HARNESS", "HANDOFF_FILE"):
        env.pop(key, None)
    env.update(HANDOFF_STATE_DIR=str(tmp_path / "handoffs"),
               HANDOFF_LEDGER_DIR=str(tmp_path / "ledger"),
               CHECKLIST_STATE_DIR=str(tmp_path / "checklist"),
               CONTINUITY_CONFIG=str(EXAMPLES / "config.json"))
    return env


def command(module, *args, env, input=None):
    result = subprocess.run([sys.executable, str(module), *map(str, args)],
                            input=input, text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_handoff_example_passes_engine_lint(tmp_path):
    assert "lint-handoff: clean" in command(
        HANDOFF, "lint", EXAMPLES / "handoff.json", env=sandbox_env(tmp_path))


def test_profile_loads_and_plan_recomposes(tmp_path):
    spec = importlib.util.spec_from_file_location("example_checklist", CHECKLIST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = EXAMPLES / "profile.md"
    assert profile.read_bytes() == (ROOT / "tests/fixtures/profiles/user.md").read_bytes()
    module.compose([module.load_profile(profile)])
    actual = json.loads(command(CHECKLIST, "plan", "--user-profile", profile,
                                "--handoff", EXAMPLES / "handoff.json",
                                "--session-id", "example-session", env=sandbox_env(tmp_path)))
    actual["profiles"] = ["docs/examples/profile.md"]
    assert actual == json.loads((EXAMPLES / "plan.json").read_text())


def test_ledger_example_uses_only_engine_written_fields(tmp_path):
    env = sandbox_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("Example capture\n")
    command(HANDOFF, "ledger-file", "README.md", "--cwd", repo,
            "--session-id", "example-session", env=env)
    command(HANDOFF, "ledger-artifact", "--location", "https://example.invalid/review",
            "--disposition", "keep", "--cwd", repo, "--session-id", "example-session", env=env)
    command(CHECKLIST, "plan", "--session-id", "example-session", env=env)
    generated = [json.loads(line) for line in
                 (tmp_path / "ledger/session-touched-paths.example-session.jsonl").read_text().splitlines()]
    examples = [json.loads(line) for line in (EXAMPLES / "ledger.jsonl").read_text().splitlines()]
    schemas = {row["kind"]: set(row) for row in generated}
    assert {row["kind"] for row in examples} == set(schemas)
    for row in examples:
        assert set(row) == schemas[row["kind"]]


def test_configuration_example_is_read_by_config_get(tmp_path):
    config = json.loads((EXAMPLES / "config.json").read_text())
    assert config
    for key, value in config.items():
        output = command(HANDOFF, "config-get", key, env=sandbox_env(tmp_path)).strip()
        assert output == ("\n".join(value) if isinstance(value, list) else str(value))


def test_storage_artifacts_have_readme_outlines_and_examples_have_links():
    readme = (ROOT / "README.md").read_text()
    assert "## What the package writes" in readme
    section = readme.split("## What the package writes", 1)[1].split("\n## ", 1)[0]
    storage = (ROOT / "INTERFACE.md").read_text().split("## 1. Storage", 1)[1].split("## 2.", 1)[0]
    rows = re.findall(r"^\| ([^|]+) \|", storage, re.M)[2:]
    for artifact in rows:
        title = artifact.strip().split(" (", 1)[0]
        assert f"### {title}" in section
    for title in ("Checklist plan", "Checklist report input", "Session summary", "Configuration file", "Profile file"):
        assert f"### {title}" in section
    for path in EXAMPLES.iterdir():
        if path.is_file():
            assert f"(docs/examples/{path.name})" in readme, path.name
