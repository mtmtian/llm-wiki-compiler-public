"""Cooperate on a single automatic writer and explicit one-shot borrowers.

The source persists its release before exposing it to iCloud. Its durable
high-water mark prevents a stale exchange snapshot or a restart from restoring
permission. No heartbeat, timeout, or conversational activity confers ownership.
"""

import json
from pathlib import Path

from common import load_json
from shared_files import SharedFiles
from replica_records import IDENTITY
from shared_materialize import _migration_lock, _reconcile, _save
import writer_checkpoint as checkpoint
import writer_records as records

STATE = 'shared-writer-state.json'


def _guard(config, create=True):
    """Keep coordination enabled once its durable local guard has been created."""
    path = Path(config['stateDir']) / STATE
    if path.is_symlink():
        raise ValueError('shared writer state cannot be a symlink')
    value = load_json(path, None)
    profile = records.settings(config)
    if value is None:
        value = {'profile': profile, 'head': None, 'adopted': None, 'outbox': None}
        if create:
            _save(path, value)
    if (not isinstance(value, dict) or set(value) != {'profile', 'head', 'adopted', 'outbox'}
            or value['profile'] != profile):
        raise ValueError('shared writer private state does not match this deployment')
    return value


def _persist(config, state):
    """Fsync the local safety decision before another machine can observe it."""
    _save(Path(config['stateDir']) / STATE, state)


def _flush(config, state):
    """Retry a previously committed immutable release after interruption."""
    if state['outbox'] is not None:
        value = state['outbox']
        if value.get('from') != config['machineId']:
            raise ValueError('shared writer outbox has a foreign issuer')
        records.write(config, records.path_for(value), value)


def _view(config, state, flush=True):
    """Require complete history containing this machine's durable high-water mark."""
    if flush:
        _flush(config, state)
    chain, pending = records.history(config)
    if state['head'] is not None and state['head'] not in {item['id'] for item in chain}:
        raise ValueError('shared writer history is behind the durable local receipt')
    return chain, pending


def _release(config, state, value):
    """Relinquish under the page lock before publishing a recoverable acknowledgement."""
    records.encode(value)
    state.update(head=value['id'], adopted=None, outbox=value)
    _persist(config, state)
    _flush(config, state)


def _transfer(config, generation, baseline, state, chain, target, mode, request_id):
    """Pass verified page ownership and required inputs to exactly one successor."""
    value = records.sealed({'version': 1, 'kind': 'release', 'bootstrapId': chain[0]['id'],
                            'previous': chain[-1]['id'], 'from': config['machineId'], 'to': target,
                            'mode': mode, 'requestId': request_id,
                            'checkpoint': checkpoint.capture(config, generation, baseline)})
    _release(config, state, value)
    return value


def coordinate(config, generation, baseline):
    """Called under the existing shared-page lock on every background sync."""
    if config.get('_sharedWriterBootstrap') is not None:
        return _bootstrap_locked(config, generation, baseline, config['_sharedWriterBootstrap'])
    state = _guard(config)
    chain, pending = _view(config, state)
    if not chain:
        return {'status': 'awaiting-handoff-bootstrap'}
    if chain[0]['baselineId'] != baseline['snapshotId']:
        raise ValueError('shared writer baseline changed')
    tip = chain[-1]
    state['head'] = tip['id']
    _persist(config, state)
    if tip['to'] != config['machineId']:
        return {'status': 'not-materializer', 'machineId': tip['to'], 'receiptId': tip['id']}
    if state['adopted'] != tip['id']:
        checkpoint.adopt(config, tip['checkpoint'], baseline)
        state['adopted'] = tip['id']
        _persist(config, state)
    result = _reconcile(config, Path(generation), baseline, Path(config['stateDir']))
    return _complete(config, generation, baseline, state, chain, pending, result)


def _complete(config, generation, baseline, state, chain, pending, result):
    """Return one-shot ownership or acknowledge the next explicit request."""
    tip = chain[-1]
    default = chain[0]['profile']['defaultMachineId']
    if tip['mode'] == 'once':
        receipt = _transfer(config, generation, baseline, state, chain, default, 'default', tip['requestId'])
        return {**result, 'status': 'returned-to-default', 'machineId': default, 'receiptId': receipt['id']}
    eligible = [item for _, item in sorted(pending.items()) if item['from'] != default]
    if eligible:
        try:
            _ready_participants(config)
        except ValueError as error:
            return {**result, 'machineId': default, 'handoffWaiting': str(error)}
        request = eligible[0]
        receipt = _transfer(config, generation, baseline, state, chain, request['from'], 'once', request['id'])
        return {**result, 'status': 'handed-off', 'machineId': request['from'], 'receiptId': receipt['id']}
    return {**result, 'machineId': default}


