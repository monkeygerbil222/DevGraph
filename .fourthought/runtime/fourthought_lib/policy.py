"""Deterministic Fourthought record validation and evidence-gated transitions.

Requires jsonschema. This module reads bundled schemas but performs no writes,
network calls, clock reads, lease arbitration, or Git operations. The coordinator
must authenticate receipts and fence workers against its authoritative live lease
before calling transition; a claim's lease state is only a recorded assertion.
"""
from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path
import re

from jsonschema import Draft202012Validator


STATES = frozenset(('intake', 'product-shaping', 'product-ready', 'triage',
                    'blocked', 'ready:plan', 'planning', 'ready:implement',
                    'implementing', 'ready:verify', 'verifying', 'ready:review',
                    'reviewing', 'remediate', 'merge-ready', 'done', 'owner-decision'))
_EDGES = {
    'intake': {'product-shaping'}, 'product-shaping': {'product-ready'},
    'product-ready': {'triage'}, 'triage': {'ready:plan'},
    'ready:plan': {'planning'}, 'planning': {'ready:implement'},
    'ready:implement': {'implementing'}, 'implementing': {'ready:verify'},
    'ready:verify': {'verifying'}, 'verifying': {'ready:review', 'remediate'},
    'ready:review': {'reviewing'}, 'reviewing': {'merge-ready', 'remediate'},
    'remediate': {'ready:verify'}, 'merge-ready': {'done'},
    'blocked': {'triage'}, 'owner-decision': {'product-shaping'}, 'done': set(),
}
_ENGINEERING = frozenset(('ready:plan', 'planning', 'ready:implement', 'implementing',
                          'ready:verify', 'verifying', 'ready:review', 'reviewing',
                          'remediate', 'merge-ready', 'done'))
_LIMITS = {'verification': 3, 'review': 2}
_HEAD = re.compile(r'[0-9a-fA-F]{40}\Z')


@lru_cache(maxsize=3)
def _validator(name):
    base = Path(__file__).resolve().parents[2]
    candidates = (base / 'schemas', base / 'templates/fourthought/.fourthought/schemas')
    for directory in candidates:
        path = directory / (name + '.schema.json')
        if path.is_file():
            try:
                schema = json.loads(path.read_text(encoding='utf-8'))
                Draft202012Validator.check_schema(schema)
                return Draft202012Validator(schema)
            except (OSError, ValueError) as exc:
                raise ValueError('Invalid bundled schema: ' + name) from exc
    raise ValueError('Missing bundled schema: ' + name)


def _validate(name, value):
    errors = list(_validator(name).iter_errors(value))
    if errors:
        error = errors[0]
        location = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise ValueError('{} {}: {}'.format(name, location, error.message))


def validate_contract(value):
    """Validate a work contract; readiness is enforced by transition."""
    _validate('work-contract', value)


def validate_claim(value):
    """Validate a claim's shape, including active-lease fencing metadata."""
    _validate('work-claim', value)
    if value['issue'] in value['depends_on']:
        raise ValueError('A claim cannot depend on its own issue')
    expires = value['lease'].get('expires_at')
    if expires is not None:
        try:
            finite = math.isfinite(expires)
        except OverflowError as exc:
            raise ValueError('Lease expiry is outside the supported numeric range') from exc
        if not finite:
            raise ValueError('Lease expiry must be finite')


def validate_receipt(value):
    """Validate typed, nonempty evidence bound to a complete Git SHA-1."""
    _validate('execution-receipt', value)


def _head(value):
    if not isinstance(value, str) or _HEAD.fullmatch(value) is None:
        raise ValueError('head must be a complete 40 hexadecimal Git SHA-1')
    return value.lower()


