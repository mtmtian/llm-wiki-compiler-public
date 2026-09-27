"""Control an optional Jev trial on an installed knowledge-flow runtime.

Only the local jevContext setting changes. Credentials and the persistent usage
ledger are never written or reset by this command; runtime upgrades remain owned
by the normal deployment installer.
"""
import argparse
import datetime
import json
import math
import os
import sqlite3
from pathlib import Path


def read(path):
    """Read local JSON without echoing configuration or credentials."""
    return json.loads(path.read_text())


def usage(config):
    """Read aggregate accounting, failing closed on corrupt or incomplete state."""
    file = Path(config['stateDir']) / 'jev-trial.sqlite'
    if not file.exists():
        return {'spentNano': 0, 'requests': 0, 'successes': 0, 'fallbacks': 0, 'pending': {}}
    with sqlite3.connect(f'{file.resolve().as_uri()}?mode=ro', uri=True, timeout=.2) as database:
        row = database.execute('SELECT value FROM trial WHERE id=1').fetchone()
    if not row:
        raise ValueError('Trial ledger is incomplete')
    return json.loads(row[0])


def grant(settings):
    """Require an explicit finite allowance and timezone-aware expiration."""
    budget = settings.get('budgetUsd')
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 < budget <= 5:
        raise ValueError('Set --budget-usd to an allocated allowance greater than zero and at most 5')
    expiry = datetime.datetime.fromisoformat(str(settings.get('expiresAt', '')).replace('Z', '+00:00'))
    if expiry.tzinfo is None:
        raise ValueError('Expiration must include a timezone')
    return budget, expiry


def supports_trial(config):
    """Avoid silently enabling settings on an older worker that ignores them."""
    worker = Path(config['worker']).read_bytes()
    return b'jev-1.13.0' in worker and b'jevContext' in worker


def status(config):
    """Report local accounting; this is not a provider balance or delivery check."""
    settings, ledger = config.get('jevContext', {}), usage(config)
    reason = 'disabled'
    allowance = 0
    if settings.get('budgetUsd') is not None:
        allowance, expiry = grant(settings)
        if settings.get('enabled'):
            reason = active_reason(config, ledger, allowance, expiry)
    return {'effectiveMode': 'jev-with-rule-fallback' if reason == 'ready' else 'rules', 'reason': reason,
            'model': 'jev-1.13.0', 'grantExpiresAt': settings.get('expiresAt'), 'trialAllowanceUsd': allowance,
            'accountedUsd': ledger['spentNano'] / 1e9,
            'remainingTrialAllowanceUsd': max(0, allowance - ledger['spentNano'] / 1e9),
            'requests': ledger['requests'], 'successes': ledger['successes'], 'fallbacks': ledger['fallbacks'],
            'unknownOutcomeReservations': len(ledger.get('pending', {})),
            'lastReason': ledger.get('lastReason'), 'lastAt': ledger.get('lastAt'),
            'accountingNote': 'Local usage accounting plus conservative unknown costs; not account balance'}


def active_reason(config, ledger, allowance, expiry):
    """Use the same stopping conditions as the worker before calling a trial ready."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if not supports_trial(config):
        return 'unsupported-worker'
    if now >= expiry:
        return 'expired'
    if ledger.get('stoppedReason'):
        return ledger['stoppedReason']
    if ledger['spentNano'] >= math.floor(allowance * 1e9):
        return 'budget-exhausted'
    if ledger.get('cooldownUntil', 0) > now.timestamp() * 1000:
        return 'cooldown'
    return 'ready'


def switch(config_path, enabled, budget=None, expires=None):
    """Atomically update trial settings, preserving the current runtime and all other keys."""
    before = config_path.read_bytes()
    config = json.loads(before)
    settings = {**config.get('jevContext', {}), 'enabled': enabled}
    if enabled:
        if not supports_trial(config):
            raise ValueError('Install a Jev-capable runtime using deployment/install.py before enabling')
        if budget is not None:
            settings['budgetUsd'] = budget
        if expires is not None:
            settings['expiresAt'] = expires
        grant(settings)
        usage(config)  # Never mask damaged accounting by re-enabling.
    config['jevContext'] = settings
    temporary = config_path.with_name(config_path.name + '.jev-' + str(os.getpid()))
    try:
        with temporary.open('x') as handle:
            os.chmod(temporary, 0o600)
            handle.write(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
            handle.flush(); os.fsync(handle.fileno())
        if config_path.read_bytes() != before:
            raise ValueError('Configuration changed concurrently')
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return config


def main():
    """Expose a portable CLI; no machine paths, keys or grant values are built in."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'on', 'off'], nargs='?', default='status')
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/llmwiki/knowledge-flow.json')
    parser.add_argument('--budget-usd', type=float, help='Allowance allocated to THIS machine, not the account total')
    parser.add_argument('--expires-at', help='Verified grant expiry as an ISO timestamp with timezone')
    args = parser.parse_args()
    if args.action != 'on' and (args.budget_usd is not None or args.expires_at is not None):
        parser.error('Grant settings apply only to on')
    try:
        config = read(args.config) if args.action == 'status' else switch(args.config, args.action == 'on', args.budget_usd, args.expires_at)
        print(json.dumps(status(config), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        parser.exit(1, 'Jev control failed: check runtime, grant settings and local accounting; nothing was reset.\n')


if __name__ == '__main__':
    main()
