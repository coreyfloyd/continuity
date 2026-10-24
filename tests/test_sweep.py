"""Sweep command contracts; every store and profile is isolated from the user."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'scripts/lib/sweep.py'
FIXTURE = ROOT / 'tests/fixtures/sweep-handoff.json'


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    store = tmp_path / 'handoffs'
    store.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'sweep': {'report_location': str(tmp_path / 'reports')}}))
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / '.codex'),
               CONTINUITY_CONFIG=str(config), HANDOFF_STATE_DIR=str(store),
               PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0')
    return tmp_path, store, config, env


def source(sandbox, name='source', directory=None, loops=None, version=2):
    base, store, _, _ = sandbox
    data = json.loads(FIXTURE.read_text())
    data['subject'] = {'working_directory': str(directory or base), 'session_id': name}
    data['git'] = {'repository_root': '', 'branch': 'main', 'head': ''}
    data['schema_version'] = version
    data['uncommitted_files'] = []
    data['resume']['open_loops'] = loops if loops is not None else [
        {'context': 'Optional observation', 'disposition': 'consider'}]
    path = store / (name + '.json')
    path.write_text(json.dumps(data))
    old = int(time.time()) - 5 * 86400
    os.utime(path, (old, old))
    return path


def command(sandbox, *args, code=0):
    result = subprocess.run([sys.executable, '-B', str(ENGINE), *args],
                            env=sandbox[3], text=True, capture_output=True)
    assert result.returncode == code, result.stderr
    return result


def report(sandbox, *args):
    result = command(sandbox, 'run', '--scope', 'all', *args)
    path = Path(result.stdout.strip())
    content = path.read_text()
    data = json.loads(content.split('```json\n', 1)[1].rsplit('\n```', 1)[0])
    return content, data


def decisions(sandbox, entries):
    path = sandbox[0] / 'decisions.json'
    inspected = json.loads(command(sandbox, 'inspect', '--scope', 'all').stdout)
    rows = []
    for candidate in inspected['candidates']:
        for index, item in enumerate(candidate['items']):
            rows.append(dict(handoff=candidate['path'], identity=item['identity'],
                             **entries[index % len(entries)]))
    path.write_text(json.dumps(rows))
    return ['--decisions', str(path)]


def test_watch_report_covers_legacy_drop_and_changes_only_report(sandbox):
    path = source(sandbox, version=1)
    before = path.read_bytes()
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'No stated outcome'}])
    text, data = report(sandbox, *args)
    assert data['mode'] == 'watch-only'
    assert data['run_id'] and data['host'] and data['started_at'] and data['ended_at']
    section = data['directories'][str(sandbox[0])]
    assert section['items_dropped'][0]['rule'] == 'R6'
    assert section['items_dropped'][0]['reason'] == 'No stated outcome'
    assert section['handoffs_archived'][0]['outcome'] == 'dry run'
    assert path.read_bytes() == before
    assert list(sandbox[1].iterdir()) == [path]
    assert len(list((sandbox[0] / 'reports').iterdir())) == 1
    headings = ['Asks raised', 'Open sessions with idle time', 'New tickets with priority',
                'Comments added', 'Documents changed', 'Items dropped with reasons',
                'Worktrees removed', 'Handoffs archived']
    assert [text.index('### ' + h) for h in headings] == sorted(text.index('### ' + h) for h in headings)


def test_no_judgment_retains_every_item_and_parse_errors(sandbox):
    path = source(sandbox)
    (sandbox[1] / 'bad.json').write_text('{')
    _, data = report(sandbox)
    section = data['directories'][str(sandbox[0])]
    assert section['filing_outcomes'][0]['outcome'] == 'pending judgment'
    assert section['handoffs_archived'] == []
    assert data['errors'][0]['path'].endswith('bad.json')
    assert data['errors'][0]['reason']
    assert path.exists()


def test_empty_run_and_recent_handoff(sandbox):
    text, data = report(sandbox)
    assert data['nothing_to_do'] and 'Nothing to do.' in text
    path = source(sandbox)
    os.utime(path, None)
    _, data = report(sandbox)
    assert data['nothing_to_do']


def test_newest_claude_and_codex_transcript_controls_idle(sandbox):
    for provider in ('claude', 'codex'):
        path = source(sandbox, name=provider)
        if provider == 'claude':
            slug = str(sandbox[0]).replace('/', '-').replace('.', '-')
            transcript = Path(sandbox[3]['HOME']) / '.claude/projects' / slug / 'claude.jsonl'
            transcript.parent.mkdir(parents=True)
            transcript.write_text('{}\n')
        else:
            transcript = Path(sandbox[3]['CODEX_HOME']) / 'sessions/2026/10/24/rollout-2026-10-24T00-00-00-codex.jsonl'
            transcript.parent.mkdir(parents=True)
            transcript.write_text('{}\n')
        _, data = report(sandbox)
        assert data['nothing_to_do'], (provider, data)
        path.unlink()


def test_exact_non_repository_scope(sandbox):
    source(sandbox, 'one', directory=sandbox[0] / 'one')
    source(sandbox, 'two', directory=sandbox[0] / 'one/child')
    result = command(sandbox, 'inspect', '--scope', str(sandbox[0] / 'one'))
    assert [c['session_id'] for c in json.loads(result.stdout)['candidates']] == ['one']


def test_repository_scope_includes_sibling_worktrees(sandbox):
    repo, sibling = sandbox[0] / 'repo', sandbox[0] / 'sibling'
    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)
    git('init', '-q', repo)
    git('-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-qm', 'base')
    git('-C', repo, 'worktree', 'add', '-qb', 'feature-a', sibling)
    source(sandbox, 'main', directory=repo)
    source(sandbox, 'sibling', directory=sibling)
    result = command(sandbox, 'inspect', '--scope', str(repo))
    assert {c['session_id'] for c in json.loads(result.stdout)['candidates']} == {'main', 'sibling'}


def test_all_filing_rules_share_checklist_validation_without_binding_calls(sandbox):
    items = [{'context': f'Item {i}', 'disposition': 'file'} for i in range(7)]
    items[2].update(type='lesson', session_id='original', produced_at='2026-10-01T00:00:00Z')
    source(sandbox, loops=items)
    profile = sandbox[0] / 'profile.json'
    profile.write_text(json.dumps({'name': 'test', 'ask': {'command': 'MUST NEVER RUN'},
        'bindings': {key: {'command': 'MUST NEVER RUN', 'consumes': []}
                     for key in ('record', 'lessons', 'knowledge')}}))
    entries = [
        {'rule': 'R1', 'information_only': True, 'reason': 'Information only'},
        {'rule': 'R3', 'destination': 'ask', 'choices': ['approve', 'decline']},
        {'rule': 'R4', 'destination': 'lessons'},
        {'rule': 'R7', 'destination': 'README.md'},
        {'rule': 'R2', 'destination': 'https://example.test/ticket', 'read_status': 'ok'},
        {'rule': 'R5', 'destination': 'record', 'read_status': 'ok', 'priority': 'p2', 'priority_reason': 'Needed'},
        {'rule': 'R6', 'reason': 'No outcome'}]
    _, data = report(sandbox, *decisions(sandbox, entries), '--user-profile', str(profile))
    section = data['directories'][str(sandbox[0])]
    assert len(section['filing_outcomes']) == 7
    assert all(row['outcome'] == 'dry run' for row in section['filing_outcomes'])
    assert section['new_tickets'][0]['priority'] == 'p2'
    assert section['asks_raised'][0]['ask']['choices'] == ['approve', 'decline']
    assert section['handoffs_archived']
    assert section['filing_outcomes'][2]['identity']['session_id'] == 'original'


def test_failed_reads_and_unbound_asks_keep_handoff(sandbox):
    source(sandbox)
    _, data = report(sandbox, *decisions(sandbox, [{'rule': 'R5', 'destination': 'record'}]))
    section = data['directories'][str(sandbox[0])]
    assert section['filing_outcomes'][0]['outcome'] == 'read failed'
    assert not section['handoffs_archived']
    _, data = report(sandbox, *decisions(sandbox, [{'rule': 'R3', 'choices': ['yes', 'no'], 'destination': 'ask'}]))
    section = data['directories'][str(sandbox[0])]
    assert section['filing_outcomes'][0]['outcome'] == 'unbound'
    assert not section['handoffs_archived']


def test_dirty_files_raise_ask_and_keep_handoff(sandbox):
    path = source(sandbox, loops=[])
    data = json.loads(path.read_text())
    data['uncommitted_files'] = [str(sandbox[0] / 'changed.txt')]
    path.write_text(json.dumps(data))
    old = int(time.time()) - 5 * 86400
    os.utime(path, (old, old))
    _, data = report(sandbox)
    section = data['directories'][str(sandbox[0])]
    ask = section['asks_raised'][0]['ask']
    assert ask['session_id'] == 'source' and ask['directory'] == str(sandbox[0])
    assert 'changed.txt' in ask['text']
    assert not section['handoffs_archived']


def test_open_session_evidence_blocks_filing(sandbox):
    source(sandbox)
    evidence = sandbox[0] / 'open.json'
    evidence.write_text(json.dumps({'session_ids': ['source'], 'method': 'interactive inspection'}))
    _, data = report(sandbox, '--open-sessions', str(evidence), '--interactive')
    section = data['directories'][str(sandbox[0])]
    assert section['open_sessions'][0]['idle_seconds'] > 2 * 86400
    assert not section['filing_outcomes']
    assert data['open_session_checks'][0]['method'] == 'interactive inspection'


def test_second_run_exits_without_any_write_when_store_locked(sandbox):
    source(sandbox)
    fd = os.open(sandbox[1], os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = command(sandbox, 'run', '--scope', 'all', code=2)
        assert 'already running' in result.stderr
        assert not (sandbox[0] / 'reports').exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize('settings', [{'mode': 'act'}, {'idle_days': -1}, {'idle_days': True},
                                      {'open_session_checks': ['unsafe command']}])
def test_invalid_or_future_settings_never_execute_actions(sandbox, settings):
    sandbox[2].write_text(json.dumps({'sweep': settings}))
    command(sandbox, 'run', '--scope', 'all', code=1)
    assert not (sandbox[0] / 'reports').exists()


def test_stale_decisions_cannot_be_applied_to_changed_item(sandbox):
    path = source(sandbox)
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'Optional'}])
    data = json.loads(path.read_text())
    data['resume']['open_loops'][0]['context'] = 'Changed outcome'
    path.write_text(json.dumps(data))
    old = int(time.time()) - 5 * 86400
    os.utime(path, (old, old))
    command(sandbox, 'run', '--scope', 'all', *args, code=1)


def test_engine_imports_with_stdlib_only():
    probe = subprocess.run([sys.executable, '-I', '-S', '-B', str(ENGINE), '--help'],
                           text=True, capture_output=True)
    assert probe.returncode == 0, probe.stderr


def test_worktree_proposal_requires_clean_merged_branch_and_preserves_files(sandbox):
    repo, bare, linked = [sandbox[0] / name for name in ('repo', 'origin.git', 'linked')]
    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)
    git('init', '--bare', '-q', bare)
    git('init', '-qb', 'main', repo)
    git('-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-qm', 'base')
    git('-C', repo, 'remote', 'add', 'origin', bare)
    git('-C', repo, 'push', '-q', 'origin', 'main')
    git('-C', repo, 'worktree', 'add', '-qb', 'feature-a', linked)
    source(sandbox, directory=linked, loops=[])
    _, data = report(sandbox)
    section = data['directories'][str(linked)]
    removal = section['worktrees_removed'][0]
    assert removal['branch'] == 'feature-a' and removal['commit']
    assert removal['checks']['clean'] and removal['checks']['contained_in_pushed_trunk']
    assert (linked / '.git').exists()
    (linked / 'untracked.txt').write_text('work')
    _, data = report(sandbox)
    section = data['directories'][str(linked)]
    assert section['worktrees_removed'] == []
    assert not section['handoffs_archived']
    assert section['asks_raised'][0]['outcome'] == 'unbound'
    assert (linked / 'untracked.txt').read_text() == 'work'


def test_missing_legacy_subject_is_decidable_in_all_scope(sandbox):
    path = source(sandbox, version=1)
    data = json.loads(path.read_text())
    data.pop('subject')
    path.write_text(json.dumps(data))
    old = int(time.time()) - 5 * 86400
    os.utime(path, (old, old))
    _, data = report(sandbox, *decisions(sandbox, [{'rule': 'R6', 'reason': 'No outcome'}]))
    assert data['directories']['unknown directory']['items_dropped'][0]['rule'] == 'R6'


def test_unsafe_transcript_discovery_keeps_every_handoff(sandbox):
    source(sandbox)
    linked = Path(sandbox[3]['CODEX_HOME']) / 'sessions'
    linked.parent.mkdir(parents=True)
    target = sandbox[0] / 'relocated'
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    _, data = report(sandbox)
    assert not data['directories']
    assert data['errors'][0]['blocks_sweep']


def test_report_settings_and_strict_idle_boundary(sandbox):
    spec = importlib.util.spec_from_file_location('test_sweep_engine', ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = source(sandbox)
    from datetime import datetime, timezone
    instant = datetime.fromtimestamp(path.stat().st_mtime + 2 * 86400, timezone.utc)
    # Direct deterministic seam; no ambient store or transcript access.
    original = dict(os.environ)
    try:
        os.environ.update(sandbox[3])
        config = module.settings()
        assert config['idle_days'] == 2 and config['archive_retention_days'] is None
        found = module.discover('all', config, instant)
        assert not found['candidates'][0]['abandoned']
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_shared_skill_is_shipped_by_both_installers():
    spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install-codex.py')
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    assert 'sweep' in module.SKILLS
    skill = (ROOT / 'skills/sweep/SKILL.md').read_text()
    assert 'name: sweep' in skill
    assert 'scripts/lib/sweep.py' in skill


def test_failed_read_is_retained_instead_of_reported_as_drop_or_new_ticket(sandbox):
    source(sandbox)
    for row in ({'rule': 'R1', 'reason': 'Possibly recorded'},
                {'rule': 'R5', 'destination': 'record'}):
        _, data = report(sandbox, *decisions(sandbox, [row]))
        section = data['directories'][str(sandbox[0])]
        assert section['filing_outcomes'][0]['outcome'] == 'read failed'
        assert not section['items_dropped'] and not section['new_tickets']


def test_generated_ask_includes_stable_source_identity(sandbox):
    path = source(sandbox, loops=[])
    data = json.loads(path.read_text())
    data['uncommitted_files'] = [str(sandbox[0] / 'changed.txt')]
    path.write_text(json.dumps(data))
    old = int(time.time()) - 5 * 86400
    os.utime(path, (old, old))
    _, first = report(sandbox)
    _, second = report(sandbox)
    first_ask = first['directories'][str(sandbox[0])]['asks_raised'][0]
    second_ask = second['directories'][str(sandbox[0])]['asks_raised'][0]
    assert first_ask['identity'] == second_ask['identity']
    assert first_ask['ask']['identity'] == first_ask['identity']


def test_captured_handoff_can_be_swept_without_losing_any_item(sandbox):
    captured = json.loads(FIXTURE.read_text())
    path = source(sandbox, loops=captured['resume']['open_loops'])
    _, data = report(sandbox, *decisions(sandbox, [{'rule': 'R1', 'information_only': True, 'reason': 'Information only'}]))
    outcomes = data['directories'][str(sandbox[0])]['filing_outcomes']
    assert len(outcomes) == len(captured['resume']['open_loops'])
    assert all(row['rule'] == 'R1' for row in outcomes)
    assert path.exists()
