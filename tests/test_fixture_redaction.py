"""The shipped Claude session fixture carries structure, not session content.

tests/fixtures/resume-work-session.jsonl is cut from a private session. The
redactor's --redact-commands mode removes every command, tool result, thinking
block, and claude.ai session URL; these tests pin that the shipped copy has
none, and that running the redactor over it again changes nothing.

Run with: uv run --with pytest pytest tests/test_fixture_redaction.py -q
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "resume-work-session.jsonl")
REDACTOR = os.path.join(HERE, "fixtures", "redact_resume_work_transcript.py")
SESSION_URL = re.compile(r"claude\.ai/(code/)?session_")


def _rows():
    with open(FIXTURE, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _blocks(rows):
    for row in rows:
        content = row.get("message", {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield row, block


def test_fixture_has_no_thinking_block_or_raw_tool_output():
    rows = _rows()
    assert not any(b.get("type") == "thinking" for _, b in _blocks(rows))
    assert not any("toolUseResult" in r for r in rows)


def test_fixture_has_no_session_url():
    assert not SESSION_URL.search(open(FIXTURE, encoding="utf-8").read())


def test_fixture_tool_results_are_placeholders():
    results = [b for _, b in _blocks(_rows()) if b.get("type") == "tool_result"]
    assert results
    for block in results:
        content = block["content"]
        texts = [content] if isinstance(content, str) else [p.get("text", "") for p in content
                                                              if isinstance(p, dict)]
        assert all(not t.strip() or t.startswith("[REDACTED") for t in texts), texts


def test_fixture_commands_and_free_text_inputs_are_placeholders():
    uses = [b for _, b in _blocks(_rows()) if b.get("type") == "tool_use"]
    assert uses
    for block in uses:
        for key in ("command", "description", "prompt", "content", "message", "summary", "query"):
            if key in block["input"]:
                assert block["input"][key].startswith("[REDACTED"), (block["name"], key)
        if "file_path" in block["input"]:
            assert block["input"]["file_path"].startswith("/redacted/")


def test_fixture_keeps_the_record_structure():
    rows = _rows()
    assert len(rows) == 699
    assert {"user", "assistant"} == {r["type"] for r in rows}
    assert {b["type"] for _, b in _blocks(rows)} == {"text", "tool_use", "tool_result"}
    assert {"tool_use", "end_turn"} <= {r["message"].get("stop_reason") for r in rows
                                         if r["type"] == "assistant"}


def test_redactor_drops_non_text_tool_result_payloads(tmp_path):
    payload = "iVBORw0KGgoPRIVATE"
    row = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
            {"type": "tool_reference", "tool_name": "SendMessage"}]}]}}
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    src.write_text(json.dumps(row) + "\n")
    done = subprocess.run([sys.executable, "-I", REDACTOR, "--redact-commands", str(src), str(out)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    blocks = json.loads(out.read_text())["message"]["content"][0]["content"]
    assert blocks == [{"type": "image"}, {"type": "tool_reference", "tool_name": "SendMessage"}]


def test_redactor_leaves_the_shipped_fixture_unchanged(tmp_path):
    out = tmp_path / "again.jsonl"
    result = subprocess.run([sys.executable, "-I", REDACTOR, "--redact-commands", "--neutral-names",
                             FIXTURE, str(out)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == open(FIXTURE, "rb").read()


NEUTRAL_NAME = re.compile(r"^(main|feature-[a-z]+)$")


def test_no_fixture_keeps_a_worktree_or_branch_name():
    """Worktree and branch names carry ticket numbers and work titles; --neutral-names
    maps them to stand-ins in every fixture (gitBranch, Codex git.branch, every cwd)."""
    names = set()
    for name in sorted(os.listdir(os.path.join(HERE, "fixtures"))):
        if not name.endswith(".jsonl"):
            continue
        text = open(os.path.join(HERE, "fixtures", name), encoding="utf-8").read()
        names |= {(name, m) for m in re.findall(r'"(?:gitBranch|branch)": "([^"]*)"', text)}
        names |= {(name, m) for m in re.findall(r'/\.worktrees/([^"/]*)', text)}
    assert names
    assert not [n for n in names if not NEUTRAL_NAME.match(n[1])], sorted(names)
