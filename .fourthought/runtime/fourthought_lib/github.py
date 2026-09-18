"""GitHub coordination anchored by protected Git history and signed issue state.

The API seam is ``api(method, repository_relative_path, data=None) -> JSON``.
``read_canonical_snapshot`` verifies the protected checkpoint, histories and mirrors.
GitHub has no issue-body CAS: all writers must use the single coordinator
workflow's repository-wide concurrency group. Human concurrent edits are
checked immediately before PATCH but cannot be atomically excluded.
"""
import json
import math
from pathlib import Path
import re

START = '<!-- fourthought:v1 -->'
END = '<!-- /fourthought -->'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite JSON number: ' + value)


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _block(body):
    if not isinstance(body, str):
        raise ValueError('Issue body must be text')
    if body.count('<!-- fourthought:') != body.count(START):
        raise ValueError('Unsupported Fourthought state marker')
    if START not in body and END not in body:
        if '<!-- fourthought:' in body:
            raise ValueError('Unsupported Fourthought state marker')
        return None
    if body.count(START) != 1 or body.count(END) != 1:
        raise ValueError('Malformed or duplicate Fourthought blocks')
    start, end = body.index(START), body.index(END)
    if end < start:
        raise ValueError('Malformed Fourthought block')
    return start, end + len(END)


