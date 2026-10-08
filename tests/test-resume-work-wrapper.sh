#!/bin/bash
# test-resume-work-wrapper.sh — #910 round 4.
#
# scripts/resume-work.sh's own contract, at the one seam it still owns: it
# finds the engine, runs it, and is answerable for what reaches stdout when the
# engine does not behave. Its header (":72-75") promises that EVERY failure
# path prints the same JSON shape. Three ways that used to be false:
#
#   F3  — a crashing engine gave the caller a Python traceback on stderr and
#         EMPTY stdout. A morning cron parsing stdout got nothing to parse.
#   F3b — the engine's exit code was never read at all (`$?` unchecked), so an
#         engine that printed valid JSON and exited 3 produced rc=0 and a
#         clean-looking report.
#   (new) an engine that exits 0 having printed something that is not JSON.
#
# The engine is stubbed by building a two-file temp tree — a copy of
# resume-work.sh next to a fake skills/resume-work/scripts/resume_work.py — so
# the $0-relative engine lookup the script header calls load-bearing is the
# thing under test, and no production surface gains a test-only env var.
#
# Bash 3.2 target (macOS system bash).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REAL="$ROOT/scripts/resume-work.sh"

# Missing gh is a hard FAIL, not a silent SKIP-as-PASS: a "no local X" skip
# must never look like a passing run (#910 eval round 1 F8, round 4 F-C).
command -v gh >/dev/null 2>&1 || { echo "FAIL: test-resume-work-wrapper: gh is not installed (required by the wrapper under test)"; exit 1; }

FAILURES=0
fail() { echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }
pass() { echo "ok:   $*"; }

SANDBOX="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$SANDBOX"' EXIT

FAKE_HOME="$SANDBOX/home"
WORKDIR="$SANDBOX/work"
mkdir -p "$FAKE_HOME/.claude" "$WORKDIR" "$SANDBOX/tmp"
printf 'macbook' > "$FAKE_HOME/.claude/.machine"

# A transcript the directory lens can resolve: two rows is enough, since the
# engine is stubbed and never reads them.
SESSION_ID="wrapper-fixture"
PDIR="$FAKE_HOME/.claude/projects/$(printf '%s' "$WORKDIR" | sed 's#[/.]#-#g')"
mkdir -p "$PDIR"
cat > "$PDIR/$SESSION_ID.jsonl" <<'JSONL'
{"type":"user","timestamp":"2026-09-01T10:00:00Z","gitBranch":"main","message":{"role":"user","content":"do the thing"}}
{"type":"assistant","timestamp":"2026-09-01T10:00:05Z","gitBranch":"main","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"stop_reason":"end_turn"}}
JSONL

# gh stub: the wrapper hard-requires gh and calls `gh repo view` for repo_nwo.
STUB_DIR="$SANDBOX/bin"; mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/gh" <<'STUB'
#!/bin/bash
case "${1:-}" in repo) echo "alexrivera/example-config" ;; *) echo "[]" ;; esac
STUB
chmod +x "$STUB_DIR/gh"

# build_engine <python-body> — a temp tree whose layout matches the $0-relative
# lookup: <tree>/scripts/resume-work.sh and <tree>/skills/resume-work/scripts/.
build_engine() {
    TREE="$SANDBOX/tree.$RANDOM"
    mkdir -p "$TREE/scripts/lib" "$TREE/skills/resume-work/scripts"
    cp "$REAL" "$TREE/scripts/resume-work.sh"
    # The wrapper asks the handoff module where handoffs are stored (#1572).
    cp "$ROOT/scripts/lib/handoff.py" "$TREE/scripts/lib/handoff.py"
    printf '%s\n' "$1" > "$TREE/skills/resume-work/scripts/resume_work.py"
}

run_wrapper() {  # -> stdout on fd1, rc in $RC
    OUT=$( cd "$WORKDIR" && HOME="$FAKE_HOME" CODEX_HOME="$FAKE_HOME/.codex" PATH="$STUB_DIR:$PATH" \
        TMPDIR="$SANDBOX/tmp" bash "$TREE/scripts/resume-work.sh" \
        --session "$SESSION_ID" 2>/dev/null )
    RC=$?
}

run_handoff_wrapper() {  # -> stdout on fd1, rc in $RC
    OUT=$( cd "$WORKDIR" && HOME="$FAKE_HOME" CODEX_HOME="$FAKE_HOME/.codex" PATH="$STUB_DIR:$PATH" \
        TMPDIR="$SANDBOX/tmp" bash "$TREE/scripts/resume-work.sh" \
        --handoff "$SANDBOX/selected.json" 2>/dev/null )
    RC=$?
}