def runtime_commit(config):
    """Read the actual installed runtime identity rather than a CLI version label."""
    path = Path(config['worker']).parent.parent / 'build-manifest.json'
    value = load_json(path, {})
    commit = value.get('commit', '')
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise ValueError('shared writer activation requires an installed runtime commit')
    return commit


def _ready_participants(config):
    """Activation requires each machine to report the same installed coordination profile."""
    root = Path(config['exchange']['root'])
    commit = runtime_commit(config)
    profile = records.settings(config)
    for machine in profile['participants']:
        with SharedFiles(root) as files:
            raw = files.read(f'machines/{machine}.json', max_bytes=65536)
        value = json.loads(raw) if raw is not None else {}
        if (value.get('machineId') != machine or value.get('runtimeCommit') != commit
                or value.get('sharedWriter') != profile or not value.get('enabled')
                or not value.get('publishEnabled')):
            raise ValueError(f'shared writer participant is not upgrade-ready: {machine}')


def _bootstrap_locked(config, generation, baseline, apply):
    """The previous owner releases its manifest only after the upgrade barrier."""
    profile = records.settings(config)
    if profile is None or config['machineId'] != profile['bootstrapMachineId']:
        raise ValueError('shared writer bootstrap requires the previous owner')
    state = _guard(config, create=apply)
    chain, _ = _view(config, state, flush=apply)
    if chain:
        return {'status': 'already-initialized', 'defaultMachineId': profile['defaultMachineId']}
    _ready_participants(config)
    if not apply:
        checkpoint.capture(config, generation, baseline)
        return {'status': 'dry-run', 'defaultMachineId': profile['defaultMachineId']}
    _reconcile(config, Path(generation), baseline, Path(config['stateDir']))
    value = records.sealed({'version': 1, 'kind': 'bootstrap', 'profile': profile,
                            'baselineId': baseline['snapshotId'], 'from': config['machineId'],
                            'to': profile['defaultMachineId'], 'mode': 'default',
                            'checkpoint': checkpoint.capture(config, generation, baseline)})
    _release(config, state, value)
    return {'status': 'initialized', 'defaultMachineId': profile['defaultMachineId'], 'receiptId': value['id']}


def request_write(config, request_id):
    """Record one explicit, idempotent user request without granting permission."""
    if not isinstance(request_id, str) or not IDENTITY.fullmatch(request_id):
        raise ValueError('shared writer request id must be a stable identifier')
    if not config.get('enabled') or not config.get('publishEnabled') or records.settings(config) is None:
        raise ValueError('shared writer requests require an enabled coordinated publisher')
    with _migration_lock(Path(config['stateDir']), False):
        state = _guard(config)
        chain, _ = _view(config, state)
        if not chain:
            raise ValueError('shared writer bootstrap has not arrived')
        root = chain[0]
        if config['machineId'] == root['profile']['defaultMachineId']:
            return {'status': 'default-writer', 'machineId': config['machineId']}
        value = records.sealed({'version': 1, 'kind': 'request', 'bootstrapId': root['id'],
                                'from': config['machineId'], 'requestId': request_id})
        records.write(config, records.path_for(value), value)
        return {'status': 'requested', 'requestId': value['id'], 'permissionGranted': False}


def status(config):
    """Read status without reconciling pages or approving a pending request."""
    profile = records.settings(config)
    if profile is None:
        return {'status': 'legacy', 'defaultMachineId': config['exchange'].get('materializerMachineId')}
    state = _guard(config, create=False)
    chain, pending = _view(config, state, flush=False)
    return {'status': 'active' if chain else 'awaiting-handoff-bootstrap',
            'defaultMachineId': profile['defaultMachineId'], 'ownerMachineId': chain[-1]['to'] if chain else None,
            'mode': chain[-1]['mode'] if chain else None, 'pendingRequests': len(pending),
            'localReceiptId': state.get('head'), 'visibleReceiptId': chain[-1]['id'] if chain else None}
