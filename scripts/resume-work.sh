#!/usr/bin/env bash
# resume-work.sh — cold-start re-entry, two lenses.
#
# Directory/session lens, plus a machine lens supplied by configuration.
#
# DEFAULT (handoff-first): lists handoff candidates for this directory and
#   its sibling worktrees. An unrelated machine-wide handoff never suppresses
#   this directory's fallback. When neither current nor sibling candidates
#   exist, reads the newest other local transcript and cross-checks git.
#   Every directory result is a JSON object including commitments fields;
#   --json remains a no-op for this path and controls the machine lens below.
#
# --machine / --all-hosts / --host (machine lens): "what does this MACHINE
#   hold?" Runs the machine-lens command named by RESUME_WORK_MACHINE_LENS or
#   the `machine_lens` configuration value, handing it the host filter and
#   output form in RESUME_WORK_WANT_HOST and RESUME_WORK_AS_JSON. With none
#   configured these flags report `machine_lens_unavailable`.
#
# The directory lens is primary because it does not depend on any claim
# plumbing, which under-fires: a session resuming an already-active ticket
# never emits a claim.
#
# Usage:
#   resume-work.sh                 # handoff candidates, or local transcript fallback (JSON)
#   resume-work.sh --handoff <p>   # selected handoff briefing (JSON)
#   resume-work.sh --session <id>  # directory lens: a specific transcript (JSON)
#   resume-work.sh --machine       # machine lens: this host's GitHub claims
#   resume-work.sh --all-hosts     # machine lens: every host
#   resume-work.sh --host <name>   # machine lens: a specific host
#   resume-work.sh --json          # raw records (machine lens only)
#   resume-work.sh --no-commitments  # skip the ~25 gh round trips COMMITMENTS costs
#   resume-work.sh --prune           # archive dead-session handoffs (never delete)
#
# COMMITMENTS (#851, JSON since #910): the resumed session's COMMITMENTS —
# tickets carrying ITS claim, at any status — as the directory lens's own
# "commitments" field: null if not collected (--no-commitments, no
# resolvable session id, or a failed gh scan), [] if collected and none
# found, an array of {repo, number, title, status_labels, handoff_pending,
# branch, url} objects otherwise. A companion "commitments_reason" field
# says WHY whenever the value is not a complete collection — skipped,
# no_session_id, gh_not_found, gh_search_failed, partial_claim_fetch,
# internal_error, lens_error — and is null when the array is complete.
# The record binding's commitments sweep stamps those claims with a
# handoff-pending state, so a promise that was never ticket-shaped ("verify
# the nightly", "watch #820 for 7 days") is inherited rather than
# re-remembered. The package ships no claim tool: claims come from the
# user's own setup. Scoped by claim, not by status label: status/next is a
# repo-wide queue, a claim is session-scoped.
#
# The scan itself lives in skills/resume-work/scripts/resume_work.py: the
# directory lens is ONE python process. This
# script keeps argument parsing and the machine lens only. It used to run
# the scan in bash and merge two JSON structures through an environment
# variable, and every open fail-mode finding sat on that seam.
set -uo pipefail

# ---------------------------------------------------------------- args
# The host name the commitments scan filters on: RESUME_WORK_HOST, else the
# `host` configuration value, else "unknown" (resolved after the configuration
# helpers below).
host="${RESUME_WORK_HOST:-}"
# The module that owns where handoffs are stored and reads the configuration.
handoff_py="${HANDOFF_PY:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/handoff.py}"
# Expand a leading ~ in a configured path.
expand_home() { case "$1" in "~"|"~/"*) printf '%s' "$HOME${1#\~}" ;; *) printf '%s' "$1" ;; esac; }
# Read one continuity configuration value into the variable named by $2. A
# malformed configuration makes it return 1 with the reason in $config_error,
# for the caller to report as `handoff_config_error`; an absent key is empty.
config_value() {  # config_value <key> <variable>
  local value
  [ -f "$handoff_py" ] || { printf -v "$2" '%s' ""; return 0; }
  if ! value="$(python3 "$handoff_py" config-get "$1" 2>&1)"; then
    config_error="$value"
    return 1
  fi
  printf -v "$2" '%s' "$value"
}
config_error=""
want_host=""
want_host_set=""
lens="dir"            # dir | machine
want_session=""
want_handoff=""
as_json=""
no_commitments=""
prune=""
directory_option=""
machine_option=""
# The handoff-marker state string and the RESUME_WORK_COMMITMENT_LIMIT /
# RESUME_WORK_COMMITMENT_JOBS knobs live in resume_work.py --
# one definition, read by the process that actually does the scan.