def parse(body):
    """Decode one strict JSON object; do not apply record policy here."""
    bounds = _block(body)
    if bounds is None:
        return None
    content = body[bounds[0] + len(START):bounds[1] - len(END)]
    match = re.fullmatch(r'\s*```json\s*\n(.*?)\n```\s*', content, re.S)
    if not match:
        raise ValueError('Fourthought block requires fenced JSON')
    result = json.loads(match.group(1), object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    if not isinstance(result, dict):
        raise ValueError('Fourthought record must be an object')
    return result


def render(body, record):
    """Replace only the canonical block, preserving human prefix and suffix."""
    parse(body)
    if not isinstance(record, dict):
        raise ValueError('Fourthought record must be an object')
    block = START + '\n```json\n' + json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + '\n```\n' + END
    bounds = _block(body)
    if bounds is None:
        return body + ('\n' if body and not body.endswith('\n') else '') + block
    return body[:bounds[0]] + block + body[bounds[1]:]


def _repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    return '/repos/' + repository


def read_snapshot(api, repository, *, include_unattached=False):
    """Fully enumerate all issues, including closed, excluding pull requests.

    Returned raw issue objects retain bodies and updated_at for stale-write
    checks. include_unattached=True also returns ordinary issues, needed to
    discover comment history after body deletion. Any malformed block aborts.
    """
    prefix = _repository(repository)
    rows, seen, page = [], set(), 1
    while True:
        batch = api('GET', prefix + '/issues?state=all&per_page=100&page=' + str(page))
        if not isinstance(batch, list):
            raise ValueError('Incomplete GitHub issue snapshot')
        for issue in batch:
            if not isinstance(issue, dict):
                raise ValueError('Malformed GitHub issue')
            number = issue.get('number')
            if type(number) is not int or number <= 0 or number in seen:
                raise ValueError('Invalid or duplicate GitHub issue identity')
            seen.add(number)
            if 'pull_request' in issue:
                continue
            if include_unattached:
                rows.append(issue)
                continue
            record = parse(issue.get('body') or '')
            if record is not None:
                if type(record.get('issue')) is not int or record['issue'] != number:
                    raise ValueError('Canonical issue identity mismatch')
                rows.append(issue)
        if len(batch) < 100:
            return rows
        page += 1


def persist(api, repository, old_issue, new_record):
    """Check old body/version, PATCH once, then verify the returned body.

    A failed or mismatched response is uncertain: inspect GitHub before retry.
    """
    number = old_issue.get('number')
    if type(number) is not int or number <= 0 or new_record.get('issue') != number:
        raise ValueError('Canonical issue identity mismatch')
    path = _repository(repository) + '/issues/' + str(number)
    current = api('GET', path)
    if not isinstance(current, dict) or current.get('number') != number or 'pull_request' in current or any(current.get(k) != old_issue.get(k) for k in ('body', 'updated_at')):
        raise ValueError('Issue changed since snapshot; restart coordination')
    body = render(old_issue.get('body') or '', new_record)
    updated = api('PATCH', path, {'body': body})
    if not isinstance(updated, dict) or updated.get('number') != number or updated.get('body') != body:
        raise ValueError('Uncertain GitHub write: response mismatch; inspect before retry')
    return new_record


def authorize(github_config, actor, role):
    from .integrations import ROLES
    actors = github_config.get('actors', {})
    if not isinstance(actors, dict) or set(actors) - ROLES:
        raise ValueError('Invalid actor role allowlists')
    for names in actors.values():
        if (not isinstance(names, list) or any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', n) for n in names)
                or len(set(names)) != len(names)):
            raise ValueError('Invalid actor role allowlists')
    allowed = actors.get(role) if isinstance(actors, dict) and isinstance(role, str) else None
    if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', actor) or not isinstance(allowed, list) or actor not in allowed:
        raise ValueError('Authenticated actor is not authorized for role')


def request_digest(request):
    import hashlib
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def runtime_actor(login, role, session):
    """Bind an authorized account to a role context, not a separate human."""
    from .integrations import ROLES
    import uuid
    if (not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', login)
            or not isinstance(role, str) or role not in ROLES or not isinstance(session, str)):
        raise ValueError('Invalid runtime actor')
    try:
        if str(uuid.UUID(session)) != session:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('Runtime session must be a canonical UUID') from None
    return login + '/' + role + '/' + session


_STAGE_ROLE = {
    'intake':'product-manager', 'product-shaping':'product-manager',
    'product-ready':'product-manager', 'triage':'triage',
    'ready:plan':'planner', 'planning':'planner',
    'ready:implement':'implementer', 'implementing':'implementer',
    'ready:verify':'verifier', 'verifying':'verifier',
    'ready:review':'reviewer', 'reviewing':'reviewer', 'remediate':'implementer',
    'merge-ready':'product-manager', 'blocked':'lead', 'owner-decision':'product-manager',
}


def _commit(api, prefix, head):
    if not isinstance(head, str) or not re.fullmatch('[a-fA-F0-9]{40}', head):
        raise ValueError('Remote HEAD requires a complete Git commit')
    result = api('GET', prefix + '/commits/' + head.lower())
    if not isinstance(result, dict) or result.get('sha', '').lower() != head.lower():
        raise ValueError('GitHub commit identity mismatch')
    return head.lower()


def _branch(api, prefix, issue, head):
    result = api('GET', prefix + '/git/ref/heads/ft/' + str(issue) + '/work')
    if not isinstance(result, dict) or result.get('object', {}).get('sha') != head:
        raise ValueError('Authoritative issue branch HEAD changed')


def _changed(api, prefix, base, head):
    """Compare exact SHAs; GitHub exposes <=300 files only on page one."""
    page, count, total, files = 1, 0, None, None
    while True:
        result = api('GET', prefix + '/compare/' + base + '...' + head + '?per_page=100&page=' + str(page))
        if not isinstance(result, dict) or not isinstance(result.get('commits'), list) or type(result.get('total_commits')) is not int:
            raise ValueError('Incomplete GitHub comparison')
        if result.get('status') not in ('ahead','identical') or result.get('merge_base_commit', {}).get('sha') != base:
            raise ValueError('Implementation HEAD must descend from its recorded base')
        if total is None:
            total = result['total_commits']; files = result.get('files')
            if total < 0 or not isinstance(files, list) or len(files) >= 300:
                raise ValueError('Comparison file list incomplete or reaches GitHub 300-file limit')
        if result['total_commits'] != total:
            raise ValueError('GitHub comparison changed during pagination')
        count += len(result['commits'])
        if count == total:
            break
        if count > total or len(result['commits']) != 100:
            raise ValueError('Incomplete GitHub comparison pagination')
        page += 1
    paths = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('filename'), str):
            raise ValueError('Malformed GitHub changed file')
        paths.add(item['filename'])
        if item.get('status') == 'renamed':
            if not isinstance(item.get('previous_filename'), str):
                raise ValueError('Renamed file lacks previous path')
            paths.add(item['previous_filename'])
    return sorted(paths)


