"""Setup's file-writing seam, through the public profile composer."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / 'skills/setup/scripts/profile.py'
COMPOSER = ROOT / 'scripts/lib/checklist.py'


def write(home, bindings, *extra, present=True):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CHECKLIST_', 'XDG_', 'CODEX_', 'CLAUDE_'))}
    env['HOME'] = str(home)
    args = [sys.executable, str(WRITER), '--bindings', json.dumps(bindings), *extra]
    if present:
        args.append('--user-present')
    return subprocess.run(args, env=env, capture_output=True, text=True)


def plan(path):
    return subprocess.run([sys.executable, str(COMPOSER), 'plan', '--user-profile', str(path)],
                          capture_output=True, text=True,
                          env={key: value for key, value in os.environ.items()
                               if not key.startswith(('CODEX_', 'CLAUDE_'))})


def test_clean_home_declined_slots_are_unbound_including_lessons(tmp_path):
    result = write(tmp_path, dict.fromkeys(('record', 'knowledge', 'lessons', 'persist')),
                   '--record-read', '/read-work')
    assert result.returncode == 0, result.stderr
    profile = tmp_path / '.config/checklist/profile.md'
    composed = plan(profile)
    assert composed.returncode == 0, composed.stderr
    value = json.loads(composed.stdout)
    assert value['record_read'] == {'command': '/read-work'}
    assert [s['id'] for s in value['steps']] == ['record', 'knowledge', 'lessons', 'persist', 'handoff']
    assert all(s['binding'] is None for s in value['steps'][:4])
    assert value['steps'][2]['carries'] == ['lesson']


def test_extend_preserves_prose_sections_steps_and_unselected_bindings(tmp_path):
    profile = tmp_path / 'profile.md'
    existing = {'name': 'user', 'bindings': {'record': {'command': '/record-step',
                'consumes': ['progress'], 'section': 'Record'}},
                'steps': [{'id': 'tidy', 'after': 'persist', 'instructions': 'Tidy up'}]}
    prefix = '# My profile\n\nKeep these notes.\n\n```checklist-profile\n'
    suffix = '\n```\n\n## Record\n\nKeep my record instructions.\n'
    profile.write_text(prefix + json.dumps(existing) + suffix)
    result = write(tmp_path, {'knowledge': {'command': '/knowledge-step', 'consumes': ['knowledge']}},
                   '--profile', str(profile), '--record-read', '/read-work')
    assert result.returncode == 0, result.stderr
    text = profile.read_text()
    assert text.startswith(prefix) and text.endswith(suffix)
    updated = json.loads(text.split('```checklist-profile\n')[1].split('\n```')[0])
    assert updated['bindings']['record'] == existing['bindings']['record']
    assert updated['steps'] == existing['steps']
    assert plan(profile).returncode == 0


def test_json_profile_and_invalid_changes_do_not_damage_existing_file(tmp_path):
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'name': 'user', 'steps': []}))
    assert write(tmp_path, {'persist': None}, '--profile', str(profile)).returncode == 0
    before = profile.read_bytes()
    result = write(tmp_path, {'record': {'command': '', 'consumes': []}}, '--profile', str(profile))
    assert result.returncode != 0
    assert profile.read_bytes() == before


def test_no_presence_confirmation_writes_nothing(tmp_path):
    result = write(tmp_path, {}, present=False)
    assert result.returncode != 0
    assert 'user present' in result.stderr.lower()
    assert not list(tmp_path.iterdir())
