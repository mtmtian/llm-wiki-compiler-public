"""Regression tests for continuous, single-writer Obsidian projection.

All roots are disposable. Tests exercise ownership across generations, peer and
reader isolation, human edit protection, and durable recovery without models.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_materialize import MANIFEST, PENDING, promote_generation
from shared_files import SharedFiles
from replica_integrity import seal_generation


class SharedMaterializeTests(unittest.TestCase):
    """Keep shared projection lifecycle checks separate from record transport."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shared, self.generation = self.root / 'shared', self.root / 'generation'
        self.config = {'machineId': 'a', 'enabled': True, 'publishEnabled': True,
                       'stateDir': str(self.root / 'state'), 'sharedWikiRoot': str(self.shared),
                       'exchange': {'materializerMachineId': 'a', 'participants': ['a', 'b']}}
        self.baseline = {'snapshotId': 'b' * 64, 'files': [
            {'path': 'wiki/MOC.md', 'text': 'Original MOC'},
            {'path': 'wiki/index.md', 'text': 'Original index'},
            {'path': 'wiki/concepts/base.md', 'text': 'Human baseline'},
            {'path': 'sources/base.md', 'text': 'Original evidence'}]}
        for item in self.baseline['files']:
            for root in (self.shared, self.generation):
                self.write(root, item['path'], item['text'])
        self.new_generation('one')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def new_generation(self, label):
        self.write(self.generation, 'wiki/MOC.md', 'Generated MOC ' + label)
        self.write(self.generation, 'wiki/index.md', 'Generated index ' + label)
        self.write(self.generation, 'wiki/concepts/page-' + label + '.md', 'Decision ' + label)
        self.write(self.generation, 'sources/evidence-' + label + '.md', 'Evidence ' + label)
        self.seal()

    def seal(self):
        self.write(self.generation, '.llmwiki/replica-response.json', '{"pages": 1, "conflicts": []}')
        seal_generation(self.generation, self.generation.name)

    def promote(self, config=None):
        return promote_generation(config or self.config, str(self.generation), self.baseline)

    def test_retry_next_generation_and_peer_do_not_conflict(self):
        self.assertEqual(self.promote()['status'], 'current')
        self.assertEqual(self.promote()['written'], 0)
        peer = copy.deepcopy(self.config)
        peer.update(machineId='b', stateDir=str(self.root / 'peer'))
        self.assertEqual(self.promote(peer)['status'], 'not-materializer')
        self.new_generation('two')
        self.assertEqual(self.promote()['status'], 'current')
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Generated MOC two')
        self.assertEqual((self.shared / 'wiki/concepts/base.md').read_text(), 'Human baseline')
        self.assertFalse((self.root / 'peer').exists())

    def test_later_conflict_retracts_owned_page_and_source(self):
        self.promote()
        for relative in ('wiki/concepts/page-one.md', 'sources/evidence-one.md'):
            (self.generation / relative).unlink()
        self.write(self.generation, 'wiki/MOC.md', 'No accepted decisions')
        self.write(self.generation, 'wiki/index.md', 'No accepted decisions')
        self.seal()
        result = self.promote()
        self.assertEqual(result['removed'], 2)
        self.assertFalse((self.shared / 'wiki/concepts/page-one.md').exists())
        self.assertFalse((self.shared / 'sources/evidence-one.md').exists())
        backups = [(self.shared / relative).read_text() for relative in result['backups']]
        self.assertIn('Decision one', backups)
        self.assertIn('Evidence one', backups)

    def test_human_navigation_conflict_is_detected_before_any_writes(self):
        self.promote()
        self.write(self.shared, 'wiki/MOC.md', 'Human navigation')
        self.new_generation('two')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.promote()
        self.assertFalse((self.shared / 'sources/evidence-two.md').exists())
        self.assertFalse((self.shared / 'wiki/concepts/page-two.md').exists())
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Human navigation')
        self.assertFalse((self.root / 'state' / PENDING).exists())

    def test_human_edited_page_is_not_deleted_when_record_becomes_conflicted(self):
        self.promote()
        self.write(self.shared, 'wiki/concepts/page-one.md', 'Human annotation')
        (self.generation / 'wiki/concepts/page-one.md').unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.promote()
        self.assertEqual((self.shared / 'wiki/concepts/page-one.md').read_text(), 'Human annotation')

    def test_reader_paused_and_unconfigured_machines_never_write(self):
        for changes, expected in (({'publishEnabled': False}, 'paused'),
                                  ({'enabled': False}, 'paused'),
                                  ({'exchange': {}}, 'disabled')):
            with self.subTest(changes=changes):
                config = {**self.config, **changes}
                self.assertEqual(self.promote(config)['status'], expected)
                self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Original MOC')
                self.assertFalse((self.root / 'state').exists())

    def test_interrupted_plan_resumes_without_losing_ownership(self):
        self.promote()
        self.new_generation('two')
        original, calls = SharedFiles.update, []
        def fail_third(files, *args):
            calls.append(args[0])
            if len(calls) == 3:
                raise OSError('interrupted')
            return original(files, *args)
        with patch.object(SharedFiles, 'update', fail_third):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.promote()
        self.assertTrue((self.root / 'state' / PENDING).exists())
        self.assertEqual(self.promote()['status'], 'current')
        self.assertFalse((self.root / 'state' / PENDING).exists())
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Generated MOC two')
        self.assertEqual(self.promote()['written'], 0)

    def test_recovery_rejects_human_edit_to_a_partially_written_file(self):
        original, calls = SharedFiles.update, []
        def fail_second(files, *args):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('interrupted')
            return original(files, *args)
        with patch.object(SharedFiles, 'update', fail_second):
            with self.assertRaises(OSError):
                self.promote()
        self.write(self.shared, calls[0], 'Concurrent human edit')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.promote()
        self.assertEqual((self.shared / calls[0]).read_text(), 'Concurrent human edit')
        self.assertEqual((self.shared / 'wiki/MOC.md').read_text(), 'Original MOC')

    def test_foreign_manifest_cannot_adopt_shared_files(self):
        self.promote()
        path = self.root / 'state' / MANIFEST
        manifest = json.loads(path.read_text())
        manifest['scope']['machineId'] = 'other'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.promote()

    def test_untracked_legacy_record_is_not_silently_left_visible(self):
        relative = 'wiki/concepts/project-record-' + 'a' * 32 + '-0.md'
        self.write(self.shared, relative, 'Untracked earlier publication')
        with self.assertRaisesRegex(ValueError, 'requires migration'):
            self.promote()
        self.assertEqual((self.shared / relative).read_text(), 'Untracked earlier publication')
        self.assertFalse((self.shared / 'sources/evidence-one.md').exists())

    def test_identical_legacy_files_are_not_automatically_adopted(self):
        for relative in ('wiki/concepts/project-record-' + 'a' * 32 + '-0.md',
                         'sources/knowledge-flow-old.md', 'wiki/concepts/page-one.md', 'wiki/MOC.md'):
            with self.subTest(relative=relative):
                self.write(self.shared, relative, 'Same prior output')
                self.write(self.generation, relative, 'Same prior output')
                self.seal()
                with self.assertRaisesRegex(ValueError, 'requires migration'):
                    self.promote()
                self.assertFalse((self.root / 'state' / MANIFEST).exists())
                self.assertEqual((self.shared / relative).read_text(), 'Same prior output')
                (self.shared / relative).unlink()

    def test_missing_cached_files_never_retract_shared_pages(self):
        self.promote()
        relatives = ('wiki/concepts/page-one.md', 'sources/evidence-one.md', 'wiki/MOC.md', 'wiki/index.md')
        contents = {relative: (self.shared / relative).read_bytes() for relative in relatives}
        for relative in relatives:
            (self.generation / relative).unlink()
        with self.assertRaises(ValueError):
            self.promote()
        self.assertEqual({relative: (self.shared / relative).read_bytes() for relative in relatives}, contents)


if __name__ == '__main__':
    unittest.main()
