"""Exercise selector polling/revision semantics without a browser or live service."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'requires Node for settings JavaScript')
class OutputUiTest(unittest.TestCase):
    def test_polling_preserves_selection_and_its_revision_until_save(self):
        html = (Path(__file__).resolve().parents[1] / 'python' / 'fly_ground_settings.html').read_text()
        render = html.split('function renderOutput(', 1)[1].split('async function api(', 1)[0]
        script = """
const assert = require('node:assert/strict');
const elements = {
 videoOutput: {value:'both',querySelector:()=>({textContent:''})},
 videoOutputState:{textContent:''}
};
const $=id=>elements[id];
const controls=()=>{};
let outputSnapshot=null, outputRevision=null;
""" + 'function renderOutput(' + render + """
renderOutput({mode:'both',revision:'revision-1',transport:'RTP',running:true,applied_mode:'both'});
elements.videoOutput.value='webrtc';
renderOutput({mode:'rpanion',revision:'revision-2',transport:'RTSP',running:true,applied_mode:'rpanion'});
assert.equal(elements.videoOutput.value,'webrtc');
assert.equal(outputRevision,'revision-1','polling silently adopted a concurrent edit revision');
renderOutput({mode:'webrtc',revision:'revision-3',transport:'RTSP',running:true,applied_mode:'webrtc'},true);
assert.equal(elements.videoOutput.value,'webrtc');
assert.equal(outputRevision,'revision-3');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
