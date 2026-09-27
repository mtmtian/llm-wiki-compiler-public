"""Transfer only verified ownership hashes and the required knowledge inputs.

Private paths and full page bodies stay out of handoff records. Adopting an
ownership manifest never authorizes overwriting bytes that arrived out of
order or were changed by a person after the previous writer released them.
"""

from pathlib import Path

from replica_records import is_hash
from replica_integrity import read_verified_generation
from shared_files import SharedFiles
from shared_retirement import retired_baseline
from shared_materialize import MANIFEST, PENDING, _load, _save, _scope, _prior_files, _hash


def validate_inputs(value):
    """Require a bounded unique publication set and an exact routing version."""
    if not isinstance(value, dict) or set(value) != {'recordIds', 'routesHash'}:
        raise ValueError('invalid shared writer inputs')
    records = value['recordIds']
    if (not isinstance(records, list) or len(records) > 10000
            or any(not is_hash(item) for item in records) or len(set(records)) != len(records)
            or value['routesHash'] is not None and not is_hash(value['routesHash'])):
        raise ValueError('invalid shared writer inputs')
    return value


def validate(checkpoint, baseline):
    """Validate the narrow ownership capability before reading shared paths."""
    if not isinstance(checkpoint, dict) or set(checkpoint) != {'files', 'retiredBaseline', 'inputs'}:
        raise ValueError('invalid shared writer checkpoint')
    original = {item['path']: item['text'].encode() for item in baseline['files']}
    _prior_files(checkpoint, original)
    retired = retired_baseline(checkpoint['retiredBaseline'], original)
    if retired.intersection(checkpoint['files']):
        raise ValueError('shared writer checkpoint retains retired files')
    validate_inputs(checkpoint['inputs'])
    return retired


def verify_files(config, checkpoint, baseline):
    """Require receipt bytes to have arrived before accepting any ownership."""
    retired = validate(checkpoint, baseline)
    with SharedFiles(Path(config['sharedWikiRoot'])) as files:
        for name, expected in checkpoint['files'].items():
            if _hash(files.read(name)) != expected:
                raise ValueError(f'shared writer checkpoint file mismatch: {name}')
        for name in retired:
            if files.read(name) is not None:
                raise ValueError(f'shared writer checkpoint retired file returned: {name}')


def capture(config, generation, baseline):
    """Snapshot completed owned files under the existing shared write lock."""
    read_verified_generation(Path(generation))
    state = Path(config['stateDir'])
    if (state / PENDING).exists():
        raise ValueError('shared writer cannot release an unfinished page transaction')
    manifest = _load(state / MANIFEST, _scope(config, baseline))
    if manifest is None:
        raise ValueError('shared writer has no completed ownership manifest')
    result = {'files': manifest['files'], 'retiredBaseline': manifest.get('retiredBaseline', []),
              'inputs': validate_inputs(config.get('_sharedWriterInputs'))}
    verify_files(config, result, baseline)
    return result


def adopt(config, checkpoint, baseline):
    """Rebind a delivered checkpoint after verifying inputs and all shared bytes."""
    validate(checkpoint, baseline)
    current = validate_inputs(config.get('_sharedWriterInputs'))
    required = checkpoint['inputs']
    if (not set(required['recordIds']).issubset(current['recordIds'])
            or required['routesHash'] != current['routesHash']):
        raise ValueError('shared writer checkpoint inputs have not arrived or routing changed')
    state = Path(config['stateDir'])
    if (state / PENDING).exists():
        raise ValueError('shared writer cannot replace a pending local transaction')
    verify_files(config, checkpoint, baseline)
    _save(state / MANIFEST, {'scope': _scope(config, baseline), 'files': checkpoint['files'],
                             'retiredBaseline': checkpoint['retiredBaseline']})