# Every failure path emits the same JSON object, including argument parsing.
json_error() {  # json_error <error-code> <message>
  python3 -c '
import json, sys
print(json.dumps({"version": 2, "error": sys.argv[1], "message": sys.argv[2],
                  "commitments": None, "commitments_reason": "lens_error"}, indent=2))
' "$1" "$2"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --session)
      [ $# -ge 2 ] || { json_error "usage" "--session requires a session id"; exit 2; }
      [ -n "$2" ] || { json_error "usage" "--session requires a non-empty session id"; exit 2; }
      [ -z "$machine_option" ] || { json_error "usage" "--session conflicts with $machine_option"; exit 2; }
      [ -z "$directory_option" ] || { json_error "usage" "--session conflicts with $directory_option"; exit 2; }
      directory_option="--session"
      want_session="$2"; shift 2 ;;
    --handoff)
      [ $# -ge 2 ] || { json_error "usage" "--handoff requires a file path"; exit 2; }
      [ -z "$machine_option" ] || { json_error "usage" "--handoff conflicts with $machine_option"; exit 2; }
      [ -z "$directory_option" ] || { json_error "usage" "--handoff conflicts with $directory_option"; exit 2; }
      directory_option="--handoff"
      want_handoff="$2"; shift 2 ;;
    --machine)
      [ -z "$directory_option" ] || { json_error "usage" "--machine conflicts with $directory_option"; exit 2; }
      machine_option="--machine"; lens="machine"; shift ;;
    --host)
      [ $# -ge 2 ] || { json_error "usage" "--host requires a host name"; exit 2; }
      [ -z "$directory_option" ] || { json_error "usage" "--host conflicts with $directory_option"; exit 2; }
      machine_option="--host"; lens="machine"; want_host="$2"; want_host_set=1; shift 2 ;;
    --all-hosts)
      [ -z "$directory_option" ] || { json_error "usage" "--all-hosts conflicts with $directory_option"; exit 2; }
      machine_option="--all-hosts"; lens="machine"; want_host=""; want_host_set=1; shift ;;
    --json)      as_json=1; shift ;;
    --no-commitments) no_commitments=1; shift ;;
    --prune)
      [ -z "$machine_option" ] || { json_error "usage" "--prune conflicts with $machine_option"; exit 2; }
      [ -z "$directory_option" ] || { json_error "usage" "--prune conflicts with $directory_option"; exit 2; }
      directory_option="--prune"; prune=1; shift ;;
    # print the header comment block, whatever its length: stop at the first
    # non-comment line. A hardcoded range goes stale the moment the header grows
    # and starts spilling code into the help output (#851).
    -h|--help)   sed -n '2,${/^[^#]/q;p;}' "$0"; exit 0 ;;
    *) json_error "usage" "unrecognized option: $1"; exit 2 ;;
  esac
done

if [ -z "$host" ]; then
  config_value host host \
    || { json_error "handoff_config_error" "resume-work: $config_error"; exit 1; }
  host="${host:-unknown}"
fi
[ -n "$want_host_set" ] || want_host="$host"

# GitHub owner whose issues carry claim comments; the engine reads it for the
# commitments scan and a machine lens scopes its search with it. From the
# environment, else the `commitment_owner` configuration value; with neither,
# commitments report `no_owner`. An unreadable configuration is reported as
# `handoff_config_error`, never folded into `no_owner`.
if [ -z "${RESUME_WORK_COMMITMENT_OWNER:-}" ]; then
  config_value commitment_owner RESUME_WORK_COMMITMENT_OWNER \
    || { json_error "handoff_config_error" "resume-work: $config_error"; exit 1; }
fi
export RESUME_WORK_COMMITMENT_OWNER="${RESUME_WORK_COMMITMENT_OWNER:-}"

