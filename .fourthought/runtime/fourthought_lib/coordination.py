"""Pure lease arbitration and local, fenced worktree lifecycle.

The caller serializes and persists the complete GitHub issue snapshot. This
module deliberately does not invent a second local issue database.
"""
import copy
import math
from pathlib import Path
import re
import time
import uuid
from .attachment import atomic, decode, encoded, git, lock, root

CLAIMABLE = {'triage', 'ready:plan', 'planning', 'ready:implement', 'implementing', 'ready:verify', 'verifying', 'ready:review', 'reviewing', 'remediate', 'merge-ready'}


def clock(now=None, ttl=900):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('Invalid lease clock')
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 86400:
        raise ValueError('Lease TTL must be positive and at most one day')
    return now


def snapshot(records):
    if not isinstance(records, list):
        raise ValueError('Complete issue snapshot must be a list')
    result = copy.deepcopy(records)
    ids = set()
    for r in result:
        if not isinstance(r, dict) or type(r.get('issue')) is not int or r['issue'] <= 0 or r['issue'] in ids:
            raise ValueError('Invalid or duplicate issue in snapshot')
        ids.add(r['issue'])
        claim = r.get('claim')
        if claim is None:
            continue  # Nonengineering issues cannot hold claims.
        from .policy import validate_claim
        validate_claim(claim)
        if claim['issue'] != r['issue']:
            raise ValueError('Claim identity mismatch')
    return result


def find(records, issue):
    if type(issue) is not int or issue <= 0:
        raise ValueError('Invalid issue')
    found = [r for r in records if r['issue'] == issue]
    if len(found) != 1:
        raise ValueError('Issue not uniquely present in snapshot')
    return found[0]


def identity(holder):
    if not isinstance(holder, str) or not holder.strip():
        raise ValueError('Lease holder is required')


def acquire(records, issue, holder, now=None, ttl=900):
    now = clock(now, ttl); identity(holder)
    result = snapshot(records); r = find(result, issue)
    if r.get('state') not in CLAIMABLE or not r.get('claim'):
        raise ValueError('Issue is not claimable')
    claim = r['claim']
    if claim['lease']['state'] not in ('pending', 'released'):
        raise ValueError('Issue has an active or quarantined lease')
    for dep in claim['depends_on']:
        if find(result, dep).get('state') != 'done':
            raise ValueError('Unsatisfied dependency: ' + str(dep))
    for other in result:
        c = other.get('claim')
        if other['issue'] == issue or not c or c['lease']['state'] in ('pending', 'released'):
            continue
        conflict = (not claim['parallel_safe'] or not c['parallel_safe'] or
                    bool(set(claim['collision_domains']) & set(c['collision_domains'])))
        if conflict:
            expired = c['lease'].get('expires_at', 0) <= now or c['lease']['state'] in ('expired', 'quarantined')
            raise ValueError(('Expired/quarantined collision; confirm worker stopped: ' if expired else 'Active collision: ') + str(other['issue']))
    claim['lease'] = {'state': 'active', 'holder': holder, 'token': uuid.uuid4().hex, 'expires_at': now + ttl}
    return result


def fence(record, holder, token, now=None, allow_expired=False):
    now = clock(now); identity(holder)
    lease = record.get('claim', {}).get('lease', {})
    if lease.get('state') != 'active' or lease.get('holder') != holder or lease.get('token') != token or not token:
        raise ValueError('Lease fencing token or holder mismatch')
    if type(lease.get('expires_at')) not in (int, float) or not math.isfinite(lease['expires_at']):
        raise ValueError('Invalid lease expiry')
    if not allow_expired and lease['expires_at'] <= now:
        raise ValueError('Lease expired; worker is quarantined')
    return lease


def renew(records, issue, holder, token, now=None, ttl=900):
    now = clock(now, ttl); result = snapshot(records)
    lease = fence(find(result, issue), holder, token, now)
    lease['expires_at'] = now + ttl
    return result


