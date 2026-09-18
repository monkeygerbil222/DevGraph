"""JSON CLI: local validation is distinct from serialized canonical issue writes."""
import argparse
import json
import os
from pathlib import Path
import sys
from .attachment import install, doctor, status, decode, root, safe


def parser():
    p=argparse.ArgumentParser(prog='fourthought')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('install','update','doctor','status','github-event'):
        s=sub.add_parser(name);s.add_argument('repo')
    for name in ('start','resume'):
        s=sub.add_parser(name);s.add_argument('repo');s.add_argument('--background',action='store_true');s.add_argument('--dry-run',action='store_true')
    for name in ('stop','sessions','queue'):
        s=sub.add_parser(name);s.add_argument('repo')
    for name in ('run','accept','repair'):
        s=sub.add_parser(name);s.add_argument('repo');s.add_argument('--issue',type=int,required=True)
        if name=='run':
            s.add_argument('--background',action='store_true')
            s.add_argument('--stop-generation',type=int,help=argparse.SUPPRESS)
    s=sub.add_parser('submit');s.add_argument('repo');s.add_argument('file',type=Path)
    s=sub.add_parser('runtime-hook');s.add_argument('repo');s.add_argument('--context',type=Path,required=True)
    s=sub.add_parser('validate');s.add_argument('kind',choices=['contract','claim','receipt']);s.add_argument('file',type=Path)
    s=sub.add_parser('transition',help='Validate a local snapshot; does not update GitHub')
    s.add_argument('record',type=Path);s.add_argument('target');s.add_argument('--receipt',type=Path);s.add_argument('--head');s.add_argument('--assurance',type=Path)
    s=sub.add_parser('worktree',help='Use a fresh canonical GitHub lease for local worktree lifecycle')
    s.add_argument('action',choices=['create','remove','check']);s.add_argument('repo');s.add_argument('--issue',type=int,required=True);s.add_argument('--holder',required=True);s.add_argument('--token',required=True)
    s=sub.add_parser('hook',help='Validate a worker snapshot; state writes require coordinator')
    s.add_argument('kind');s.add_argument('repo');s.add_argument('--record',type=Path,required=True)
    for name in ('receipt','assurance','snapshot'):
        s.add_argument('--'+name,type=Path)
    for name in ('role','holder','token','target','head'):
        s.add_argument('--'+name)
    return p


def execute(args):
    if args.command in ('install','update'):
        return install(args.repo,update=args.command=='update')
    if args.command=='doctor':return doctor(args.repo)
    if args.command=='status':
        from . import sessions,engine
        result=status(args.repo)
        if result.get('installed'):
            result['product_session']=sessions.status(args.repo)
            result['runs']=engine.status(args.repo)
        return result
    if args.command in ('start','resume'):
        from .sessions import start
        doctor(args.repo)
        return start(args.repo,resume=args.command=='resume',background=args.background,dry_run=args.dry_run)
    if args.command=='sessions':
        from . import sessions,engine
        return {'product':sessions.status(args.repo),'runs':engine.status(args.repo)}
    if args.command=='stop':
        from . import sessions,engine
        result=engine.request_stop(args.repo)
        try:result['product']=sessions.stop(args.repo)
        except ValueError as exc:result['product_notice']=str(exc)
        return result
    if args.command=='runtime-hook':
        from .sessions import tool_hook
        try:payload=json.load(sys.stdin)
        except (ValueError,TypeError):payload={}
        return tool_hook(args.repo,payload,context=args.context)
    if args.command=='queue':
        from .dispatch import Client
        return {'issues':Client(args.repo).snapshot()}
    if args.command=='repair':
        from .dispatch import Client
        return Client(args.repo).repair(args.issue)
    if args.command=='submit':
        from .dispatch import Client
        import uuid
        value=decode(args.file)
        if not isinstance(value,dict) or set(value)!={'title','body','record'}:
            raise ValueError('Submission requires title, body and initial record')
        return Client(args.repo).create(value['record'],value['title'],value['body'],session=str(uuid.uuid4()))
    if args.command in ('run','accept'):
        from . import engine
        if args.command=='run' and args.background:return engine.launch(args.repo,args.issue)
        if args.command=='run':return engine.run(args.repo,args.issue,launch_generation=args.stop_generation)
        return engine.accept(args.repo,args.issue)

    if args.command=='validate':
        from .policy import validate_contract,validate_claim,validate_receipt
        {'contract':validate_contract,'claim':validate_claim,'receipt':validate_receipt}[args.kind](decode(args.file))
        return {'ok':True,'kind':args.kind}
    if args.command=='transition':
        from .policy import transition
        r=transition(decode(args.record),args.target,receipt=decode(args.receipt) if args.receipt else None,
                     head=args.head,assurance=decode(args.assurance) if args.assurance else None)
        return {'validation_only':True,'record':r}
    if args.command=='hook':
        from .hooks import run
        return run(args.kind,args.repo,decode(args.record),role=args.role,holder=args.holder,token=args.token,
                   receipt=decode(args.receipt) if args.receipt else None,target=args.target,head=args.head,
                   assurance_receipt=decode(args.assurance) if args.assurance else None,
                   records=decode(args.snapshot) if args.snapshot else None)
    if args.command=='github-event':
        if os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or os.environ.get('GITHUB_ACTIONS')!='true':
            raise ValueError('Canonical writes require the serialized Fourthought workflow')
        event=decode(Path(os.environ['GITHUB_EVENT_PATH']))
        from .github import coordinate
        request=json.loads(event['inputs']['request'])
        return coordinate(args.repo,request)
    if args.command=='worktree':
        from . import coordination,github
        repo=root(args.repo);doctor(repo)
        config=decode(safe(repo,'.fourthought/config.json'))
        if not config['github']['enabled']:
            raise ValueError('Worktree execution requires enabled GitHub canonical state')
        api=github.GitHubAPI()
        records=github.read_canonical_snapshot(api,config['github']['repository'],config['github']['state_public_key'])
        record=coordination.find(records,args.issue)
        op={'create':coordination.create_worktree,'remove':coordination.remove_worktree,'check':coordination.check_worktree}[args.action]
        return op(repo,record,args.holder,args.token)
    raise ValueError('Unknown command')


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=execute(args)
        print(json.dumps(result,sort_keys=True,indent=2,allow_nan=False))
        return 1 if args.command in ('run','accept') and isinstance(result,dict) and result.get('state') in ('blocked','owner-decision') else 0
    except (ValueError,OSError,KeyError,TypeError,ImportError,OverflowError) as exc:
        print('Fourthought blocked: '+str(exc),file=sys.stderr)
        return 2 if args.command=='runtime-hook' else 1
