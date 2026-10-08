"""What the package ships: six hook adapters, no reference to a script it lacks, no personal reference.

Everything in the repository ships, tests and fixtures included, so the checks
walk the whole tree.
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, "scripts", "lib", "reference_check.py")
SKIPPED_DIRS = {".git", "__pycache__", ".pytest_cache"}

# The six hooks: session start restores, pre-compaction saves, prompt submit warns
# about context, a tool call feeds the ledger, stop checks staleness, and a tool
# call reminds about the checklist. Each is a thin adapter over a shared module.
HOOK_ADAPTERS = {
    "scripts/post-compact-restore.sh": "lib/handoff.py",
    "scripts/pre-compact.sh": "lib/handoff.py",
    "scripts/context-guard.sh": "lib/handoff.py",
    "hooks/handoff-ledger.sh": "lib/handoff.py",
    "hooks/handoff-staleness-guard.sh": "lib/handoff.py",
    "hooks/checklist-nudge.sh": "lib/checklist.py",
}


def shipped_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(files):
            yield os.path.join(root, name)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        return None


def test_all_six_hook_adapters_ship_and_call_their_module():
    assert len(HOOK_ADAPTERS) == 6
    for path, module in HOOK_ADAPTERS.items():
        full = os.path.join(ROOT, path)
        assert os.path.isfile(full), path
        assert module in read_text(full), path


def test_every_hook_adapter_fails_open_on_an_empty_payload(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), HANDOFF_STATE_DIR=str(tmp_path / "state"),
               HANDOFF_LEDGER_DIR=str(tmp_path / "ledger"), HANDOFF_COMPACT_STATE_DIR=str(tmp_path / "compact"),
               CONTINUITY_CONFIG=str(tmp_path / "config.json"), TMPDIR=str(tmp_path))
    for path in HOOK_ADAPTERS:
        result = subprocess.run(["bash", os.path.join(ROOT, path)], input="{}", text=True, capture_output=True, env=env)
        assert result.returncode == 0, (path, result.stderr)


SCRIPT_NAME = re.compile(r"(?<![\w.-])([\w./<>-]+\.(?:sh|py|mjs))\b")
# Names that are not scripts the package should ship: a project marker file the
# surface detector looks for.
NOT_SCRIPTS = {"setup.py"}


def test_no_shipped_file_names_a_script_the_package_does_not_ship():
    """Skills, hooks, scripts, and documents: the files a user reads or runs."""
    shipped = {os.path.basename(path) for path in shipped_files()}
    missing = {}
    for path in shipped_files():
        if os.path.relpath(path, ROOT).startswith("tests" + os.sep):
            continue  # tests name the stubs they build, and recorded sessions name what they ran
        text = read_text(path)
        if text is None:
            continue
        for match in SCRIPT_NAME.finditer(text):
            name = os.path.basename(match.group(1).strip("<>"))
            if name in NOT_SCRIPTS or name in shipped:
                continue
            missing.setdefault(name, set()).add(os.path.relpath(path, ROOT))
    assert not missing, {name: sorted(paths) for name, paths in missing.items()}


def test_the_resume_work_skill_names_no_claim_tool():
    text = read_text(os.path.join(ROOT, "skills", "resume-work", "SKILL.md"))
    assert "claim-comment" not in text and "claim-heartbeat" not in text


TERMS = os.environ.get("REFERENCE_CHECK_TERMS")


@pytest.mark.skipif(not TERMS, reason="REFERENCE_CHECK_TERMS names the private term list")
def test_no_shipped_file_carries_a_listed_term():
    """The list of terms lives outside this repository, so the check itself ships none."""
    result = subprocess.run([sys.executable, CHECK, "--terms", TERMS, ROOT], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr
