"""The checklist's candidate detector carries no standing surface of its own (#1709)."""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "checklist-surfaces.sh")


def make_repo(path, profile=False):
    os.makedirs(path)
    subprocess.run(["git", "init", "-q", path], check=True)
    if profile:
        os.makedirs(os.path.join(path, ".claude"))
        with open(os.path.join(path, ".claude", "checklist-profile.md"), "w") as handle:
            handle.write("profile\n")
    else:
        with open(os.path.join(path, "dirty.txt"), "w") as handle:
            handle.write("change\n")


def run(cwd, home, config=None):
    env = dict(os.environ, HOME=str(home), CONTINUITY_CONFIG=str(config or home / "absent.json"))
    result = subprocess.run(["bash", SCRIPT], cwd=cwd, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_unconfigured_detector_reports_only_the_session_repository(tmp_path):
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    make_repo(str(home / "Development" / "tool"), profile=True)
    make_repo(str(elsewhere))
    os.makedirs(home / "Obsidian")
    assert run(str(elsewhere), home) == []
    own = str(home / "Development" / "tool")
    assert run(own, home) == [f"discovered\t{own}/.claude/checklist-profile.md"]


def test_configured_standing_surfaces_and_baselines_are_candidates(tmp_path):
    home = tmp_path / "home"
    standing = home / "standing"
    own = tmp_path / "own"
    make_repo(str(standing), profile=True)
    make_repo(str(own))
    baselines = home / "baselines"
    os.makedirs(baselines)
    (baselines / "own.yaml").write_text("suite: x\n")
    config = home / "config.json"
    config.write_text(json.dumps({"checklist_surfaces": ["~/standing"],
                                  "checklist_suite_baselines_dir": "~/baselines"}))
    assert run(str(own), home, config) == [
        f"code\t{os.path.realpath(own)}",
        f"discovered\t{os.path.realpath(standing)}/.claude/checklist-profile.md",
    ]
