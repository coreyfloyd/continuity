#!/usr/bin/env python3
"""Identity-bound sweep planning and configured act execution; never removes worktrees."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
handoff = load_module('sweep_handoff', HERE / 'handoff.py')
checklist = load_module('sweep_checklist', HERE / 'checklist.py')
pickup = load_module('sweep_pickup', HERE.parent.parent / 'skills/pickup/scripts/pickup.py')
SECTIONS = (
    ('asks_raised', 'Asks raised'),
    ('open_sessions', 'Open sessions with idle time'),
    ('new_tickets', 'New tickets with priority'),
    ('comments_added', 'Comments added'),
    ('documents_changed', 'Documents changed'),
    ('items_dropped', 'Items dropped with reasons'),
    ('worktrees_removed', 'Worktrees removed'),
    ('handoffs_archived', 'Handoffs archived'),
)


def settings():
    config = handoff.read_config().get('sweep', {})
    if not isinstance(config, dict):
        raise ValueError('sweep configuration must be an object')
    result = dict(mode='watch-only', idle_days=2, open_session_checks=[],
                  new_ticket_limit=10, report_location=str(Path(handoff.state_dir()).parent / 'sweep-reports'),
                  archive_retention_days=None, binding_timeout_seconds=30)
    result.update(config)
    if result['mode'] not in ('watch-only', 'act'):
        raise ValueError('sweep.mode must be watch-only or act')
    for key in ('idle_days', 'new_ticket_limit', 'archive_retention_days', 'binding_timeout_seconds'):
        value = result[key]
        if key == 'archive_retention_days' and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f'sweep.{key} must be a non-negative finite number')
    if result['binding_timeout_seconds'] <= 0:
        raise ValueError('sweep.binding_timeout_seconds must be positive')
    if type(result['new_ticket_limit']) is not int:
        raise ValueError('sweep.new_ticket_limit must be an integer')
    if result['open_session_checks'] != []:
        raise ValueError('configured open-session commands are not supported yet; no sweep performed')
    if not isinstance(result['report_location'], str) or not result['report_location'].strip():
        raise ValueError('sweep.report_location must be a non-empty directory path')
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be an ISO timestamp with timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.timestamp()


def item_identity(item, data, path):
    """Legacy provenance falls back to its source, never to its item position."""
    subject = data.get('subject', {})
    return {'session_id': item.get('session_id') or subject.get('session_id') or data.get('session_id') or path.stem,
            'produced_at': item.get('produced_at') or data.get('written_at') or
                           datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            'text': item.get('context', '')}


def identity_key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def subjects(data):
    subject = data.get('subject', {})
    if not isinstance(subject, dict):
        raise ValueError('subject must be an object')
    directory = subject.get('working_directory') or data.get('working_directory') or data.get('cwd')
    if directory and not isinstance(directory, str):
        raise ValueError('working directory must be a string')
    return directory, subject.get('session_id') or data.get('session_id')


def transcript_index(home, codex_home):
    """Reuse pickup's store traversal and both providers' transcript locations."""
    found = {}
    root = pickup.claude_store(home)
    for name in sorted(pickup.list_store_dir(root, 'Claude projects directory') or []):
        project = Path(root) / name
        if project.is_symlink():
            raise ValueError(f'Claude project directory is a symbolic link: {project}')
        if not project.is_dir():
            continue
        for name in pickup.claude_project_transcripts(str(project)) or []:
            path = project / name
            found.setdefault(path.stem, []).append(path)
    for path, session in pickup.codex_rollouts(codex_home):
        found.setdefault(session, []).append(Path(path))
    return found


def read_handoff_source(path):
    """Capture bytes and file identity from one stable read, without rewriting."""
    if path.is_symlink():
        raise ValueError('handoff is a symbolic link; left in place')
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    with path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        content = handle.read()
        after = os.fstat(handle.fileno())
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError('handoff changed while reading; left in place')
    return content, {'sha256': hashlib.sha256(content).hexdigest(),
                     **{field: getattr(after, field) for field in fields}}


def check_handoff_source(candidate):
    """Equal JSON is insufficient: any byte or file-identity change keeps it."""
    _, current = read_handoff_source(Path(candidate['path']))
    if current != candidate['source_snapshot']:
        raise ValueError('handoff changed since discovery')


def discover(scope, config, now):
    store = Path(handoff.state_dir()).resolve()
    scoped = None if scope == 'all' else handoff.canonical_subject(scope)
    repo = handoff.repository_root(scoped) if scoped else None
    result = {'scope': scope if scoped is None else scoped, 'candidates': [], 'errors': []}
    try:
        transcripts = transcript_index(os.path.expanduser('~'),
                                       os.path.expanduser(os.environ.get('CODEX_HOME', '~/.codex')))
    except (OSError, ValueError, pickup.SourceError) as exc:
        result['errors'].append({'path': 'transcript stores', 'reason': str(exc), 'blocks_sweep': True})
        return result
    if not store.exists():
        return result
    for path in sorted(store.glob('*.json')):
        try:
            source_bytes, source_snapshot = read_handoff_source(path)
            data = json.loads(source_bytes)
            errors, _ = handoff.lint_data(data)
            if errors:
                raise ValueError('; '.join(errors))
            directory, session = subjects(data)
            # v1 did not require a subject. Keep it decidable in all scope,
            # but never guess that an unidentified directory matches a scope.
            if scoped and not directory:
                raise ValueError('legacy handoff has no directory; cannot assign directory scope')
            if directory:
                directory = handoff.canonical_subject(directory)
            stored_repo = data.get('git', {}).get('repository_root')
            actual_repo = handoff.repository_root(directory) if directory else None
            source_repo = actual_repo or (handoff.canonical_subject(stored_repo) if stored_repo else None)
            if scoped and not ((repo and source_repo == repo) or (not repo and directory == scoped)):
                continue
            activity = path.stat().st_mtime
            if data.get('written_at'):
                activity = max(activity, timestamp(data['written_at']))
            for transcript in transcripts.get(session, []):
                activity = max(activity, transcript.stat().st_mtime)
            idle = max(0, now.timestamp() - activity)
            items = [{'item': item, 'identity': item_identity(item, data, path)}
                     for item in data.get('resume', {}).get('open_loops', [])]
            occurrences = Counter(identity_key(item['identity']) for item in items)
            for item in items:
                item['identity_occurrences'] = occurrences[identity_key(item['identity'])]
            result['candidates'].append({'path': str(path), 'directory': directory or 'unknown directory',
                'session_id': session or path.stem, 'activity_at': activity, 'idle_seconds': idle,
                'abandoned': idle > config['idle_days'] * 86400,
                'items': items, 'handoff': data, 'source_snapshot': source_snapshot})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            result['errors'].append({'path': str(path), 'reason': str(exc)})
    return result


@contextmanager
def store_lock():
    """Lock a directory inode: no lock file or state directory is written.

    An absent store locks its nearest existing ancestor. This can conservatively
    serialize two empty stores but still changes nothing outside the report.
    """
    path = Path(handoff.state_dir()).resolve()
    while not path.exists():
        path = path.parent
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('a sweep is already running over this handoff store') from exc
        yield
    finally:
        os.close(fd)


def read_open_sessions(path):
    if path is None:
        return {'session_ids': [], 'method': 'idle threshold alone; no checks configured'}
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or not isinstance(data.get('session_ids'), list)
            or not all(isinstance(s, str) and s for s in data['session_ids'])
            or not isinstance(data.get('method'), str) or not data['method'].strip()):
        raise ValueError('open-session evidence requires session_ids and a non-empty method')
    return data


def read_decisions(path, discovery):
    rows = [] if path is None else json.loads(path.read_text())
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('decisions must be an array of objects')
    allowed = {(c['path'], identity_key(i['identity'])): i['identity_occurrences']
               for c in discovery['candidates'] for i in c['items']}
    result = {}
    for row in rows:
        key = (row.get('handoff'), identity_key(row.get('identity')))
        if key not in allowed:
            raise ValueError('stale or unknown decision identity; inspect again')
        if allowed[key] > 1:
            # No decision can select one copy without extending the identity.
            # Report every ambiguous copy, regardless of supplied judgments.
            continue
        if key in result:
            raise ValueError('duplicate decision for unique item identity; inspect again')
        result[key] = row
    return result


def plan_item(candidate, item, decision, plan, ask):
    base = dict(identity=item['identity'], source_handoff=candidate['path'], item=item['item'])
    occurrences = item['identity_occurrences']
    if occurrences > 1:
        return dict(base, rule=None, outcome='ambiguous identity', identity_occurrences=occurrences,
                    reason=f'Identity occurs {occurrences} times in this handoff; kept for manual judgment; '
                           'no filing decision applied')
    if decision is None:
        return dict(base, rule=None, outcome='pending judgment', reason='No agent filing decision supplied')
    row = {k: v for k, v in decision.items() if k not in ('handoff', 'identity')}
    rule = row.get('rule')
    row.update(index=0, status='dropped' if rule in ('R1', 'R6') else 'filed')
    validation_item = item['item']
    if rule == 'R4' and 'type' not in validation_item and row.get('destination') in ('lessons', 'knowledge'):
        # The agent's chosen slot classifies a legacy item for the shared
        # validator. Preserve the original item and identity in the report.
        validation_item = dict(validation_item, type='lesson' if row['destination'] == 'lessons' else 'knowledge')
    # Reuse the wrap's validation, binding resolution, read failure handling and
    # ask payload. It only does bookkeeping; no binding command is ever invoked.
    single = dict(candidate['handoff'], subject={'working_directory': candidate['directory'],
                  'session_id': candidate['session_id']}, resume={'open_loops': [validation_item]})
    checked = checklist.file_items(plan, single, [row], ask)['filing']['items'][0]
    checked.pop('index', None)
    if 'ask' in checked:
        checked['ask']['identity'] = item['identity']
    status = checked.pop('status')
    return dict(base, **{k: v for k, v in checked.items() if k != 'item'},
                outcome='dry run' if status in ('filed', 'dropped') else status)


def git_read(directory, *args):
    done = subprocess.run(['git', '-C', directory, *args], capture_output=True, text=True,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def worktree_plan(candidate, protected):
    """Read-only eligibility checks, shared seam for a later removal executor."""
    directory = candidate['directory']
    root = handoff.repository_root(directory)
    if not root:
        return None
    code, output, error = git_read(directory, 'worktree', 'list', '--porcelain')
    if code:
        return {'path': directory, 'outcome': 'kept', 'reason': error or 'worktree lookup failed'}
    entries = [dict(line.split(' ', 1) if ' ' in line else (line, '')
                    for line in block.splitlines()) for block in output.split('\n\n')]
    match = next((e for e in entries if e.get('worktree') == directory), None)
    if not match or directory == entries[0].get('worktree'):
        return None  # never propose removing the main checkout or a subdirectory
    row = {'path': directory, 'branch': match.get('branch', '').removeprefix('refs/heads/'),
           'commit': match.get('HEAD'), 'outcome': 'dry run'}
    reason = None
    # Subjects are canonical directories, but an active subject may be below
    # this worktree root. Compare path components, not string prefixes.
    if any(Path(directory) == Path(active) or Path(directory) in Path(active).parents
           for active in protected):
        reason = 'another open or active session uses this worktree'
    status, dirty, error = git_read(directory, 'status', '--porcelain', '--untracked-files=all')
    if status or dirty:
        reason = error or 'uncommitted or untracked changes'
    code, trunk, _ = git_read(directory, 'symbolic-ref', 'refs/remotes/origin/HEAD')
    if code:
        # A bare origin may have no symbolic remote HEAD; require an existing
        # conventional remote trunk rather than guessing a local branch.
        trunk = next((ref for ref in ('refs/remotes/origin/main', 'refs/remotes/origin/master')
                      if git_read(directory, 'rev-parse', '--verify', ref)[0] == 0), '')
    merged = bool(trunk) and git_read(directory, 'merge-base', '--is-ancestor', row['commit'], trunk)[0] == 0
    if not merged:
        reason = 'branch not contained in the locally observed pushed trunk'
    if not row['branch']:
        reason = 'detached worktree has no local branch'
    row['checks'] = {'clean': status == 0 and not dirty, 'contained_in_pushed_trunk': merged,
                     'plain_removal': 'not attempted in watch-only mode'}
    if reason:
        row.update(outcome='kept', reason=reason)
    return row


def archive_worktree_check(candidate):
    """Archive needs a clean repository even when no removal is proposed."""
    directory = candidate['directory']
    previous = candidate.get('archive_worktree_check') or {}
    root = (handoff.repository_root(directory) or previous.get('path') or
            candidate['handoff'].get('git', {}).get('repository_root'))
    if not root:
        return None
    code, output, error = git_read(directory, 'status', '--porcelain', '--untracked-files=all')
    return {'path': root, 'clean': code == 0 and not output,
            'reason': (error or 'worktree status read failed') if code else
                      ('uncommitted or untracked changes' if output else 'worktree is clean')}


def add_ask(section, candidate, text, choices, reason, ask):
    identity = {'session_id': candidate['session_id'],
                'produced_at': datetime.fromtimestamp(candidate['activity_at'], timezone.utc).isoformat(),
                'text': text}
    section['asks_raised'].append({'rule': 'R3', 'outcome': 'dry run' if ask else 'unbound', 'reason': reason,
        'source_handoff': candidate['path'], 'identity': identity, 'ask': {'text': text, 'identity': identity, 'session_id': candidate['session_id'],
        'directory': candidate['directory'], 'ticket': None, 'choices': choices}})


def build_report(discovery, decisions, plan, ask, checks, interactive, started):
    report = {'run_id': uuid.uuid4().hex, 'host': socket.gethostname(), 'started_at': started.isoformat(),
              'scope': discovery['scope'], 'mode': 'watch-only', 'interactive': interactive,
              'open_session_checks': [checks], 'errors': discovery['errors'], 'directories': {}}
    protected = {c['directory'] for c in discovery['candidates']
                 if c['session_id'] in checks['session_ids'] or not c['abandoned']}
    for candidate in discovery['candidates']:
        is_open = candidate['session_id'] in checks['session_ids']
        if not candidate['abandoned'] and not is_open:
            continue
        section = report['directories'].setdefault(candidate['directory'],
            {**{key: [] for key, _ in SECTIONS}, 'filing_outcomes': [], 'worktrees_kept': [], 'handoffs_kept': []})
        if is_open:
            section['open_sessions'].append({key: candidate[key] for key in
                                            ('session_id', 'idle_seconds', 'activity_at', 'path')})
            if candidate['abandoned']:
                add_ask(section, candidate, 'Open idle session: ' + candidate['session_id'],
                        ['wrap it', 'leave it', 'close it'],
                        'present directly to user' if interactive else 'would raise an ask; no ask sent in watch-only', ask)
            continue
        outcomes = []
        for item in candidate['items']:
            decision = decisions.get((candidate['path'], identity_key(item['identity'])))
            try:
                row = plan_item(candidate, item, decision, plan, ask)
            except Exception as exc:
                # One item's validation or planning failure must not hide the
                # run. Keep its source and continue reporting the other items.
                row = dict(identity=item['identity'], source_handoff=candidate['path'], item=item['item'],
                           rule=decision.get('rule') if decision else None,
                           destination=decision.get('destination') if decision else None,
                           outcome='failed', error=f'{type(exc).__name__}: {exc}')
            outcomes.append(row)
            section['filing_outcomes'].append(row)
            binding = row.get('binding')
            if binding == 'ask':
                section['asks_raised'].append(row)
            elif row.get('rule') in ('R1', 'R6') and row['outcome'] == 'dry run':
                section['items_dropped'].append(row)
            elif row.get('rule') in ('R2', 'R5', 'R7') and row['outcome'] in ('dry run', 'unbound'):
                key = {'R2': 'comments_added', 'R5': 'new_tickets', 'R7': 'documents_changed'}[row['rule']]
                section[key].append(row)
        dirty = candidate['handoff'].get('uncommitted_files', [])
        if dirty:
            add_ask(section, candidate, 'Uncommitted files for session ' + candidate['session_id'] +
                    ' in ' + candidate['directory'] + ': ' + ', '.join(dirty),
                    ['keep work', 'review files'], 'handoff lists uncommitted files', ask)
        worktree = worktree_plan(candidate, protected)
        if worktree:
            if worktree['outcome'] == 'dry run':
                if worktree not in section['worktrees_removed']:
                    section['worktrees_removed'].append(worktree)
            else:
                section['worktrees_kept'].append(worktree)
                add_ask(section, candidate, 'Keep worktree ' + worktree['path'] + ': ' + worktree['reason'],
                        ['review worktree', 'leave it'], worktree['reason'], ask)
        clean_check = archive_worktree_check(candidate)
        candidate['archive_worktree_check'] = clean_check
        archive = (not dirty and (clean_check is None or clean_check['clean']) and
                   all(row['outcome'] == 'dry run' for row in outcomes))
        if worktree and worktree['outcome'] == 'kept' and ask is None:
            archive = False
        key = 'handoffs_archived' if archive else 'handoffs_kept'
        section[key].append({'path': candidate['path'], 'outcome': 'dry run' if archive else 'kept',
            'worktree_check': clean_check,
            'destination': str(Path(candidate['path']).parent / 'archive' / Path(candidate['path']).name),
            'reason': 'all items would be filed or dropped' if archive else 'unfiled items, unbound ask, or uncommitted files'})
    report['nothing_to_do'] = not report['directories'] and not report['errors']
    report['ended_at'] = datetime.now(timezone.utc).isoformat()
    return report


def binding_call(binding, request, timeout):
    """Commands are argv strings, never shell programs; JSON travels on stdin."""
    if binding is None:
        raise ValueError('binding is unbound')
    done = subprocess.run(shlex.split(binding['command']), input=json.dumps(request),
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise ValueError(done.stderr.strip() or f'binding exited {done.returncode}')
    response = json.loads(done.stdout)
    if not isinstance(response, dict):
        raise ValueError('binding response must be a JSON object')
    return response


def execute_act(report, discovery, plan, ask, reader, config, path):
    """Execute only binding writes, asks and archive moves; journal in the report.

    Identity queries precede every attempted write, even without an earlier report.
    Bindings must enforce identity uniqueness atomically, including delayed writes.
    """
    report['mode'] = 'act'
    for section in report['directories'].values():
        section['unfiled_items'] = []
    bindings = {step['slot']: step.get('binding') for step in plan if 'slot' in step}
    bindings['ask'] = ask
    record = bindings.get('record')
    timeout = config['binding_timeout_seconds']
    created = 0
    tripwires = {}

    def save():
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        with path.open('w') as handle:
            handle.write(render_report(report))
            handle.flush()
            os.fsync(handle.fileno())

    def request(row, operation):
        return dict(operation=operation, identity=row['identity'],
                    session_id=row['identity']['session_id'],
                    directory=row['directory'], text=row['identity']['text'],
                    source_handoff=row['source_handoff'])

    def query(row, binding, operation):
        payload = request(row, operation)
        payload['destination'] = row.get('destination')
        return binding_call(binding, payload, timeout)

    def write(row, binding, operation):
        # Persist intent before starting a process: a crash or a lost reply is
        # recoverable through the destination's durable identity index.
        row.update(outcome='uncertain', operation=operation)
        save()
        payload = request(row, operation)
        payload.update(destination=row.get('destination'), rule=row['rule'])
        if operation == 'file_slot':
            payload['slot'] = bindings[row['binding']]
        if operation == 'create_ticket':
            payload.update(priority=row['priority'], priority_reason=row['priority_reason'])
        if operation == 'ask':
            payload.update(row['ask'])
        try:
            result = binding_call(binding, payload, timeout)
            if result.get('status') == 'pending' and operation == 'file_slot':
                row.update(outcome='pending', reason=result.get('reason') or 'slot left item pending')
            elif result.get('status') == 'failed':
                row.update(outcome='failed', error=result.get('error') or 'binding rejected write')
            elif (result.get('status') == 'written' and isinstance(result.get('receipt'), str)
                  and result['receipt'].strip() and isinstance(result.get('destination'), str)
                  and result['destination'].strip()):
                row.update(outcome='written', receipt=result['receipt'], destination=result['destination'])
            else:
                row['error'] = 'missing or malformed write acknowledgement'
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            # A process error may follow a successful remote write too.
            row['error'] = str(exc)
        save()
        return row['outcome'] == 'written'

    def exists(row, binding):
        result = query(row, binding, 'identity_exists')
        if type(result.get('exists')) is not bool:
            raise ValueError('identity_exists requires a boolean exists')
        if result['exists']:
            if not all(isinstance(result.get(k), str) and result[k].strip()
                       for k in ('receipt', 'destination')):
                raise ValueError('existing identity requires receipt and destination')
            row.update(outcome='written', receipt=result['receipt'], destination=result['destination'],
                       already_written=True)
        return result['exists']

    def send_ask(row):
        row.update(binding='ask', destination='ask')
        if ask is None:
            row['outcome'] = 'unbound'
            return
        try:
            if not exists(row, ask):
                write(row, ask, 'ask')
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            row.update(outcome='failed', error='ask identity read failed: ' + str(exc))

    for directory, section in report['directories'].items():
        # Rebuild category arrays from actual outcomes rather than proposals.
        synthetic_asks = [row for row in section['asks_raised'] if row not in section['filing_outcomes']]
        for key in ('asks_raised', 'new_tickets', 'comments_added', 'documents_changed',
                    'items_dropped', 'worktrees_removed', 'handoffs_archived', 'handoffs_kept'):
            section[key] = []
        section['unfiled_items'] = []
        for row in section['filing_outcomes']:
            row['directory'] = directory
            rule = row['rule']
            try:
                eligible = row['outcome'] in ('dry run', 'unbound')
                if eligible and rule == 'R2' and record and exists(row, record):
                    continue
                if rule == 'R5' and eligible and record is None:
                    row.update(outcome='read failed', error='record binding is unbound; open-ticket search unavailable')
                elif eligible and (rule == 'R2' or (rule == 'R1' and not row.get('information_only'))):
                    result = query(row, reader, 'read_item')
                    if type(result.get('present')) is not bool or type(result.get('open')) is not bool:
                        raise ValueError('read_item requires boolean present and open')
                    if rule == 'R1' and not result['present']:
                        raise ValueError('R1 already-recorded claim was not confirmed')
                    if rule == 'R2' and not result['open']:
                        raise ValueError('R2 destination is not open')
                if row['outcome'] not in ('dry run', 'unbound'):
                    continue
                if rule in ('R1', 'R6'):
                    row['outcome'] = 'dropped'
                elif row['binding'] == 'ask':
                    send_ask(row)
                elif rule == 'R4':
                    slot = row['binding']
                    if bindings.get(slot) and record:
                        if not exists(row, record):
                            write(row, record, 'file_slot')
                    else:
                        row['outcome'] = 'pending'
                    if row['outcome'] == 'pending':
                        row.update(binding='ask', destination='ask', ask=dict(
                            text=row['identity']['text'], identity=row['identity'],
                            session_id=row['identity']['session_id'], directory=directory,
                            ticket=(row['item'].get('anchor') or {}).get('ticket'),
                            choices=['save through ' + slot, 'leave it']))
                        send_ask(row)
                elif record is None:
                    row['outcome'] = 'unbound'
                elif rule != 'R2' and exists(row, record):
                    pass
                else:
                    operation = {'R2': 'comment', 'R7': 'edit_document', 'R5': 'create_ticket'}[rule]
                    if rule == 'R5':
                        result = query(row, record, 'search_open')
                        matches = result.get('matches')
                        if (not isinstance(matches, list) or
                                not all(isinstance(m, str) and m.strip() for m in matches) or
                                len(matches) != len(set(matches))):
                            raise ValueError('search_open requires distinct non-empty match references')
                        row['matches'] = matches
                        if len(matches) > 1:
                            row.update(rule='R3', binding='ask', destination='ask', ask=dict(
                                text=row['identity']['text'], identity=row['identity'],
                                session_id=row['identity']['session_id'], directory=directory,
                                ticket=(row['item'].get('anchor') or {}).get('ticket'),
                                choices=['use ' + m for m in matches] + ['leave it']))
                            send_ask(row)
                            continue
                        if matches:
                            row.update(rule='R2', destination=matches[0])
                            row.pop('priority', None)
                            row.pop('priority_reason', None)
                            operation = 'comment'
                        elif created >= config['new_ticket_limit']:
                            row.update(outcome='tripwire', reason='new-ticket limit reached')
                            tripwires.setdefault(directory, row['source_handoff'])
                            continue
                    write(row, record, operation)
                    if operation == 'create_ticket' and row['outcome'] in ('written', 'uncertain'):
                        # Unknown acknowledgements reserve capacity too.
                        created += 1
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                row.update(outcome='read failed', error=str(exc))
            finally:
                if row.get('binding') == 'ask':
                    section['asks_raised'].append(row)
                elif row['outcome'] == 'dropped':
                    section['items_dropped'].append(row)
                elif row['outcome'] == 'written' and not row.get('already_written') and row['rule'] in ('R2', 'R5', 'R7'):
                    section[{'R2': 'comments_added', 'R5': 'new_tickets', 'R7': 'documents_changed'}[row['rule']]].append(row)
                if row['outcome'] not in ('written', 'dropped'):
                    section['unfiled_items'].append(row)
                save()
        for row in synthetic_asks:
            section['asks_raised'].append(row)
            row['directory'] = directory
            # Open sessions are observational in this slice. Their ask lifecycle
            # belongs to open-session integration; never act on their handoffs.
            if any(s['path'] == row['source_handoff'] for s in section['open_sessions']):
                row['outcome'] = 'pending open-session integration'
            else:
                send_ask(row)
            save()
        for candidate in discovery['candidates']:
            if candidate['directory'] != directory or not candidate['abandoned']:
                continue
            if candidate['session_id'] in report['open_session_checks'][0]['session_ids']:
                continue
            source = candidate['path']
            outcomes = [r for r in section['filing_outcomes'] if r['source_handoff'] == source]
            asks = [r for r in section['asks_raised'] if r['source_handoff'] == source]
            initial_check = candidate.get('archive_worktree_check')
            live_check = archive_worktree_check(candidate)
            clean = all(check is None or check['clean'] for check in (initial_check, live_check))
            archived = (clean and not candidate['handoff'].get('uncommitted_files') and
                        all(r['outcome'] in ('written', 'dropped') for r in outcomes) and
                        all(r['outcome'] == 'written' for r in asks))
            row = dict(path=source, outcome='kept', worktree_check=live_check,
                       reason='unfiled items, asks, or uncommitted files')
            if not clean:
                failed_check = next(check for check in (initial_check, live_check)
                                    if check is not None and not check['clean'])
                row['reason'] = 'worktree is not confirmed clean: ' + failed_check['reason']
            if archived:
                try:
                    original = Path(source)
                    check_handoff_source(candidate)
                    target = original.parent / 'archive'
                    if target.is_symlink():
                        raise ValueError('archive is a symbolic link')
                    target.mkdir(exist_ok=True)
                    target = target / original.name
                    if target.exists() or target.is_symlink():
                        target = target.with_name(report['run_id'] + '-' + original.name)
                    row.update(destination=str(target), outcome='uncertain', reason='archive move pending')
                    section['handoffs_archived'].append(row)
                    save()
                    check_handoff_source(candidate)
                    original.rename(target)
                    row.update(outcome='archived', reason='every item filed or dropped')
                except (ValueError, OSError) as exc:
                    row.update(outcome='kept', error=str(exc))
                    if row in section['handoffs_archived']:
                        section['handoffs_archived'].remove(row)
                    section['handoffs_kept'].append(row)
            else:
                section['handoffs_kept'].append(row)
            save()
    if tripwires:
        directory, source = next(iter(tripwires.items()))
        candidate = next(c for c in discovery['candidates'] if c['path'] == source)
        temporary = {'asks_raised': []}
        add_ask(temporary, candidate, 'New-ticket limit reached in ' + ', '.join(sorted(tripwires)),
                ['review unfiled items', 'raise limit'], 'tripwire', ask)
        row = temporary['asks_raised'][0]
        row['directory'] = directory
        report['directories'][directory]['asks_raised'].append(row)
        send_ask(row)
    save()


def render_report(report):
    lines = ['# Sweep report', '', ('Watch-only: every action below is proposed; no action executed.' if report['mode'] == 'watch-only'
              else 'Act: outcomes and receipts describe executed actions. New this run lists created tickets.'), '',
             *[f'{key}: {report[key]}' for key in ('run_id', 'host', 'started_at', 'ended_at', 'scope', 'mode', 'interactive', 'open_session_checks')]]
    if report['nothing_to_do']:
        lines.extend(['', 'Nothing to do.'])
    for directory, section in sorted(report['directories'].items()):
        lines.extend(['', '## ' + directory])
        for key, title in (*SECTIONS, ('filing_outcomes', 'Every filing outcome'),
                           ('worktrees_kept', 'Worktrees kept'), ('handoffs_kept', 'Handoffs kept'),
                           *((('unfiled_items', 'Unfiled items'),) if report['mode'] == 'act' else ())):
            lines.extend(['', '### ' + title])
            lines.extend(['- ' + json.dumps(row, ensure_ascii=False, sort_keys=True) for row in section[key]] or ['None.'])
    if report['errors']:
        lines.extend(['', '## Errors; sources left in place', *[json.dumps(e) for e in report['errors']]])
    lines.extend(['', '## Structured data', '', '```json', json.dumps(report, indent=2, ensure_ascii=False), '```', ''])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'run'))
    parser.add_argument('--scope', required=True, help='all or a directory')
    parser.add_argument('--decisions', type=Path)
    parser.add_argument('--user-profile', type=Path)
    parser.add_argument('--open-sessions', type=Path, help='agent-supplied session_ids and method JSON')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = settings()
        checks = read_open_sessions(args.open_sessions)
        if args.interactive and args.open_sessions is None:
            raise ValueError('interactive sweep requires open-session inspection evidence, including an empty result')
        with store_lock():
            started = datetime.now(timezone.utc)
            discovery = discover(args.scope, config, started)
            if args.command == 'inspect':
                print(json.dumps(discovery, indent=2))
                return 0
            rows = read_decisions(args.decisions, discovery)
            profile_path = args.user_profile
            if profile_path is None:
                profile_path = Path(os.environ.get('CHECKLIST_USER_PROFILE') or
                    str(Path(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))) / 'checklist/profile.md'))
                if not profile_path.exists():
                    profile_path = None
            profiles = [checklist.load_profile(profile_path)] if profile_path else []
            plan = checklist.compose(profiles)
            ask = checklist.ask_binding(profiles[0]) if profiles else None
            report = build_report(discovery, rows, plan, ask, checks, args.interactive, started)
            destination = Path(config['report_location']).expanduser().resolve()
            store = Path(handoff.state_dir()).resolve()
            if destination == store or store in destination.parents:
                raise ValueError('report location must be outside the handoff store')
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / (report['run_id'] + '.md')
            with path.open('x') as handle:
                handle.write(render_report(report))
            if config['mode'] == 'act':
                execute_act(report, discovery, plan, ask,
                            checklist.record_read_binding(profiles[0]) if profiles else None, config, path)
            print(path)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print('sweep: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
