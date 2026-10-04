"""Given/When/Then regressions for private generation integrity and recovery.

These fixtures use the real publication, sync, status and consumer paths. Only
the model-free materializer is injected; all files live in disposable roots.
"""

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import test_replica
from common import load_json
from queue_replica import prepare
from replica import publish_record, replica_status, sync_replica
from replica_integrity import read_verified_generation


class ReplicaRecoveryTests(unittest.TestCase):
    """No role may hand a corrupt cached generation to another consumer."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = copy.deepcopy(self.fixture.configs['b'])
        self.config['exchange']['materializerMachineId'] = 'a'
        self.state = Path(self.config['stateDir'])
        self.renders = 0
        self.publish('one')

    def publish(self, name):
        job, result = self.fixture._submission('a', 'Trusted ' + name, 'always', name)
        publish_record(self.fixture.configs['a'], job, result)

    def materialize(self, stage, records):
        self.renders += 1
        for record in records:
            claim = record['payload']['claims'][0]
            (Path(stage) / 'wiki/concepts' / (claim['title'] + '.md')).write_text(claim['text'])
        return {'pages': len(records), 'conflicts': []}

    def materialize_with_embedding(self, stage, records):
        result = self.materialize(stage, records)
        target = Path(stage) / '.llmwiki/embeddings.bin'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'vectors')
        return result

    def damage(self, root):
        (Path(root) / 'wiki/concepts/one.md').write_text('Tampered context')

    def assert_recovery(self):
        first = sync_replica(self.config, self.materialize)
        cached = Path(first['generationRoot'])
        self.damage(cached)
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        failed = replica_status(self.config)
        self.assertTrue(failed['errors'])
        self.assertFalse(failed['current'])
        self.assertIsNone(failed['generationRoot'])
        quarantined = list((self.state / 'replica/quarantine').glob('*/wiki/concepts/one.md'))
        self.assertEqual([path.read_text() for path in quarantined], ['Tampered context'])
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(Path(repaired['generationRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')
        self.assertEqual(self.renders, 2)
        self.assertTrue(quarantined[0].exists())
        self.assertFalse(repaired['errors'])
        audit = {'job': {'id': 'review', 'projectId': 'project'}}
        prepared = prepare(self.config, audit, self.state / 'batches/review.json')
        self.assertEqual(Path(prepared['wikiRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')

    def test_reader_rejects_damaged_current_then_rebuilds_from_publications(self):
        """Given a reader's seal, When a page changes, Then sync fails visibly and rebuilds."""
        self.config['publishEnabled'] = False
        self.assert_recovery()

    def test_last_successful_sync_survives_a_later_failed_sync(self):
        """A later integrity failure changes current health but preserves the last successful timestamp."""
        first = sync_replica(self.config, self.materialize)
        successful_at = first["lastSuccessfulSyncAt"]
        self.assertTrue(successful_at.endswith("Z"))
        self.damage(first["generationRoot"])

        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)

        failed_status = load_json(self.state / "replica/status.json")
        self.assertEqual(failed_status["lastSuccessfulSyncAt"], successful_at)
        self.assertEqual(failed_status["lastError"], "ValueError")

    def test_baseline_consumer_tampering_quarantines_then_rebuilds(self):
        """Given copied baseline inputs, When one changes, Then quarantine and rebuild restore it."""
        first = sync_replica(self.config, self.materialize)
        current = Path(first['generationRoot'])
        target = current / 'sources/notes.txt'
        target.write_text('Tampered baseline', encoding='utf-8')
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        quarantined = list((self.state / 'replica/quarantine').glob('*/sources/notes.txt'))
        self.assertEqual([path.read_text() for path in quarantined], ['Tampered baseline'])
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(Path(repaired['generationRoot'], 'sources/notes.txt').read_text(), 'baseline notes')

    def test_embedding_tampering_quarantines_then_rebuilds(self):
        """Given an embedding store, When bytes change, Then recovery restores it."""
        first = sync_replica(self.config, self.materialize_with_embedding)
        target = Path(first['generationRoot']) / '.llmwiki/embeddings.bin'
        target.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize_with_embedding)
        quarantined = list((self.state / 'replica/quarantine').glob('*/.llmwiki/embeddings.bin'))
        self.assertEqual([path.read_bytes() for path in quarantined], [b'tampered'])
        repaired = sync_replica(self.config, self.materialize_with_embedding)
        self.assertEqual(Path(repaired['generationRoot'], '.llmwiki/embeddings.bin').read_bytes(), b'vectors')

    def test_embedding_deletion_quarantines_then_rebuilds(self):
        """Given an embedding store, When it disappears, Then recovery restores it."""
        first = sync_replica(self.config, self.materialize_with_embedding)
        (Path(first['generationRoot']) / '.llmwiki/embeddings.bin').unlink()
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize_with_embedding)
        repaired = sync_replica(self.config, self.materialize_with_embedding)
        self.assertEqual(Path(repaired['generationRoot'], '.llmwiki/embeddings.bin').read_bytes(), b'vectors')

    def test_embedding_insertion_quarantines_then_rebuilds(self):
        """Given an embedding store, When a file is inserted, Then recovery rebuilds."""
        first = sync_replica(self.config, self.materialize_with_embedding)
        extra = Path(first['generationRoot']) / '.llmwiki/embeddings-extra.bin'
        extra.write_bytes(b'unexpected')
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize_with_embedding)
        quarantined = list((self.state / 'replica/quarantine').glob('*/.llmwiki/embeddings-extra.bin'))
        self.assertEqual([path.read_bytes() for path in quarantined], [b'unexpected'])
        repaired = sync_replica(self.config, self.materialize_with_embedding)
        self.assertFalse(Path(repaired['generationRoot'], '.llmwiki/embeddings-extra.bin').exists())

    def test_non_materializer_rejects_damaged_current_then_rebuilds(self):
        """Given a peer's seal, When a page changes, Then the shared-writer gate cannot bypass validation."""
        self.assert_recovery()

    def test_failed_rebuild_never_restores_corrupt_current(self):
        """Given quarantined damage, When rebuilding fails, Then consumers receive no current."""
        first = sync_replica(self.config, self.materialize)
        self.damage(first['generationRoot'])
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        with self.assertRaisesRegex(RuntimeError, 'worker failed'):
            sync_replica(self.config, lambda *_: (_ for _ in ()).throw(RuntimeError('worker failed')))
        status = replica_status(self.config)
        self.assertFalse(status['current'])
        self.assertIsNone(status['generationRoot'])
        self.assertTrue(status['errors'])
        self.assertEqual(status['lastError'], 'RuntimeError')

    def test_existing_frozen_basis_cannot_bypass_integrity_checks(self):
        """Given a pinned review, When its files change, Then retry rejects them without changing basis."""
        first = sync_replica(self.config, self.materialize)
        frozen = {'basisRecordIds': first['fullyVisibleRecordIds'],
                  'generation': first['digest'], 'wikiRoot': first['generationRoot']}
        audit = {'job': {'id': 'review', 'projectId': 'project'}, 'replicaBasis': frozen.copy()}
        self.damage(first['generationRoot'])
        with self.assertRaises(ValueError):
            prepare(self.config, audit, self.state / 'batches/review.json')
        self.assertEqual(audit['replicaBasis'], frozen)
        self.assertTrue(replica_status(self.config)['errors'])

    def test_bad_historical_cache_preserves_healthy_current_and_rebuilds(self):
        """Given a healthy current and older cache, When the old cache is selected, Then damage cannot replace current."""
        first = sync_replica(self.config, self.materialize)
        worker = self.state / 'worker-version'
        worker.write_text('other version')
        newer = {**self.config, 'worker': str(worker)}
        healthy = sync_replica(newer, self.materialize)
        self.damage(first['generationRoot'])
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        self.assertEqual(Path(self.config['wikiRoot']).resolve(), Path(healthy['generationRoot']))
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(repaired['rollbackRoot'], healthy['generationRoot'])
        self.assertEqual(Path(repaired['generationRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')

    def test_rebuilt_current_keeps_healthy_rollback_and_frozen_basis(self):
        """Given a rollback and pinned current, When current is repaired, Then both history and basis survive."""
        rollback = sync_replica(self.config, self.materialize)
        self.publish('two')
        first = sync_replica(self.config, self.materialize)
        audit = {'job': {'id': 'review', 'projectId': 'project'}}
        prepare(self.config, audit, self.state / 'batches/review.json')
        basis = copy.deepcopy(audit['replicaBasis'])
        self.damage(first['generationRoot'])
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(repaired['rollbackRoot'], rollback['generationRoot'])
        self.assertTrue(Path(rollback['generationRoot']).is_dir())
        prepared = prepare(self.config, audit, self.state / 'batches/review.json')
        self.assertEqual(audit['replicaBasis'], basis)
        self.assertEqual(Path(prepared['wikiRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')

    def test_verified_response_is_consumed_without_reopening_metadata(self):
        """Given verified metadata, When the file changes after validation, Then only the verified response is used."""
        sync_replica(self.config, self.materialize)
        def change_after_verification(generation):
            snapshot = read_verified_generation(generation)
            (generation / '.llmwiki/replica-response.json').write_text('{"pages": 999, "conflicts": []}')
            return snapshot
        with patch('replica_recovery.read_verified_generation', side_effect=change_after_verification):
            status = sync_replica(self.config, self.materialize)
        self.assertEqual(status['pages'], 1)

    def test_frozen_generation_identity_must_match_its_directory(self):
        """Given a valid directory, When an audit names a different identity, Then it cannot supply context."""
        first = sync_replica(self.config, self.materialize)
        audit = {'job': {'id': 'review', 'projectId': 'project'},
                 'replicaBasis': {'basisRecordIds': [], 'generation': 'f' * 64,
                                  'wikiRoot': first['generationRoot']}}
        with self.assertRaises(ValueError):
            prepare(self.config, audit, self.state / 'batches/review.json')

    def test_corrupt_error_record_cannot_block_a_trusted_rebuild(self):
        """Given a damaged diagnostic, When rebuilding succeeds, Then healthy context and visible diagnostics coexist."""
        first = sync_replica(self.config, self.materialize)
        self.damage(first['generationRoot'])
        with self.assertRaises(ValueError):
            sync_replica(self.config, self.materialize)
        diagnostic = self.state / 'replica-errors/generation-integrity.json'
        diagnostic.write_text('{')
        for _ in range(2):
            repaired = sync_replica(self.config, self.materialize)
            self.assertEqual(Path(repaired['generationRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')
            self.assertTrue(any(item.get('operation') == 'generation-integrity-metadata' for item in repaired['errors']))
        self.assertEqual(diagnostic.read_text(), '{')
        self.assertEqual(self.renders, 2)

    def test_historical_symlink_cannot_revoke_a_different_healthy_current(self):
        """Given current B, When pinned historical A becomes a link to B, Then only A is quarantined."""
        first = sync_replica(self.config, self.materialize)
        worker = self.state / 'worker-version'
        worker.write_text('other version')
        healthy = sync_replica({**self.config, 'worker': str(worker)}, self.materialize)
        historical = Path(first['generationRoot'])
        historical.rename(self.state / 'original-historical')
        historical.symlink_to(healthy['generationRoot'], target_is_directory=True)
        audit = {'job': {'id': 'review', 'projectId': 'project'},
                 'replicaBasis': {'basisRecordIds': first['fullyVisibleRecordIds'],
                                  'generation': first['digest'], 'wikiRoot': str(historical)}}
        with self.assertRaises(ValueError):
            prepare(self.config, audit, self.state / 'batches/review.json')
        current = Path(self.config['wikiRoot'])
        self.assertTrue(current.is_symlink())
        self.assertEqual(current.resolve(), Path(healthy['generationRoot']))
        self.assertTrue(Path(healthy['generationRoot']).is_dir())

    def test_historical_symlink_to_outside_is_quarantined_then_rebuilt(self):
        """Given an unsafe cached link, When sync selects it, Then the link is preserved and its outside target untouched."""
        first = sync_replica(self.config, self.materialize)
        worker = self.state / 'worker-version'
        worker.write_text('other version')
        healthy = sync_replica({**self.config, 'worker': str(worker)}, self.materialize)
        historical = Path(first['generationRoot'])
        historical.rename(self.state / 'original-historical')
        outside = self.state / 'outside'
        outside.mkdir()
        (outside / 'marker').write_text('Unrelated data')
        historical.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            sync_replica(self.config, self.materialize)
        self.assertTrue(replica_status(self.config)['errors'])
        self.assertFalse(historical.is_symlink())
        self.assertEqual(Path(self.config['wikiRoot']).resolve(), Path(healthy['generationRoot']))
        repaired = sync_replica(self.config, self.materialize)
        self.assertEqual(repaired['rollbackRoot'], healthy['generationRoot'])
        self.assertEqual(Path(repaired['generationRoot'], 'wiki/concepts/one.md').read_text(), 'Trusted one')
        self.assertEqual((outside / 'marker').read_text(), 'Unrelated data')


if __name__ == '__main__':
    unittest.main()
