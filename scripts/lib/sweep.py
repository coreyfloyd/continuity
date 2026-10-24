#!/usr/bin/env python3
"""Read-only sweep planning. Agent judgments enter as identity-bound decisions.

The command never invokes write bindings, archives handoffs, or removes worktrees.
Its only filesystem write is one report containing text and structured data.
"""
from __future__ import annotations

import argparse
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
                  archive_retention_days=None)
    result.update(config)
    if result['mode'] != 'watch-only':
        raise ValueError('this sweep supports watch-only mode only; act mode is not available yet')
    for key in ('idle_days', 'new_ticket_limit', 'archive_retention_days'):
        value = result[key]
        if key == 'archive_retention_days' and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f'sweep.{key} must be a non-negative finite number')
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
            if path.is_symlink():
                raise ValueError('handoff is a symbolic link; left in place')
            data = json.loads(path.read_text())
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
            result['candidates'].append({'path': str(path), 'directory': directory or 'unknown directory',
                'session_id': session or path.stem, 'activity_at': activity, 'idle_seconds': idle,
                'abandoned': idle > config['idle_days'] * 86400,
                'items': items, 'handoff': data})
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
    allowed = {(c['path'], identity_key(i['identity'])) for c in discovery['candidates'] for i in c['items']}
    result = {}
    for row in rows:
        key = (row.get('handoff'), identity_key(row.get('identity')))
        if key not in allowed or key in result:
            raise ValueError('stale, duplicate, or unknown decision identity; inspect again')
        result[key] = row
    return result


def plan_item(candidate, item, decision, plan, ask):
    base = dict(identity=item['identity'], source_handoff=candidate['path'], item=item['item'])
    if decision is None:
        return dict(base, rule=None, outcome='pending judgment', reason='No agent filing decision supplied')
    row = {k: v for k, v in decision.items() if k not in ('handoff', 'identity')}
    rule = row.get('rule')
    row.update(index=0, status='dropped' if rule in ('R1', 'R6') else 'filed')
    # Reuse the wrap's validation, binding resolution, read failure handling and
    # ask payload. It only does bookkeeping; no binding command is ever invoked.
    single = dict(candidate['handoff'], subject={'working_directory': candidate['directory'],
                  'session_id': candidate['session_id']}, resume={'open_loops': [item['item']]})
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
    if directory in protected:
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
            row = plan_item(candidate, item, decision, plan, ask)
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
        archive = not dirty and all(row['outcome'] == 'dry run' for row in outcomes)
        if worktree and worktree['outcome'] == 'kept' and ask is None:
            archive = False
        key = 'handoffs_archived' if archive else 'handoffs_kept'
        section[key].append({'path': candidate['path'], 'outcome': 'dry run' if archive else 'kept',
            'destination': str(Path(candidate['path']).parent / 'archive' / Path(candidate['path']).name),
            'reason': 'all items would be filed or dropped' if archive else 'unfiled items, unbound ask, or uncommitted files'})
    report['nothing_to_do'] = not report['directories'] and not report['errors']
    report['ended_at'] = datetime.now(timezone.utc).isoformat()
    return report


def render_report(report):
    lines = ['# Sweep report', '', 'Watch-only: every action below is proposed; no action executed.', '',
             *[f'{key}: {report[key]}' for key in ('run_id', 'host', 'started_at', 'ended_at', 'scope', 'mode', 'interactive', 'open_session_checks')]]
    if report['nothing_to_do']:
        lines.extend(['', 'Nothing to do.'])
    for directory, section in sorted(report['directories'].items()):
        lines.extend(['', '## ' + directory])
        for key, title in (*SECTIONS, ('filing_outcomes', 'Every filing outcome'),
                           ('worktrees_kept', 'Worktrees kept'), ('handoffs_kept', 'Handoffs kept')):
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
            profiles = [checklist.load_profile(args.user_profile)] if args.user_profile else []
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
