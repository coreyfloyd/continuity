#!/usr/bin/env python3
"""Identity-bound sweep planning, open-session protection and safe act execution."""
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
    checks = result['open_session_checks']
    if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks):
        raise ValueError('sweep.open_session_checks must be a list of non-empty command strings')
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


def open_session_checks(config, evidence):
    """Union successful checks; any failure protects the entire scope."""
    result = dict(evidence, results=[], failed=False)
    sessions = set(result['session_ids'])
    for command in config['open_session_checks']:
        row = {'command': command, 'session_ids': []}
        try:
            done = subprocess.run(shlex.split(command), capture_output=True, text=True,
                                  timeout=config['binding_timeout_seconds'])
            if done.returncode:
                raise ValueError(done.stderr.strip() or f'check exited {done.returncode}')
            row.update(outcome='ok', session_ids=done.stdout.split())
            sessions.update(row['session_ids'])
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            row.update(outcome='failed', error=str(exc))
            result['failed'] = True
        result['results'].append(row)
    result['session_ids'] = sorted(sessions)
    if config['open_session_checks']:
        result['method'] = 'configured commands' + (' and ' + evidence['method'] if evidence['method'] != 'idle threshold alone; no checks configured' else '')
    return result


def ask_history_path(config):
    return Path(config['report_location']).expanduser().resolve() / 'open-session-asks.json'


def read_ask_history(config):
    path = ask_history_path(config)
    if path.is_symlink():
        raise ValueError('open-session ask history is a symbolic link')
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not all(isinstance(v, (int, float)) for v in data.values()):
        raise ValueError('malformed open-session ask history')
    return data


