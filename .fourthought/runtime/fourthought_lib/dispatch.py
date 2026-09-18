"""Authenticated workflow dispatch with signed, revision-fenced result correlation."""
import copy
import json
import re
import subprocess
import time
import uuid
from urllib.parse import quote, urlencode
from . import attachment, github, policy

actor = github.runtime_actor


def _credential():
    try:
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('GitHub CLI authentication unavailable') from None
    if result.returncode or not result.stdout.strip():
        raise ValueError('GitHub CLI authentication required')
    return result.stdout.strip()


class Client:
    """Only workflow dispatch mutates canonical state; snapshots verify its chain.

    Injected api/login are offline testing seams. Runtime contexts are trusted
    caller assertions from the local supervisor, not independent human accounts.
    """
    def __init__(self, repo, *, api=None, login=None, poll_interval=2, timeout=120,
                 sleep=time.sleep, clock=time.monotonic):
        self.repo = attachment.root(repo)
        config = attachment.validate_config(attachment.decode(attachment.safe(self.repo, '.fourthought/config.json')))
        self.config = config['github']
        if not self.config['enabled']:
            raise ValueError('GitHub canonical coordination is disabled')
        self.repository = self.config['repository']
        self.prefix = github._repository(self.repository)
        self.public_key = self.config.get('state_public_key')
        if not self.public_key:
            raise ValueError('Canonical public key is required')
        if api is None:
            if login is not None:
                raise ValueError('Production login cannot be overridden')
            api = github.GitHubAPI(token=_credential())
            identity = api('GET', '/user')
            login = identity.get('login') if isinstance(identity, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', login):
            raise ValueError('Authenticated GitHub login is required')
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError('Polling limits must be positive')
        self.api, self.login = api, login
        self.timeout, self.poll_interval, self.sleep, self.clock = timeout, poll_interval, sleep, clock

    def snapshot(self):
        return github.read_canonical_snapshot(self.api, self.repository, self.public_key)

    def _identity(self, request):
        role = request.get('role'); github.authorize(self.config, self.login, role)
        if self.config.get('runtime_sessions', False):
            return actor(self.login, role, request.get('session'))
        if 'session' in request:
            raise ValueError('Runtime sessions are not enabled')
        return self.login

    def request(self, request):
        if not isinstance(request, dict):
            raise ValueError('Coordination request must be an object')
        request = copy.deepcopy(request)
        if set(request) & {'request_id', 'expected_revision'}:
            raise ValueError('Client owns request correlation fields')
        identity = self._identity(request)
        if type(request.get('issue')) is not int or request['issue'] <= 0:
            raise ValueError('Request requires a positive issue')
        old = [r for r in self.snapshot() if r['issue'] == request['issue']]
        if request.get('op') == 'init':
            if old: raise ValueError('Issue already initialized')
            revision = 0
        else:
            if len(old) != 1: raise ValueError('Issue has no canonical state')
            revision = old[0]['_revision']
        request.update(request_id=str(uuid.uuid4()), expected_revision=revision)
        self._dispatch(request)
        correlation = {'id':request['request_id'],'op':request['op'],'actor':identity,
                       'role':request['role'],'previous_revision':revision,
                       'digest':github.request_digest(request)}
        def matches(record):
            return record.get('_revision') == revision + 1 and record.get('_request') == correlation
        try:
            current = [r for r in self.snapshot() if r['issue'] == request['issue']]
        except ValueError:
            checkpoint = github._read_checkpoint(self.api, self.repository, self.public_key)
            sealed = checkpoint['records'].get(request['issue'])
            if sealed is None:
                raise ValueError('No committed checkpoint result; inspect before retry') from None
            committed = github.verify_record(sealed, self.public_key, self.repository)
            if not matches(committed):
                raise ValueError('Checkpoint result does not match workflow request; inspect before retry') from None
            current = [self._repair(request['issue'], checkpoint)]
        if len(current) != 1 or not matches(current[0]):
            raise ValueError('Canonical result does not match workflow request; inspect before retry')
        return current[0]

    def repair(self, issue):
        github.authorize(self.config, self.login, 'lead')
        if type(issue) is not int or issue <= 0:
            raise ValueError('Repair requires a positive issue')
        checkpoint = github._read_checkpoint(self.api, self.repository, self.public_key)
        return self._repair(issue, checkpoint)

    def _repair(self, issue, checkpoint):
        github.authorize(self.config, self.login, 'lead')
        sealed = checkpoint['records'].get(issue)
        if sealed is None:
            raise ValueError('Issue has no checkpoint state')
        original = github.verify_record(sealed, self.public_key, self.repository)
        request = {'op':'repair','issue':issue,'role':'lead','session':str(uuid.uuid4()),
                   'checkpoint':checkpoint['head'],'request_id':str(uuid.uuid4()),
                   'expected_revision':original['_revision']}
        self._dispatch(request)
        current = [r for r in self.snapshot() if r['issue'] == issue]
        after = github._read_checkpoint(self.api, self.repository, self.public_key)
        if after['head'] != checkpoint['head'] or current != [original]:
            raise ValueError('Repair result changed checkpoint or canonical record; inspect before retry')
        return current[0]

    def _dispatch(self, request):
        metadata = self.api('GET', self.prefix)
        branch = metadata.get('default_branch') if isinstance(metadata, dict) else None
        if not isinstance(branch, str) or not branch:
            raise ValueError('Missing repository default branch')
        ref = self.api('GET', self.prefix + '/git/ref/heads/' + quote(branch, safe='/'))
        sha = ref.get('object', {}).get('sha') if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('Invalid default branch SHA')
        workflow_path = self.prefix + '/actions/workflows/fourthought-router.yml'
        workflow = self.api('GET', workflow_path)
        if (not isinstance(workflow, dict) or type(workflow.get('id')) is not int
                or workflow.get('path') != '.github/workflows/fourthought-router.yml'
                or workflow.get('state') != 'active'):
            raise ValueError('Canonical coordinator workflow unavailable')
        request_id = request['request_id']
        self.api('POST', workflow_path + '/dispatches', {'ref':branch, 'inputs':{
            'request_id':request_id, 'request':json.dumps(request, separators=(',', ':'), allow_nan=False)}})
        deadline = self.clock() + self.timeout
        run_id = None
        while True:
            page, matches = 1, []
            while True:
                listing = self.api('GET', workflow_path + '/runs?' + urlencode({
                    'event':'workflow_dispatch','actor':self.login,'branch':branch,'per_page':100,'page':page}))
                if not isinstance(listing, dict) or not isinstance(listing.get('workflow_runs'), list):
                    raise ValueError('Incomplete workflow run listing')
                batch = listing['workflow_runs']
                matches.extend(r for r in batch if isinstance(r, dict) and r.get('display_title') == 'fourthought:' + request_id)
                if len(batch) < 100: break
                page += 1
                if page > 10 or self.clock() >= deadline:
                    raise ValueError('Workflow correlation listing limit reached; inspect before retry')
            if len(matches) > 1:
                raise ValueError('Ambiguous workflow request; inspect before retry')
            if matches:
                run = matches[0]
                if (type(run.get('id')) is not int or (run_id is not None and run['id'] != run_id)
                        or run.get('workflow_id') != workflow['id'] or run.get('event') != 'workflow_dispatch'
                        or run.get('actor', {}).get('login') != self.login
                        or run.get('triggering_actor', {}).get('login') != self.login
                        or run.get('head_sha') != sha or run.get('head_branch') != branch or run.get('run_attempt') != 1):
                    raise ValueError('Workflow identity mismatch; inspect before retry')
                run_id = run['id']
                if run.get('status') == 'completed':
                    return run.get('conclusion')
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ValueError('Coordinator workflow timed out; inspect before retry')
            self.sleep(min(self.poll_interval, remaining))

    def create(self, record, title, body, *, session=None):
        record = copy.deepcopy(record); policy._record(record)
        request = {'op':'init','role':'product-manager'}
        if session is not None: request['session'] = session
        self._identity(request)
        if (record['state'] != 'intake' or record['receipts'] or record['claim']['lease'] != {'state':'pending'}
                or record['attempts'] != {'verification':0,'review':0}
                or any(k in record for k in ('_seal','_revision','_request'))):
            raise ValueError('New issue requires a clean intake record')
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or github.parse(body) is not None:
            raise ValueError('New issue requires title and ordinary human body')
        self.snapshot()  # Verify provisioning before creating even the ordinary issue.
        issue = self.api('POST', self.prefix + '/issues', {'title':title, 'body':body})
        number = issue.get('number') if isinstance(issue, dict) else None
        if (type(number) is not int or number <= 0 or 'pull_request' in issue
                or issue.get('title') != title or issue.get('body') != body):
            raise ValueError('Uncertain issue creation; inspect before retry')
        record['issue'] = record['contract']['issue'] = record['claim']['issue'] = number
        request.update(issue=number,record=record)
        try:
            return self.request(request)
        except ValueError as exc:
            raise ValueError('Issue #' + str(number) + ' created; canonical initialization incomplete: ' + str(exc)) from None
