"""Sweep command contracts; every store and profile is isolated from the user."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
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


def bound_profile(sandbox):
    stub = sandbox[0] / 'binding.sh'
    stub.write_text('printf called > "$0.called"\nexit 1\n')
    binding = {'command': 'sh ' + shlex.quote(str(stub))}
    profile = sandbox[0] / 'profile.json'
    profile.write_text(json.dumps({'name': 'test', 'ask': binding,
        'bindings': {slot: dict(binding, consumes=[])
                     for slot in ('record', 'lessons', 'knowledge')}}))
    return profile


def outside_report_snapshot(sandbox):
    root = sandbox[0]
    reports = root / 'reports'
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            if path.is_file() else None
            for path in root.rglob('*') if path != reports and reports not in path.parents}


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('copies', ['identical', 'different-metadata'])
@pytest.mark.parametrize('judgments', ['none', 'one', 'repeated', 'conflicting'])
def test_duplicate_identities_in_one_handoff_are_retained_as_ambiguous(sandbox, version, copies, judgments):
    captured = json.loads(FIXTURE.read_text())['resume']['open_loops']
    first = dict(captured[0])
    if version == 1:
        first = {key: value for key, value in first.items()
                 if key not in ('type', 'session_id', 'produced_at')}
    second = dict(first)
    if copies == 'different-metadata':
        # These fields affect filing but are outside the approved identity.
        second.update(anchor={'ticket': 'https://example.test/other-ticket'}, disposition='consider')
        if version == 2:
            second['type'] = 'knowledge'
    items = [first, second, captured[3]]
    path = source(sandbox, loops=items, version=version)
    profile = bound_profile(sandbox)
    inspected = json.loads(command(sandbox, 'inspect', '--scope', 'all').stdout)['candidates'][0]['items']
    assert inspected[0]['identity'] == inspected[1]['identity']
    identity = inspected[0]['identity']
    drop = {'rule': 'R6', 'reason': 'No stated outcome'}
    rows = [dict(handoff=str(path), identity=inspected[2]['identity'], **drop)]
    decision = dict(handoff=str(path), identity=identity, rule='R2',
                    destination='https://example.test/ticket', read_status='ok')
    if judgments != 'none':
        rows.append(decision)
    if judgments in ('repeated', 'conflicting'):
        rows.append(dict(decision) if judgments == 'repeated' else
                    dict(handoff=str(path), identity=identity, **drop))
    choices = sandbox[0] / 'decisions.json'
    choices.write_text(json.dumps(rows))
    before = outside_report_snapshot(sandbox)

    text, data = report(sandbox, '--decisions', str(choices), '--user-profile', str(profile))

    section = data['directories'][str(sandbox[0])]
    outcomes = section['filing_outcomes']
    assert len(outcomes) == len(items)
    for item, row in zip(items[:2], outcomes[:2]):
        assert row['outcome'] == 'ambiguous identity'
        assert row['rule'] is None
        assert row['identity'] == identity
        assert row['item'] == item and row['source_handoff'] == str(path)
        assert row['identity_occurrences'] == 2
        assert 'manual judgment' in row['reason']
        assert 'no filing decision applied' in row['reason']
        rendered = json.dumps(row, ensure_ascii=False, sort_keys=True)
        assert text.split('## Structured data')[0].count(rendered) == outcomes.count(row)
    assert [item['identity_occurrences'] for item in inspected] == [2, 2, 1]
    assert outcomes[2]['outcome'] == 'dry run'
    assert section['items_dropped'] == [outcomes[2]]
    for key in ('asks_raised', 'new_tickets', 'comments_added', 'documents_changed', 'handoffs_archived'):
        assert not section[key]
    assert section['handoffs_kept'][0]['path'] == str(path)
    assert not data['nothing_to_do']
    assert outside_report_snapshot(sandbox) == before
    assert len(list((sandbox[0] / 'reports').iterdir())) == 1


@pytest.mark.parametrize('field', ['session_id', 'produced_at', 'context'])
def test_each_identity_component_distinguishes_items_in_one_handoff(sandbox, field):
    captured = json.loads(FIXTURE.read_text())['resume']['open_loops']
    first = dict(captured[0])
    second = dict(first)
    second[field] = {'session_id': 'another-producer', 'produced_at': '2026-10-02T00:00:00Z',
                     'context': captured[1]['context']}[field]
    path = source(sandbox, loops=[first, second])
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'No stated outcome'}])

    _, data = report(sandbox, *args)

    section = data['directories'][str(sandbox[0])]
    assert len(section['items_dropped']) == 2
    assert all(row['outcome'] == 'dry run' for row in section['filing_outcomes'])
    assert section['filing_outcomes'][0]['identity'] != section['filing_outcomes'][1]['identity']
    assert section['handoffs_archived'][0]['path'] == str(path)


def test_same_identity_carried_to_a_successor_is_not_a_within_handoff_ambiguity(sandbox):
    item = json.loads(FIXTURE.read_text())['resume']['open_loops'][0]
    original = source(sandbox, name='original', loops=[item])
    successor = source(sandbox, name='successor', directory=sandbox[0] / 'successor', loops=[item])
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'No stated outcome'}])

    _, data = report(sandbox, *args)

    identities = []
    for directory, path in ((sandbox[0], original), (sandbox[0] / 'successor', successor)):
        section = data['directories'][str(directory)]
        row, = section['filing_outcomes']
        assert row['outcome'] == 'dry run' and row['source_handoff'] == str(path)
        assert section['handoffs_archived'][0]['path'] == str(path)
        identities.append(row['identity'])
    assert identities[0] == identities[1] == {
        'session_id': item['session_id'], 'produced_at': item['produced_at'], 'text': item['context']}


def test_duplicate_identity_added_after_inspection_blocks_the_existing_decision(sandbox):
    item = json.loads(FIXTURE.read_text())['resume']['open_loops'][0]
    path = source(sandbox, loops=[item])
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'No stated outcome'}])
    source(sandbox, loops=[item, dict(item)])
    before = outside_report_snapshot(sandbox)

    _, data = report(sandbox, *args)

    section = data['directories'][str(sandbox[0])]
    assert len(section['filing_outcomes']) == 2
    assert all(row['outcome'] == 'ambiguous identity' for row in section['filing_outcomes'])
    assert not section['items_dropped'] and not section['handoffs_archived']
    assert section['handoffs_kept'][0]['path'] == str(path)
    assert outside_report_snapshot(sandbox) == before


def test_duplicate_decisions_for_a_unique_source_identity_are_still_rejected(sandbox):
    source(sandbox)
    args = decisions(sandbox, [{'rule': 'R6', 'reason': 'No stated outcome'}])
    path = Path(args[1])
    rows = json.loads(path.read_text())
    path.write_text(json.dumps(rows + rows))
    before = outside_report_snapshot(sandbox)

    result = command(sandbox, 'run', '--scope', 'all', *args, code=1)

    assert 'duplicate' in result.stderr
    assert outside_report_snapshot(sandbox) == before
    assert not (sandbox[0] / 'reports').exists()


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('entry,binding,section_key', [
    pytest.param({'rule': 'R1', 'information_only': True, 'reason': 'Information only'},
                 None, 'items_dropped', id='R1-information'),
    pytest.param({'rule': 'R1', 'read_status': 'ok', 'reason': 'Already recorded'},
                 None, 'items_dropped', id='R1-recorded'),
    pytest.param({'rule': 'R3', 'destination': 'ask', 'choices': ['approve', 'decline']},
                 'ask', 'asks_raised', id='R3'),
    pytest.param({'rule': 'R4', 'destination': 'lessons'},
                 'lessons', 'filing_outcomes', id='R4-lesson'),
    pytest.param({'rule': 'R4', 'destination': 'knowledge'},
                 'knowledge', 'filing_outcomes', id='R4-knowledge'),
    pytest.param({'rule': 'R4', 'destination': 'ask', 'choices': ['save', 'leave it']},
                 'ask', 'asks_raised', id='R4-pending'),
    pytest.param({'rule': 'R7', 'destination': 'README.md'},
                 'record', 'documents_changed', id='R7'),
    pytest.param({'rule': 'R2', 'destination': 'https://example.test/ticket', 'read_status': 'ok'},
                 'record', 'comments_added', id='R2'),
    pytest.param({'rule': 'R5', 'destination': 'record', 'read_status': 'ok',
                  'priority': 'p2', 'priority_reason': 'Needed'},
                 'record', 'new_tickets', id='R5'),
    pytest.param({'rule': 'R6', 'reason': 'No stated outcome'},
                 None, 'items_dropped', id='R6'),
])
def test_untyped_legacy_items_receive_every_filing_outcome(sandbox, version, entry, binding, section_key):
    captured = json.loads(FIXTURE.read_text())['resume']['open_loops'][2]
    item = {key: value for key, value in captured.items()
            if key not in ('type', 'session_id', 'produced_at')}
    path = source(sandbox, loops=[item], version=version)
    profile = bound_profile(sandbox)
    args = decisions(sandbox, [entry])
    identity = json.loads(Path(args[1]).read_text())[0]['identity']
    before = outside_report_snapshot(sandbox)

    text, data = report(sandbox, *args, '--user-profile', str(profile))

    section = data['directories'][str(sandbox[0])]
    row, = section['filing_outcomes']
    assert row['item'] == item
    assert row['identity'] == identity
    assert row['source_handoff'] == str(path)
    assert row['rule'] == entry['rule'] and row['binding'] == binding
    assert row['outcome'] == 'dry run'
    for key, value in entry.items():
        assert row[key] == value
    if binding == 'ask':
        assert row['ask']['identity'] == identity
    assert section[section_key] == [row]
    assert section['handoffs_archived'][0]['path'] == str(path)
    assert not section['handoffs_kept']
    assert json.dumps(row, ensure_ascii=False, sort_keys=True) in text.split('## Structured data')[0]
    assert outside_report_snapshot(sandbox) == before
    assert len(list((sandbox[0] / 'reports').iterdir())) == 1


@pytest.mark.parametrize('slot', ['lessons', 'knowledge'])
def test_untyped_legacy_slot_without_binding_is_reported_and_kept(sandbox, slot):
    captured = json.loads(FIXTURE.read_text())['resume']['open_loops'][2]
    item = {key: value for key, value in captured.items() if key != 'type'}
    path = source(sandbox, loops=[item], version=1)
    profile = sandbox[0] / 'profile.json'
    profile.write_text(json.dumps({'name': 'test', 'bindings': {slot: None}}))
    args = decisions(sandbox, [{'rule': 'R4', 'destination': slot}])
    before = outside_report_snapshot(sandbox)

    _, data = report(sandbox, *args, '--user-profile', str(profile))

    section = data['directories'][str(sandbox[0])]
    row, = section['filing_outcomes']
    assert row['rule'] == 'R4' and row['binding'] == slot and row['outcome'] == 'unbound'
    assert row['item'] == item
    assert row['identity']['session_id'] == item['session_id']
    assert row['identity']['produced_at'] == item['produced_at']
    assert not section['handoffs_archived']
    assert section['handoffs_kept'][0]['path'] == str(path)
    assert outside_report_snapshot(sandbox) == before


@pytest.mark.parametrize('bad_index', [0, 2])
@pytest.mark.parametrize('bad_entry,item_overrides,error', [
    pytest.param({'rule': 'R1', 'information_only': True}, {}, 'requires dropped with a reason', id='R1'),
    pytest.param({'rule': 'R3', 'destination': 'ask'}, {}, "an ask requires the user's choices", id='R3'),
    pytest.param({'rule': 'R4', 'destination': 'record'}, {}, 'R4 requires', id='R4'),
    pytest.param({'rule': 'R7'}, {}, 'a filed item requires its destination', id='R7'),
    pytest.param({'rule': 'R2', 'read_status': 'ok'}, {}, 'a filed item requires its destination', id='R2'),
    pytest.param({'rule': 'R5', 'destination': 'record', 'read_status': 'ok'}, {},
                 'a new ticket requires priority', id='R5'),
    pytest.param({'rule': 'R6', 'reason': ''}, {}, 'requires dropped with a reason', id='R6'),
    pytest.param({'rule': 'unknown'}, {}, 'unknown rule', id='unknown-rule'),
    pytest.param({'rule': 'R3', 'destination': 'ask', 'choices': ['approve', 'decline']},
                 {'anchor': ['invalid anchor']}, 'has no attribute', id='malformed-legacy-anchor'),
])
def test_item_failure_is_reported_without_aborting_other_items_or_handoffs(sandbox, bad_index, bad_entry,
                                                                         item_overrides, error):
    captured = json.loads(FIXTURE.read_text())['resume']['open_loops']
    items = [{key: value for key, value in item.items() if key != 'type'} for item in captured[:3]]
    items[bad_index].update(item_overrides)
    path = source(sandbox, name='broken', loops=items, version=1)
    other_directory = sandbox[0] / 'other'
    other = source(sandbox, name='healthy', directory=other_directory, loops=[captured[3]])
    profile = bound_profile(sandbox)
    valid = {'rule': 'R6', 'reason': 'No stated outcome'}
    entries = [bad_entry if index == bad_index else valid for index in range(len(items))]
    args = decisions(sandbox, entries)
    rows = json.loads(Path(args[1]).read_text())
    rows = [dict(handoff=row['handoff'], identity=row['identity'], **valid)
            if row['handoff'] == str(other) else row for row in rows]
    Path(args[1]).write_text(json.dumps(rows))
    before = outside_report_snapshot(sandbox)

    text, data = report(sandbox, *args, '--user-profile', str(profile))

    section = data['directories'][str(sandbox[0])]
    outcomes = section['filing_outcomes']
    assert len(outcomes) == len(items)
    for index, row in enumerate(outcomes):
        assert row['item'] == items[index]
        assert row['identity'] == rows[index]['identity']
        assert row['source_handoff'] == str(path)
        assert row['outcome'] == ('failed' if index == bad_index else 'dry run')
    failed = outcomes[bad_index]
    assert failed['rule'] == bad_entry['rule']
    assert failed.get('destination') == bad_entry.get('destination')
    assert error in failed['error']
    assert json.dumps(failed, ensure_ascii=False, sort_keys=True) in text.split('## Structured data')[0]
    assert len(section['items_dropped']) == len(items) - 1
    for key in ('asks_raised', 'comments_added', 'new_tickets', 'documents_changed', 'handoffs_archived'):
        assert not section[key]
    assert section['handoffs_kept'][0]['path'] == str(path)
    healthy = data['directories'][str(other_directory)]
    assert healthy['filing_outcomes'][0]['outcome'] == 'dry run'
    assert healthy['handoffs_archived'][0]['path'] == str(other)
    assert not data['nothing_to_do']
    assert outside_report_snapshot(sandbox) == before
    assert len(list((sandbox[0] / 'reports').iterdir())) == 1


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


@pytest.mark.parametrize('scope_kind', ['all', 'repository-subdirectory'])
@pytest.mark.parametrize('activity', ['open-subdirectory', 'recent-subdirectory', 'open-root', 'open-sibling-prefix'])
def test_active_sessions_protect_containing_worktree_without_merging_directories(sandbox, scope_kind, activity):
    repo, bare, linked = [sandbox[0] / name for name in ('repo', 'origin.git', 'linked')]

    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)

    git('init', '--bare', '-q', bare)
    git('init', '-qb', 'main', repo)
    git('-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        'commit', '--allow-empty', '-qm', 'base')
    git('-C', repo, 'remote', 'add', 'origin', bare)
    git('-C', repo, 'push', '-q', 'origin', 'main')
    git('-C', repo, 'worktree', 'add', '-qb', 'feature-a', linked)
    (repo / 'docs').mkdir()
    (linked / 'docs').mkdir()
    old = source(sandbox, 'abandoned', directory=linked, loops=[])
    if activity == 'open-sibling-prefix':
        sibling = sandbox[0] / 'linked-other'
        git('-C', repo, 'worktree', 'add', '-qb', 'feature-b', sibling)
        active_dir = sibling
    else:
        active_dir = linked if activity == 'open-root' else linked / 'docs'
    active = source(sandbox, 'active', directory=active_dir, loops=[])
    if activity == 'recent-subdirectory':
        os.utime(active, None)
    evidence = sandbox[0] / 'open.json'
    evidence.write_text(json.dumps({'session_ids': [] if activity == 'recent-subdirectory' else ['active'],
                                    'method': 'isolated test session evidence'}))
    scope = 'all' if scope_kind == 'all' else str(repo / 'docs')
    inspected = json.loads(command(sandbox, 'inspect', '--scope', scope).stdout)
    assert {c['directory'] for c in inspected['candidates']} == {str(linked), str(active_dir)}
    before = {path: path.read_bytes() for path in (old, active)}
    _, data = report(sandbox, '--scope', scope, '--open-sessions', str(evidence))
    section = data['directories'][str(linked)]
    if activity == 'open-sibling-prefix':
        assert section['worktrees_removed'][0]['path'] == str(linked)
        assert not section['worktrees_kept']
    else:
        assert section['worktrees_removed'] == []
        kept = section['worktrees_kept'][0]
        assert kept['path'] == str(linked)
        assert kept['reason'] == 'another open or active session uses this worktree'
    if activity.startswith('open'):
        assert data['directories'][str(active_dir)]['open_sessions'][0]['session_id'] == 'active'
    assert {path: path.read_bytes() for path in before} == before
    assert (linked / '.git').exists()