def _record(value):
    if not isinstance(value, dict):
        raise ValueError('record must be an object')
    required = ('issue', 'state', 'contract', 'claim', 'head', 'receipts', 'attempts')
    if any(key not in value for key in required):
        raise ValueError('record requires ' + ', '.join(required))
    if not isinstance(value['state'], str) or value['state'] not in STATES:
        raise ValueError('Unknown source state')
    if type(value['issue']) is not int or value['issue'] <= 0:
        raise ValueError('record issue must be a positive integer')
    validate_contract(value['contract'])
    validate_claim(value['claim'])
    if value['issue'] != value['contract']['issue'] or value['issue'] != value['claim']['issue']:
        raise ValueError('Contract and claim must belong to the record issue')
    _head(value['head'])
    if not isinstance(value['receipts'], list):
        raise ValueError('receipts must be a list')
    for item in value['receipts']:
        validate_receipt(item)
        if item['issue'] != value['issue']:
            raise ValueError('Receipt belongs to another issue')
    attempts = value['attempts']
    if not isinstance(attempts, dict) or set(attempts) != set(_LIMITS):
        raise ValueError('attempts requires verification and review counters')
    if any(type(count) is not int or count < 0 for count in attempts.values()):
        raise ValueError('attempts must be nonnegative integers')
    if 'blocked_reason' in value and not isinstance(value['blocked_reason'], dict):
        raise ValueError('blocked_reason must be an object')
    if 'assurance_required' in value and type(value['assurance_required']) is not bool:
        raise ValueError('assurance_required must be boolean')


def _latest(record, stages):
    for index in range(len(record['receipts']) - 1, -1, -1):
        item = record['receipts'][index]
        if item['stage'] in stages:
            return index, item
    raise ValueError('Missing {} evidence'.format('/'.join(stages)))


def _passing(record, stage, head):
    index, item = _latest(record, (stage,))
    if item['result'] != 'pass' or _head(item['head']) != head:
        raise ValueError('Missing passing {} evidence at current HEAD'.format(stage))
    return index, item


def _implementation(record, head):
    plan_index, plan = _latest(record, ('plan',))
    original_index, original = _latest(record, ('implement',))
    if plan['result'] != 'pass' or plan_index >= original_index:
        raise ValueError('Implementation must follow a passing plan')
    if original['result'] != 'pass':
        raise ValueError('Missing passing original implementation evidence')
    index, item = _latest(record, ('implement', 'remediate'))
    if item['result'] != 'pass' or _head(item['head']) != head:
        raise ValueError('Missing passing implementation evidence at current HEAD')
    return index, item


def _verified(record, head):
    implementation_index, implementation = _implementation(record, head)
    verification_index, verification = _passing(record, 'verify', head)
    if verification_index <= implementation_index:
        raise ValueError('Verification must follow the latest implementation')
    return verification_index, implementation


def _independent(record, actor):
    authors = {item['actor'] for item in record['receipts']
               if item['stage'] in ('implement', 'remediate')}
    if actor in authors:
        raise ValueError('Review and Assurance must be independent of all implementation actors')


def _reviewed(record, head):
    verification_index, implementation = _verified(record, head)
    review_index, review = _passing(record, 'review', head)
    _independent(record, review['actor'])
    if review_index <= verification_index:
        raise ValueError('Review must independently follow current verification')
    return review_index


def _exhausted(record):
    for stage, limit in _LIMITS.items():
        count = record['attempts'][stage]
        if count > limit:
            record['state'] = 'blocked'
            record['blocked_reason'] = {
                'code': 'loop-exhausted', 'stage': stage, 'attempts': count,
                'limit': limit, 'route_to': ['lead', 'project-manager', 'product-manager'],
            }
            return True
    return False


