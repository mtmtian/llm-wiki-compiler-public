"""Reject and preserve corrupt private generations before consumers reuse them.

The failed attempt revokes an affected current pointer and records a durable
diagnostic. A later sync rebuilds the original generation identity from its
immutable inputs; frozen jobs keep their original record basis throughout.
"""

import datetime
import os
import uuid
from pathlib import Path

from common import load_json, save_json
from replica_cleanup import GENERATION_ID
from replica_integrity import VerifiedGeneration, read_verified_generation

ERROR_FILE = Path('replica-errors/generation-integrity.json')


def _quarantine(state: Path, generation: Path, error: Exception) -> None:
    """Revoke the damaged current, log the failure, and retain its original bytes."""
    current = state / 'replica/current'
    if current.is_symlink():
        target = current.parent / os.readlink(current)
        if target.parent.resolve() / target.name == generation:
            current.unlink()
    if not generation.exists() and not generation.is_symlink():
        prior = load_json(state / ERROR_FILE, {})
        if not isinstance(prior, dict) or prior.get('generationRoot') != str(generation):
            save_json(state / ERROR_FILE, {'operation': 'generation-integrity',
                      'generationRoot': str(generation), 'quarantinedRoot': None, 'error': str(error)})
        return
    quarantine = state / 'replica/quarantine'
    if quarantine.is_symlink():
        raise ValueError('generation quarantine must not be a symlink')
    quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = quarantine / uuid.uuid4().hex
    diagnostic = {'operation': 'generation-integrity', 'generationId': generation.name,
                  'generationRoot': str(generation),
                  'quarantinedRoot': str(destination), 'error': str(error),
                  'at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save_json(state / ERROR_FILE, diagnostic)
    save_json(destination.with_suffix('.json'), diagnostic)
    if generation.exists() or generation.is_symlink():
        os.rename(generation, destination)


def verify_generation(state: Path, generation: Path) -> VerifiedGeneration:
    """Validate a confined generation; corruption aborts this consumer attempt."""
    state, generation = Path(state).resolve(), Path(generation)
    generation = generation.parent.resolve() / generation.name
    generations = state / 'replica/generations'
    if (generation.parent != generations
            or not GENERATION_ID.fullmatch(generation.name)
            or generations.is_symlink() or generations.parent.is_symlink()):
        raise ValueError('consumer generation must be a private generation directory')
    try:
        return read_verified_generation(generation)
    except (OSError, ValueError, TypeError) as error:
        _quarantine(state, generation, error)
        raise ValueError('generation integrity failed; retained for diagnosis; retry sync to rebuild') from error


def clear_recovered_error(state: Path, generation: Path) -> None:
    """Clear the active fault after successful verification, keeping quarantine history."""
    path = Path(state) / ERROR_FILE
    metadata_error = path.with_name('generation-integrity-metadata.json')
    try:
        error = load_json(path, {})
        if isinstance(error, dict) and error.get('generationRoot') == str(generation):
            path.unlink(missing_ok=True)
        metadata_error.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as error:
        try:
            save_json(metadata_error, {'operation': 'generation-integrity-metadata',
                      'path': str(path), 'error': str(error)})
        except OSError:
            pass  # Preserve the original diagnostic even if its sidecar cannot be written.
