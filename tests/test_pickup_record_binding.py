"""The agent receives requests, then supplies skill output for validated rendering."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
READER = ROOT / 'scripts/lib/pickup_record.py'


@pytest.fixture
def selected(tmp_path):
    handoff = json.loads((ROOT / 'docs/examples/handoff.json').read_text())
    handoff['subject']['working_directory'] = str(tmp_path)
    handoff['resume']['open_loops'].append({
        'type': 'commitment', 'context': 'Carry the work',
        'anchor': {'ticket': 'work:17'}, 'session_id': 'example-session',
        'produced_at': handoff['written_at'], 'disposition': 'verify'})
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps(handoff))
    profile = tmp_path / 'profile.md'
    profile.write_text('```checklist-profile\n' + json.dumps({
        'name': 'user', 'record_read': {'command': '/read-work'}}) + '\n```\n')
    env = dict(os.environ, CHECKLIST_USER_PROFILE=str(profile), HOME=str(tmp_path))
    return path, handoff, env


def pickup(selected, *args):
    path, _, env = selected
    result = subprocess.run(['bash', str(ROOT / 'scripts/pickup.sh'), '--handoff', str(path), *args],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def finish(selected, report, tmp_path, ticket=None, commitments=None):
    args = []
    for kind, value in [('ticket-status', ticket), ('commitments', commitments)]:
        if value is not None:
            path = tmp_path / kind
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            args += ['--' + kind, str(path)]
    result = subprocess.run([sys.executable, str(READER), *args], input=json.dumps(report),
                            env=selected[2], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def lines(report):
    return '\n'.join(row['line'] for row in report['briefing'])


def test_bound_pickup_prepares_requests_for_resumed_session_without_executing_skill(selected):
    report = pickup(selected)
    read = report['record_read']
    assert read['command'] == '/read-work'
    assert read['requests'] == {
        'ticket_status': {'kind': 'ticket_status', 'references': ['work:17']},
        'commitments': {'kind': 'commitments', 'session_id': 'example-session',
                        'repository': selected[1]['git']['repository_root'], 'handoff': selected[1]}}
    assert 'No record read is bound.' not in lines(report)
    assert 'from handoff' in lines(report)
    assert 'pending' in lines(report)


def test_validated_record_fields_and_both_commitment_sources(selected, tmp_path):
    report = pickup(selected)
    row = dict(reference='work:17', title='Carry the work', state='active',
               link='https://example.org/work/17')
    result = finish(selected, report, tmp_path,
                    dict(kind='ticket_status', rows=[dict(row, latest_progress='Contract approved')]),
                    dict(kind='commitments', rows=[row]))
    for value in [*row.values(), 'Contract approved', 'from handoff', 'from record']:
        assert value in lines(result)
    assert result['handoff'] == selected[1]
    assert all(item in result['briefing'] for item in report['briefing'] if 'record read' not in item['line'])


@pytest.mark.parametrize('response,reason', [
    ('{', 'malformed JSON'),
    ({'kind': 'error', 'reason': 'service offline'}, 'service offline'),
    ({'kind': 'ticket_status', 'rows': [{}]}, 'missing fields'),
    ({'kind': 'ticket_status', 'rows': []}, 'requested references'),
])
def test_failures_preserve_handoff_and_never_look_empty(selected, tmp_path, response, reason):
    report = pickup(selected)
    result = finish(selected, report, tmp_path, response, {'kind': 'commitments', 'rows': []})
    assert 'record read unavailable: ' in lines(result)
    assert reason in lines(result)
    assert result['resume'] == selected[1]['resume']
    assert 'ticket_status' not in result['record_read'].get('results', {})
    assert selected[1]['resume']['headline'] in lines(result)


def test_skip_commitments_survives_wrapper_and_finish(selected, tmp_path):
    report = pickup(selected, '--no-commitments')
    assert 'commitments' not in report['record_read']['requests']
    assert 'from record: skipped (--no-commitments)' in lines(report)
    result = finish(selected, report, tmp_path, {'kind': 'ticket_status', 'rows': [dict(
        reference='work:17', title='', state='not_found', link='', latest_progress='')]},
        {'kind': 'error', 'reason': 'must be ignored'})
    assert 'not_found' in lines(result)
    assert 'must be ignored' not in lines(result)
    assert 'from record: skipped (--no-commitments)' in lines(result)


def test_unreadable_profile_is_failure_not_unbound(selected):
    Path(selected[2]['CHECKLIST_USER_PROFILE']).write_text('```checklist-profile\n{\n```\n')
    assert 'record read unavailable:' in lines(pickup(selected))


def test_default_profile_path_and_no_record_writes(selected, tmp_path):
    configured = tmp_path / '.config/checklist/profile.md'
    configured.parent.mkdir(parents=True)
    Path(selected[2].pop('CHECKLIST_USER_PROFILE')).rename(configured)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert pickup(selected)['record_read']['command'] == '/read-work'
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('response', [
    '{', {'kind': 'error', 'reason': 'service offline'},
    {'kind': 'commitments', 'rows': [{}]},
])
def test_commitment_failure_does_not_hide_valid_ticket_or_carried_items(selected, tmp_path, response):
    result = finish(selected, pickup(selected), tmp_path,
                    {'kind': 'ticket_status', 'rows': [dict(reference='work:17', title='',
                     state='not_found', link='', latest_progress='')]}, response)
    assert 'not_found' in lines(result)
    assert 'record read unavailable: commitments:' in lines(result)
    assert 'Commitments from handoff:' in lines(result)
    assert 'commitments' not in result['record_read']['results']
    assert 'Commitments from record: none' not in lines(result)


def test_missing_binding_outputs_are_unavailable(selected):
    report = pickup(selected)
    result = subprocess.run([sys.executable, str(READER), '--complete'],
                            input=json.dumps(report), env=selected[2], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    completed = json.loads(result.stdout)
    assert completed['record_read']['results'] == {}
    assert lines(completed).count('record read unavailable:') == 2
    assert 'none (validated empty response)' not in lines(completed)


def test_ticket_reference_coverage_rejects_unrequested_rows(selected, tmp_path):
    result = finish(selected, pickup(selected), tmp_path,
                    {'kind': 'ticket_status', 'rows': [dict(reference='work:missing', title='',
                     state='not_found', link='', latest_progress='')]})
    assert 'record read unavailable: ticket_status:' in lines(result)
    assert 'ticket_status' not in result['record_read']['results']


def test_explicit_missing_profile_is_unavailable(selected):
    Path(selected[2]['CHECKLIST_USER_PROFILE']).unlink()
    report = pickup(selected)
    assert 'record read unavailable: user profile:' in lines(report)
    assert 'No record read is bound.' not in lines(report)
