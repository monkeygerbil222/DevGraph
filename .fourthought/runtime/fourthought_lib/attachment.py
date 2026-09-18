"""Preflighted, versioned attachment; project content never becomes framework-owned."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from contextlib import contextmanager
from . import VERSION


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Git failed: ' + result.stderr.strip())
    return result.stdout.strip()


def root(value):
    supplied = Path(value).expanduser().absolute()
    if supplied.is_symlink():
        raise ValueError('Repository root must not be a symlink')
    repo = supplied.resolve()
    if Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise ValueError('Supply the repository root, not a subdirectory')
    git(repo, 'rev-parse', '--verify', 'HEAD')
    return repo


def safe(repo, name):
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(x in ('..', '.') for x in p.parts) or '\\' in name:
        raise ValueError('Unsafe managed path: ' + name)
    target = repo
    for index, part in enumerate(p.parts):
        target = target / part
        if target.is_symlink():
            raise ValueError('Symlink in managed path: ' + name)
        if index < len(p.parts) - 1 and target.exists() and not target.is_dir():
            raise ValueError('Managed path ancestor is not a directory: ' + name)
    if not target.resolve().is_relative_to(repo):
        raise ValueError('Path escapes repository: ' + name)
    return target


def decode(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Invalid JSON file ' + str(path) + ': ' + str(exc)) from exc


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.fourthought-write-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(repo):
    # All linked worktrees share this coordinator lock. No PID-based stale unlock.
    import fcntl
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    path = common / 'fourthought-attachment.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise ValueError('Another Fourthought operation holds the repository lock') from exc
    finally:
        os.close(fd)


def source_files():
    package = Path(__file__).resolve().parent
    framework = package.parents[1]
    templates = framework / 'templates/fourthought'
    if not templates.is_dir():
        raise ValueError('Use the source Fourthought CLI to install/update; installed runtime is for doctor/status and execution')
    files = {}
    for p in sorted(templates.rglob('*')):
        if p.is_symlink():
            raise ValueError('Framework template is a symlink')
        if p.is_file():
            files[p.relative_to(templates).as_posix()] = p.read_bytes()
    for p in sorted(package.glob('*.py')):
        files['.fourthought/runtime/fourthought_lib/' + p.name] = p.read_bytes()
    files['scripts/fourthought'] = (framework / 'scripts/fourthought').read_bytes()
    files['.fourthought/version'] = (VERSION + '\n').encode()
    return files


def config_default(repo):
    return {'schema': 'fourthought-config/v1', 'project': {'name': repo.name},
            'loops': {'verification': 3, 'review': 2},
            'github': {'enabled': False, 'repository': '', 'coordinator': 'github-actions',
                       'actors': {}, 'state_public_key': '', 'runtime_sessions': False},
            'runtime': {'verify_commands': [], 'push_remote': 'origin', 'worker_timeout': 900, 'lease_ttl': 900},
            'assurance': {'mode': 'detect'}, 'skills': {'pins': {}, 'roles': {}}}


def validate_config(value):
    if not isinstance(value, dict) or value.get('schema') != 'fourthought-config/v1':
        raise ValueError('Unsupported configuration schema')
    if not isinstance(value.get('project'), dict) or not isinstance(value['project'].get('name'), str) or not value['project']['name'].strip():
        raise ValueError('Configuration requires project.name')
    if value.get('loops') != {'verification': 3, 'review': 2} or any(type(x) is not int for x in value.get('loops', {}).values()):
        raise ValueError('Phase 1 loop limits must be verification=3, review=2')
    g = value.get('github')
    if not isinstance(g, dict) or type(g.get('enabled')) is not bool or g.get('coordinator') != 'github-actions':
        raise ValueError('Invalid GitHub coordinator configuration')
    if not isinstance(g.get('repository'), str) or (g['enabled'] and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', g['repository'])):
        raise ValueError('Enabled GitHub requires owner/repository')
    if 'runtime_sessions' in g and type(g['runtime_sessions']) is not bool:
        raise ValueError('github.runtime_sessions must be boolean')
    runtime = value.get('runtime', {})
    if not isinstance(runtime, dict) or set(runtime) - {'verify_commands','push_remote','worker_timeout','lease_ttl'}:
        raise ValueError('Invalid runtime configuration')
    commands = runtime.get('verify_commands', [])
    if (not isinstance(commands, list) or any(not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or '\0' in arg for arg in argv)
            or not argv[0].strip() for argv in commands)):
        raise ValueError('runtime.verify_commands must be a list of nonempty argument arrays')
    remote = runtime.get('push_remote', 'origin')
    if not isinstance(remote, str) or not remote.strip() or '\0' in remote:
        raise ValueError('runtime.push_remote must be a nonempty string')
    for field, low, high in [('worker_timeout',10,7200),('lease_ttl',600,86400)]:
        number = runtime.get(field, 900)
        if type(number) is not int or not low <= number <= high:
            raise ValueError('runtime.' + field + ' must be an integer from ' + str(low) + ' to ' + str(high))
    if value.get('assurance') not in ({'mode': 'detect'}, {'mode': 'required'}):
        raise ValueError('Assurance mode must be detect or required')
    s = value.get('skills')
    if not isinstance(s, dict) or not isinstance(s.get('pins'), dict) or not isinstance(s.get('roles'), dict):
        raise ValueError('Invalid skill configuration')
    return value


_SCHEMA_NAMES = ('work-contract', 'work-claim', 'execution-receipt')
_RUNTIME_MODULES = ('__init__', 'attachment', 'cli', 'coordination', 'github',
                    'hooks', 'integrations', 'policy')


def required_files(version=VERSION):
    """Version-specific runtime minimum, also usable without the source tree."""
    from .integrations import ROLES
    modules = _RUNTIME_MODULES + (('sessions','engine','dispatch') if tuple(map(int, version.split('.'))) >= (0,2,0) else ())
    return ({'.fourthought/runtime/fourthought_lib/' + name + '.py' for name in modules}
            | {'.fourthought/roles/' + role + '.md' for role in ROLES}
            | {'.fourthought/schemas/' + name + '.schema.json' for name in _SCHEMA_NAMES}
            | {'scripts/fourthought', '.fourthought/version'})


def runtime_issues(repo, files):
    """Validate required artifacts and schemas in installed or proposed bytes."""
    issues = []
    try:
        content = files['.fourthought/version']
        if not isinstance(content, bytes):
            content = safe(repo, '.fourthought/version').read_bytes()
        version = content.decode('utf-8').strip()
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Invalid version')
    except (KeyError, OSError, ValueError):
        issues.append('Missing or invalid framework version')
        version = VERSION
    issues += ['Missing required framework ownership: ' + name
               for name in sorted(required_files(version) - set(files))]
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return issues + ['Required runtime dependency unavailable: jsonschema']
    for name in _SCHEMA_NAMES:
        path = '.fourthought/schemas/' + name + '.schema.json'
        try:
            content = files[path]
            if not isinstance(content, bytes):
                content = safe(repo, path).read_bytes()
            schema = json.loads(content)
            Draft202012Validator.check_schema(schema)
        except (KeyError, OSError, ValueError, SchemaError) as exc:
            issues.append('Invalid bundled schema ' + name + ': ' + str(exc))
    return issues


def github_ready(config):
    """Check public verification configuration without a coordinator secret."""
    from .integrations import ROLES
    if not config['enabled']:
        return
    actors = config.get('actors')
    if not isinstance(actors, dict) or not actors or not set(actors) <= ROLES:
        raise ValueError('Enabled GitHub requires valid actor role allowlists')
    for names in actors.values():
        if (not isinstance(names, list)
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', name) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('Enabled GitHub requires valid actor role allowlists')
    public_key = config.get('state_public_key')
    if not isinstance(public_key, str) or not public_key.strip():
        raise ValueError('Enabled GitHub requires state_public_key')
    try:
        checked = subprocess.run(['openssl', 'pkey', '-pubin', '-noout', '-text_pub'],
                                 input=public_key, capture_output=True, text=True)
    except OSError as exc:
        raise ValueError('Enabled GitHub requires openssl for state verification') from exc
    if checked.returncode:
        raise ValueError('Invalid GitHub state_public_key: openssl rejected public key')
    if not checked.stdout.startswith('ED25519 Public-Key:'):
        raise ValueError('GitHub state_public_key must use Ed25519')


def integration_issues(repo, config):
    from .integrations import ROLES, assurance, skills
    issues = []
    try:
        assurance(repo, required=config['assurance']['mode'] == 'required')
    except (OSError, ValueError) as exc:
        issues.append(str(exc))
    for role in sorted(ROLES):
        try:
            skills(repo, config['skills'], role)
        except (OSError, ValueError) as exc:
            issues.append('Skill integration for ' + role + ': ' + str(exc))
    try:
        github_ready(config['github'])
    except ValueError as exc:
        issues.append(str(exc))
    return issues


def manifest(repo):
    value = decode(safe(repo, '.fourthought/manifest.json'))
    if not isinstance(value, dict) or value.get('schema') != 'fourthought-manifest/v1' or not re.fullmatch(r'\d+\.\d+\.\d+', str(value.get('version', ''))):
        raise ValueError('Unsupported ownership manifest')
    if not isinstance(value.get('files'), dict) or not value['files']:
        raise ValueError('Empty ownership manifest')
    for name, meta in value['files'].items():
        safe(repo, name)
        allowed = name.startswith(('.fourthought/runtime/', '.fourthought/roles/', '.fourthought/schemas/', '.fourthought/hooks/', '.fourthought/loops/', '.fourthought/workflows/', '.fourthought/policy/')) or name in ('.fourthought/version', 'scripts/fourthought', '.github/workflows/fourthought-router.yml', '.github/ISSUE_TEMPLATE/fourthought-feature.yml', '.github/ISSUE_TEMPLATE/fourthought-bug.yml')
        if not allowed or not isinstance(meta, dict) or meta.get('owner') != 'framework' or not re.fullmatch(r'[a-f0-9]{64}', str(meta.get('sha256', ''))):
            raise ValueError('Invalid framework ownership: ' + name)
    if not isinstance(value.get('project_files'), list) or not value['project_files']:
        raise ValueError('Missing project ownership metadata')
    for name in value['project_files']:
        if not isinstance(name, str) or not (name.startswith('.fourthought/product/') or name in ('.fourthought/config.json', '.fourthought/repo-profile.json')):
            raise ValueError('Invalid project ownership path')
        safe(repo, name)
    return value


def inventory(repo):
    shapes = [('pyproject.toml', 'python'), ('package.json', 'node'), ('Cargo.toml', 'rust'), ('go.mod', 'go'), ('Makefile', 'make')]
    return {'schema': 'fourthought-profile/v1', 'languages': [lang for f, lang in shapes if (repo / f).is_file()],
            'test_files': sorted(str(p.relative_to(repo)) for p in (repo / 'tests').glob('test_*') if p.is_file()),
            'ci': sorted(p.name for p in (repo / '.github/workflows').glob('*') if p.is_file()),
            'instructions': [p for p in ('AGENTS.md', 'CLAUDE.md') if (repo / p).is_file()],
            'head_at_attachment': git(repo, 'rev-parse', 'HEAD'), 'branch_at_attachment': git(repo, 'branch', '--show-current'),
            'accountability_present': (repo / '.accountability/ENGAGEMENT.yaml').is_file()}


def inspect(repo, allow_incomplete=False):
    if not allow_incomplete and safe(repo, '.fourthought/INCOMPLETE').exists():
        raise ValueError('Incomplete attachment: inspect retained files before recovery')
    m = manifest(repo)
    config = validate_config(decode(safe(repo, '.fourthought/config.json')))
    issues = runtime_issues(repo, m['files']) + integration_issues(repo, config)
    version_path = safe(repo, '.fourthought/version')
    if not version_path.is_file() or version_path.read_text().strip() != m['version']:
        issues.append('Framework version differs from ownership manifest')
    for name, meta in m['files'].items():
        p = safe(repo, name)
        if not p.is_file() or sha(p.read_bytes()) != meta['sha256']:
            issues.append('Framework conflict: ' + name)
    for name in m['project_files']:
        if not safe(repo, name).is_file():
            issues.append('Missing project-owned file: ' + name)
    return m, config, issues


def install(value, update=False):
    repo = root(value)
    with lock(repo):
        if safe(repo, '.fourthought/INCOMPLETE').exists():
            raise ValueError('Incomplete attachment: explicit recovery required')
        previous = None
        if safe(repo, '.fourthought/manifest.json').exists():
            previous, config, issues = inspect(repo)
            if issues:
                raise ValueError('; '.join(issues))
        elif update:
            raise ValueError('Repository is not attached')
        files = source_files()
        project_files = {name: data for name, data in files.items() if name.startswith('.fourthought/product/')}
        files = {name: data for name, data in files.items() if name not in project_files}
        project_files['.fourthought/config.json'] = encoded(config_default(repo))
        project_files['.fourthought/repo-profile.json'] = encoded(inventory(repo))
        if previous and not update:
            project_files = {name: data for name, data in project_files.items()
                             if name in previous['project_files']}
        writes = {}
        conflicts = []
        for name, data in files.items():
            target = safe(repo, name)
            if target.exists():
                if not previous or name not in previous['files']:
                    conflicts.append('Unowned framework conflict: ' + name)
                elif update and target.read_bytes() != data:
                    writes[name] = data
            else:
                writes[name] = data
        # Preserve already owned artifacts removed from a newer distribution; no deletion.
        owned = dict(previous['files']) if previous else {}
        if previous and not update:
            files = {name: safe(repo, name).read_bytes() for name in previous['files']}
            writes = {}  # install is not an implicit upgrade
        for name, data in files.items():
            owned[name] = {'owner': 'framework', 'sha256': sha(data)}
        for name, data in project_files.items():
            p = safe(repo, name)
            if not p.exists() and (not previous or
                                   (update and name not in previous['project_files'])):
                writes[name] = data
            elif p.exists() and not p.is_file():
                conflicts.append('Project path is not a file: ' + name)
        if conflicts:
            raise ValueError('; '.join(conflicts))
        if not previous:
            config_path = safe(repo, '.fourthought/config.json')
            config = validate_config(decode(config_path) if config_path.exists() else config_default(repo))
        proposed = {name: files[name] if name in files else safe(repo, name).read_bytes()
                    for name in owned}
        readiness = runtime_issues(repo, proposed) + integration_issues(repo, config)
        if readiness:
            raise ValueError('; '.join(readiness))
        m = {'schema': 'fourthought-manifest/v1', 'version': VERSION if update or not previous else previous['version'],
             'files': owned, 'project_files': sorted(set(project_files) | set(previous['project_files'] if previous else []))}
        if previous == m and not writes:
            return {'installed': True, 'changed': [], 'version': m['version']}
        marker = safe(repo, '.fourthought/INCOMPLETE')
        atomic(marker, b'Attachment interrupted; preserve files and inspect manifest before manual recovery.\n')
        for name, data in writes.items():
            atomic(safe(repo, name), data)
            if name == 'scripts/fourthought' or name.startswith('.fourthought/hooks/'):
                safe(repo, name).chmod(0o755)
        atomic(safe(repo, '.fourthought/manifest.json'), encoded(m))
        _, _, final_issues = inspect(repo, allow_incomplete=True)
        if final_issues:
            raise ValueError('; '.join(final_issues))
        marker.unlink()
        return {'installed': True, 'changed': sorted(writes), 'version': m['version']}


def doctor(value):
    repo = root(value)
    m, config, issues = inspect(repo)
    if issues:
        raise ValueError('; '.join(issues))
    return {'ok': True, 'installed': True, 'version': m['version'], 'github_enabled': config['github']['enabled']}


def status(value):
    repo = root(value)
    if not safe(repo, '.fourthought/manifest.json').exists():
        return {'installed': False}
    m, config, issues = inspect(repo)
    return {'installed': True, 'version': m['version'], 'healthy': not issues, 'issues': issues,
            'github_enabled': config['github']['enabled']}
