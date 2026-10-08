#!/usr/bin/env python3

"""Install continuity-managed Codex hooks and persist their trust hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


STATE_HEADER = re.compile(
    r'^\[hooks\.state\.(?P<key>"(?:[^"\\]|\\.)*")\]\s*$'
)
AGENT_BLOCK_START = "# >>> continuity codex agents >>>"
SUPPORTED_HANDLER_FIELDS = {"type", "command", "timeout", "async"}


def atomic_write(path: Path, content: str) -> bool:
    old = path.read_text() if path.exists() else None
    if old == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def restore_file(path: Path, content: str | None) -> None:
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    atomic_write(path, content)


def handler_key(handler: dict[str, Any]) -> tuple[str, str]:
    handler_type = handler.get("type")
    command = handler.get("command")
    if handler_type != "command" or not isinstance(command, str) or not command:
        raise ValueError("tracked Codex hooks must be non-empty command handlers")
    return handler_type, command


def validate_source(source: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(source, dict) or not isinstance(source.get("hooks"), dict):
        raise ValueError("tracked hooks.json must contain a hooks object")
    hooks = source["hooks"]
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise ValueError("tracked hook events must map to group lists")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"tracked {event} hook group must contain a hooks list")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError(f"tracked {event} handlers must be objects")
                handler_key(handler)
                unsupported = set(handler) - SUPPORTED_HANDLER_FIELDS
                if unsupported:
                    names = ", ".join(sorted(unsupported))
                    raise ValueError(
                        f"tracked {event} handler has unsupported hash fields: {names}"
                    )
                timeout = handler.get("timeout")
                if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0:
                    raise ValueError(
                        f"tracked {event} command handlers require an integer timeout"
                    )
                if "async" in handler and not isinstance(handler["async"], bool):
                    raise ValueError(f"tracked {event} async must be boolean")
    return hooks


def merge_hooks(
    source_hooks: dict[str, list[dict[str, Any]]], target: Any
) -> dict[str, Any]:
    if not isinstance(target, dict):
        target = {}
    hooks = target.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("installed hooks.json hooks value must be an object")

    for event, source_groups in source_hooks.items():
        target_groups = hooks.setdefault(event, [])
        if not isinstance(target_groups, list):
            raise ValueError(f"installed {event} hooks value must be a list")
        for source_group in source_groups:
            source_keys = [handler_key(handler) for handler in source_group["hooks"]]
            matching_group = None
            matching_handlers: dict[tuple[str, str], int] = {}
            for group_index, candidate in enumerate(target_groups):
                if not isinstance(candidate, dict) or not isinstance(
                    candidate.get("hooks"), list
                ):
                    continue
                candidate_handlers = {
                    handler_key(handler): handler_index
                    for handler_index, handler in enumerate(candidate["hooks"])
                    if isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and isinstance(handler.get("command"), str)
                    and handler.get("command")
                }
                if all(key in candidate_handlers for key in source_keys):
                    matching_group = group_index
                    matching_handlers = candidate_handlers
                    break
            if matching_group is None:
                target_groups.append(json.loads(json.dumps(source_group)))
                continue

            installed_group = target_groups[matching_group]
            if "matcher" in source_group:
                installed_group["matcher"] = source_group["matcher"]
            elif "matcher" in installed_group:
                del installed_group["matcher"]
            for handler in source_group["hooks"]:
                installed_group["hooks"][matching_handlers[handler_key(handler)]] = (
                    json.loads(json.dumps(handler))
                )
    return target


def event_label(event: str) -> str:
    with_word_breaks = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", event)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", with_word_breaks).lower()


def normalized_timeout(event: str, timeout: int) -> int:
    # Codex normalizes SessionEnd/Interrupt to 1..3 seconds before hashing;
    # other events have a one-second floor. Mirror discovery.rs exactly.
    if event in ("SessionEnd", "Interrupt"):
        return min(3, max(1, timeout))
    return max(1, timeout)


def normalized_handler(event: str, handler: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "command",
        "command": handler["command"],
        "async": handler.get("async", False),
        "timeout": normalized_timeout(event, handler["timeout"]),
    }


def hook_hash(event: str, group: dict[str, Any], handler: dict[str, Any]) -> str:
    identity: dict[str, Any] = {
        "event_name": event_label(event),
        "hooks": [normalized_handler(event, handler)],
    }
    if "matcher" in group:
        identity["matcher"] = group["matcher"]
    canonical = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def managed_state(
    source_hooks: dict[str, list[dict[str, Any]]],
    installed: dict[str, Any],
    target_path: Path,
) -> dict[str, str]:
    installed_hooks = installed["hooks"]
    source_realpath = os.path.realpath(target_path)
    state: dict[str, str] = {}
    for event, source_groups in source_hooks.items():
        target_groups = installed_hooks[event]
        for source_group in source_groups:
            source_matcher = source_group.get("matcher")
            for source_handler in source_group["hooks"]:
                key = handler_key(source_handler)
                matches = []
                for group_index, target_group in enumerate(target_groups):
                    if not isinstance(target_group, dict):
                        continue
                    if target_group.get("matcher") != source_matcher:
                        continue
                    for handler_index, target_handler in enumerate(
                        target_group.get("hooks", [])
                    ):
                        if (
                            isinstance(target_handler, dict)
                            and target_handler.get("type") == "command"
                            and isinstance(target_handler.get("command"), str)
                            and handler_key(target_handler) == key
                        ):
                            matches.append((group_index, handler_index, target_group))
                if len(matches) != 1:
                    raise ValueError(
                        f"tracked {event} hook {key[1]!r} has {len(matches)} installed matches"
                    )
                group_index, handler_index, target_group = matches[0]
                state_key = (
                    f"{source_realpath}:{event_label(event)}:"
                    f"{group_index}:{handler_index}"
                )
                state[state_key] = hook_hash(event, target_group, source_handler)
    return state


def state_key_from_header(line: str) -> str | None:
    match = STATE_HEADER.match(line)
    if not match:
        return None
    try:
        value = json.loads(match.group("key"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def update_config_text(text: str, hashes: dict[str, str]) -> str:
    lines = text.splitlines()
    sections: dict[str, tuple[int, int]] = {}
    for start, line in enumerate(lines):
        key = state_key_from_header(line)
        if key is None:
            continue
        end = start + 1
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            end += 1
        if key in sections:
            raise ValueError(f"config.toml has duplicate hook state table for {key}")
        sections[key] = (start, end)

    missing: list[tuple[str, str]] = []
    insertions: list[tuple[int, str]] = []
    for key, trusted_hash in sorted(hashes.items()):
        assignment = f"trusted_hash = {json.dumps(trusted_hash)}"
        if key not in sections:
            missing.append((key, assignment))
            continue
        start, end = sections[key]
        trusted_lines = [
            index
            for index in range(start + 1, end)
            if re.match(r"^\s*trusted_hash\s*=", lines[index])
        ]
        if len(trusted_lines) > 1:
            raise ValueError(f"config.toml has duplicate trusted_hash for {key}")
        if trusted_lines:
            lines[trusted_lines[0]] = assignment
        else:
            insertions.append((start + 1, assignment))

    for index, assignment in sorted(insertions, reverse=True):
        lines.insert(index, assignment)

    if missing:
        insert_at = (
            lines.index(AGENT_BLOCK_START)
            if AGENT_BLOCK_START in lines
            else len(lines)
        )
        block: list[str] = []
        if insert_at > 0 and lines[insert_at - 1] != "":
            block.append("")
        for key, assignment in missing:
            if block and block[-1] != "":
                block.append("")
            block.extend(
                (f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]", assignment)
            )
        if insert_at < len(lines) and block[-1] != "":
            block.append("")
        lines[insert_at:insert_at] = block
    return "\n".join(lines).rstrip() + "\n"


def verify_with_hooks_list(
    codex_bin: str, codex_home: Path, cwd: Path, hashes: dict[str, str]
) -> None:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    process = subprocess.Popen(
        [codex_bin, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None

    def request(payload: dict[str, Any], response_id: int) -> dict[str, Any]:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == response_id:
                return response
        raise RuntimeError(f"app-server returned no response for request {response_id}")

    try:
        initialize = request(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "continuity-install", "version": "1"}
                },
            },
            1,
        )
        if "error" in initialize:
            raise RuntimeError(f"initialize failed: {initialize['error']}")
        response = request(
            {"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}},
            2,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if "error" in response:
        raise RuntimeError(f"hooks/list failed: {response['error']}")
    listed = {
        hook["key"]: hook
        for entry in response["result"]["data"]
        for hook in entry["hooks"]
    }
    failures = []
    for key, expected_hash in hashes.items():
        hook = listed.get(key)
        if hook is None:
            failures.append(f"{key}: missing")
        elif hook.get("currentHash") != expected_hash:
            failures.append(
                f"{key}: currentHash {hook.get('currentHash')} != {expected_hash}; "
                f"metadata={json.dumps(hook, sort_keys=True)}"
            )
        elif hook.get("trustStatus") != "trusted":
            failures.append(f"{key}: {hook.get('trustStatus')}")
    if failures:
        raise RuntimeError("hooks/list trust verification failed: " + "; ".join(failures))