is_error_object() {  # is_error_object <json>
    printf '%s' "$1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert isinstance(d, dict), d
assert d.get("version") == 2, d
assert d.get("error"), d
assert "commitments" in d, d
' >/dev/null 2>&1
}

# ---------------------------------------------------------------- W1
# Engine crashes: traceback on stderr, nothing on stdout, non-zero exit.
build_engine 'raise RuntimeError("boom")'
run_wrapper
if is_error_object "$OUT"; then pass "W1: a crashing engine still yields the error object on stdout"
else fail "W1: stdout was not a version-2 error object: [$OUT]"; fi
if [ "$RC" -ne 0 ]; then pass "W1: a crashing engine yields a non-zero exit ($RC)"
else fail "W1: exit 0 for a crashed engine"; fi

# ---------------------------------------------------------------- W2  (F3b)
# Engine prints VALID JSON and exits non-zero. The exit code is the only
# signal; discarding it produced a clean-looking report from a failed run.
build_engine 'import json, sys
print(json.dumps({"version": 2, "session": "x", "commitments": None}))
sys.exit(3)'
run_wrapper
if [ "$RC" -ne 0 ]; then pass "W2: a non-zero engine exit propagates ($RC)"
else fail "W2: engine exited 3, wrapper returned 0 — F3b"; fi
if is_error_object "$OUT"; then pass "W2: stdout is still the documented error object"
else fail "W2: stdout was not an error object: [$OUT]"; fi

# ---------------------------------------------------------------- W3
# Engine exits 0 having printed something that is not JSON.
build_engine 'print("not json at all")'
run_wrapper
if is_error_object "$OUT"; then pass "W3: non-JSON on a zero exit yields the error object"
else fail "W3: stdout was not an error object: [$OUT]"; fi
if [ "$RC" -ne 0 ]; then pass "W3: non-JSON output yields a non-zero exit ($RC)"
else fail "W3: exit 0 for unparseable engine output"; fi

# ---------------------------------------------------------------- W3b
# Engine exits 0 having printed VALID JSON that is not an object. `[]` is the
# machine-lens array shape the directory lens must never emit; json.load
# accepts it, so parseability alone is not the guard (#910 eval round 4, F-A).
build_engine 'print("[]")'
run_wrapper
if is_error_object "$OUT"; then pass "W3b: a bare JSON array on a zero exit yields the error object"
else fail "W3b: array passed the object guard: [$OUT]"; fi
if [ "$RC" -ne 0 ]; then pass "W3b: non-object JSON yields a non-zero exit ($RC)"
else fail "W3b: exit 0 for non-object engine output — F-A"; fi

# ---------------------------------------------------------------- W4
# The control: a well-behaved engine passes through byte-for-byte, so W1-W3 are
# not passing because the wrapper errors on everything.
build_engine 'import json
print(json.dumps({"version": 2, "session": "wrapper-fixture", "commitments": [], "commitments_reason": None}))'
run_wrapper
if [ "$RC" -eq 0 ]; then pass "W4: a healthy engine exits 0"
else fail "W4: healthy engine gave rc=$RC — [$OUT]"; fi
if printf '%s' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["commitments"]==[] and "error" not in d' 2>/dev/null; then
    pass "W4: the engine's own object reaches the caller unaltered"
else fail "W4: report was rewritten: [$OUT]"; fi

# ---------------------------------------------------------------- W5
# The selected-handoff path must use the same fail-closed engine wrapper as
# the transcript path. A direct `python3 "$engine" --handoff ...` leaks an
# empty stdout/traceback when the engine crashes.
build_engine 'raise RuntimeError("boom")'
run_handoff_wrapper
if is_error_object "$OUT"; then pass "W5: a crashing selected-handoff engine yields the error object";
else fail "W5: selected-handoff stdout was not an error object: [$OUT]"; fi
if [ "$RC" -ne 0 ]; then pass "W5: a crashing selected-handoff engine yields a non-zero exit ($RC)";
else fail "W5: selected-handoff crash returned 0"; fi

# ---------------------------------------------------------------- W6-W9 (#1709)
# The entry point carries no GitHub owner and no machine lens of its own: both
# come from the environment or the continuity configuration. The engine is a
# stub that reports the owner it was handed.
OWNER_ENGINE='import json, os
print(json.dumps({"version": 2, "owner": os.environ.get("RESUME_WORK_COMMITMENT_OWNER")}))'
CFG="$SANDBOX/continuity.json"