def coordinate(repo, request, *, api=None, actor=None, now=None, state_key=None):
    """Authenticate and persist one init/acquire/renew/release/transition request.

    Request fields: op, positive issue, role; record for init; token and optional
    ttl for lease operations; stopped=true for release; target and optional
    head/receipt/assurance for transition. Unknown fields fail closed. Injected
    api/actor/state_key are test seams; production identity comes from Actions.
    Production signing key is FOURTHOUGHT_STATE_KEY (Ed25519 private PEM); the
    repository config holds state_public_key. Protected Git ancestry anchors
    signed comment history; both history and body mirror must match that anchor.
    """
    import copy
    import os
    from . import attachment, coordination, integrations, policy
    from .hooks import check_scope

    repo = attachment.root(repo)
    config = attachment.validate_config(attachment.decode(attachment.safe(repo, '.fourthought/config.json')))
    github = config['github']
    if not github['enabled']:
        raise ValueError('GitHub coordination is disabled')
    if not isinstance(request, dict) or type(request.get('issue')) is not int or request['issue'] <= 0:
        raise ValueError('Request requires a positive issue')
    op = request.get('op')
    fields = {'repair':{'checkpoint'}, 'init':{'record'}, 'acquire':{'ttl'}, 'renew':{'token','ttl'},
              'release':{'token','stopped'}, 'attest':{'token','receipt'}, 'transition':{'token','target','receipt','head','assurance'}}
    if not isinstance(op, str) or op not in fields or set(request) - ({'op','issue','role','session','request_id','expected_revision'} | fields[op]):
        raise ValueError('Unknown coordination operation or request fields')
    request_id = request.get('request_id')
    if request_id is not None:
        runtime_actor('request', 'implementer', request_id)
        if type(request.get('expected_revision')) is not int or request['expected_revision'] < 0:
            raise ValueError('Correlated request requires expected revision')
    elif 'expected_revision' in request:
        raise ValueError('Revision fence requires request ID')
    prefix = _repository(github['repository'])
    if api is None:
        if actor is not None or now is not None or state_key is not None:
            raise ValueError('Production identity and clock cannot be overridden')
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_WORKFLOW') != 'Fourthought coordinator'
                or os.environ.get('GITHUB_REPOSITORY') != github['repository']):
            raise ValueError('Canonical writes require the serialized Fourthought coordinator workflow')
        if request_id is not None:
            event = attachment.decode(Path(os.environ.get('GITHUB_EVENT_PATH', '')))
            if event.get('inputs', {}).get('request_id') != request_id:
                raise ValueError('Workflow request correlation mismatch')
        api = GitHubAPI()
        metadata = api('GET', prefix)
        if not isinstance(metadata, dict) or not metadata.get('default_branch') or os.environ.get('GITHUB_REF') != 'refs/heads/' + metadata['default_branch']:
            raise ValueError('Coordinator must run from the repository default branch')
        actor = os.environ.get('GITHUB_ACTOR')
    state_key = state_key if state_key is not None else os.environ.get('FOURTHOUGHT_STATE_KEY')
    if not isinstance(state_key, str) or '-----BEGIN PRIVATE KEY-----' not in state_key:
        raise ValueError('Coordinator private FOURTHOUGHT_STATE_KEY is required')
    public_key = github.get('state_public_key')
    role = request.get('role'); authorize(github, actor, role)
    permission = api('GET', prefix + '/collaborators/' + actor + '/permission')
    if not isinstance(permission, dict) or permission.get('permission') not in ('admin','maintain','write'):
        raise ValueError('Authenticated actor lacks repository write permission')
    login = actor
    if type(github.get('runtime_sessions', False)) is not bool:
        raise ValueError('runtime_sessions must be boolean')
    if github.get('runtime_sessions', False):
        actor = runtime_actor(login, role, request.get('session'))
    elif 'session' in request and op == 'repair':
        runtime_actor(login, role, request['session'])
    elif 'session' in request:
        raise ValueError('Runtime sessions are not enabled')
    checkpoint = _read_checkpoint(api, github['repository'], public_key)
    if op == 'repair':
        sealed = checkpoint['records'].get(request['issue'])
        if (role != 'lead' or request_id is None or sealed is None
                or request.get('checkpoint') != checkpoint['head']
                or request['expected_revision'] != sealed['_revision']):
            raise ValueError('Repair requires Lead and the exact checkpoint head and revision')
        return _repair_publication(api, github['repository'], public_key, checkpoint, request['issue'])
    snapshot = _canonical_snapshot(api, github['repository'], public_key, checkpoint=checkpoint)
    issues = [item[0] for item in snapshot]
    records = [item[1] for item in snapshot]
    for r in records:
        policy._record(r)
    number = request['issue']
    previous_revision = next((r['_revision'] for r in records if r['issue'] == number), 0)
    if request_id is not None and request['expected_revision'] != previous_revision:
        raise ValueError('Canonical revision changed before request; inspect before retry')
    if op == 'init':
        if role != 'product-manager':
            raise ValueError('Only Product Manager can initialize an issue')
        if any(r['issue'] == number for r in records):
            raise ValueError('Issue already has canonical state')
        old = api('GET', prefix + '/issues/' + str(number))
        if not isinstance(old, dict) or old.get('number') != number or 'pull_request' in old or parse(old.get('body') or '') is not None:
            raise ValueError('Initialization requires an ordinary uninitialized issue')
        result = copy.deepcopy(request.get('record')); policy._record(result)
        if any(k in result for k in ('_seal', '_revision', '_request')):
            raise ValueError('Initialization cannot supply a state signature')
        if (result['issue'] != number or result['state'] != 'intake' or result['receipts']
                or result['attempts'] != {'verification':0,'review':0}
                or result['claim']['lease'] != {'state':'pending'}):
            raise ValueError('Initialization requires intake, empty evidence/counters, and pending claim')
        _commit(api, prefix, result['head'])
    else:
        found = [item for item in issues if item['number'] == number]
        if len(found) != 1:
            raise ValueError('Issue has no canonical state')
        old = found[0]; current = coordination.find(records, number)
        if op in ('acquire','transition') and _STAGE_ROLE.get(current['state']) != role and not (op == 'acquire' and role == 'assurance' and current['state'] == 'reviewing'):
            raise ValueError('Authenticated role does not own source stage')
        if op == 'acquire':
            result = coordination.find(coordination.acquire(records, number, actor, now, request.get('ttl',900)), number)
        elif op == 'renew':
            result = coordination.find(coordination.renew(records, number, actor, request.get('token'), now, request.get('ttl',900)), number)
        elif op == 'release':
            result = coordination.find(coordination.release(records, number, actor, request.get('token'), now, request.get('stopped',False)), number)
        elif op == 'attest':
            if role != 'assurance' or current['state'] != 'reviewing':
                raise ValueError('Only Assurance can attest during reviewing')
            coordination.fence(current, actor, request.get('token'), now)
            evidence = request.get('receipt'); policy.validate_receipt(evidence)
            if (evidence['actor'] != actor or evidence['stage'] != 'assurance'
                    or evidence['issue'] != number or evidence['head'] != current['head']):
                raise ValueError('Assurance receipt must bind authenticated actor and current issue HEAD')
            policy._independent(current, actor)
            policy._implementation(current, current['head'])
            integrations.assurance(repo, required=True)
            _commit(api, prefix, current['head']); _branch(api, prefix, number, current['head'])
            result = copy.deepcopy(current); result['receipts'].append(copy.deepcopy(evidence))
        else:
            target = request.get('target')
            if not isinstance(target, str) or target not in policy.STATES:
                raise ValueError('Unknown transition target')
            if current['state'] in policy._ENGINEERING or target in policy._ENGINEERING:
                coordination.fence(current, actor, request.get('token'), now)
            head = _commit(api, prefix, request.get('head', current['head']))
            evidence, assurance = request.get('receipt'), request.get('assurance')
            for value in (evidence, assurance):
                if value is not None:
                    policy.validate_receipt(value)
                    if value['actor'] != actor:
                        raise ValueError('Receipt actor must equal authenticated actor')
                    _commit(api, prefix, value['head'])
            if assurance is not None:
                authorize(github, login, 'assurance')
            if evidence is not None and evidence['stage'] in ('implement','remediate'):
                paths = _changed(api, prefix, current['head'], head)
                if sorted(evidence['changed_paths']) != paths:
                    raise ValueError('Receipt changed_paths must equal the complete remote comparison')
                check_scope(paths, current['claim'])
            branch_required = (current['state'] in {'implementing','remediate','ready:verify','verifying','ready:review','reviewing','merge-ready'}
                               or target in {'ready:verify','verifying','ready:review','reviewing','merge-ready','done'})
            if branch_required:
                _branch(api, prefix, number, head)
            elif evidence is not None and evidence['stage'] == 'plan':
                metadata = api('GET', prefix)
                branch = metadata.get('default_branch') if isinstance(metadata, dict) else None
                if not isinstance(branch, str) or not branch:
                    raise ValueError('Missing repository default branch')
                from urllib.parse import quote
                default = api('GET', prefix + '/git/ref/heads/' + quote(branch, safe='/'))
                if not isinstance(default, dict) or default.get('object', {}).get('sha') != head:
                    _branch(api, prefix, number, head)
            required = (config['assurance']['mode'] == 'required' or current.get('assurance_required',False)
                        or current['contract'].get('assurance',{}).get('required',False)
                        or current['claim']['risk'] in ('high','critical')
                        or current['claim']['class'] in ('architecture','security'))
            current = copy.deepcopy(current)
            if required:
                current['assurance_required'] = True
                if target in ('merge-ready','done'):
                    integrations.assurance(repo, required=True)
            result = policy.transition(current, target, evidence, head, assurance)
            if branch_required:
                _branch(api, prefix, number, head)
    result['_revision'] = (current['_revision'] + 1) if op != 'init' else 1
    result.pop('_request', None)
    if request_id is not None:
        result['_request'] = {'id':request_id, 'op':op, 'actor':actor, 'role':role,
                              'previous_revision':previous_revision, 'digest':request_digest(request)}
    sealed = _seal_record(result, state_key, github['repository'])
    verify_record(sealed, public_key, github['repository'])
    return _persist_revision(api, github['repository'], old, sealed, checkpoint)