# ================================================================== DIRECTORY LENS
engine_path() {
  local self_dir
  if [ -n "${RESUME_WORK_ENGINE:-}" ]; then
    [ -f "$RESUME_WORK_ENGINE" ] && printf '%s' "$RESUME_WORK_ENGINE"
    return 0
  fi
  self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  if [ -f "$self_dir/../skills/resume-work/scripts/resume_work.py" ]; then
    printf '%s' "$self_dir/../skills/resume-work/scripts/resume_work.py"
  fi
}

run_engine_json() {
  local engine out_file err_file engine_rc
  engine="$(engine_path)"
  if [ -z "$engine" ]; then
    json_error "engine_not_found" "cannot find resume_work.py (looked at RESUME_WORK_ENGINE and next to $0 under skills/resume-work/scripts)"
    return 1
  fi
  out_file="$(mktemp "${TMPDIR:-/tmp}/resume-report.XXXXXX" 2>/dev/null || mktemp /tmp/resume-report.XXXXXX 2>/dev/null)"
  err_file="$(mktemp "${TMPDIR:-/tmp}/resume-report-err.XXXXXX" 2>/dev/null || mktemp /tmp/resume-report-err.XXXXXX 2>/dev/null)"
  if [ -z "$out_file" ] || [ -z "$err_file" ]; then
    json_error "tmpfile_failed" "cannot create a temporary file (TMPDIR=${TMPDIR:-/tmp})"
    rm -f "$out_file" "$err_file"
    return 1
  fi
  python3 "$engine" "$@" >"$out_file" 2>"$err_file"
  engine_rc=$?
  if ! python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(open(sys.argv[1])), dict) else 1)' "$out_file" 2>/dev/null; then
    json_error "engine_bad_output" "resume_work.py exited $engine_rc: $(tr '\n' ' ' < "$err_file" | tail -c 400)"
    rm -f "$out_file" "$err_file"
    return 1
  fi
  if [ "$engine_rc" -ne 0 ] && ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("error") else 1)' "$out_file" 2>/dev/null; then
    json_error "engine_failed" "resume_work.py exited $engine_rc: $(tr '\n' ' ' < "$err_file" | tail -c 400)"
    rm -f "$out_file" "$err_file"
    return "$engine_rc"
  fi
  cat "$out_file"
  rm -f "$out_file" "$err_file"
  return "$engine_rc"
}

# ---------------------------------------------------------------- dispatch
if [ "$lens" = "machine" ]; then
  machine_lens="${RESUME_WORK_MACHINE_LENS:-}"
  if [ -z "$machine_lens" ]; then
    config_value machine_lens machine_lens \
      || { json_error "handoff_config_error" "resume-work: $config_error"; exit 1; }
  fi
  machine_lens="$(expand_home "$machine_lens")"
  if [ ! -f "$machine_lens" ]; then
    json_error "machine_lens_unavailable" "resume-work: no machine lens is configured (RESUME_WORK_MACHINE_LENS or machine_lens in the continuity configuration)"
    exit 1
  fi
  RESUME_WORK_WANT_HOST="$want_host" RESUME_WORK_AS_JSON="$as_json" exec bash "$machine_lens"
else
  if [ -n "$want_handoff" ]; then
    run_engine_json --handoff "$want_handoff"
  else
    # The handoff module owns where handoffs are stored (HANDOFF_STATE_DIR,
    # else the continuity configuration, else its default).
    if ! state_dir="$(python3 "$handoff_py" state-dir 2>&1)"; then
      json_error "handoff_config_error" "resume-work: $state_dir"
      exit 1
    fi
    current_session="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
    # The invoking Codex thread is excluded too (#1453): the automatic lens
    # lists both providers' sessions, and under Codex the newest rollout for
    # this directory is the caller itself. Exclusion only; the store that
    # owns a session is decided by the engine from the stores, not this env.
    engine_args=(--directory-resume "$state_dir" "$PWD" "$HOME" --codex-home "${CODEX_HOME:-$HOME/.codex}"
                 --current-session "$current_session" --current-session "${CODEX_THREAD_ID:-}"
                 --commitments-host "$host")
    [ -n "$want_session" ] && engine_args+=(--transcript-session "$want_session")
    [ -n "$no_commitments" ] && engine_args+=(--no-commitments)
    [ -n "$prune" ] && engine_args+=(--prune)
    run_engine_json "${engine_args[@]}"
  fi
fi
