"""Install and replay the captured Codex hook contracts in an isolated home."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/codex-continuity'
INSTALL = ROOT / 'scripts/install-codex.py'
ADAPTER = ROOT / 'scripts/codex-hook.py'


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for key in ('CODEX_THREAD_ID', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CONTINUITY_HARNESS'):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / 'home'; home.mkdir()
    work = tmp_path / 'work'; work.mkdir()
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    subprocess.run(['git', '-C', str(work), '-c', 'user.name=Example', '-c', 'user.email=example@example.org',
                    'commit', '--allow-empty', '-qm', 'Initial'], check=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setenv('HANDOFF_STATE_DIR', str(home / 'state/handoffs'))
    monkeypatch.setenv('HANDOFF_LEDGER_DIR', str(home / 'state/ledger'))
    monkeypatch.setenv('HANDOFF_COMPACT_STATE_DIR', str(home / 'state/compact'))
    monkeypatch.setenv('CHECKLIST_STATE_DIR', str(home / 'state/checklist'))
    monkeypatch.setenv('CONTINUITY_CONFIG', str(home / 'config.json'))
    return home, work


def cli(path, *args, data=None, cwd=None, env=None):
    return subprocess.run([sys.executable, str(path), *map(str,args)], input=json.dumps(data) if data is not None else '',
                          text=True, capture_output=True, cwd=cwd, env=env)


def payload(moment, sandbox, source='interactive', **changes):
    home, work = sandbox
    value = json.loads((FIX / (moment + '.json')).read_text())
    transcript = home / '.codex/sessions/rollout-captured-session.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text((FIX / (source + '.jsonl')).read_text())
    value.update(cwd=str(work), transcript_path=str(transcript), **changes)
    return value


def checkpoint(work, sid='captured-session'):
    result = cli(ROOT/'scripts/lib/handoff.py', 'checkpoint', '--cwd', work, '--session-id', sid,
                 data={'resume': {'headline': 'Continue here', 'next_action': 'Read the record', 'open_loops': []},
                       'spawned_processes': []})
    assert result.returncode == 0, result.stderr
    return next(Path(os.environ['HANDOFF_STATE_DIR']).glob('*.json'))


def test_derived_fixtures_keep_the_probe_hashes():
    manifest = json.loads((FIX/'manifest.json').read_text())
    assert len(manifest['source_payload_sha256']) == 5
    for name, digest in manifest['derived_payload_sha256'].items():
        assert hashlib.sha256((FIX/name).read_bytes()).hexdigest() == digest


def test_one_install_registers_six_hooks_and_guard_preserves_other_entries_and_uninstalls(sandbox):
    home, work = sandbox
    codex = home/'.codex';codex.mkdir()
    (codex/'config.toml').write_text('model = "custom"\n')
    foreign={'hooks': {'Stop': [{'hooks':[{'type':'command','command':'echo foreign','timeout':1}]}]}}
    (codex/'hooks.json').write_text(json.dumps(foreign))
    handoff = checkpoint(work)
    result=cli(INSTALL, '--codex-home', codex)
    assert result.returncode == 0, result.stderr
    hooks=json.loads((codex/'hooks.json').read_text())['hooks']
    assert sum(len(g['hooks']) for groups in hooks.values() for g in groups)==8
    assert {p.name for p in (codex/'skills').iterdir() if p.name != '.system'} == {
        'checklist','resume-work','resume-checkpoint','wip','handoff-prompt','harness-improve'}
    before=(codex/'hooks.json').read_text()
    assert cli(INSTALL,'--codex-home',codex).returncode==0
    assert (codex/'hooks.json').read_text()==before
    # The installed commands point at a durable package copy, including paths with spaces.
    for groups in hooks.values():
        for group in groups:
            for hook in group['hooks']:
                if 'codex-hook.py' in hook['command']:
                    moment='Stop' if ' guard-decide' in hook['command'] else 'SessionStart'
                    p=payload(moment,sandbox)
                    run=subprocess.run(hook['command'],shell=True,input=json.dumps(p),text=True,capture_output=True)
                    assert run.returncode==0,run.stderr
    assert cli(INSTALL,'--codex-home',codex,'--uninstall').returncode==0
    assert json.loads((codex/'hooks.json').read_text()) == foreign
    assert (codex/'config.toml').read_text() == 'model = "custom"\n'
    assert handoff.exists()
    assert not [p for p in (codex/'skills').iterdir() if p.name != '.system']


def test_install_collision_and_verification_failure_do_not_change_user_files(sandbox):
    home,_=sandbox;codex=home/'.codex';(codex/'skills/checklist').mkdir(parents=True)
    result=cli(INSTALL,'--codex-home',codex)
    assert result.returncode != 0
    assert 'existing skill' in result.stderr
    assert not (codex/'hooks.json').exists()
    (codex/'skills/checklist').rmdir()
    fake=home/'codex-fail';fake.write_text('#!/bin/sh\nexit 1\n');fake.chmod(0o755)
    result=cli(INSTALL,'--codex-home',codex,'--codex',fake)
    assert result.returncode != 0
    assert not (codex/'hooks.json').exists()
    assert not (codex/'config.toml').exists()
    assert not [p for p in (codex/'skills').iterdir() if p.name != '.system']


def test_restore_precompact_and_context_half_window(sandbox):
    _,work=sandbox;checkpoint(work)
    start=payload('SessionStart',sandbox)
    assert 'handoff exists' in cli(ADAPTER,'post-compact',data=start).stdout
    start['source']='compact'
    assert 'Continue here' in cli(ADAPTER,'post-compact',data=start).stdout
    p=payload('PreCompact',sandbox)
    assert 'COMPACTION IMMINENT' in cli(ADAPTER,'pre-compact',data=p).stdout
    assert len(list(Path(os.environ['HANDOFF_COMPACT_STATE_DIR']).glob('*.json')))==1
    p=payload('UserPromptSubmit',sandbox);transcript=Path(p['transcript_path'])
    row=json.loads((FIX/'usage.jsonl').read_text());info=row['payload']['info']
    info['last_token_usage']['input_tokens']=info['model_context_window']//2
    transcript.write_text(transcript.read_text()+json.dumps(row)+'\n')
    assert '50%' in cli(ADAPTER,'context-guard',data=p).stdout
    info['last_token_usage']['input_tokens']-=1
    transcript.write_text((FIX/'interactive.jsonl').read_text()+json.dumps(row)+'\n')
    assert cli(ADAPTER,'context-guard',data=p).stdout==''


@pytest.mark.parametrize('source', ['headless','subagent'])
def test_headless_cli_and_guard_refuse_wrap(sandbox,source):
    home,_=sandbox;p=payload('PostToolUse',sandbox,source, tool_input={'command':'python3 scripts/lib/checklist.py plan'})
    env=dict(os.environ,CODEX_THREAD_ID='captured-session')
    for operation in ('plan','settle','report','wrap-check'):
        result=cli(ROOT/'scripts/lib/checklist.py',operation,env=env)
        assert result.returncode!=0 and 'never runs' in result.stderr
    result=cli(ADAPTER,'actor-guard',data=p,env=env)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    assert cli(ADAPTER,'checklist-nudge',data=p,env=env).stdout==''


def test_interactive_codex_ignores_headless_claude_parent(sandbox):
    p=payload('PostToolUse',sandbox,tool_input={'command':'python3 scripts/lib/checklist.py plan'})
    env=dict(os.environ,CODEX_THREAD_ID='captured-session',CLAUDE_CODE_ENTRYPOINT='sdk-cli')
    assert cli(ROOT/'scripts/lib/checklist.py','plan',env=env).returncode==0
    assert cli(ADAPTER,'actor-guard',data=p,env=env).stdout==''


@pytest.mark.parametrize('command,output', [('git commit -m work','committed'),('git push','pushed'),
                                          ('pytest -q','8 passed'),('pytest -q','1 failed, 8 passed')])
def test_nudge_once_and_no_failed_tests(sandbox,command,output):
    p=payload('PostToolUse',sandbox,tool_input={'command':command},tool_response=output)
    first=cli(ADAPTER,'checklist-nudge',data=p)
    assert first.returncode==0
    if 'failed' in output:
        assert first.stdout==''
    else:
        assert '/checklist' in first.stdout
        assert cli(ADAPTER,'checklist-nudge',data=p).stdout==''


def test_ledger_commit_makes_handoff_stale(sandbox):
    _,work=sandbox;checkpoint(work)
    p=payload('PostToolUse',sandbox)
    assert cli(ADAPTER,'ledger-record',data=p).returncode==0
    commit=subprocess.run(['git','-C',str(work),'-c','user.name=Example','-c','user.email=example@example.org',
                    'commit','--allow-empty','-m','Work'],check=True,text=True,capture_output=True)
    p['tool_input']['command']='git commit --allow-empty -m Work';p['tool_response']=commit.stdout
    assert cli(ADAPTER,'ledger-record',data=p).returncode==0
    stop=payload('Stop',sandbox)
    result=cli(ADAPTER,'guard-decide',data=stop)
    assert json.loads(result.stdout)['decision']=='block'
    stop['stop_hook_active']=True
    assert cli(ADAPTER,'guard-decide',data=stop).stdout==''


def test_unattended_completion_marks_wrap_until_success(sandbox):
    home,work=sandbox;handoff=checkpoint(work)
    module=ROOT/'tests/support/unattended_state.py'
    modefile=home/'mode.json'
    modefile.write_text(json.dumps({'session_id':'captured-session','mode':'afk','updated_by':'operator','updated_at':'2026-01-01T00:00:00Z'}))
    # The support module uses UNATTENDED_STATE_DIR, one record per session.
    modes=home/'modes';modes.mkdir();(modes/'captured-session').write_text(modefile.read_text())
    os.environ['UNATTENDED_STATE_DIR']=str(modes)
    config={'unattended':{'state_module':str(module),'completion_command':'finish-work','completed_by':['complete']}}
    Path(os.environ['CONTINUITY_CONFIG']).write_text(json.dumps(config))
    p=payload('PostToolUse',sandbox,tool_input={'command':'git commit -m work'},tool_response='committed')
    assert 'checkpoint' in cli(ADAPTER,'checklist-nudge',data=p).stdout.lower()
    (modes/'captured-session').write_text(json.dumps({'session_id':'captured-session','mode':'hitl','updated_by':'complete','updated_at':'2026-01-02T00:00:00Z'}))
    p['tool_input']['command']='finish-work'
    assert '--unattended' in cli(ADAPTER,'checklist-nudge',data=p).stdout
    check=ROOT/'scripts/lib/checklist.py'
    assert cli(check,'wrap-check','--written-handoff',handoff).returncode==1
    assert cli(check,'wrap-check','--written-handoff',handoff,'--unattended').returncode==0
    assert not list(Path(os.environ['CHECKLIST_STATE_DIR']).glob('*.unattended-wrap'))


def test_broken_advisory_payload_fails_open(sandbox):
    for operation in ('post-compact','pre-compact','context-guard','ledger-record','guard-decide','checklist-nudge'):
        result=subprocess.run([sys.executable,str(ADAPTER),operation],input='invalid',text=True,capture_output=True)
        assert result.returncode==0 and result.stdout==''


def test_codex_cli_uses_child_identity_instead_of_inherited_claude_identity(sandbox):
    _,work=sandbox
    payload('SessionStart',sandbox)
    env=dict(os.environ,CODEX_THREAD_ID='captured-session',CLAUDE_SESSION_ID='parent-session',
             CLAUDE_CODE_SESSION_ID='parent-session',CLAUDE_CODE_ENTRYPOINT='sdk-cli')
    result=cli(ROOT/'scripts/lib/checklist.py','plan',env=env)
    assert result.returncode==0, result.stderr
    assert (Path(os.environ['HANDOFF_LEDGER_DIR'])/'session-touched-paths.captured-session.jsonl').exists()
    assert not (Path(os.environ['HANDOFF_LEDGER_DIR'])/'session-touched-paths.parent-session.jsonl').exists()


def test_uninstall_preserves_trust_for_a_user_hook_added_after_install(sandbox):
    import importlib.util
    spec=importlib.util.spec_from_file_location('codex_trust_test',ROOT/'scripts/lib/codex_trust.py')
    trust=importlib.util.module_from_spec(spec);spec.loader.exec_module(trust)
    home,_=sandbox;codex=home/'.codex'
    result=cli(INSTALL,'--codex-home',codex)
    assert result.returncode==0,result.stderr
    target=codex/'hooks.json';config=codex/'config.toml'
    value=json.loads(target.read_text())
    later={'hooks':[{'type':'command','command':'echo user-later','timeout':2}]}
    value['hooks']['Stop'].append(later)
    target.write_text(json.dumps(value))
    oldkey=str(target.resolve())+':stop:1:0'
    digest=trust.hook_hash('Stop',later,later['hooks'][0])
    config.write_text(trust.update_config_text(config.read_text(),{oldkey:digest}))
    trust.verify_with_hooks_list(shutil.which('codex'),codex,codex,{oldkey:digest})
    result=cli(INSTALL,'--codex-home',codex,'--uninstall')
    assert result.returncode==0,result.stderr
    newkey=str(target.resolve())+':stop:0:0'
    trust.verify_with_hooks_list(shutil.which('codex'),codex,codex,{newkey:digest})


def test_quiet_commit_uses_the_captured_transcript_completion_exit_code(sandbox):
    _,work=sandbox;checkpoint(work)
    p=payload('PostToolUse',sandbox)
    assert cli(ADAPTER,'ledger-record',data=p).returncode==0
    subprocess.run(['git','-C',str(work),'-c','user.name=Example','-c','user.email=example@example.org',
                    'commit','--allow-empty','-qm','Quiet work'],check=True)
    p['tool_input']['command']='git commit --allow-empty -qm "Quiet work"'
    p['tool_response']=''
    row=json.loads((FIX/'completion.jsonl').read_text())
    item=row['payload']['item'];item['id']=p['tool_use_id']
    item['command'][-1]=p['tool_input']['command'];item['aggregated_output']=''
    with Path(p['transcript_path']).open('a') as handle:handle.write(json.dumps(row)+'\n')
    assert cli(ADAPTER,'ledger-record',data=p).returncode==0
    stop=payload('Stop',sandbox)
    result=cli(ADAPTER,'guard-decide',data=stop)
    assert result.stdout, 'a quiet owned commit still makes the handoff stale'
    assert json.loads(result.stdout)['decision']=='block'
