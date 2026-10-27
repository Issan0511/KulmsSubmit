import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kulms_submit.KulmsSources import read_notes, note_digest, render_note, export_command
from kulms_submit.KulmsClient import KulmsError

class NotebookContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.note = dict(id='note1', title='Fluid Mechanics', pages=[dict(id='page1', strokes=[], texts=[], paper='plain', aspect=.75)])
        self.raw = json.dumps(self.note).encode(); self.checksum = hashlib.sha256(self.raw).hexdigest()
        self.folder = self.root/'courses/mechanics/note1'; self.folder.mkdir(parents=True)
        (self.folder/'note.json').write_bytes(self.raw)
        (self.folder/'.history').mkdir(); (self.folder/'.history'/(self.checksum+'.json')).write_bytes(self.raw)
        self.index = dict(version=2, activeId='note1', notes=[dict(id='note1', path='courses/mechanics/note1/note.json', sha256=self.checksum)])
        self.write_index()

    def write_index(self):
        (self.root/'index.json').write_text(json.dumps(self.index))

    def test_versions_and_uncommitted_head_use_same_content_hash(self):
        legacy = self.root/'legacy.json'; legacy.write_text(json.dumps(dict(version=1, notes=[self.note])))
        self.assertEqual(note_digest(read_notes(legacy)[1][0]),note_digest(read_notes(self.root)[1][0]))
        (self.folder/'note.json').write_text('{}')
        self.assertEqual(read_notes(self.root)[1],[self.note])

    def test_traversal_and_corrupt_revisions_are_rejected(self):
        self.index['notes'][0]['path']='../../outside/note.json';self.write_index()
        with self.assertRaises(KulmsError): read_notes(self.root)
        self.index['notes'][0]['path']='courses/mechanics/note1/note.json';self.write_index()
        (self.folder/'note.json').write_text('{}');(self.folder/'.history'/(self.checksum+'.json')).write_text('{}')
        with self.assertRaises(KulmsError): read_notes(self.root)

    def test_changed_legacy_source_blocks_stale_snapshot(self):
        legacy=self.root/'legacy.json';legacy.write_bytes(b'old')
        self.index['migration']=dict(source=str(legacy),sha256=hashlib.sha256(b'old').hexdigest());self.write_index()
        legacy.write_bytes(b'edited')
        with self.assertRaises(KulmsError): read_notes(self.root)

    def test_exporter_is_explicit_and_failure_preserves_existing_output(self):
        with patch.dict('os.environ',{},clear=True),patch('shutil.which',return_value=None):
            with self.assertRaises(KulmsError): export_command()
        output=self.root/'answer.pdf';output.write_bytes(b'existing')
        with patch('kulms_submit.KulmsSources.export_command',return_value=['invalid-exporter']),patch('kulms_submit.KulmsSources.subprocess.run') as run:
            run.return_value.returncode=1
            with self.assertRaises(KulmsError): render_note(self.folder/'note.json',output)
        self.assertEqual(output.read_bytes(),b'existing')