def release(records, issue, holder, token, now=None, stopped=False):
    result = snapshot(records)
    lease = fence(find(result, issue), holder, token, now, allow_expired=True)
    if stopped is not True:
        raise ValueError('Explicit stopped-worker confirmation required before releasing collision domains')
    lease['state'] = 'released'
    return result


def binding_path(repo, issue):
    if type(issue) is not int or issue <= 0:
        raise ValueError('Invalid worktree issue')
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    directory = common / 'fourthought-worktrees'
    if directory.is_symlink():
        raise ValueError('Symlinked worktree binding directory')
    path = directory / (str(issue) + '.json')
    if path.is_symlink():
        raise ValueError('Symlinked worktree binding')
    return path


def create_worktree(value, record, holder, token, now=None):
    repo = root(value)
    fence(record, holder, token, now)
    issue = record['issue']
    if record.get('state') not in CLAIMABLE:
        raise ValueError('Worktree requires a claimable issue')
    head = record.get('head', '')
    if not isinstance(head, str) or not re.fullmatch('[a-f0-9]{40}', head):
        raise ValueError('Worktree requires an exact base HEAD')
    git(repo, 'cat-file', '-e', head + '^{commit}')
    parent = repo.parent / ('.' + repo.name + '-fourthought-worktrees')
    target = parent / ('issue-' + str(issue))
    if parent.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError('Worktree destination is already present or symlinked')
    with lock(repo):
        marker = binding_path(repo, issue)
        if marker.exists():
            raise ValueError('Worktree binding already exists; inspect before retry')
        branch = 'ft/' + str(issue) + '/work'
        preserved_head = git(repo, 'branch', '--list', '--format=%(objectname)', branch)
        if preserved_head:
            if preserved_head != head:
                raise ValueError('Preserved issue branch HEAD differs from authoritative record HEAD')
            # Reuse without force: Git refuses a branch checked out elsewhere.
            add_args = (str(target), branch)
        else:
            add_args = ('-b', branch, str(target), head)
        binding = {'schema': 'fourthought-worktree/v1', 'issue': issue, 'token': token,
                   'holder': holder, 'path': str(target), 'branch': branch, 'base': head, 'state': 'creating',
                   'common_dir': git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')}
        atomic(marker, encoded(binding))
        # Failure leaves the creating marker and any files: no shared-checkout fallback.
        git(repo, 'worktree', 'add', *add_args)
        binding['state'] = 'ready'
        atomic(marker, encoded(binding))
        return binding


def check_worktree(value, record, holder, token, now=None):
    repo = root(value); fence(record, holder, token, now)
    marker = binding_path(repo, record['issue'])
    b = decode(marker)
    if b.get('schema') != 'fourthought-worktree/v1' or b.get('state') != 'ready' or b.get('issue') != record['issue'] or b.get('token') != token or b.get('holder') != holder:
        raise ValueError('Worktree binding mismatch or incomplete creation')
    expected = repo.parent / ('.' + repo.name + '-fourthought-worktrees') / ('issue-' + str(record['issue']))
    tree = Path(b.get('path', ''))
    if tree != expected or tree.is_symlink() or tree.parent.is_symlink():
        raise ValueError('Worktree path mismatch')
    common = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if git(tree, 'rev-parse', '--path-format=absolute', '--git-common-dir') != common or b.get('common_dir') != common or git(tree, 'branch', '--show-current') != b.get('branch'):
        raise ValueError('Worktree repository or branch mismatch')
    return b


def remove_worktree(value, record, holder, token, now=None):
    repo = root(value)
    with lock(repo):
        b = check_worktree(repo, record, holder, token, now)
        tree = Path(b['path'])
        if git(tree, 'status', '--porcelain', '--untracked-files=all'):
            raise ValueError('Dirty worktree; preserve worker changes')
        # Git considers ignored files disposable during worktree removal. Preserve
        # them explicitly: they may contain worker-local settings or credentials.
        if git(tree, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z'):
            raise ValueError('Ignored files in worktree; preserve worker files')
        git(repo, 'worktree', 'remove', str(tree))
        binding_path(repo, record['issue']).unlink()
        return {'removed': str(tree), 'branch_preserved': b['branch']}
