"""CLI acceptance: switching must preserve runtime/config/state and require explicit grants.

The real process, filesystem and SQLite cover switching and damaged state. The
worker fixture only declares capability; API behavior is covered by the runtime
suite and the recorded native hook smoke, without requesting real credit in CI.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('jev-control.py')


class TrialSwitch(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = self.root / 'worker.mjs'
        self.worker.write_text('// jevContext: jev-1.13.0')
        self.config = self.root / 'config.json'
        self.original = {'worker': str(self.worker), 'stateDir': str(self.root), 'maxContextChars': 2400,
                         'intakeEnabled': True, 'projects': {'example': {'pages': ['concepts/owned']}}}
        self.config.write_text(json.dumps(self.original))

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, action, *args, succeeds=True):
        result = subprocess.run([sys.executable, str(SCRIPT), action, '--config', str(self.config), *args],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0 if succeeds else 1, result.stderr)
        return json.loads(result.stdout) if succeeds else result

    def enable(self):
        return self.command('on', '--budget-usd', '0.5', '--expires-at', '2030-01-01T00:00:00Z')

    def test_given_installed_runtime_switching_preserves_all_other_configuration(self):
        self.assertEqual(self.command('status')['reason'], 'disabled')
        self.command('on', succeeds=False)  # No guessed grant or expiry.
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        self.assertEqual(self.enable()['effectiveMode'], 'jev-with-rule-fallback')
        self.assertEqual(self.command('off')['reason'], 'disabled')
        self.assertEqual(self.command('on')['reason'], 'ready')
        config = json.loads(self.config.read_text())
        self.assertEqual({k: v for k, v in config.items() if k != 'jevContext'}, self.original)

    def test_given_old_worker_enable_is_refused_without_replacing_the_runtime(self):
        self.worker.write_text('// older worker')
        self.command('on', '--budget-usd', '0.5', '--expires-at', '2030-01-01T00:00:00Z', succeeds=False)
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        self.assertEqual(self.command('off')['reason'], 'disabled')

    def test_given_exhausted_allowance_reenabling_never_resets_accounting(self):
        self.enable()
        ledger = {'spentNano': 500_000_000, 'requests': 42, 'successes': 41, 'fallbacks': 1,
                  'stoppedReason': 'credit-exhausted', 'pending': {}}
        with sqlite3.connect(self.root / 'jev-trial.sqlite') as database:
            database.execute('CREATE TABLE trial (id INTEGER PRIMARY KEY, value TEXT)')
            database.execute('INSERT INTO trial VALUES(1,?)', (json.dumps(ledger),))
        self.command('off')
        state = self.command('on')
        self.assertEqual(state['reason'], 'credit-exhausted')
        self.assertEqual(state['remainingTrialAllowanceUsd'], 0)
        self.assertEqual(state['requests'], 42)

    def test_given_damaged_accounting_enable_fails_without_changing_configuration(self):
        before = self.config.read_bytes()
        (self.root / 'jev-trial.sqlite').write_bytes(b'not a database')
        self.command('on', '--budget-usd', '0.5', '--expires-at', '2030-01-01T00:00:00Z', succeeds=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual((self.root / 'jev-trial.sqlite').read_bytes(), b'not a database')


if __name__ == '__main__':
    unittest.main()
