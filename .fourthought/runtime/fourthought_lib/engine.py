"""Bounded Claude supervisor; GitHub alone owns issue/lease state."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time
import threading
import uuid
from . import attachment, coordination, hooks, integrations, policy

OUTPUT_SCHEMA = {'type':'object','additionalProperties':False,'required':['result','summary'],
                 'properties':{'result':{'enum':['pass','fail']},'summary':{'type':'string','minLength':1}}}
PLAN_SCHEMA = dict(OUTPUT_SCHEMA, required=['result','summary','planned_paths'],
                   properties=dict(OUTPUT_SCHEMA['properties'], planned_paths={
                       'type':'array','items':{'type':'string','minLength':1},'uniqueItems':True}))
STAGES = {'ready:plan':('planner','planning','plan','ready:implement'),
          'ready:implement':('implementer','implementing','implement','ready:verify'),
          'remediate':('implementer','remediate','remediate','ready:verify'),
          'ready:verify':('verifier','verifying','verify','ready:review'),
          'ready:review':('reviewer','reviewing','review','merge-ready')}


def settings(config):
    value=config.get('runtime',{})
    commands=value.get('verify_commands')
    if not isinstance(commands,list) or not commands:
        raise ValueError('Configure runtime.verify_commands with actual verification argv arrays before engineering')
    for cmd in commands:
        if not isinstance(cmd,list) or not cmd or any(not isinstance(arg,str) or not arg or '\0' in arg for arg in cmd):
            raise ValueError('Each verification command must be a nonempty argv array')
    result={'verify_commands':commands,'push_remote':value.get('push_remote','origin'),
            'worker_timeout':value.get('worker_timeout',900),'lease_ttl':value.get('lease_ttl',900)}
    if not isinstance(result['push_remote'],str) or not result['push_remote'] or result['push_remote'].startswith('-'):
        raise ValueError('Invalid runtime.push_remote')
    for name,minimum,maximum in [('worker_timeout',10,7200),('lease_ttl',600,86400)]:
        if type(result[name]) is not int or not minimum<=result[name]<=maximum:
            raise ValueError('Invalid runtime.'+name)
    return result


def directory(repo):
    common=Path(attachment.git(repo,'rev-parse','--path-format=absolute','--git-common-dir'))
    path=common/'fourthought-runs'
    if path.is_symlink():raise ValueError('Symlinked run metadata')
    path.mkdir(mode=0o700,exist_ok=True)
    return path


@contextmanager
def run_lock(repo,issue,wait_seconds=0):
    import fcntl
    path=directory(repo)/(str(issue)+'.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        deadline=time.monotonic()+wait_seconds
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError as exc:
                if time.monotonic()>=deadline:raise ValueError('An issue supervisor is already running') from exc
                time.sleep(0.05)
        yield
    finally:os.close(fd)


def request_stop(repo):
    repo=attachment.root(repo);path=directory(repo)/'stop'
    attachment.atomic(path,b'Stop requested\n')
    return {'stop_requested':True,'note':'Active supervisors stop their own workers; leases stay fenced until reconciliation'}


def status(repo):
    repo=attachment.root(repo)
    common=Path(attachment.git(repo,'rev-parse','--path-format=absolute','--git-common-dir'))
    path=common/'fourthought-runs'
    if path.is_symlink():raise ValueError('Symlinked run metadata')
    result=[]
    if path.is_dir():
        for file in sorted(path.glob('*.json')):
            if file.is_symlink():raise ValueError('Symlinked run record')
            data=attachment.decode(file)
            result.append({k:data[k] for k in ('issue','role','state','updated','error') if k in data})
    return result


def group_alive(pid):
    try:os.killpg(pid,0);return True
    except ProcessLookupError:return False


def terminate(process):
    # The leader may already have exited while descendants still own this group.
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:process.wait(timeout=1)
    except subprocess.TimeoutExpired:pass
    # Never let a TERM-ignoring descendant outlive a successful leader wait.
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:raise ValueError('Worker group termination could not be confirmed')


def execute(argv,cwd,env,heartbeat,timeout,input_text=None):
    """An independent watchdog fences workers even while renewal HTTP blocks."""
    first_communication=True
    started=time.monotonic();finished=threading.Event();failure=[]
    process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    def watch():
        while not finished.wait(.1):
            deadline=getattr(heartbeat,'deadline',lambda:None)()
            cancelled=getattr(heartbeat,'cancelled',lambda:False)()
            if cancelled or (deadline is not None and time.time()>=deadline) or time.monotonic()-started>timeout:
                failure.append('Worker lease expired, stop requested, or execution timed out; worktree preserved')
                terminate(process)
                return
    guard=threading.Thread(target=watch,daemon=True);guard.start()
    try:
        while True:
            heartbeat()
            if failure:raise ValueError(failure[0])
            try:
                out,err=process.communicate(input=input_text if first_communication else None,timeout=1)
                if failure:raise ValueError(failure[0])
                if group_alive(process.pid):
                    terminate(process)
                    raise ValueError('Worker left running descendants; progression blocked')
                heartbeat()
                if failure:raise ValueError(failure[0])
                return process.returncode,out,err
            except subprocess.TimeoutExpired:
                first_communication=False
                continue
    except BaseException as exc:
        terminate(process)
        # A detached descendant can inherit these pipes after the supervised
        # process group is dead. Bound the drain so evidence collection cannot
        # turn a worker timeout into an unbounded supervisor hang.
        try:
            out,err=process.communicate(timeout=1)
        except subprocess.TimeoutExpired as drain:
            def text(value):
                if value is None:return ''
                return value.decode(errors='replace') if isinstance(value,bytes) else value
            out,err=text(drain.output),text(drain.stderr)
            for stream in (process.stdout,process.stderr):
                if stream is not None and not stream.closed:stream.close()
        exc.worker_output=(process.returncode,out,err)
        raise
    finally:
        finished.set();guard.join(timeout=6)
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream is not None and not stream.closed:stream.close()


def usage_fields(data):
    """Allowlisted numeric telemetry only; null means unreported, never zero.

    usage is the invocation total; modelUsage is its per-model breakdown.
    Consumers must not add the two representations together.
    """
    def number(value):
        return value if type(value) in (int,float) and value>=0 and math.isfinite(value) else None
    def counters(value,names):
        return {name:number(value.get(name)) for name in names} if isinstance(value,dict) else None
    usage=counters(data.get('usage'),('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'))
    if usage is not None:
        usage['cache_creation']=counters(data['usage'].get('cache_creation'),
                                        ('ephemeral_5m_input_tokens','ephemeral_1h_input_tokens'))
        usage['server_tool_use']=counters(data['usage'].get('server_tool_use'),
                                         ('web_search_requests','web_fetch_requests'))
    models=data.get('modelUsage')
    return {'usage':usage,
            'modelUsage':{name:counters(value,('inputTokens','outputTokens','cacheReadInputTokens',
                          'cacheCreationInputTokens','webSearchRequests','costUSD','contextWindow','maxOutputTokens'))
                          for name,value in models.items()} if isinstance(models,dict) else None,
            **{name:number(data.get(name)) for name in ('duration_ms','duration_api_ms','total_cost_usd','num_turns')}}


def model(repo,tree,role,session,prompt,claim,heartbeat,timeout,resume=False):
    from .sessions import worker_command
    command=worker_command(repo,tree,role,session,{'text':prompt,'claim':claim},PLAN_SCHEMA if role=='planner' else OUTPUT_SCHEMA)
    if resume:
        command['argv'][command['argv'].index('--session-id')]='--resume'
    # One exclusive directory per invocation, including retries of a bound session.
    usage=attachment.safe(directory(repo),'usage')
    usage.mkdir(mode=0o700,exist_ok=True)
    invocation=str(uuid.uuid4());path=usage/invocation
    path.mkdir(mode=0o700)
    started=time.time();clock=time.monotonic()
    code=None;out=err='';data={};state='execution_error'
    try:
        try:
            code,out,err=execute(command['argv'][:-2],command['cwd'],command['env'],heartbeat,timeout,input_text=command['argv'][-1])
        except BaseException as exc:
            code,out,err=getattr(exc,'worker_output',(None,'',''))
            raise
        state='worker_error' if code else 'invalid_output'
        try:
            parsed=json.loads(out)
            if isinstance(parsed,dict):data=parsed
        except ValueError:pass
        if code:raise ValueError('Claude worker failed (exit '+str(code)+'); worktree and lease preserved')
        try:
            if data.get('is_error') or data.get('subtype','success')!='success':raise ValueError('Claude returned an error result')
            result=data['structured_output']
        except (ValueError,KeyError,TypeError) as exc:
            raise ValueError('Claude did not return the required structured stage result') from exc
        state='success'
        return result
    finally:
        # Keep raw output private and separate from prompt-free accounting evidence.
        # An execution error can still leave a complete JSON usage report.
        if not data:
            try:
                parsed=json.loads(out)
                if isinstance(parsed,dict):data=parsed
            except ValueError:pass
        record={'schema':'fourthought-usage/v1','invocation_id':invocation,
                'session_id':session,'role':role,'resume':resume,'started_at':started,
                'elapsed_ms':round((time.monotonic()-clock)*1000),'exit_code':code,'status':state,
                **usage_fields(data)}
        for name,content in (('stdout.log',out.encode()),('stderr.log',err.encode()),
                             ('usage.json',attachment.encoded(record))):
            fd=os.open(path/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())


def output(value, role=None):
    if (not isinstance(value,dict) or set(value)!= ({'result','summary','planned_paths'} if role=='planner' else {'result','summary'}) or value['result'] not in ('pass','fail')
            or not isinstance(value['summary'],str) or not value['summary'].strip()):
        raise ValueError('Invalid structured worker result')
    if role=='planner' and (not isinstance(value['planned_paths'],list) or any(not isinstance(p,str) or not p for p in value['planned_paths'])):
        raise ValueError('Invalid planned-path manifest')
    return value


def changed(tree):
    paths=set()
    for args in [('diff','--name-only','--no-renames','-z','HEAD','--'),
                 ('ls-files','--others','--exclude-standard','-z')]:
        p=subprocess.run(['git','-C',str(tree),*args],capture_output=True,check=True)
        paths.update(x.decode() for x in p.stdout.split(b'\0') if x)
    return sorted(paths)


def clean(tree,head):
    if attachment.git(tree,'rev-parse','HEAD')!=head:
        raise ValueError('Read-only stage changed Git HEAD; preserve and inspect')
    if attachment.git(tree,'status','--porcelain','--untracked-files=all'):
        raise ValueError('Read-only stage changed worktree; preserve and inspect')


def stop_generation(repo):
    path=directory(repo)/'stop'
    return path.stat().st_mtime_ns if path.exists() else 0


def run(repo,issue,*,client=None,worker=None,acceptance_only=False,launch_generation=None):
    repo=attachment.root(repo)
    stop_stamp=stop_generation(repo) if launch_generation is None else launch_generation
    attachment.doctor(repo)
    if type(issue) is not int or issue<=0:raise ValueError('Positive issue number required')
    config=attachment.decode(attachment.safe(repo,'.fourthought/config.json'));cfg=settings(config)
    injected=client is not None
    if client is None:
        from .dispatch import Client
        client=Client(repo)
        if config['github'].get('runtime_sessions') is not True:
            raise ValueError('Enable github.runtime_sessions for independently bound Claude role sessions')
        remote=attachment.git(repo,'remote','get-url',cfg['push_remote'])
        expected=client.repository
        if remote not in ('https://github.com/'+expected,'https://github.com/'+expected+'.git',
                          'git@github.com:'+expected,'git@github.com:'+expected+'.git'):
            raise ValueError('runtime.push_remote must point to the configured GitHub repository')
    with run_lock(repo,issue,5 if launch_generation is not None else 0):
        records=client.snapshot()
        if stop_generation(repo)!=stop_stamp:raise ValueError('Stop requested during startup')
        record=coordination.find(records,issue)
        policy._record(record)
        if acceptance_only and record['state'] not in ('merge-ready','done'):
            raise ValueError('Product acceptance requires merge-ready engineering evidence')
        if record['contract']['engineering']['status']!='ready':raise ValueError('Engineering contract is not ready')
        if record['contract']['owner_decisions']['status'] not in ('resolved','not-required'):raise ValueError('Owner decisions unresolved')
        if record['claim']['lease']['state'] not in ('pending','released'):
            raise ValueError('Existing active/quarantined lease: inspect previous worker before recovery')
        meta=directory(repo);stop=meta/'stop'
        session='';role='';token=None;holder='';last_renew=0
        def save(error=None):
            value={'issue':issue,'state':record['state'],'role':role,'session':session,'updated':time.time()}
            if error:value['error']=str(error)
            attachment.atomic(meta/(str(issue)+'.json'),attachment.encoded(value))
        def request(op,**fields):
            nonlocal record,last_renew
            req={'op':op,'issue':issue,'role':role,'session':session,**fields}
            record=client.request(req);save()
            if op in ('acquire','renew'):last_renew=time.monotonic()
            return record
        def heartbeat():
            nonlocal token
            if stop.exists() and stop.stat().st_mtime_ns!=stop_stamp:raise ValueError('Stop requested; worker terminated, worktree preserved')
            if token:
                coordination.fence(record,holder,token)
                if time.monotonic()-last_renew>=cfg['lease_ttl']/3:
                    request('renew',token=token,ttl=cfg['lease_ttl'])
                    coordination.fence(record,holder,token)
        heartbeat.deadline=lambda: record['claim']['lease'].get('expires_at') if token else None
        heartbeat.cancelled=lambda: stop.exists() and stop.stat().st_mtime_ns!=stop_stamp
        def command(tree,*args):
            heartbeat()
            code,out,err=execute(['git','-C',str(tree),*args],tree,dict(os.environ,PYTHONDONTWRITEBYTECODE='1',GIT_TERMINAL_PROMPT='0'),heartbeat,cfg['worker_timeout'])
            if code:raise ValueError('Git supervisor command failed: '+err.strip())
            return out.strip()
        def acquire(new_role):
            nonlocal session,role,token,holder
            session=str(uuid.uuid4());role=new_role;holder=client.login+'/'+role+'/'+session
            request('acquire',ttl=cfg['lease_ttl']);token=record['claim']['lease']['token']
        def release(tree=None):
            nonlocal token
            heartbeat()
            if tree is not None:coordination.remove_worktree(repo,record,holder,token)
            request('release',token=token,stopped=True);token=None
        def stage_worker(tree):
            prompt=('Work only on this issue and stage. Return result pass/fail and a factual summary. '
                    'Do not commit, push, change framework files or call other agents. The supervisor owns tests, commits and canonical transitions. '
                    'For planning describe a concrete implementation that satisfies the engineering contract entirely within claim.scope.likely_paths. '
                    'For planning, return planned_paths listing every repository-relative file to create, edit or delete, including tests. '
                    'Collision domains do not authorize additional paths. If scope prevents satisfying the contract, return fail and explain the required claim revision. '
                    'For implementation/remediation edit only claimed paths. Bash is unavailable; use file tools. The supervisor runs all verification commands. '
                    'For verification inspect acceptance against actual changes. For review independently inspect the diff, correctness and tests.\n'
                    +json.dumps(record,sort_keys=True))
            if role in ('verifier','reviewer','assurance','product-manager'):
                plan=next((r for r in record['receipts'] if r['stage']=='plan'),None)
                if not plan:raise ValueError('Missing planning base for independent diff review')
                diff=command(tree,'diff','--no-ext-diff','--no-textconv',plan['head'],record['head'],'--')
                if len(diff)>200000:raise ValueError('Review diff exceeds bounded context; split the issue')
                prompt+='\nSupervisor Git diff '+plan['head']+'..'+record['head']+':\n'+diff
            for attempt in range(2 if role=='planner' else 1):
                result=worker(role,tree,session,prompt,heartbeat) if worker else model(repo,tree,role,session,prompt,record['claim'],heartbeat,cfg['worker_timeout'],resume=attempt>0)
                heartbeat();result=output(result,role)
                if role!='planner':return result
                clean(tree,record['head'])
                try:
                    hooks.check_scope(result['planned_paths'],record['claim'])
                except ValueError as exc:
                    diagnostic='Planning scope rejected: '+str(exc)
                    attachment.atomic(meta/(session+'-plan-scope.json'),attachment.encoded({'result':result,'error':diagnostic}))
                    if attempt==0:
                        prompt+='\n'+diagnostic+'\nRefine the plan within the existing approved claim and contract. Do not drop acceptance criteria. If impossible, return fail with the necessary claim revision; do not expand scope. Previous proposal: '+json.dumps(result)
                        continue
                    return {'result':'fail','summary':diagnostic}
                return {'result':result['result'],'summary':result['summary']+'\nPlanned paths: '+json.dumps(result['planned_paths'])}
        try:
            for step in range(40):
                state=record['state'];save()
                print('Fourthought issue '+str(issue)+': '+state,file=sys.stderr,flush=True)
                if state in ('done','blocked','owner-decision') or (state=='merge-ready' and not acceptance_only):return record
                if state in ('intake','product-shaping','product-ready'):
                    role='product-manager';session=str(uuid.uuid4())
                    request('transition',target={'intake':'product-shaping','product-shaping':'product-ready','product-ready':'triage'}[state]);continue
                if state=='triage':
                    acquire('triage');request('transition',token=token,target='ready:plan');release();continue
                stages=dict(STAGES)
                if acceptance_only:stages['merge-ready']=('product-manager','merge-ready','product','done')
                if state not in stages:raise ValueError('Interrupted running stage requires explicit reconciliation: '+state)
                selected,running,receipt_stage,target=stages[state]
                required=(config['assurance']['mode']=='required' or record.get('assurance_required',False)
                          or record['contract'].get('assurance',{}).get('required',False)
                          or record['claim']['risk'] in ('high','critical') or record['claim']['class'] in ('architecture','security'))
                if required:integrations.assurance(repo,required=True)
                acquire(selected)
                if state!=running:request('transition',token=token,target=running)
                # Fetch only missing objects; never reset a preserved issue branch.
                try:attachment.git(repo,'cat-file','-e',record['head']+'^{commit}')
                except ValueError:command(repo,'fetch',cfg['push_remote'],record['head'])
                binding=coordination.create_worktree(repo,record,holder,token);tree=Path(binding['path'])
                hooks.run('pre-agent',repo,record,role=role,holder=holder,token=token)
                result=stage_worker(tree);base=record['head'];head=base;paths=[]
                evidence=['Claude '+role+' session '+session+': '+result['summary'][:12000]]
                if selected=='implementer':
                    if attachment.git(tree,'rev-parse','HEAD')!=base:raise ValueError('Worker changed Git HEAD outside supervisor')
                    paths=changed(tree);hooks.check_scope(paths,record['claim'])
                    if result['result']!='pass':raise ValueError('Implementation worker reported failure; changes preserved')
                    if paths:
                        command(tree,'add','--',*paths)
                        hooks.run('pre-commit',repo,record,role=role,holder=holder,token=token)
                        command(tree,'commit','-m','Fourthought issue '+str(issue)+': '+receipt_stage)
                    head=attachment.git(tree,'rev-parse','HEAD')
                    actual=hooks.diff_paths(tree,base,head)
                    if sorted(actual)!=paths:raise ValueError('Committed paths differ from worker manifest')
                    evidence.append('Git changes '+base+'..'+head)
                else:clean(tree,base)
                if selected=='verifier':
                    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',
                             UV_PROJECT_ENVIRONMENT=str(meta/'environments'/str(issue)),
                             UV_CACHE_DIR=str(meta/'cache/uv'),RUFF_CACHE_DIR=str(meta/'cache/ruff'),
                             CARGO_TARGET_DIR=str(meta/'cache/cargo'),
                             PYTEST_ADDOPTS=os.environ.get('PYTEST_ADDOPTS','')+' '+shlex.join(['-o','cache_dir='+str(meta/'cache/pytest'/str(issue))]))
                    for verify_argv in cfg['verify_commands']:
                        code,out,err=execute(verify_argv,tree,env,heartbeat,cfg['worker_timeout'])
                        log=(out+'\n'+err).encode();digest=hashlib.sha256(log).hexdigest()
                        attachment.atomic(meta/(str(issue)+'-'+session+'-'+digest+'.log'),log)
                        evidence.append('argv='+json.dumps(verify_argv)+' exit='+str(code)+' sha256='+digest+' output='+(out+'\n'+err)[-6000:])
                        if code:result['result']='fail'
                        clean(tree,base)
                    clean(tree,base)
                receipt={'issue':issue,'stage':receipt_stage,'result':result['result'],'head':head,
                         'actor':holder,'evidence':evidence,'changed_paths':paths}
                policy.validate_receipt(receipt);heartbeat()
                if selected in ('implementer','planner'):
                    command(tree,'push',cfg['push_remote'],'HEAD:refs/heads/ft/'+str(issue)+'/work')
                if selected in ('planner','product-manager') and result['result']=='fail':
                    request('transition',token=token,target='blocked');release(tree);return record
                if selected in ('verifier','reviewer') and result['result']=='fail':target='remediate'
                if selected=='reviewer' and result['result']=='pass' and required:
                    # The existing Assurance boundary is mandatory; independent attestation uses its own session and lease.
                    release(tree);acquire('assurance')
                    b=coordination.create_worktree(repo,record,holder,token);assurance_tree=Path(b['path'])
                    assurance_result=stage_worker(assurance_tree);clean(assurance_tree,record['head'])
                    if assurance_result['result']!='pass':raise ValueError('Independent Assurance did not pass; progression blocked')
                    attest={'issue':issue,'stage':'assurance','result':'pass','head':record['head'],'actor':holder,
                            'evidence':['Claude assurance session '+session+': '+assurance_result['summary']],'changed_paths':[]}
                    request('attest',token=token,receipt=attest);release(assurance_tree)
                    acquire('reviewer');b=coordination.create_worktree(repo,record,holder,token);tree=Path(b['path'])
                    # A new reviewer session produces its own receipt rather than borrowing the former actor.
                    reviewed=stage_worker(tree);clean(tree,record['head'])
                    receipt.update(actor=holder,result=reviewed['result'],evidence=['Claude reviewer session '+session+': '+reviewed['summary']])
                    if reviewed['result']=='fail':target='remediate'
                request('transition',token=token,target=target,head=head,receipt=receipt)
                release(tree)
            raise ValueError('Supervisor transition bound exhausted')
        except BaseException as exc:
            save(exc)
            raise


def accept(repo,issue,*,client=None,worker=None):
    return run(repo,issue,client=client,worker=worker,acceptance_only=True)


def launch(repo,issue):
    """Start an explicitly requested detached supervisor; logs remain inspectable."""
    repo=attachment.root(repo);generation=stop_generation(repo);attachment.doctor(repo)
    if type(issue) is not int or issue<=0:raise ValueError('Positive issue number required')
    config=attachment.decode(attachment.safe(repo,'.fourthought/config.json'));settings(config)
    if not config['github']['enabled'] or config['github'].get('runtime_sessions') is not True:
        raise ValueError('GitHub coordination and runtime_sessions must be enabled before launching engineering')
    from .dispatch import Client
    coordination.find(Client(repo).snapshot(),issue)
    cli=Path(__file__).resolve().parents[1]/'fourthought'
    if not cli.is_file():cli=repo/'scripts/fourthought'
    with run_lock(repo,issue):
        log=directory(repo)/(str(issue)+'-supervisor-'+str(uuid.uuid4())+'.log')
        fd=os.open(log,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as stream:
            process=subprocess.Popen([sys.executable,'-B',str(cli),'run',str(repo),'--issue',str(issue),'--stop-generation',str(generation)],
                                     cwd=repo,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),
                                     stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                                     start_new_session=True)
    return {'launched':True,'issue':issue,'pid':process.pid,'log':str(log),
            'note':'Launch is not completion; inspect sessions, queue and this log. Stop requests preserve unfinished work.'}
