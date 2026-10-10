import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from baby_video.credentials import load_credentials, credential_presence, prepare_template, activate_credentials
from baby_video.__main__ import doctor
from baby_video.state import PipelineError


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, text):
        file = self.root / '.env.local'
        file.write_text(text)
        file.chmod(0o600)
        return file

    def test_blank_template_and_existing_input_preserved(self):
        self.assertTrue(prepare_template(self.root))
        self.assertEqual(stat.S_IMODE((self.root / '.env.local').stat().st_mode), 0o600)
        self.assertFalse(any(credential_presence(self.root).values()))
        file = self.write('FAL_KEY=MOCK-preserve-owner-content\nOTHER_FIELD=unchanged\n')
        before = file.read_bytes()
        self.assertFalse(prepare_template(self.root))
        self.assertEqual(file.read_bytes(), before)

    def test_private_read_no_console_values(self):
        self.write('FAL_KEY="MOCK-fal-private-value"\nFISH_API_KEY=MOCK-fish-private-value\n')
        console = io.StringIO()
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            values = load_credentials(self.root)
        self.assertEqual(console.getvalue(), '')
        self.assertEqual(values['FAL_KEY'], 'MOCK-fal-private-value')
        self.assertTrue(all(credential_presence(self.root).values()))

    def test_insecure_permissions_refused(self):
        file = self.write('FAL_KEY=MOCK-private-value\n')
        file.chmod(0o644)
        with self.assertRaises(PipelineError):
            load_credentials(self.root)

    def test_symlink_refused(self):
        (self.root / '.env.local').symlink_to(self.root / 'other-file')
        with self.assertRaises(PipelineError):
            load_credentials(self.root)
        with self.assertRaises(PipelineError):
            prepare_template(self.root)

    def test_invalid_value_not_in_exception(self):
        private = 'MOCK invalid private value'
        self.write('FAL_KEY=' + private + '\n')
        with self.assertRaises(PipelineError) as error:
            load_credentials(self.root)
        self.assertNotIn(private, str(error.exception))

    def test_legacy_json_and_inherited_environment_never_fallback(self):
        legacy = self.root / '.credentials'
        legacy.mkdir()
        (legacy / 'key-environment.json').write_text('not valid JSON; must never be read')
        with patch.dict(os.environ, {'FAL_KEY': 'MOCK-inherited-key', 'FISH_API_KEY': 'MOCK-inherited-fish', 'FISH_AUDIO_API_KEY': 'MOCK-alias'}):
            activate_credentials(self.root)
            self.assertFalse(any(doctor()['credential_environment_present'].values()))
        self.assertEqual(load_credentials(self.root), {})

    def test_doctor_output_contains_presence_only(self):
        secrets = ['MOCK-fal-private-value', 'MOCK-fish-private-value']
        self.write('FAL_KEY=' + secrets[0] + '\nFISH_API_KEY=' + secrets[1] + '\n')
        with patch.dict(os.environ, {}, clear=False):
            activate_credentials(self.root)
            output = json.dumps(doctor())
        for value in secrets:
            self.assertNotIn(value, output)
        self.assertIn('"live_api_called": false', output)


if __name__ == '__main__':
    unittest.main()