def save_ask_history(config, history):
    path = ask_history_path(config)
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as handle:
            json.dump(history, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def worktree_protection(discovery, report, checks, act=False):
    """Gather every source's veto before planning any shared-path removal."""
    retained = {row['path'] for section in report['directories'].values()
                for row in section['handoffs_kept']} if not act else set()
    if act:
        for section in report['directories'].values():
            for row in section['filing_outcomes'] + section['asks_raised']:
                if row['outcome'] not in ('written', 'dropped', 'present directly to user'):
                    retained.add(row['source_handoff'])
    protected = {}
    for candidate in discovery['candidates']:
        reasons = protected.setdefault(candidate['directory'], set())
        if not candidate['abandoned'] or candidate['session_id'] in checks['session_ids']:
            reasons.add('another open or active session uses this worktree')
        if candidate['handoff'].get('uncommitted_files'):
            reasons.add('handoff lists uncommitted files: ' + candidate['path'])
        if candidate['path'] in retained:
            reasons.add('handoff is kept: ' + candidate['path'])
        if candidate.get('worktree_retention_reason'):
            reasons.add(candidate['worktree_retention_reason'])
    return {directory: '; '.join(sorted(reasons)) for directory, reasons in protected.items() if reasons}


def worktree_plan(candidate, protected):
    """Read-only eligibility checks, repeated immediately before plain removal."""
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
    reasons = {why for active, why in protected.items()
               if Path(directory) == Path(active) or Path(directory) in Path(active).parents}
    if reasons:
        reason = '; '.join(sorted(reasons))
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
    if candidate['handoff'].get('uncommitted_files'):
        reason = 'handoff lists uncommitted files'
    if sum(e.get('branch') == match.get('branch') for e in entries) > 1:
        reason = 'local branch is used by another worktree'
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


def build_report(discovery, decisions, plan, ask, checks, interactive, started, history=None):
    report = {'run_id': uuid.uuid4().hex, 'host': socket.gethostname(), 'started_at': started.isoformat(),
              'scope': discovery['scope'], 'mode': 'watch-only', 'interactive': interactive,
              'open_session_checks': [checks], 'errors': discovery['errors'], 'directories': {}}
    asked_sessions = set()
    for candidate in discovery['candidates']:
        is_open = candidate['session_id'] in checks['session_ids']
        if not candidate['abandoned'] and not is_open and not checks.get('failed'):
            continue
        section = report['directories'].setdefault(candidate['directory'],
            {**{key: [] for key, _ in SECTIONS}, 'filing_outcomes': [], 'worktrees_kept': [], 'handoffs_kept': []})
        if is_open:
            section['open_sessions'].append({key: candidate[key] for key in
                                            ('session_id', 'idle_seconds', 'activity_at', 'path')})
            if (candidate['idle_seconds'] > discovery['idle_days'] * 86400
                    and (history or {}).get(candidate['session_id'], -1) < candidate['activity_at']
                    and candidate['session_id'] not in asked_sessions):
                add_ask(section, candidate, 'Open idle session: ' + candidate['session_id'],
                        ['wrap it', 'leave it', 'close it'],
                        'present directly to user' if interactive else 'would raise an ask; no ask sent in watch-only', ask)
                asked_sessions.add(candidate['session_id'])
                section['asks_raised'][-1].update(open_session=True, activity_at=candidate['activity_at'])
                section['asks_raised'][-1]['ask']['activity_at'] = candidate['activity_at']
            continue
        if not candidate['abandoned']:
            section['handoffs_kept'].append(dict(path=candidate['path'], outcome='kept',
                reason='open-session check failed; abandonment unknown'))
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
        clean_check = archive_worktree_check(candidate)
        candidate['archive_worktree_check'] = clean_check
        archive = (not dirty and (clean_check is None or clean_check['clean']) and
                   all(row['outcome'] == 'dry run' for row in outcomes))
        key = 'handoffs_archived' if archive else 'handoffs_kept'
        section[key].append({'path': candidate['path'], 'outcome': 'dry run' if archive else 'kept',
            'worktree_check': clean_check,
            'destination': str(Path(candidate['path']).parent / 'archive' / Path(candidate['path']).name),
            'reason': 'all items would be filed or dropped' if archive else 'unfiled items, unbound ask, or uncommitted files'})
    protected = worktree_protection(discovery, report, checks)
    for candidate in discovery['candidates']:
        if not candidate['abandoned']:
            continue
        section = report['directories'][candidate['directory']]
        worktree = worktree_plan(candidate, protected)
        if worktree:
            if worktree['outcome'] == 'dry run':
                if worktree not in section['worktrees_removed']:
                    section['worktrees_removed'].append(worktree)
            else:
                section['worktrees_kept'].append(worktree)
                add_ask(section, candidate, 'Keep worktree ' + worktree['path'] + ': ' + worktree['reason'],
                        ['review worktree', 'leave it'], worktree['reason'], ask)
                section['asks_raised'][-1]['worktree'] = True
                if ask is None:
                    for row in list(section['handoffs_archived']):
                        if row['path'] == candidate['path']:
                            section['handoffs_archived'].remove(row)
                            row.update(outcome='kept', reason='unbound worktree ask')
                            section['handoffs_kept'].append(row)
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


def execute_act(report, discovery, plan, ask, reader, config, path, history=None):
    """Execute binding writes, asks, checked worktree removals and archive moves.

    Identity queries precede every attempted write, even without an earlier report.
    Bindings must enforce identity uniqueness atomically, including delayed writes.
    """
    history = {} if history is None else history
    report['mode'] = 'act'
    report['undo_items'] = []
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
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x') as handle:
                handle.write(render_report(report))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

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
        intent = None
        if operation != 'ask':
            intent = dict(kind='record', write_operation=operation,
                          run_id=report['run_id'], **request(row, 'reverse'))
            intent.update(destination=row.get('destination'), write_outcome='uncertain')
            report['undo_items'].append(intent)
        save()
        payload = request(row, operation)
        payload.update(destination=row.get('destination'), rule=row['rule'], run_id=report['run_id'])
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
        if intent is not None:
            intent.update(write_outcome=row['outcome'])
            for key in ('receipt', 'destination'):
                if key in row:
                    intent[key] = row[key]
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
        synthetic_asks = [row for row in section['asks_raised']
                          if row not in section['filing_outcomes'] and not row.get('worktree')]
        for key in ('asks_raised', 'new_tickets', 'comments_added', 'documents_changed',
                    'items_dropped', 'worktrees_removed', 'worktrees_kept', 'handoffs_archived'):
            section[key] = []
        section['handoffs_kept'] = [row for row in section['handoffs_kept']
                                    if report['open_session_checks'][0].get('failed')]
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
            if row.get('open_session') and report['interactive']:
                row['outcome'] = 'present directly to user'
            else:
                send_ask(row)
            save()
            if row.get('open_session') and row['outcome'] in ('written', 'present directly to user'):
                history[row['identity']['session_id']] = row['activity_at']
                save_ask_history(config, history)
    # All sections finish filing before a worktree can be removed. Include
    # sibling sources below the worktree root, not just exact directory peers.
    checks = report['open_session_checks'][0]
    for candidate in discovery['candidates']:
        try:
            check_handoff_source(candidate)
            initial = candidate.get('archive_worktree_check')
            current = archive_worktree_check(candidate)
            if any(check is not None and not check['clean'] for check in (initial, current)):
                candidate['worktree_retention_reason'] = 'related handoff has unconfirmed clean worktree: ' + candidate['path']
        except (ValueError, OSError) as exc:
            candidate['worktree_retention_reason'] = 'related handoff changed or unreadable: ' + candidate['path'] + ': ' + str(exc)
    protected = worktree_protection(discovery, report, checks, act=True)
    removed_paths = set()
    attempted_paths = set()
    for directory, section in report['directories'].items():
        for candidate in discovery['candidates']:
            if candidate['directory'] != directory or not candidate['abandoned']:
                continue
            if directory in attempted_paths:
                continue
            proposed = worktree_plan(candidate, protected)
            if not proposed:
                continue
            attempted_paths.add(directory)
            if proposed['outcome'] != 'dry run':
                if not any(r['path'] == proposed['path'] for r in section['worktrees_kept']):
                    section['worktrees_kept'].append(proposed)
                    temporary = {'asks_raised': []}
                    add_ask(temporary, candidate, 'Keep worktree ' + directory + ': ' + proposed['reason'],
                            ['review worktree', 'leave it'], proposed['reason'], ask)
                    failure = temporary['asks_raised'][0]
                    failure['directory'] = directory
                    section['asks_raised'].append(failure)
                    send_ask(failure)
                    save()
                continue
            row = proposed
            try:
                related = [c for c in discovery['candidates']
                           if Path(directory) == Path(c['directory']) or Path(directory) in Path(c['directory']).parents]
                for source in related:
                    check_handoff_source(source)
                # Resolve the main checkout before removing this linked checkout.
                _, output, _ = git_read(directory, 'worktree', 'list', '--porcelain')
                main = output.splitlines()[0].removeprefix('worktree ')
                row.update(outcome='uncertain', reason='plain worktree removal pending')
                section['worktrees_removed'].append(row)
                save()
                for source in related:
                    check_handoff_source(source)
                code, _, error = git_read(main, 'worktree', 'remove', directory)
                row['checks']['plain_removal'] = code == 0
                if code:
                    raise ValueError(error or 'plain worktree removal failed')
                removed_paths.add(directory)
                row.update(outcome='removed', reason='clean branch contained in pushed trunk')
                # Compare-and-delete preserves a concurrently moved branch.
                code, _, error = git_read(main, 'update-ref', '-d',
                    'refs/heads/' + row['branch'], row['commit'])
                row['branch_deleted'] = code == 0
                if code:
                    row['branch_error'] = error or 'local branch deletion failed'
                save()
            except (ValueError, OSError) as exc:
                row.update(outcome='kept', reason=str(exc))
                if row in section['worktrees_removed']:
                    section['worktrees_removed'].remove(row)
                section['worktrees_kept'].append(row)
                temporary = {'asks_raised': []}
                add_ask(temporary, candidate, 'Keep worktree ' + directory + ': ' + str(exc),
                        ['review worktree', 'leave it'], str(exc), ask)
                failure = temporary['asks_raised'][0]
                failure['directory'] = directory
                section['asks_raised'].append(failure)
                send_ask(failure)
                save()
    for directory, section in report['directories'].items():
        for candidate in discovery['candidates']:
            if candidate['directory'] != directory or not candidate['abandoned']:
                continue
            if candidate['session_id'] in report['open_session_checks'][0]['session_ids']:
                continue
            source = candidate['path']
            outcomes = [r for r in section['filing_outcomes'] if r['source_handoff'] == source]
            asks = [r for r in section['asks_raised'] if r['source_handoff'] == source]
            initial_check = candidate.get('archive_worktree_check')
            live_check = (initial_check if any(Path(directory) == Path(root) or Path(root) in Path(directory).parents
                                              for root in removed_paths) else archive_worktree_check(candidate))
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
                    report['undo_items'].append(dict(kind='handoff', path=source,
                        archive=str(target), snapshot=candidate['source_snapshot']))
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


def save_operation(path, report):
    """Atomically replace the durable operation journal after every outcome."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as handle:
            handle.write('# Sweep ' + report['mode'] + ' report\n\n' +
                         report.get('note', '') + '\n\n```json\n' +
                         json.dumps(report, indent=2) + '\n```\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_report(path):
    if path.is_symlink():
        raise ValueError('report is a symbolic link')
    return json.loads(path.read_text().split('```json\n', 1)[1].rsplit('\n```', 1)[0])


def report_directory(config):
    destination = Path(config['report_location']).expanduser().resolve()
    store = Path(handoff.state_dir()).resolve()
    if destination == store or store in destination.parents:
        raise ValueError('report location must be outside the handoff store')
    return destination


def execute_undo(config, run_id, record):
    if not run_id or any(c not in '0123456789abcdef' for c in run_id):
        raise ValueError('run id must be a hexadecimal sweep run id')
    destination = report_directory(config)
    original = read_report(destination / (run_id + '.md'))
    if original.get('mode') != 'act' or original.get('run_id') != run_id:
        raise ValueError('undo requires an act run')
    if 'undo_items' not in original:
        raise ValueError('run has no reversal journal')
    path = destination / (run_id + '.undo.md')
    report = read_report(path) if path.exists() else dict(
        mode='undo', run_id=run_id, items=[dict(row, outcome='failed')
            for row in original['undo_items'] if row['kind'] == 'handoff'
            or row.get('write_outcome') in ('written', 'uncertain')],
        note='Worktree removal and local branch deletion cannot be reversed. '
             'Asks remain delivered. Conflicts are preserved; only failed items retry.')
    save_operation(path, report)
    store = Path(handoff.state_dir()).resolve()
    archive = store / 'archive'
    for row in report['items']:
        if row['outcome'] != 'failed':
            continue
        try:
            if row['kind'] == 'handoff':
                source, target = Path(row['archive']), Path(row['path'])
                if (archive.is_symlink() or source.parent != archive or target.parent != store
                        or source.is_symlink() or target.is_symlink()):
                    raise ValueError('restore paths must be regular handoffs in this store and archive')
                if target.exists():
                    # Recover a crash between the non-overwriting link and unlink.
                    restored = False
                    if not source.exists():
                        _, current = read_handoff_source(target)
                        restored = all(current[k] == v for k, v in row['snapshot'].items()
                                       if k != 'st_ctime_ns')
                    if restored:
                        row.update(outcome='reversed')
                    elif source.exists() and os.path.samefile(source, target):
                        source.unlink()
                        row.update(outcome='reversed')
                    else:
                        row.update(outcome='conflict', reason='original path is occupied; archived handoff preserved')
                elif not source.exists():
                    raise ValueError('archived handoff is missing (possibly removed by retention)')
                else:
                    _, current = read_handoff_source(source)
                    if any(current[k] != v for k, v in row['snapshot'].items() if k != 'st_ctime_ns'):
                        row.update(outcome='conflict', reason='archived handoff changed')
                    else:
                        # link is exclusive: a newly written handoff is never overwritten.
                        os.link(source, target, follow_symlinks=False)
                        source.unlink()
                        row.update(outcome='reversed')
            else:
                if row.get('write_outcome') == 'uncertain' and not row.get('receipt'):
                    result = binding_call(record, dict(row, operation='identity_exists'),
                                          config['binding_timeout_seconds'])
                    if result.get('exists') is not True or result.get('run_id') != run_id:
                        raise ValueError('uncertain write ownership not confirmed for this run')
                    if not all(isinstance(result.get(k), str) and result[k].strip()
                               for k in ('receipt', 'destination')):
                        raise ValueError('uncertain write needs receipt and destination')
                    row.update(receipt=result['receipt'], destination=result['destination'])
                    save_operation(path, report)
                result = binding_call(record, dict(row, operation='reverse'),
                                      config['binding_timeout_seconds'])
                status = result.get('status')
                if status not in ('reversed', 'conflict', 'failed'):
                    raise ValueError('unsupported or malformed reversal acknowledgement')
                row.update(outcome=status, reason=result.get('reason') or result.get('error') or status)
        except FileExistsError:
            row.update(outcome='conflict', reason='original path is occupied')
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            row.update(outcome='failed', reason=str(exc))
        save_operation(path, report)
    return path


def execute_retention(config, now, save=None):
    report = dict(mode='retention', started_at=now.isoformat(),
                  archive_retention_days=config['archive_retention_days'], items=[])
    archive = Path(handoff.state_dir()).resolve() / 'archive'
    days = config['archive_retention_days']
    if days is None or not archive.exists():
        if save:
            save(report)
        return report
    if archive.is_symlink():
        raise ValueError('archive is a symbolic link')
    for path in sorted(archive.glob('*.json')):
        if path.is_symlink() or not path.is_file():
            continue
        row = dict(path=str(path), outcome='kept')
        report['items'].append(row)
        try:
            _, snapshot = read_handoff_source(path)
            row['modified_at'] = snapshot['st_mtime_ns'] / 1e9
            if now.timestamp() - row['modified_at'] > days * 86400:
                row.update(outcome='failed', reason='deletion pending')
                if save:
                    save(report)
                _, current = read_handoff_source(path)
                if current != snapshot:
                    raise ValueError('archived handoff changed during retention')
                path.unlink()
                row.update(outcome='deleted', reason='strictly older than configured age')
            else:
                row['reason'] = 'at or newer than age boundary'
        except (OSError, ValueError) as exc:
            row.update(outcome='failed', reason=str(exc))
        if save:
            save(report)
    if save:
        save(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'run', 'undo', 'retention'))
    parser.add_argument('--scope', help='all or a directory')
    parser.add_argument('--decisions', type=Path)
    parser.add_argument('--user-profile', type=Path)
    parser.add_argument('--open-sessions', type=Path, help='agent-supplied session_ids and method JSON')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--user-started', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = settings()
        if args.command in ('undo', 'retention'):
            with store_lock():
                if args.command == 'undo':
                    if not args.user_started:
                        raise ValueError('undo must be explicitly user-started with --user-started')
                    profile_path = args.user_profile or Path(os.environ.get('CHECKLIST_USER_PROFILE') or
                        str(Path(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))) / 'checklist/profile.md'))
                    profiles = [checklist.load_profile(profile_path)] if args.user_profile or profile_path.exists() else []
                    plan = checklist.compose(profiles)
                    record = next((step.get('binding') for step in plan if step.get('slot') == 'record'), None)
                    path = execute_undo(config, args.run_id, record)
                else:
                    destination = report_directory(config)
                    destination.mkdir(parents=True, exist_ok=True)
                    path = destination / (uuid.uuid4().hex + '.retention.md')
                    execute_retention(config, datetime.now(timezone.utc),
                                      lambda report: save_operation(path, report))
                print(path)
            return 0
        if args.scope is None:
            raise ValueError('inspect and run require --scope')
        checks = read_open_sessions(args.open_sessions)
        if args.interactive and args.open_sessions is None and not config['open_session_checks']:
            raise ValueError('interactive sweep requires open-session inspection evidence, including an empty result')
        with store_lock():
            started = datetime.now(timezone.utc)
            checks = open_session_checks(config, checks)
            history = read_ask_history(config)
            discovery = discover(args.scope, config, started)
            discovery['idle_days'] = config['idle_days']
            discovery['open_session_checks'] = checks
            for check in checks['results']:
                if check['outcome'] == 'failed':
                    discovery['errors'].append(dict(check, reason='open-session check failed; no session counts as abandoned'))
            for candidate in discovery['candidates']:
                if checks['failed'] or candidate['session_id'] in checks['session_ids']:
                    candidate['abandoned'] = False
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
            report = build_report(discovery, rows, plan, ask, checks, args.interactive, started, history)
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
                            checklist.record_read_binding(profiles[0]) if profiles else None, config, path, history)
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
