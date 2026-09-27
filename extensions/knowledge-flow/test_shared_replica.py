"""Integration tests for private replica availability and shared projection status.

Materializers use disposable deterministic fixtures; record validation, sync,
ownership reconciliation and status persistence use their production paths.
"""

import copy
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import test_replica
from maintenance import report
from replica import initialize_baseline, publish_record, sync_replica
from replica_records import validate_config


class SharedReplicaTests(unittest.TestCase):
    """A shared edit must neither block private knowledge nor appear successful."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.shared = self.fixture.shared
        (self.shared / 'wiki/MOC.md').write_text('Baseline MOC')
        (self.shared / 'wiki/index.md').write_text('Baseline index')
        for config in self.fixture.configs.values():
            config['exchange'].update(root=str(Path(self.fixture.temp.name) / 'new-exchange'),
                                      materializerMachineId='a')
        self.config = self.fixture.configs['a']
        initialize_baseline(self.config)
        self.renders = 0

    def publish(self, suffix, machine='a'):
        job, result = self.fixture._submission(machine, suffix, 'always', suffix)
        publish_record(self.fixture.configs[machine], job, result)

    def materialize(self, stage, records):
        self.renders += 1
        root = Path(stage)
        for record in records:
            name = record['payload']['claims'][0]['title']
            (root / 'wiki/concepts' / (name + '.md')).write_text(name)
        for relative in ('wiki/MOC.md', 'wiki/index.md'):
            (root / relative).write_text('Generated ' + str(len(records)))
        return {'pages': len(records), 'conflicts': []}

    def test_shared_conflict_does_not_freeze_private_view_and_same_digest_retries(self):
        self.publish('one')
        first = sync_replica(self.config, self.materialize)
        (self.shared / 'wiki/MOC.md').write_text('Human navigation')
        self.publish('two')
        second = sync_replica(self.config, self.materialize)
        self.assertNotEqual(first['generationRoot'], second['generationRoot'])
        self.assertTrue((Path(second['generationRoot']) / 'wiki/concepts/two.md').exists())
        self.assertEqual(second['sharedMaterialization']['status'], 'error')
        self.assertFalse((self.shared / 'wiki/concepts/two.md').exists())
        error = Path(self.config['stateDir']) / 'replica-errors/shared-materialization.json'
        self.assertTrue(error.exists())
        with patch('maintenance.checks', return_value=[]):
            self.assertEqual(report(self.config, True)['checks'], [{'name': 'shared-materialization', 'passed': False}])
        (self.shared / 'wiki/MOC.md').write_text('Generated 1')
        retried = sync_replica(self.config, self.materialize)
        self.assertEqual(retried['sharedMaterialization']['status'], 'current')
        self.assertTrue((self.shared / 'wiki/concepts/two.md').exists())
        self.assertEqual(self.renders, 2)
        self.assertFalse(error.exists())

    def test_peer_receives_records_without_touching_shared_projection(self):
        self.publish('one')
        sync_replica(self.config, self.materialize)
        (self.shared / 'wiki/MOC.md').write_text('Human edit outside peer responsibility')
        self.publish('two', 'b')
        status = sync_replica(self.fixture.configs['b'], self.materialize)
        self.assertEqual(status['count'], 2)
        self.assertEqual(status['sharedMaterialization']['status'], 'not-materializer')
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Human edit outside peer responsibility')
        self.assertFalse((self.shared / 'wiki/concepts/two.md').exists())

    def test_late_conflict_removes_previously_visible_shared_record(self):
        self.publish('one')
        sync_replica(self.config, self.materialize)
        self.publish('two', 'b')
        def conflicted(stage, records):
            for relative in ('wiki/MOC.md', 'wiki/index.md'):
                (Path(stage) / relative).write_text('Conflicts held for review')
            return {'pages': 0, 'conflicts': [{'recordIds': [record['id'] for record in records]}]}
        status = sync_replica(self.config, conflicted)
        self.assertEqual(status['fullyVisibleRecordIds'], [])
        self.assertFalse((self.shared / 'wiki/concepts/one.md').exists())
        self.assertFalse((self.shared / 'wiki/concepts/two.md').exists())
        self.assertEqual(status['sharedMaterialization']['status'], 'current')

    def test_reader_never_promotes_even_when_its_machine_is_designated(self):
        self.publish('one')
        reader = copy.deepcopy(self.config)
        reader.update(intakeEnabled=False, publishEnabled=False)
        status = sync_replica(reader, self.materialize)
        self.assertEqual(status['count'], 1)
        self.assertEqual(status['sharedMaterialization']['status'], 'paused')
        self.assertFalse((self.shared / 'wiki/concepts/one.md').exists())
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Baseline MOC')

    def test_unknown_materializer_fails_configuration_validation(self):
        self.config['exchange']['materializerMachineId'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'materializerMachineId'):
            validate_config(self.config)

    def test_same_digest_cache_damage_preserves_shared_files_and_reports_error(self):
        self.publish('one')
        first = sync_replica(self.config, self.materialize)
        relatives = ('wiki/concepts/one.md', 'wiki/MOC.md', 'wiki/index.md')
        original = {relative: (self.shared / relative).read_bytes() for relative in relatives}
        for relative in relatives:
            (Path(first['generationRoot']) / relative).unlink()
        with self.assertRaisesRegex(ValueError, 'integrity'):
            sync_replica(self.config, self.materialize)
        self.assertEqual({relative: (self.shared / relative).read_bytes() for relative in relatives}, original)
        self.assertEqual(self.renders, 1)
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(repaired['sharedMaterialization']['status'], 'current')
        self.assertEqual(self.renders, 2)

    def test_quarantined_cache_is_rebuilt_from_records_on_next_sync(self):
        self.publish('one')
        first = sync_replica(self.config, self.materialize)
        cached = Path(first['generationRoot'])
        (cached / 'wiki/concepts/one.md').unlink()
        recovery = Path(self.config['stateDir']) / 'quarantined-generation'
        shutil.move(cached, recovery)
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(repaired['sharedMaterialization']['status'], 'current')
        self.assertEqual((cached / 'wiki/concepts/one.md').read_text(), 'one')
        self.assertTrue(recovery.is_dir())
        self.assertEqual(self.renders, 2)


if __name__ == '__main__':
    unittest.main()
