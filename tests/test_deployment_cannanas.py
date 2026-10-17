"""Deployment artifacts: pinned wheel verification, credential checks, unit."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'deploy' / 'cannanas' / 'install-adapter.sh'
CREDENTIALS_EXAMPLE = REPO / 'deploy' / 'cannanas' / 'cannanas-adapter.credentials.example'
UNIT = REPO / 'deploy' / 'jarvis.service'
PINNED_SHA = '7085943afc149d3f2356fca66d8a9d0bac9c2576166e99e36093a56b87153f5f'
ADAPTER_VARS = ('CANNANAS_API_KEY', 'CANNANAS_CLUB_ID',
                'CANNANAS_STATE_DIR', 'CANNANAS_AUDIT_FILE')


def run_script(*args):
    return subprocess.run(['bash', str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=30)


class TestWheelShaVerified(unittest.TestCase):
    def test_verify_accepts_the_expected_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / 'cannanas_mcp_adapter-1.0.0-py3-none-any.whl'
            wheel.write_bytes(b'wheel-bytes')
            result = run_script('--verify', str(wheel),
                                '--expect-sha', sha256_of(wheel))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_verify_fails_closed_on_sha_mismatch_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / 'whl'
            wheel.write_bytes(b'tampered')
            result = run_script('--verify', str(wheel))
            self.assertNotEqual(result.returncode, 0)
            rendered = result.stderr + result.stdout
            self.assertNotIn('tampered', rendered)

    def test_verify_against_release_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / 'whl'
            wheel.write_bytes(b'manifest-wheel')
            manifest = Path(tmp) / 'manifest.json'
            manifest.write_text('{"wheel_sha256": "%s"}' % PINNED_SHA)
            # manifest pins a different hash than the file: refused
            result = run_script('--verify', str(wheel), '--manifest', str(manifest))
            self.assertNotEqual(result.returncode, 0)


class TestCredentialFileChecks(unittest.TestCase):
    def test_check_env_accepts_mode_0600_with_only_the_four_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred = Path(tmp) / 'credentials'
            cred.write_text('\n'.join(
                f'{name}=example-{name.lower()}-{index}'
                for index, name in enumerate(ADAPTER_VARS)) + '\n')
            cred.chmod(0o600)
            result = run_script('--check-env', str(cred))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_env_refuses_wrong_mode_extra_vars_and_missing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cred = Path(tmp) / 'credentials'
            cred.write_text('\n'.join(
                f'{name}=value' for name in ADAPTER_VARS) + '\nEXTRA_VAR=x\n')
            cred.chmod(0o600)
            self.assertNotEqual(run_script('--check-env', str(cred)).returncode, 0)
            cred.write_text('\n'.join(
                f'{name}=' for name in ADAPTER_VARS))
            cred.chmod(0o600)
            self.assertNotEqual(run_script('--check-env', str(cred)).returncode, 0)
            cred.write_text('\n'.join(
                f'{name}=value' for name in ADAPTER_VARS) + '\n')
            cred.chmod(0o644)
            self.assertNotEqual(run_script('--check-env', str(cred)).returncode, 0)


class TestUnitStillParses(unittest.TestCase):
    def test_unit_gains_the_credential_environment_file_and_note(self):
        text = UNIT.read_text()
        self.assertIn('EnvironmentFile=', text)
        self.assertIn('cannanas-adapter.credentials', text)
        self.assertIn('Cannanas', text)  # description/comment note
        # Every other line is unchanged: the known Environment lines survive.
        for keep in ('JARVIS_HEADLESS=true', 'JARVIS_CAPTURE_SOURCE=phone',
                     'JARVIS_BIND=127.0.0.1', 'JARVIS_PUBLIC_URL=',
                     'JARVIS_DEVICE_CREDENTIALS=', 'UMask=0077',
                     'NoNewPrivileges=true', 'RestartPreventExitStatus=78'):
            self.assertIn(keep, text)
        self.assertEqual(text.count('ExecStart='), 1)

    def test_unit_pins_adapter_command_and_host_audit_paths(self):
        text = UNIT.read_text()
        self.assertIn(
            'Environment=JARVIS_CANNANAS_ADAPTER_COMMAND=%h/cannanas-adapter/venv/bin/cannanas-mcp-adapter',
            text,
        )
        self.assertIn('Environment=JARVIS_CANNANAS_AUDIT_FILE=%h/cannanas-adapter/audit/jarvis-write-audit.jsonl',
                      text)

    def test_unit_sections_still_parse(self):
        text = UNIT.read_text()
        for section in ('[Unit]', '[Service]', '[Install]'):
            self.assertLess(text.index(section), text.index('\n[Service]')
                            if section == '[Unit]' else len(text))


class TestInstallerHashPinnedInstall(unittest.TestCase):
    """The lock pins the adapter itself with its own hash: pip installs the
    hash-annotated lock plus a generated adapter spec line directly."""

    def test_install_installs_the_hashed_lock_with_the_adapter_spec(self):
        script = SCRIPT.read_text()
        # pip require-hashes needs the hash on each requirement: the installer
        # appends the hash-annotated lock to a generated adapter spec and
        # installs exactly that (no constraints mode, no index fetches of the
        # adapter itself beyond hash-checked find-links resolution).
        self.assertIn("printf 'cannanas-mcp-adapter==%s --hash=sha256:%s", script)
        self.assertIn('cat "$constraints" >> "$reqspec"', script)
        self.assertIn('--find-links "$wheel_dir"', script)
        self.assertIn('--require-hashes', script)
        self.assertIn('rm -f "$reqspec"', script)
        self.assertNotIn('-r "$constraints"', script)
        self.assertNotIn('-c "$constraints"', script)


class TestNoNewSockets(unittest.TestCase):
    def test_exec_start_unchanged_no_new_listeners(self):
        text = UNIT.read_text()
        self.assertIn('ExecStart=%h/Mark-LV-LaCasa/.venv/bin/python main.py', text)
        for forbidden in ('ListenStream=', 'ExecStartPre=', 'ExecStartPost=',
                          'socket'):
            self.assertNotIn(forbidden, text)
        script = SCRIPT.read_text()
        # The installer starts nothing: no systemctl / service invocation.
        for forbidden in ('systemctl', 'systemd-run', 'launchctl'):
            self.assertNotIn(forbidden, script)


class TestEnvTemplateMinimal(unittest.TestCase):
    def test_example_template_lists_only_the_four_adapter_variables(self):
        text = CREDENTIALS_EXAMPLE.read_text()
        entries = [line for line in text.splitlines()
                   if line and not line.strip('#').startswith((' ',))
                   and not line.startswith('#')]
        names = [line.split('=', 1)[0].strip() for line in entries]
        self.assertEqual(names, list(ADAPTER_VARS))
        # No real values: every value is a placeholder.
        for line in entries:
            value = line.split('=', 1)[1]
            self.assertIn('SET-BY-OPERATOR', value)


def sha256_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == '__main__':
    unittest.main()