class GitHubAPI:
    """Authenticated standard-library transport, fixed origin, no redirects/retry."""
    def __init__(self, token=None, protection_token=None):
        import os
        import urllib.request
        token = token or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            raise ValueError('GH_TOKEN or GITHUB_TOKEN is required')
        self._token = token
        self._protection_token = protection_token or os.environ.get('FOURTHOUGHT_PROTECTION_TOKEN') or token

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self._opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, method, path, data=None):
        import urllib.error
        import urllib.request
        if method not in ('GET','POST','PATCH') or not isinstance(path, str) or not (path.startswith('/repos/') or (method == 'GET' and path == '/user')) or any(c in path for c in ('\\','\r','\n','#')):
            raise ValueError('Unsupported GitHub API request')
        payload = None if data is None else json.dumps(data, allow_nan=False).encode('utf-8')
        token = (self._protection_token if method == 'GET' and re.fullmatch(r'/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/branches/fourthought-state/protection', path) else self._token)
        request = urllib.request.Request('https://api.github.com' + path, data=payload, method=method,
            headers={'Authorization':'Bearer ' + token, 'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2026-03-10', 'Content-Type':'application/json',
                     'User-Agent':'fourthought-phase1'})
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float) if raw else None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Never expose response bodies, Authorization, token, or URL errors.
            state = 'Uncertain GitHub write; inspect before retry' if method in ('POST','PATCH') else 'GitHub read failed'
            raise ValueError(state) from None


