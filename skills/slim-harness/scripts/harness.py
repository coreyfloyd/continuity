#!/usr/bin/env python3
"""Measure explicit session inputs and journal exact, individually reversible edits.

No semantic review, file discovery, generator execution, or git mutation occurs here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


def size(text):
    return dict(lines=len(text.splitlines()), words=len(text.split()), bytes=len(text.encode('utf-8')))


def frontmatter(text):
    """Read scalar discovery fields, including quoted and block YAML strings.

Reject unsupported discovery-field syntax rather than silently undercount it.
"""
    lines=text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return {}, text
    end=next((i for i in range(1,len(lines)) if lines[i].strip()=='---'),None)
    if end is None:
        raise ValueError('unterminated skill frontmatter')
    fields={}; i=1
    while i < end:
        match=re.match(r'^(description|when_to_use):\s*(.*?)\s*$',lines[i])
        i+=1
        if not match:
            continue
        key,value=match.groups()
        if value not in ('|','|-','|+','>','>-','>+'):
            continuation=i
            while continuation < end and (not lines[continuation].strip()
                    or lines[continuation].lstrip().startswith('#')):
                continuation+=1
            if continuation < end and lines[continuation][0:1].isspace():
                raise ValueError('unsupported multiline skill discovery scalar: '+key)
        if value in ('|','|-','|+','>','>-','>+'):
            block=[]
            while i < end and (lines[i][0:1].isspace() or not lines[i].strip()):
                block.append(lines[i]); i+=1
            import textwrap
            value=textwrap.dedent(''.join(block))
            if match.group(2).startswith('>'):
                # YAML folds adjacent nonempty lines; blank lines retain paragraph breaks.
                value=re.sub(r'(?<=\S)\n(?=\S)', ' ', value)
            if match.group(2).endswith('-'):
                value=value.rstrip('\n')
            elif not match.group(2).endswith('+'):
                value=value.rstrip('\n')+'\n'
        elif value.startswith('"'):
            value=json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value=value[1:-1].replace("''", "'")
        elif value.startswith(('[','{','&','*','!')):
            raise ValueError('unsupported skill discovery scalar: '+key)
        else:
            value=re.split(r'\s+#',value,maxsplit=1)[0]
        fields[key]=value
    return fields, ''.join(lines[end+1:])


def measure(manifest):
    """Only measure caller-attested loaded files; listed skills expose metadata only.

