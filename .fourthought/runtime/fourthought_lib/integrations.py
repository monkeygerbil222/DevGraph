"""Read-only compatibility boundaries; no installs, credentials or runtime launches."""
import re
from .attachment import safe, sha

ROLES = {'product-manager', 'project-manager', 'triage', 'planner', 'implementer', 'verifier', 'reviewer', 'lead', 'assurance'}


def assurance(repo, required=False):
    path = safe(repo, '.accountability/ENGAGEMENT.yaml')
    result = {'provider': 'accountability', 'available': False, 'reason': 'No existing Accountability engagement'}
    if path.is_file():
        text = path.read_text(encoding='utf-8-sig')
        schema = re.findall(r'^schema:\s*[\'"]?([^\s\'"#]+)[\'"]?\s*$', text, re.M)
        project = re.findall(r'^project_id:\s*[\'"]?([^\s\'"#]+)[\'"]?\s*$', text, re.M)
        review = re.findall(r'^\s+C2:\s*[\'"]?([^\n]+)', text, re.M)
        result['available'] = (schema == ['accountability-engagement/v1'] and len(project) == 1
                               and len(review) == 1 and 'assurance' in review[0].lower()
                               and safe(repo, '.accountability/reviews').is_dir())
        result['reason'] = 'Existing engagement reused; exact-HEAD review still required' if result['available'] else 'Incompatible or incomplete Accountability engagement'
        result['engagement_sha256'] = sha(path.read_bytes())
        result['reviews'] = '.accountability/reviews'
    if required and not result['available']:
        raise ValueError('Required Assurance unavailable: ' + result['reason'])
    return result


def skills(repo, config, role):
    if role not in ROLES or not isinstance(config, dict) or set(config) != {'pins', 'roles'}:
        raise ValueError('Unknown role or invalid skill configuration')
    pins, roles = config['pins'], config['roles']
    if not isinstance(pins, dict) or not isinstance(roles, dict) or not set(roles) <= ROLES:
        raise ValueError('Invalid skill role allowlist')
    selected = roles.get(role, [])
    if not isinstance(selected, list) or any(not isinstance(n, str) for n in selected) or len(set(selected)) != len(selected):
        raise ValueError('Selected skills must be a unique list')
    result = []
    for name in selected:
        pin = pins.get(name)
        if not isinstance(pin, dict) or set(pin) != {'path', 'revision', 'sha256'}:
            raise ValueError('Selected skill is not pinned: ' + name)
        if not isinstance(pin['revision'], str) or not re.fullmatch('[a-f0-9]{40}', pin['revision']):
            raise ValueError('Skill revision must be an immutable full Git commit')
        if not isinstance(pin['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', pin['sha256']):
            raise ValueError('Invalid skill content pin')
        if not isinstance(pin['path'], str):
            raise ValueError('Skill path must be repository-relative')
        path = safe(repo, pin['path'])
        if not path.is_file() or sha(path.read_bytes()) != pin['sha256']:
            raise ValueError('Selected skill missing or changed: ' + name)
        result.append({'name': name, **pin})
    return result
