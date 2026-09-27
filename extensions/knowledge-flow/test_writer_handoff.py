"""Exercise observable two-machine writing through real temporary files.

The two exchange directories deliberately receive files only when a test
copies them, so silence and out-of-order iCloud delivery are not locks.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from replica_integrity import seal_generation
from shared_materialize import promote_generation


class WriterHandoffTests(unittest.TestCase):
    """A write grant must survive retries and never overlap the previous owner."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shared = self.root / 'shared'
        self.baseline = {'snapshotId': 'b' * 64, 'files': [
            {'path': 'wiki/MOC.md', 'text': 'baseline'},
            {'path': 'wiki/index.md', 'text': 'baseline'},
            {'path': 'sources/base.md', 'text': 'evidence'}]}
        for item in self.baseline['files']:
            self.write(self.shared / item['path'], item['text'])
        self.configs = {m: self.config(m) for m in ('peer-a', 'peer')}
        self.generations = {m: self.generation(m, 'initial') for m in self.configs}
        self.legacy = copy.deepcopy(self.configs['peer'])
        self.legacy['exchange'].pop('sharedWriter')
        self.legacy['exchange']['materializerMachineId'] = 'peer'
        promote_generation(self.legacy, str(self.generations['peer']), self.baseline)
        from exchange import announce_machine
        for machine, config in self.configs.items():
            self.write(Path(config['worker']).parent.parent / 'build-manifest.json',
                       json.dumps({'commit': 'a' * 40}))
            announce_machine(config, '2026-09-18T00:00:00Z')
        self.sync_files('peer-a', 'peer')
        self.sync_files('peer', 'peer-a')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def config(self, machine):
        return {'machineId': machine, 'enabled': True, 'publishEnabled': True,
                'worker': str(self.root / machine / 'runtime/knowledge-flow/worker.mjs'),
                'stateDir': str(self.root / machine / 'state'),
                'sharedWikiRoot': str(self.shared),
                '_sharedWriterInputs': {'recordIds': [], 'routesHash': None},
                'exchange': {'root': str(self.root / machine / 'exchange'),
                             'protocolVersion': 2, 'participants': ['peer-a', 'peer'],
                             'materializerMachineId': 'peer-a',
                             'sharedWriter': {'version': 1, 'bootstrapMachineId': 'peer'}}}

    def generation(self, machine, label):
        root = self.root / machine / ('generation-' + label)
        for item in self.baseline['files']:
            self.write(root / item['path'], item['text'])
        self.write(root / 'wiki/concepts/decision.md', label)
        self.write(root / 'wiki/MOC.md', label)
        self.write(root / '.llmwiki/replica-response.json', '{"pages":1,"conflicts":[]}')
        seal_generation(root, root.name)
        return root

    def sync_files(self, sender, receiver):
        source = Path(self.configs[sender]['exchange']['root'])
        target = Path(self.configs[receiver]['exchange']['root'])
        if source.exists():
            shutil.copytree(source, target, dirs_exist_ok=True)

    def run_host(self, machine):
        return promote_generation(self.configs[machine], str(self.generations[machine]), self.baseline)

    def activate(self):
        self.bootstrap(apply=True)
        self.sync_files('peer', 'peer-a')
        self.assertEqual(self.run_host('peer-a')['status'], 'current')

    def bootstrap(self, apply=False):
        """Use the same activation flag and shared lock as normal maintenance."""
        config = {**self.configs['peer'], '_sharedWriterBootstrap': apply}
        return promote_generation(config, str(self.generations['peer']), self.baseline)

    def request(self, request_id='test-one'):
        from writer_handoff import request_write
        self.sync_files('peer-a', 'peer')
        return request_write(self.configs['peer'], request_id)

    def contents(self):
        return (self.shared / 'wiki/concepts/decision.md').read_text()

    def test_opt_in_waits_for_explicit_bootstrap(self):
        """Given no release receipt, both background workers leave shared pages intact."""
        for machine in self.configs:
            self.assertEqual(self.run_host(machine)['status'], 'awaiting-handoff-bootstrap')
        self.assertEqual(self.contents(), 'initial')

    def test_new_default_automatically_writes_and_peer_stays_a_worker(self):
        """Given the old owner released ownership, peer-a resumes normal automatic updates."""
        self.activate()
        self.generations['peer-a'] = self.generation('peer-a', 'automatic')
        self.assertEqual(self.run_host('peer-a')['status'], 'current')
        self.assertEqual(self.run_host('peer')['status'], 'not-materializer')
        self.assertEqual(self.contents(), 'automatic')

    def test_request_waits_for_ack_then_writes_once_and_returns(self):
        """Given an explicit request, only a delivered release allows one update."""
        self.complete_roundtrip()

    def complete_roundtrip(self):
        """Exercise one full explicit request through the public writer boundary."""
        self.activate()
        self.request()
        self.generations['peer'] = self.generation('peer', 'requested')
        self.assertEqual(self.run_host('peer')['status'], 'not-materializer')
        self.sync_files('peer', 'peer-a')
        self.assertEqual(self.run_host('peer-a')['status'], 'handed-off')
        self.assertEqual(self.run_host('peer')['status'], 'not-materializer')
        self.sync_files('peer-a', 'peer')
        self.assertEqual(self.run_host('peer')['status'], 'returned-to-default')
        self.assertEqual(self.contents(), 'requested')
        self.generations['peer-a'] = self.generation('peer-a', 'requested')
        self.sync_files('peer', 'peer-a')
        self.assertEqual(self.run_host('peer-a')['written'], 0)
        self.assertEqual(self.run_host('peer')['status'], 'not-materializer')

    def test_missing_return_and_stale_exchange_do_not_resume_default(self):
        """Given a durable release, deleting its visible copy never restores authority."""
        self.activate()
        self.request()
        before = self.root / 'stale-exchange'
        shutil.copytree(Path(self.configs['peer-a']['exchange']['root']), before)
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        exchange = Path(self.configs['peer-a']['exchange']['root'])
        shutil.rmtree(exchange)
        shutil.copytree(before, exchange)
        self.generations['peer-a'] = self.generation('peer-a', 'must-not-write')
        with self.assertRaisesRegex(ValueError, 'shared writer release'):
            self.run_host('peer-a')
        self.assertEqual(self.contents(), 'initial')

    def test_human_change_after_grant_is_preserved(self):
        """Given changed shared bytes after release, adoption refuses to overwrite them."""
        self.activate()
        self.request()
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        self.sync_files('peer-a', 'peer')
        self.write(self.shared / 'wiki/concepts/decision.md', 'human decision')
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.run_host('peer')
        self.assertEqual(self.contents(), 'human decision')

    def test_missing_knowledge_inputs_cannot_overwrite_newer_pages(self):
        """Given a receipt before its publication, the borrower waits for that input."""
        self.activate()
        self.configs['peer-a']['_sharedWriterInputs']['recordIds'] = ['a' * 64]
        self.request()
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        self.sync_files('peer-a', 'peer')
        with self.assertRaisesRegex(ValueError, 'inputs'):
            self.run_host('peer')
        self.assertEqual(self.contents(), 'initial')

    def test_same_explicit_request_cannot_be_replayed(self):
        """Given a finished request, repeating its id never grants another write."""
        self.complete_roundtrip()
        self.request()
        self.sync_files('peer', 'peer-a')
        self.assertEqual(self.run_host('peer-a')['status'], 'current')
        self.generations['peer'] = self.generation('peer', 'replayed')
        self.assertEqual(self.run_host('peer')['status'], 'not-materializer')
        self.assertEqual(self.contents(), 'requested')

    def test_release_is_durable_before_icloud_publication(self):
        """Given interruption after publishing a grant, stale delivery cannot restore writing."""
        import writer_records
        self.activate()
        self.request()
        self.sync_files('peer', 'peer-a')
        exchange = Path(self.configs['peer-a']['exchange']['root'])
        before = self.root / 'before-grant'
        shutil.copytree(exchange, before)
        original = writer_records.write

        def lose_response(config, path, value):
            original(config, path, value)
            if value['kind'] == 'release':
                raise OSError('stopped after publication')

        with patch.object(writer_records, 'write', lose_response):
            with self.assertRaises(OSError):
                self.run_host('peer-a')
        self.sync_files('peer-a', 'peer')
        shutil.rmtree(exchange)
        shutil.copytree(before, exchange)
        self.generations['peer-a'] = self.generation('peer-a', 'must-not-write')
        self.assertEqual(self.run_host('peer-a')['status'], 'not-materializer')
        self.assertEqual(self.contents(), 'initial')

    def test_old_runtime_announcement_prevents_activation(self):
        """Given a participant without coordinated runtime readiness, activation changes no page."""
        path = Path(self.configs['peer']['exchange']['root']) / 'machines/peer-a.json'
        value = json.loads(path.read_text())
        value.pop('sharedWriter')
        path.write_text(json.dumps(value))
        self.generations['peer'] = self.generation('peer', 'must-not-write')
        with self.assertRaisesRegex(ValueError, 'upgrade-ready'):
            self.bootstrap(apply=True)
        self.assertEqual(self.contents(), 'initial')

    def test_grant_before_page_download_waits_for_matching_bytes(self):
        """Given separate vault copies, a received grant cannot stand in for downloaded pages."""
        self.activate()
        peer_vault = self.root / 'peer-vault'
        shutil.copytree(self.shared, peer_vault)
        self.configs['peer']['sharedWikiRoot'] = str(peer_vault)
        self.generations['peer-a'] = self.generation('peer-a', 'latest')
        self.generations['peer'] = self.generation('peer', 'latest')
        self.request()
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        self.sync_files('peer-a', 'peer')
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.run_host('peer')
        self.assertEqual((peer_vault / 'wiki/concepts/decision.md').read_text(), 'initial')
        shutil.copytree(self.shared, peer_vault, dirs_exist_ok=True)
        self.assertEqual(self.run_host('peer')['status'], 'returned-to-default')

    def test_partial_borrowed_write_recovers_before_return(self):
        """Given an interrupted page update, the old owner stays stopped until repair completes."""
        from shared_files import SharedFiles
        self.activate()
        self.request()
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        self.sync_files('peer-a', 'peer')
        self.generations['peer'] = self.generation('peer', 'recovered')
        original = SharedFiles.update

        def fail_page(files, relative, *args):
            if relative == 'wiki/concepts/decision.md':
                raise OSError('interrupted page update')
            return original(files, relative, *args)

        with patch.object(SharedFiles, 'update', fail_page):
            with self.assertRaises(OSError):
                self.run_host('peer')
        self.assertEqual(self.run_host('peer-a')['status'], 'not-materializer')
        self.assertEqual(self.run_host('peer')['status'], 'returned-to-default')
        self.assertEqual(self.contents(), 'recovered')

    def test_disabling_coordination_cannot_restore_legacy_authority(self):
        """Given installed coordination, removing its config never re-enables an old writer."""
        self.activate()
        self.configs['peer-a']['exchange'].pop('sharedWriter')
        with self.assertRaisesRegex(ValueError, 'cannot be disabled'):
            self.run_host('peer-a')

    def test_bootstrap_dry_run_does_not_publish_or_persist_permission(self):
        """Given an upgrade preview, neither shared files nor private permission change."""
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.bootstrap()['status'], 'dry-run')
        after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_incompatible_request_keeps_default_automation_running(self):
        """Given a peer awaiting upgrade, its request cannot stop automatic default updates."""
        self.activate()
        self.request()
        self.sync_files('peer', 'peer-a')
        path = Path(self.configs['peer-a']['exchange']['root']) / 'machines/peer.json'
        value = json.loads(path.read_text())
        value['runtimeCommit'] = 'c' * 40
        path.write_text(json.dumps(value))
        self.generations['peer-a'] = self.generation('peer-a', 'still-automatic')
        result = self.run_host('peer-a')
        self.assertEqual(result['status'], 'current')
        self.assertIn('upgrade-ready', result['handoffWaiting'])
        self.assertEqual(self.contents(), 'still-automatic')

    def test_receipt_fork_stops_both_writers(self):
        """Given two conflicting grants from one parent, neither writer chooses a winner."""
        import writer_records
        self.activate()
        self.request()
        self.request('test-two')
        self.sync_files('peer', 'peer-a')
        self.run_host('peer-a')
        chain, pending = writer_records.history(self.configs['peer-a'])
        grant = chain[-1]
        fork = writer_records.sealed({**grant, 'requestId': next(iter(pending))})
        writer_records.write(self.configs['peer-a'], writer_records.path_for(fork), fork)
        self.sync_files('peer-a', 'peer')
        for machine in self.configs:
            with self.assertRaisesRegex(ValueError, 'fork'):
                self.run_host(machine)
        self.assertEqual(self.contents(), 'initial')

    def test_pending_request_cannot_transfer_to_a_disabled_publisher(self):
        """Given a borrower demoted after requesting, default updates continue without granting it ownership."""
        from exchange import announce_machine
        self.activate()
        self.request()
        self.configs['peer']['publishEnabled'] = False
        announce_machine(self.configs['peer'], '2026-09-18T00:01:00Z')
        self.sync_files('peer', 'peer-a')
        self.generations['peer-a'] = self.generation('peer-a', 'still-publishing')
        result = self.run_host('peer-a')
        self.assertEqual(result['status'], 'current')
        self.assertIn('upgrade-ready', result['handoffWaiting'])
        self.assertEqual(self.contents(), 'still-publishing')

    def test_oversized_checkpoint_preserves_the_current_writer(self):
        """Given an unsendable grant, the current writer retains ownership and can retry."""
        import writer_records
        from writer_handoff import status
        self.activate()
        self.request()
        self.sync_files('peer', 'peer-a')
        generation = self.generation('peer-a', 'large')
        for number in range(60):
            self.write(generation / 'wiki/concepts' / (('topic-' * 8) + str(number) + '.md'), 'knowledge')
        seal_generation(generation, generation.name)
        self.generations['peer-a'] = generation
        with patch.object(writer_records, 'MAX_RECORD_BYTES', 3000):
            with self.assertRaisesRegex(ValueError, 'invalid shared writer record'):
                self.run_host('peer-a')
            self.assertEqual(status(self.configs['peer-a'])['ownerMachineId'], 'peer-a')
        self.generations['peer-a'] = self.generation('peer-a', 'retry')
        self.assertEqual(self.run_host('peer-a')['status'], 'handed-off')
        self.assertEqual(self.contents(), 'retry')


if __name__ == '__main__':
    unittest.main()