Kinds: rules, user-rules, skill, agent, role, output-style. The parent provides
role_paths from the actual definition and chain from root to session start.
"""
    tool=manifest['tool']
    if tool not in ('claude','codex'):
        raise ValueError('tool must be claude or codex')
    result=dict(over_limit=[], measured=[], unresolved=[])
    queue=list(manifest.get('files',[])); seen=set(); descriptions=[]; loaded_sizes={}
    while queue:
        item=queue.pop(0);loaded=item.get('loaded');kind=item['kind'];path=Path(item['path']).expanduser()
        if loaded not in ('full','auto','listed') or (loaded=='listed' and kind!='skill'):
            continue
        if kind=='output-style' and tool!='claude':
            continue
        if kind not in ('rules','user-rules','skill','agent','role','output-style'):
            raise ValueError('unsupported instruction kind: '+kind)
        key=(str(path.resolve()),kind,loaded)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            result['unresolved'].append(str(path));continue
        text=path.read_bytes().decode('utf-8');stats=size(text);limits=[]
        if loaded!='listed':
            loaded_sizes[str(path.resolve())]=stats
            count=stats['lines'];limit=200;unit='lines'
            if kind=='skill':
                _,body=frontmatter(text);body_stats=size(body)
                count=body_stats['lines'];limit=500;unit='body lines'
                if body_stats['bytes']>20000:
                    limits.append(dict(unit='body bytes',measured=body_stats['bytes'],limit=20000))
            elif kind=='user-rules' and tool=='codex':
                count=stats['bytes'];limit=32768;unit='bytes'
            if count>limit:
                limits.append(dict(unit=unit,measured=count,limit=limit))
        if kind=='skill':
            fields,_=frontmatter(text);desc=fields.get('description','');when=fields.get('when_to_use','')
            stats['description_characters']=len(desc)
            descriptions.append(dict(path=str(path),characters=len(desc)))
            if len(desc)>1024:
                limits.append(dict(unit='description characters',measured=len(desc),limit=1024))
            if tool=='claude' and len(desc)+len(when)>1536:
                limits.append(dict(unit='description + when_to_use characters',measured=len(desc)+len(when),limit=1536))
        row=dict(path=str(path),source=str(path.resolve()),size=stats,limits=limits,
                 action='run /slim-harness on `'+str(path)+'`')
        # Listed-only bodies are not reported or used for size triggers.
        if loaded=='listed':
            row['size']=dict(description_characters=len(fields.get('description','')))
        result['measured'].append(row)
        if limits:
            # One row per file, even if two limits are exceeded or the manifest repeats it.
            existing=next((r for r in result['over_limit'] if r['source']==row['source']),None)
            if existing:
                existing['size'].update(row['size'])
                for violation in limits:
                    if violation not in existing['limits']:existing['limits'].append(violation)
            else:result['over_limit'].append(row)
        if kind=='agent' and loaded!='listed':
            for role in item.get('role_paths',[]):
                role_path=Path(role).expanduser()
                if not role_path.is_absolute():role_path=path.parent/role_path
                queue.append(dict(path=str(role_path),kind='role',loaded='full'))
    if tool=='codex':
        chain=manifest.get('chain',[]);chain_stats=[]
        for named in chain:
            resolved=str(Path(named).expanduser().resolve())
            if resolved not in loaded_sizes:
                raise ValueError('chain member is not an attested loaded file: '+str(named))
            if resolved not in [p for p,_ in chain_stats]:chain_stats.append((resolved,loaded_sizes[resolved]))
        total={unit:sum(stats[unit] for _,stats in chain_stats) for unit in ('lines','words','bytes')}
        if total['bytes']>32768:
            result['over_limit'].append(dict(path='AGENTS.md chain',source='AGENTS.md chain',members=chain,
                size=total,limits=[dict(unit='bytes',measured=total['bytes'],limit=32768)],
                action='choose one chain member; run /slim-harness on that file'))
        if manifest.get('shortened_descriptions'):
            window=manifest.get('context_window')
            result['listing']=dict(budget=int(window*.02) if window else 8000,
                largest=sorted(descriptions,key=lambda d:d['characters'],reverse=True),trigger=False)
    return result


def dirty_repositories(paths):
    repos=set()
    for path in paths:
        root=subprocess.run(['git','-C',str(Path(path).parent),'rev-parse','--show-toplevel'],capture_output=True,text=True)
        if root.returncode==0:
            repo=root.stdout.strip()
            status=subprocess.run(['git','-C',repo,'status','--porcelain'],capture_output=True,text=True,check=True)
            if status.stdout:repos.add(repo)
    return sorted(repos)


def edit(path,before,after,journal,mode='interactive'):
    if mode!='interactive':
        return dict(changed=False,needs_user=True,message='slim-harness needs the user present; edited nothing')
    source=Path(path).expanduser().resolve(strict=True);current=source.read_bytes().decode('utf-8')
    if before==after:
        return dict(changed=False,source=str(source),dirty_repositories=[])
    if not before or not after:
        raise ValueError('include unchanged context around insertions and deletions')
    if current.count(before)!=1:
        raise ValueError('own lines are missing or ambiguous; use unique unchanged context')
    updated=current.replace(before,after,1)
    if updated.count(after)!=1:
        raise ValueError('result would be ambiguous to revert; include more unchanged context')
    journal=Path(journal)
    records=json.loads(journal.read_text()) if journal.exists() else []
    record=dict(number=len(records)+1,source=str(source),before=before,after=after,reverted=False)
    # Persist recovery information before changing the source; no full-file rollback.
    journal.write_text(json.dumps(records+[record],indent=2)+'\n')
    source.write_bytes(updated.encode('utf-8'))
    return dict(changed=True,source=str(source),number=record['number'],dirty_repositories=dirty_repositories([source]))


def revert(journal,number):
    journal=Path(journal);records=json.loads(journal.read_text());record=next(r for r in records if r['number']==number)
    source=Path(record['source']);current=source.read_bytes().decode('utf-8')
    if record['reverted']:
        return dict(changed=False,dirty_repositories=dirty_repositories([source]))
    if current.count(record['after'])!=1:
        raise ValueError('own lines changed or ambiguous; ask user to reconcile, never restore whole file')
    source.write_bytes(current.replace(record['after'],record['before'],1).encode('utf-8'));record['reverted']=True
    journal.write_text(json.dumps(records,indent=2)+'\n')
    return dict(changed=True,dirty_repositories=dirty_repositories([r['source'] for r in records]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('measure');p.add_argument('manifest',type=Path)
    p=sub.add_parser('edit');p.add_argument('request',type=Path);p.add_argument('journal',type=Path)
    p=sub.add_parser('revert');p.add_argument('journal',type=Path);p.add_argument('number',type=int)
    args=parser.parse_args()
    try:
        if args.operation=='measure':result=measure(json.loads(args.manifest.read_text()))
        elif args.operation=='edit':result=edit(journal=args.journal,**json.loads(args.request.read_text()))
        else:result=revert(args.journal,args.number)
        print(json.dumps(result,indent=2));return 0
    except (OSError,ValueError,KeyError,StopIteration) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':
    sys.exit(main())
