"""Safety behaviour of the on-site terminal QR display helper."""
import io
import subprocess
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.show_device_qr import main  # noqa: E402
from core.device_credentials import DeviceCredentialStore, write_device_artifacts  # noqa: E402


class ShowDeviceQRTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.root.chmod(0o700)
        self.store_path = self.root / 'devices.json'
        self.qr_dir = self.root / 'qr'

    def _call(self, label):
        argv = sys.argv
        sys.argv = ['show_device_qr.py', '--label', label,
                    '--store', str(self.store_path), '--qr-dir', str(self.qr_dir)]
        try:
            return main()
        finally:
            sys.argv = argv

    def test_unknown_label_refuses_via_argv(self):
        DeviceCredentialStore(self.store_path).create('LaCasa iPhone 1')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._call('Nope')
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue() + err.getvalue(), '')

    def test_existing_label_renders_a_terminal_qr_without_the_url(self):
        created = DeviceCredentialStore(self.store_path).create('LaCasa iPhone 1')
        url_path, _ = write_device_artifacts(created, 'https://example.test', self.qr_dir)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._call('LaCasa iPhone 1')
        rendered = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn('█', rendered, 'the helper must render a terminal QR block')
        self.assertNotIn('https://', rendered, 'the URL must never print as text')
        self.assertNotIn('#device=', rendered)
        self.assertNotIn(created.secret, rendered)

    def test_missing_artifact_fails_without_printing_the_url(self):
        DeviceCredentialStore(self.store_path).create('LaCasa iPhone 1')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self._call('LaCasa iPhone 1')
        self.assertEqual(code, 2)
        self.assertNotIn('https://', out.getvalue() + err.getvalue())


if __name__ == '__main__':
    unittest.main()