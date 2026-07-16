import hashlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "config-ui"))

from control_api import workspace_fingerprint


@unittest.skipUnless(shutil.which("node"), "Node is required for cross-language hashing")
class WorkspaceFingerprintTests(unittest.TestCase):
    def test_python_and_node_hash_identical_saved_bytes(self):
        fixtures = (
            b'{"value":1.0}\n',
            b'{"value":-0.0}\n',
            b'{"value":1e-7}\n',
            b'{"value":1e20}\n',
            '{"label":"Leeds — café"}\n'.encode(),
        )
        script = (
            "import {createHash} from 'node:crypto';"
            "const chunks=[];"
            "for await (const chunk of process.stdin) chunks.push(chunk);"
            "process.stdout.write(createHash('sha256')"
            ".update(Buffer.concat(chunks)).digest('hex'));"
        )
        for raw in fixtures:
            with self.subTest(raw=raw):
                node_hash = subprocess.run(
                    ["node", "--input-type=module", "-e", script],
                    input=raw,
                    check=True,
                    capture_output=True,
                ).stdout.decode()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), node_hash)
                self.assertEqual(workspace_fingerprint(raw), node_hash)


if __name__ == "__main__":
    unittest.main()