run_configured() {  # run_configured <config-file-or-empty> <args...> -> $OUT, $RC
    _cfg="$1"; shift
    OUT=$( cd "$WORKDIR" && HOME="$FAKE_HOME" CODEX_HOME="$FAKE_HOME/.codex" PATH="$STUB_DIR:$PATH" \
        TMPDIR="$SANDBOX/tmp" CONTINUITY_CONFIG="$_cfg" HANDOFF_STATE_DIR="$SANDBOX/handoffs" \
        bash "$TREE/scripts/resume-work.sh" "$@" 2>/dev/null )
    RC=$?
}

build_engine "$OWNER_ENGINE"
run_configured "$SANDBOX/absent.json" --session "$SESSION_ID"
if [ "$OUT" != "" ] && printf '%s' "$OUT" | python3 -c 'import json,sys; assert json.load(sys.stdin)["owner"] in ("", None)' 2>/dev/null; then
    pass "W6: with no owner configured, the engine is handed none (commitments report no_owner)"
else fail "W6: an owner leaked into an unconfigured run: [$OUT]"; fi

printf '{"commitment_owner": "configured-owner"}' > "$CFG"
run_configured "$CFG" --session "$SESSION_ID"
if printf '%s' "$OUT" | python3 -c 'import json,sys; assert json.load(sys.stdin)["owner"] == "configured-owner"' 2>/dev/null; then
    pass "W7: the configured owner reaches the engine"
else fail "W7: configured owner not passed: [$OUT]"; fi

run_configured "$SANDBOX/absent.json" --machine
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; assert json.load(sys.stdin)["error"] == "machine_lens_unavailable"' 2>/dev/null; then
    pass "W8: --machine with no lens configured reports machine_lens_unavailable"
else fail "W8: expected machine_lens_unavailable, rc=$RC: [$OUT]"; fi

LENS="$SANDBOX/lens.sh"
printf '#!/bin/bash\nprintf "host=%%s json=%%s owner=%%s\\n" "$RESUME_WORK_WANT_HOST" "$RESUME_WORK_AS_JSON" "$RESUME_WORK_COMMITMENT_OWNER"\n' > "$LENS"
printf '{"commitment_owner": "configured-owner", "machine_lens": "%s"}' "$LENS" > "$CFG"
run_configured "$CFG" --host box1 --json
if [ "$RC" -eq 0 ] && [ "$OUT" = "host=box1 json=1 owner=configured-owner" ]; then
    pass "W9: a configured machine lens runs with the host filter, output form, and owner"
else fail "W9: lens not run as configured, rc=$RC: [$OUT]"; fi

# ---------------------------------------------------------------- W10-W12 (#1679)
# A malformed continuity configuration is a configuration error. It used to be
# folded into `no_owner` (directory lens) or `machine_lens_unavailable`
# (machine lens) because the lookup discarded stderr and fell back to empty.
printf '{not json' > "$CFG"
run_configured "$CFG" --session "$SESSION_ID"
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; d = json.load(sys.stdin); assert d["error"] == "handoff_config_error" and "unreadable" in d["message"]' 2>/dev/null; then
    pass "W10: a malformed configuration on the directory lens reports handoff_config_error"
else fail "W10: expected handoff_config_error, rc=$RC: [$OUT]"; fi

run_configured "$CFG" --machine
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; assert json.load(sys.stdin)["error"] == "handoff_config_error"' 2>/dev/null; then
    pass "W11: a malformed configuration on the machine lens reports handoff_config_error"
else fail "W11: expected handoff_config_error, rc=$RC: [$OUT]"; fi

printf '{"commitment_owner": 7}' > "$CFG"
run_configured "$CFG" --session "$SESSION_ID"
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | python3 -c 'import json,sys; d = json.load(sys.stdin); assert d["error"] == "handoff_config_error" and "commitment_owner" in d["message"]' 2>/dev/null; then
    pass "W12: a wrongly typed value is a configuration error naming the key"
else fail "W12: expected handoff_config_error naming commitment_owner, rc=$RC: [$OUT]"; fi

echo
if [ "$FAILURES" -eq 0 ]; then echo "test-resume-work-wrapper: PASS"; exit 0; fi
echo "test-resume-work-wrapper: FAIL — $FAILURES assertion(s)"
exit 1
