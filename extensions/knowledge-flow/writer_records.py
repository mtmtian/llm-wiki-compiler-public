"""Immutable, bounded handoff records for cooperative shared-page writers.

Each release is issued only by the preceding owner. This is a causal chain,
not a distributed filesystem lock: a machine must durably stop locally before
publishing a release, and missing history never grants writing authority.
"""

import hashlib
import json
from pathlib import Path

from replica_records import canonical, is_hash, IDENTITY
from shared_files import SharedFiles

ROOT = 'v2/shared-writer'
BOOTSTRAP = ROOT + '/bootstrap.json'
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 10000


def settings(config):
    """Validate the opt-in profile shared by all participating machines."""
    exchange = config.get('exchange', {})
    value = exchange.get('sharedWriter')
    if value is None:
        return None
    members = exchange.get('participants', [])
    if (not isinstance(members, list) or not members
            or any(not isinstance(item, str) or not IDENTITY.fullmatch(item) for item in members)
            or len(set(members)) != len(members)
            or not isinstance(value, dict) or set(value) != {'version', 'bootstrapMachineId'}
            or type(value['version']) is not int or value['version'] != 1
            or exchange.get('protocolVersion') != 2 or value['bootstrapMachineId'] not in members
            or exchange.get('materializerMachineId') not in members):
        raise ValueError('invalid exchange.sharedWriter configuration')
    return {**value, 'defaultMachineId': exchange['materializerMachineId'],
            'participants': sorted(members)}


def identity(value):
    """Compute a record identity independently of JSON whitespace."""
    return hashlib.sha256(canonical({k: v for k, v in value.items() if k != 'id'}).encode()).hexdigest()


def sealed(value):
    """Attach the content identity used for immutable record paths."""
    return {**value, 'id': identity(value)}


def read(config, relative):
    """Read exact confined bytes; malformed or partial JSON is never absence."""
    root = Path(config['exchange']['root'])
    if not root.exists():
        return None
    with SharedFiles(root) as files:
        raw = files.read(relative, max_bytes=MAX_RECORD_BYTES)
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('id') != identity(value):
        raise ValueError('shared writer record identity mismatch')
    return value


def encode(value):
    """Validate transport bounds before committing a local release decision."""
    encoded = (canonical(value) + '\n').encode()
    if len(encoded) > MAX_RECORD_BYTES or value.get('id') != identity(value):
        raise ValueError('invalid shared writer record')
    return encoded


def write(config, relative, value):
    """Expose a complete immutable record, rejecting conflicting prior bytes."""
    encoded = encode(value)
    root = Path(config['exchange']['root'])
    root.mkdir(parents=True, exist_ok=True)
    with SharedFiles(root) as files:
        previous = files.read(relative, max_bytes=MAX_RECORD_BYTES)
        if previous is not None and previous != encoded:
            raise ValueError('immutable shared writer record changed')
        if previous is None:
            files.update(relative, encoded, None, value['id'][:32])


def path_for(value):
    """Give each request and release a single issuer-owned path."""
    kind = value['kind']
    if kind == 'bootstrap':
        return BOOTSTRAP
    directory = 'requests' if kind == 'request' else 'releases'
    return f"{ROOT}/{directory}/{value['from']}/{value['id']}.json"


def _inventory(config, directory):
    """Bound the immutable inventory and reject unexpected files or symlinks."""
    root = Path(config['exchange']['root'])
    parent = root / ROOT / directory
    if not parent.exists():
        return []
    if parent.is_symlink():
        raise ValueError('shared writer directory cannot be a symlink')
    paths = []
    for machine in parent.iterdir():
        if machine.name.startswith('.'):
            continue
        if machine.name not in config['exchange']['participants'] or not machine.is_dir() or machine.is_symlink():
            raise ValueError('unexpected shared writer issuer directory')
        paths.extend(machine.iterdir())
        if len(paths) > MAX_RECORDS:
            raise ValueError('shared writer inventory exceeds its bound')
    return [p.relative_to(root).as_posix() for p in paths if not p.name.startswith('.')]


def requests(config, bootstrap_id):
    """Validate explicit requests; background workers never manufacture them."""
    result = {}
    for relative in _inventory(config, 'requests'):
        value = read(config, relative)
        keys = {'version', 'kind', 'bootstrapId', 'from', 'requestId', 'id'}
        if (value is None or set(value) != keys or type(value['version']) is not int
                or value['version'] != 1 or value['kind'] != 'request'
                or value['bootstrapId'] != bootstrap_id or value['from'] not in config['exchange']['participants']
                or not isinstance(value['requestId'], str) or not IDENTITY.fullmatch(value['requestId'])
                or relative != path_for(value)):
            raise ValueError('invalid shared writer request')
        result[value['id']] = value
    return result


def _validate_bootstrap(config, value):
    """Bind activation to the configured participants, default and old owner."""
    keys = {'version', 'kind', 'profile', 'baselineId', 'from', 'to', 'mode', 'checkpoint', 'id'}
    profile = settings(config)
    if (set(value) != keys or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'bootstrap' or value['profile'] != profile
            or value['from'] != profile['bootstrapMachineId'] or value['to'] != profile['defaultMachineId']
            or value['mode'] != 'default' or not is_hash(value['baselineId'])):
        raise ValueError('shared writer bootstrap does not match this deployment')


def _validate_release(value, relative, root, request_records):
    """Validate the release envelope before examining its parent relationship."""
    keys = {'version', 'kind', 'bootstrapId', 'previous', 'from', 'to', 'mode', 'requestId', 'checkpoint', 'id'}
    members = root['profile']['participants']
    if (value is None or set(value) != keys or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'release' or value['bootstrapId'] != root['id']
            or not is_hash(value['previous']) or value['from'] not in members or value['to'] not in members
            or value['from'] == value['to'] or value['mode'] not in ('once', 'default')
            or value['requestId'] not in request_records or relative != path_for(value)):
        raise ValueError('invalid shared writer release')


def _validate_successor(parent, child, root, request_records):
    """Only the current owner can yield, and a borrower can only return once."""
    if child['from'] != parent['to']:
        raise ValueError('shared writer release is not from the current owner')
    if parent['mode'] == 'default':
        request = request_records[child['requestId']]
        valid = child['mode'] == 'once' and child['to'] == request['from']
    else:
        valid = (child['mode'] == 'default' and child['to'] == root['profile']['defaultMachineId']
                 and child['requestId'] == parent['requestId'])
    if not valid:
        raise ValueError('invalid shared writer ownership transition')


def history(config):
    """Return one complete ownership chain; forks and missing parents stop writes."""
    root = read(config, BOOTSTRAP)
    if root is None:
        return [], {}
    _validate_bootstrap(config, root)
    pending = requests(config, root['id'])
    children = {}
    for relative in _inventory(config, 'releases'):
        value = read(config, relative)
        _validate_release(value, relative, root, pending)
        if value['previous'] in children:
            raise ValueError('shared writer history fork')
        children[value['previous']] = value
    chain, used = [root], set()
    while chain[-1]['id'] in children:
        child = children.pop(chain[-1]['id'])
        _validate_successor(chain[-1], child, root, pending)
        if child['mode'] == 'once':
            if child['requestId'] in used:
                raise ValueError('shared writer request replay')
            used.add(child['requestId'])
        chain.append(child)
    if children:
        raise ValueError('incomplete shared writer history')
    return chain, {key: value for key, value in pending.items() if key not in used}
