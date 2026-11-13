import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

class OutputTest(unittest.TestCase):
    def setUp(self):
        from video_output import VideoOutputStore, read_output_mode, Conflict
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'output.json'
        self.store = VideoOutputStore(self.path)
        self.read_mode, self.conflict = read_output_mode, Conflict

    def test_missing_defaults_both_and_atomic_readable_save(self):
        initial = self.store.read()
        self.assertEqual(initial['mode'], 'both')
        self.assertEqual(self.read_mode(self.path), 'both')
        saved = self.store.save('rpanion', initial['revision'])
        self.assertEqual(saved, self.store.read())
        self.assertEqual(self.read_mode(self.path), 'rpanion')
        self.assertEqual(json.loads(self.path.read_text()), {'mode': 'rpanion'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertNotEqual(initial['revision'], saved['revision'])
        with self.assertRaises(self.conflict): self.store.save('webrtc', initial['revision'])
        self.assertEqual(self.read_mode(self.path), 'rpanion')

    def test_strict_validation_and_malformed_input(self):
        initial = self.store.read()
        for mode in ('RTP', '', None, [], True):
            with self.assertRaises(ValueError): self.store.save(mode, initial['revision'])
        self.assertFalse(self.path.exists())
        for raw in ('{', '{}', '[]', '{"mode":"bad"}', '{"mode":"both","extra":1}'):
            self.path.write_text(raw)
            with self.assertRaises(ValueError): self.read_mode(self.path)
            with self.assertRaises(ValueError): self.store.read()

    def test_all_modes_and_stale_external_write(self):
        for mode in ('webrtc', 'rpanion', 'both'):
            current = self.store.read()
            self.store.save(mode, current['revision'])
            self.assertEqual(self.read_mode(self.path), mode)
        old = self.store.read()
        self.path.write_text('{"mode":"webrtc"}')
        with self.assertRaises(self.conflict): self.store.save('rpanion', old['revision'])

if __name__ == '__main__': unittest.main()
