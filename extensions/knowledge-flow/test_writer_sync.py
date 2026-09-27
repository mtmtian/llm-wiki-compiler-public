"""Integrate handoff with the normal replica and publication boundaries.

Only page rendering uses a deterministic fixture. Real configuration validation,
input identities, seals, shared ownership, publication transport and recovery
status are exercised without invoking a model or touching the live vault.
"""

import json
import unittest
from pathlib import Path

import test_shared_replica
from exchange import announce_machine
from replica import sync_replica
from writer_handoff import request_write


class WriterSyncTests(unittest.TestCase):
    """A role handoff changes shared writing while independent publishing continues."""

    def setUp(self):
        self.fixture = test_shared_replica.SharedReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.configs = self.fixture.fixture.configs
        self.fixture.publish('one', 'a')
        self.sync('a')
        for config in self.configs.values():
            config.update(enabled=True)
            config['exchange'].update(materializerMachineId='b',
                                      sharedWriter={'version': 1, 'bootstrapMachineId': 'a'})
            runtime = Path(config['stateDir']) / 'runtime'
            runtime.mkdir(parents=True)
            (runtime / 'build-manifest.json').write_text(json.dumps({'commit': 'a' * 40}))
            config['worker'] = str(runtime / 'knowledge-flow/worker.mjs')
            announce_machine(config, '2026-09-18T00:00:00Z')

    def sync(self, machine, **overrides):
        """Use the complete production synchronization path and its returned status."""
        return sync_replica({**self.configs[machine], **overrides}, self.fixture.materialize)

    def test_full_sync_switch_and_roundtrip_preserve_independent_publication(self):
        """Given automatic workers, explicit receipts switch writing without blocking publication."""
        for machine in self.configs:
            self.assertEqual(self.sync(machine)['sharedMaterialization']['status'], 'awaiting-handoff-bootstrap')
        self.fixture.publish('two', 'b')
        self.assertEqual(self.sync('a', _sharedWriterBootstrap=True)['sharedMaterialization']['status'], 'initialized')
        self.assertEqual(self.sync('b')['sharedMaterialization']['status'], 'current')
        request_write(self.configs['a'], 'one-shot')
        self.assertEqual(self.sync('b')['sharedMaterialization']['status'], 'handed-off')
        self.fixture.publish('three', 'a')
        borrowed = self.sync('a')
        self.assertEqual(borrowed['sharedMaterialization']['status'], 'returned-to-default')
        self.assertEqual(borrowed['count'], 3)
        returned = self.sync('b')
        self.assertEqual(returned['sharedMaterialization']['status'], 'current')
        self.assertEqual(returned['sharedMaterialization']['written'], 0)
        self.assertEqual(returned['errors'], [])
        self.assertEqual((self.fixture.shared / 'wiki/concepts/three.md').read_text(), 'three')


if __name__ == '__main__':
    unittest.main()