def transition(record, target, receipt=None, head=None, assurance=None):
    """Return a deep copy advanced one legal edge, or blocked on exhaustion.

    Invalid data, edges, readiness or evidence raise ValueError. Attempts count
    remediations already requested: failures 1..3 (verify) and 1..2 (review)
    permit remediation; the next failure blocks. A changed head is permitted
    only with a successful implementation or remediation completion receipt.
    Explicit Assurance is an assurance-stage receipt, persisted in receipts.
    """
    _record(record)
    if not isinstance(target, str) or target not in STATES:
        raise ValueError('Unknown target state')
    result = deepcopy(record)
    source = result['state']
    if source == 'blocked' and result.get('blocked_reason', {}).get('code') == 'loop-exhausted':
        raise ValueError('Exhaustion requires Lead/Project/Product intervention')
    if _exhausted(result):
        return result
    allowed = _EDGES[source] | ({'blocked', 'owner-decision'} if source != 'done' else set())
    if target not in allowed or target == source:
        raise ValueError('Illegal transition: {} -> {}'.format(source, target))
    current_head = _head(result['head'] if head is None else head)
    if current_head != _head(result['head']) and (source, target) not in {
            ('implementing', 'ready:verify'), ('remediate', 'ready:verify')}:
        raise ValueError('HEAD changed outside implementation/remediation completion')
    if target in _ENGINEERING:
        contract = result['contract']
        if contract['engineering']['status'] != 'ready':
            raise ValueError('Engineering contract is not ready')
        if contract['owner_decisions']['status'] not in ('resolved', 'not-required'):
            raise ValueError('Owner decisions are unresolved')
        if result['claim']['lease']['state'] != 'active':
            raise ValueError('Engineering progression requires an active lease')

    expected = {
        ('planning', 'ready:implement'): ('plan', 'pass'),
        ('implementing', 'ready:verify'): ('implement', 'pass'),
        ('remediate', 'ready:verify'): ('remediate', 'pass'),
        ('verifying', 'ready:review'): ('verify', 'pass'),
        ('reviewing', 'merge-ready'): ('review', 'pass'),
        ('merge-ready', 'done'): ('product', 'pass'),
        ('verifying', 'remediate'): ('verify', 'fail'),
        ('reviewing', 'remediate'): ('review', 'fail'),
    }.get((source, target))
    if expected is None and receipt is not None:
        raise ValueError('This transition does not accept a stage receipt')
    if expected is not None:
        validate_receipt(receipt)
        if (receipt['stage'], receipt['result']) != expected:
            raise ValueError('Transition requires {} {} receipt'.format(*expected))
        if receipt['issue'] != result['issue'] or _head(receipt['head']) != current_head:
            raise ValueError('Receipt must bind to the issue and current HEAD')
        if receipt['stage'] in ('verify', 'review'):
            _, implementation = _implementation(result, current_head)
            if receipt['stage'] == 'review':
                _verified(result, current_head)
                _independent(result, receipt['actor'])
        result['receipts'].append(deepcopy(receipt))
    result['head'] = current_head

    if target == 'remediate':
        counter = 'verification' if source == 'verifying' else 'review'
        result['attempts'][counter] += 1
        if _exhausted(result):
            return result
    if target == 'implementing':
        _passing(result, 'plan', current_head)
    if target == 'verifying':
        _implementation(result, current_head)
    if target in ('ready:review', 'reviewing'):
        _verified(result, current_head)
    if target in ('merge-ready', 'done'):
        _reviewed(result, current_head)
        required = (result.get('assurance_required', False)
                    or result['contract'].get('assurance', {}).get('required', False)
                    or result['claim']['risk'] in ('high', 'critical')
                    or result['claim']['class'] in ('architecture', 'security'))
        if assurance is not None:
            validate_receipt(assurance)
            if assurance['stage'] != 'assurance' or assurance['issue'] != result['issue']:
                raise ValueError('Explicit Assurance must be an assurance receipt for this issue')
            result['receipts'].append(deepcopy(assurance))
        if required or assurance is not None:
            assurance_index, evidence = _passing(result, 'assurance', current_head)
            implementation_index, implementation = _implementation(result, current_head)
            _independent(result, evidence['actor'])
            if assurance_index <= implementation_index:
                raise ValueError('Assurance must independently follow current implementation')
    elif assurance is not None:
        raise ValueError('Assurance evidence is accepted only at merge/product acceptance')
    result['state'] = target
    return result