def _signature(record, key, signature=None):
    """Ed25519 via OpenSSL, with private files in a private temporary directory."""
    import base64
    import subprocess
    import tempfile
    from pathlib import Path
    if not isinstance(key, str) or not key.strip():
        raise ValueError('State signature key is required')
    payload = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    with tempfile.TemporaryDirectory(prefix='fourthought-signature-') as directory:
        directory = Path(directory)
        def write(name, content):
            path = directory / name
            with path.open('xb') as stream:
                path.chmod(0o600); stream.write(content)
            return str(path)
        key_path = write('key.pem', key.encode('utf-8'))
        data_path = write('record.json', payload)
        args = ['openssl', 'pkeyutl', '-rawin', '-inkey', key_path, '-in', data_path]
        if signature is None:
            args += ['-sign']
        else:
            try:
                raw = base64.b64decode(signature, validate=True)
                if len(raw) != 64:
                    raise ValueError('Signature must be Ed25519')
            except (TypeError, ValueError) as exc:
                raise ValueError('Invalid canonical state signature') from None
            args += ['-verify', '-pubin', '-sigfile', write('signature', raw)]
        try:
            result = subprocess.run(args, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('OpenSSL state signature operation unavailable') from None
        if result.returncode:
            raise ValueError('Canonical state signature verification or signing failed')
        return base64.b64encode(result.stdout).decode('ascii') if signature is None else None


def _seal_record(record, state_key, repository):
    import copy
    result = copy.deepcopy(record)
    result.pop('_seal', None)
    _repository(repository)
    result['_seal'] = _signature({'repository':repository, 'record':result}, state_key)
    return result


def verify_record(record, public_key, repository):
    """Verify trusted coordinator authorship using only a public Ed25519 key.

    Returns an unsealed copy including _revision. For current state, use
    read_canonical_snapshot, which also checks the append-only comment log.
    """
    import copy
    if not isinstance(record, dict) or not isinstance(record.get('_seal'), str):
        raise ValueError('Canonical issue state lacks a coordinator signature')
    result = copy.deepcopy(record); signature = result.pop('_seal')
    _repository(repository)
    if type(result.get('_revision')) is not int or result['_revision'] <= 0:
        raise ValueError('Canonical state requires a positive signed revision')
    _signature({'repository':repository, 'record':result}, public_key, signature)
    return result


def _canonical_snapshot(api, repository, public_key, *, checkpoint=None):
    """Discover authoritative signed comment history even if a body was erased."""
    prefix = _repository(repository)
    checkpoint = _read_checkpoint(api, repository, public_key) if checkpoint is None else checkpoint
    result = []
    for issue in read_snapshot(api, repository, include_unattached=True):
        revisions, page, seen = {}, 1, set()
        while True:
            comments = api('GET', prefix + '/issues/' + str(issue['number']) + '/comments?per_page=100&page=' + str(page))
            if not isinstance(comments, list):
                raise ValueError('Incomplete canonical comment history')
            for comment in comments:
                if not isinstance(comment, dict) or type(comment.get('id')) is not int or comment['id'] in seen:
                    raise ValueError('Invalid or duplicate comment identity')
                seen.add(comment['id'])
                if not isinstance(comment.get('user'), dict) or comment['user'].get('login') != 'github-actions[bot]':
                    continue
                body = parse(comment.get('body') or '')
                if body is None:
                    continue
                record = verify_record(body, public_key, repository)
                if record.get('issue') != issue['number']:
                    raise ValueError('Canonical comment issue identity mismatch')
                revision = record['_revision']
                if revision in revisions and revisions[revision] != body:
                    raise ValueError('Conflicting signed state revisions')
                revisions[revision] = body
            if len(comments) < 100:
                break
            page += 1
        if not revisions:
            continue
        mirror = parse(issue.get('body') or '')
        latest = max(revisions)
        if min(revisions) != 1 or len(revisions) != latest:
            raise ValueError('Canonical comment history has missing revisions')
        if mirror != revisions[latest]:
            raise ValueError('Canonical issue mirror differs from signed history; trusted reconciliation required')
        if checkpoint['records'].get(issue['number']) != revisions[latest]:
            raise ValueError('Issue history differs from durable checkpoint; trusted reconciliation required')
        record = verify_record(revisions[latest], public_key, repository)
        from .policy import _record
        _record(record)
        result.append((issue, record))
    if {record['issue'] for issue, record in result} != set(checkpoint['records']):
        raise ValueError('Durable checkpoint issue history is missing; trusted reconciliation required')
    if _checkpoint_head(api, repository) != checkpoint['head']:
        raise ValueError('Checkpoint advanced during issue snapshot; restart coordination')
    _checkpoint_protection(api, repository)
    return result


def read_canonical_snapshot(api, repository, public_key):
    """Return records after protected checkpoint and full issues/comments checks."""
    return [record for issue, record in _canonical_snapshot(api, repository, public_key)]


def _persist_revision(api, repository, old, sealed, checkpoint):
    """Advance protected checkpoint, then history and mirror; never retry writes."""
    path = _repository(repository) + '/issues/' + str(old['number'])
    current = api('GET', path)
    if not isinstance(current, dict) or any(current.get(k) != old.get(k) for k in ('number','body','updated_at')):
        raise ValueError('Issue changed before canonical history append')
    _persist_checkpoint(api, repository, checkpoint, sealed)
    body = render('', sealed)
    comment = api('POST', path + '/comments', {'body':body})
    if (not isinstance(comment, dict) or type(comment.get('id')) is not int or comment.get('body') != body
            or not isinstance(comment.get('user'), dict) or comment['user'].get('login') != 'github-actions[bot]'):
        raise ValueError('Uncertain canonical history write; inspect before retry')
    # Adding a comment can change updated_at; only the body may not change here.
    after = api('GET', path)
    if not isinstance(after, dict) or after.get('number') != old['number'] or after.get('body') != old.get('body'):
        raise ValueError('Canonical history appended but issue mirror changed; reconcile before retry')
    return persist(api, repository, after, sealed)


_STATE_BRANCH = 'fourthought-state'
_EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
_GENESIS = 'fourthought-state/v1 genesis'


def _checkpoint_protection(api, repository):
    protection = api('GET', _repository(repository) + '/branches/' + _STATE_BRANCH + '/protection')
    for field, required in [('enforce_admins',True),('allow_force_pushes',False),('allow_deletions',False)]:
        setting = protection.get(field) if isinstance(protection, dict) else None
        if not isinstance(setting, dict) or setting.get('enabled') is not required:
            raise ValueError('Checkpoint branch requires enforced protection against force pushes and deletion')


def _checkpoint_head(api, repository):
    ref = api('GET', _repository(repository) + '/git/ref/heads/' + _STATE_BRANCH)
    obj = ref.get('object') if isinstance(ref, dict) else None
    if not isinstance(obj, dict) or obj.get('type') != 'commit' or not isinstance(obj.get('sha'), str) or not re.fullmatch('[a-f0-9]{40}',obj['sha']):
        raise ValueError('Protected checkpoint branch is missing or invalid; explicit provisioning required')
    return obj['sha']


def _read_checkpoint(api, repository, public_key):
    """Verify the complete immutable ancestry, never just a replaceable tip file.

    Ordinary writers may append but cannot remove ancestors under enforced
    non-force/deletion protection. Invalid or replayed appends block safely.
    """
    _checkpoint_protection(api, repository)
    head = cursor = _checkpoint_head(api, repository)
    commits, seen = [], set()
    while True:
        if cursor in seen:
            raise ValueError('Checkpoint ancestry contains a cycle')
        seen.add(cursor)
        commit = api('GET', _repository(repository) + '/git/commits/' + cursor)
        if (not isinstance(commit, dict) or commit.get('sha') != cursor
                or not isinstance(commit.get('message'), str)
                or not isinstance(commit.get('parents'), list)
                or not isinstance(commit.get('tree'), dict) or commit['tree'].get('sha') != _EMPTY_TREE):
            raise ValueError('Invalid checkpoint commit')
        parents = commit['parents']
        if not parents:
            if commit['message'].strip() != _GENESIS:
                raise ValueError('Checkpoint history lacks the provisioned genesis commit')
            break
        if len(parents) != 1 or not isinstance(parents[0], dict) or not isinstance(parents[0].get('sha'), str) or not re.fullmatch('[a-f0-9]{40}',parents[0]['sha']):
            raise ValueError('Checkpoint history must be a complete single-parent chain')
        commits.append(commit)
        cursor = parents[0]['sha']
    records, history = {}, {}
    from .policy import _record
    for commit in reversed(commits):
        sealed = parse(commit['message'])
        record = verify_record(sealed, public_key, repository)
        _record(record)
        previous = records.get(record['issue'])
        expected = previous['_revision'] + 1 if previous else 1
        if record['_revision'] != expected:
            raise ValueError('Checkpoint revision rollback, duplicate or gap')
        records[record['issue']] = sealed
        history.setdefault(record['issue'], []).append(sealed)
    if _checkpoint_head(api, repository) != head:
        raise ValueError('Checkpoint advanced during snapshot; restart coordination')
    return {'head':head, 'records':records, 'history':history}


def _persist_checkpoint(api, repository, checkpoint, sealed):
    """Append one immutable commit and atomically advance a non-force Git ref."""
    _checkpoint_protection(api, repository)
    previous = checkpoint['records'].get(sealed['issue'])
    if sealed['_revision'] != (previous['_revision'] + 1 if previous else 1):
        raise ValueError('Checkpoint revision is not the next revision')
    if _checkpoint_head(api, repository) != checkpoint['head']:
        raise ValueError('Checkpoint changed before write; restart coordination')
    message = render('',sealed)
    commit = api('POST', _repository(repository) + '/git/commits',
                 {'message':message,'tree':_EMPTY_TREE,'parents':[checkpoint['head']]})
    if (not isinstance(commit, dict) or not isinstance(commit.get('sha'),str)
            or not re.fullmatch('[a-f0-9]{40}',commit['sha'])
            or commit.get('message') != message or not isinstance(commit.get('parents'),list)
            or any(not isinstance(parent,dict) for parent in commit['parents'])
            or [parent.get('sha') for parent in commit['parents']] != [checkpoint['head']]
            or not isinstance(commit.get('tree'),dict) or commit['tree'].get('sha') != _EMPTY_TREE):
        raise ValueError('Uncertain checkpoint commit write; inspect before retry')
    # Sibling commits race safely: GitHub rejects the losing non-fast-forward
    # update, even if the issue-body/comment APIs offer no compare-and-swap.
    updated = api('PATCH', _repository(repository) + '/git/refs/heads/' + _STATE_BRANCH,
                  {'sha':commit['sha'],'force':False})
    if (not isinstance(updated, dict) or not isinstance(updated.get('object'),dict)
            or updated['object'].get('sha') != commit['sha']):
        raise ValueError('Uncertain checkpoint ref update; inspect before retry')
    # Retry only visibility reads, never the mutation. A different successor
    # means concurrent work, not replica lag, and must fail immediately.
    import time
    for attempt in range(5):
        visible = _checkpoint_head(api, repository)
        if visible == commit['sha']:
            return
        if visible != checkpoint['head'] or attempt == 4:
            raise ValueError('Uncertain checkpoint ref update; inspect before retry')
        time.sleep(0.05 * 2 ** attempt)


def _repair_publication(api, repository, public_key, checkpoint, number):
    """Publish only an existing signed tail after validating every issue.

    No transition, signature, lease change or checkpoint append occurs here.
    Historical deletion and altered mirrors require investigation, not repair.
    """
    prefix = _repository(repository)
    found, target = set(), None
    for issue in read_snapshot(api, repository, include_unattached=True):
        issue_number = issue['number']
        mirror = parse(issue.get('body') or '')
        history = checkpoint['history'].get(issue_number, [])
        revisions, seen, page = {}, set(), 1
        while True:
            comments = api('GET', prefix + '/issues/' + str(issue_number) + '/comments?per_page=100&page=' + str(page))
            if not isinstance(comments, list):
                raise ValueError('Incomplete canonical comment history')
            for comment in comments:
                if not isinstance(comment, dict) or type(comment.get('id')) is not int or comment['id'] in seen:
                    raise ValueError('Invalid or duplicate comment identity')
                seen.add(comment['id'])
                if not isinstance(comment.get('user'), dict) or comment['user'].get('login') != 'github-actions[bot]':
                    continue
                sealed = parse(comment.get('body') or '')
                if sealed is None:
                    continue
                record = verify_record(sealed, public_key, repository)
                revision = record['_revision']
                if (record.get('issue') != issue_number or revision > len(history)
                        or sealed != history[revision - 1]):
                    raise ValueError('Comment differs from immutable checkpoint history')
                revisions[revision] = sealed
            if len(comments) < 100:
                break
            page += 1
        if not history:
            if mirror is not None or revisions:
                raise ValueError('Issue state has no immutable checkpoint')
            continue
        found.add(issue_number)
        count = len(history)
        if set(revisions) != set(range(1, len(revisions) + 1)):
            raise ValueError('Historical comment gap cannot be repaired')
        if issue_number != number:
            if len(revisions) != count or mirror != history[-1]:
                raise ValueError('Unrelated issue requires reconciliation')
            continue
        predecessor = history[-2] if count > 1 else None
        if len(revisions) not in (count - 1, count) or mirror not in (predecessor, history[-1]):
            raise ValueError('Repair requires an intact history and exact tail or predecessor mirror')
        # Publication writes comment first. A newer mirror with missing comment
        # cannot result from our interrupted publication and is not repaired.
        if len(revisions) == count - 1 and mirror != predecessor:
            raise ValueError('Issue mirror is ahead of publication history')
        target = (issue, len(revisions) != count, mirror != history[-1])
    if found != set(checkpoint['records']) or target is None:
        raise ValueError('Checkpoint issue is absent from complete issue snapshot')
    _checkpoint_protection(api, repository)
    if _checkpoint_head(api, repository) != checkpoint['head']:
        raise ValueError('Checkpoint changed before repair')
    old, missing_comment, missing_mirror = target
    path = prefix + '/issues/' + str(number)
    current = api('GET', path)
    if not isinstance(current, dict) or any(current.get(k) != old.get(k) for k in ('number', 'body', 'updated_at')):
        raise ValueError('Issue changed before repair')
    sealed = checkpoint['records'][number]
    if missing_comment:
        body = render('', sealed)
        comment = api('POST', path + '/comments', {'body':body})
        if (not isinstance(comment, dict) or type(comment.get('id')) is not int or comment.get('body') != body
                or not isinstance(comment.get('user'), dict) or comment['user'].get('login') != 'github-actions[bot]'):
            raise ValueError('Uncertain repair comment write; inspect before retry')
    if missing_mirror:
        after = api('GET', path)
        if not isinstance(after, dict) or after.get('number') != number or after.get('body') != old.get('body'):
            raise ValueError('Issue mirror changed during repair')
        if _checkpoint_head(api, repository) != checkpoint['head']:
            raise ValueError('Checkpoint changed during repair')
        persist(api, repository, after, sealed)
    snapshot = _canonical_snapshot(api, repository, public_key, checkpoint=checkpoint)
    return next(record for issue, record in snapshot if record['issue'] == number)
