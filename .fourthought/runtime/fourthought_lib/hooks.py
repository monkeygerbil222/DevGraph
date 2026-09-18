"""Executable validation hooks; validation never authorizes a state write itself."""
import fnmatch
from pathlib import PurePosixPath
from .attachment import doctor, git, root, decode, safe
from .coordination import acquire, check_worktree, fence
from .integrations import assurance, skills
from .policy import transition, validate_contract, validate_claim, validate_receipt

ROLE_STATES = {
 'product-manager': {'intake','product-shaping','product-ready','owner-decision','merge-ready'},
 'project-manager': {'blocked'}, 'lead': {'blocked'}, 'triage': {'triage'},
 'planner': {'ready:plan','planning'}, 'implementer': {'ready:implement','implementing','remediate'},
 'verifier': {'ready:verify','verifying'}, 'reviewer': {'ready:review','reviewing'},
 'assurance': {'reviewing','merge-ready'},
}
HOOKS = {'pre-claim','pre-agent','post-agent','pre-commit','post-commit','pre-transition','post-transition','pre-merge'}


def check_role(role,state):
    if role not in ROLE_STATES or state not in ROLE_STATES[role]:
        raise ValueError('Role does not own this issue stage')


def check_scope(paths,claim):
    patterns=claim.get('scope',{}).get('likely_paths',[])
    if not isinstance(paths,list) or not patterns:
        raise ValueError('Missing changed-path manifest or allowed scope')
    for name in paths:
        if not isinstance(name,str) or not name or name.startswith('/') or '\\' in name or any(p in ('.','..') for p in name.split('/')):
            raise ValueError('Unsafe changed path')
        if name.split('/')[0] in ('.git','.github','.fourthought','.accountability'):
            raise ValueError('Protected control-plane path: '+name)
        if not any(fnmatch.fnmatchcase(name,p) or (p.endswith('/') and name.startswith(p)) for p in patterns):
            raise ValueError('Changed path outside claim: '+name)


def diff_paths(repo,base,head):
    # NUL delimiters preserve spaces/newlines. No rename detection so both paths are checked.
    import subprocess
    p=subprocess.run(['git','-C',str(repo),'diff','--name-only','--no-renames','-z',base,head,'--'],capture_output=True)
    if p.returncode:
        raise ValueError('Cannot inspect changed paths')
    return [v.decode('utf-8') for v in p.stdout.split(b'\0') if v]


def check_changes(repo,base,receipt,claim):
    if git(repo,'rev-parse','HEAD') != receipt['head']:
        raise ValueError('Receipt HEAD does not match worktree HEAD')
    if git(repo,'status','--porcelain','--untracked-files=all'):
        raise ValueError('Uncommitted worker changes cannot be receipted')
    actual=diff_paths(repo,base,receipt['head'])
    if sorted(actual)!=sorted(receipt['changed_paths']):
        raise ValueError('Receipt changed_paths does not match Git evidence')
    check_scope(actual,claim)
    return actual


def run(kind,repo,record,*,role=None,holder=None,token=None,receipt=None,target=None,head=None,assurance_receipt=None,records=None,now=None):
    if kind not in HOOKS:
        raise ValueError('Unknown hook')
    repo=root(repo); doctor(repo)
    validate_contract(record['contract']); validate_claim(record['claim'])
    if record['contract']['issue']!=record['issue'] or record['claim']['issue']!=record['issue']:
        raise ValueError('Issue identity mismatch')
    if kind=='pre-claim':
        if records is None: raise ValueError('Complete snapshot required')
        acquire(records,record['issue'],holder,now=now)
    elif kind in ('pre-transition','post-transition','pre-merge'):
        fence(record,holder,token,now)
        if kind=='pre-merge':
            if record['state'] not in ('reviewing','merge-ready'):
                raise ValueError('Issue is not at a merge gate')
            head=git(repo,'rev-parse','refs/heads/ft/'+str(record['issue'])+'/work')
            target='merge-ready' if record['state']=='reviewing' else 'done'
        cfg=decode(safe(repo,'.fourthought/config.json'))
        required=cfg['assurance']['mode']=='required' or record.get('assurance_required',False) or record['contract'].get('assurance',{}).get('required',False) or record['claim']['risk'] in ('high','critical') or record['claim']['class'] in ('architecture','security')
        if target in ('merge-ready','done') and required:
            assurance(repo,required=True)
            record=dict(record,assurance_required=True)
        result=transition(record,target,receipt=receipt,head=head,assurance=assurance_receipt)
        if result['state']=='blocked':
            raise ValueError('Remediation loop exhausted')
    else:
        check_role(role,record['state'])
        if record['contract']['engineering']['status']!='ready' or record['contract']['owner_decisions']['status'] not in ('resolved','not-required'):
            raise ValueError('Engineering contract is not ready')
        b=check_worktree(repo,record,holder,token,now)
        cfg=decode(safe(repo,'.fourthought/config.json'))
        selected=skills(repo,cfg['skills'],role)
        if kind=='pre-agent':
            if git(b['path'],'rev-parse','HEAD')!=record['head'] or git(b['path'],'status','--porcelain','--untracked-files=all'):
                raise ValueError('Worker starting HEAD/state is not clean and current')
            return {'ok':True,'validation_only':True,'worktree':b['path'],'role':role,'skills':selected}
        if kind=='pre-commit':
            import subprocess
            result=subprocess.run(['git','-C',b['path'],'diff','--cached','--name-only','--no-renames','-z'],capture_output=True,check=True)
            check_scope([v.decode() for v in result.stdout.split(b'\0') if v],record['claim'])
        else:
            validate_receipt(receipt)
            if receipt['actor']!=holder or receipt['issue']!=record['issue']:
                raise ValueError('Receipt actor or issue mismatch')
            check_changes(b['path'],record['head'],receipt,record['claim'])
    return {'ok':True,'validation_only':True,'hook':kind}
