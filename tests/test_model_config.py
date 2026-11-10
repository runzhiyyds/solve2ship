import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from solve2ship.__main__ import main
from solve2ship.model_config import ProfileError, load_profile, save_profile, validate_profile


def profile():
    return {'base_url': 'https://example.com/v1', 'model': 'user-selected-model', 'api_key': 'unit-test-placeholder'}


class ModelConfigTests(unittest.TestCase):
    def test_status_redacts_secret_even_if_mistyped_into_model_or_url_path(self):
        data=profile(); data['model']=data['api_key'];data['base_url']+='/' + data['api_key']
        self.assertNotIn(data['api_key'],json.dumps(validate_profile(data).status()))


    def test_saved_file_is_owner_only_and_status_never_returns_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.sts/model.json'
            saved = save_profile(path, profile())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_profile(path), saved)
            self.assertNotIn(profile()['api_key'], repr(saved))
            self.assertNotIn(profile()['api_key'], json.dumps(saved.status()))
            self.assertFalse(saved.status()['connection_verified'])
            path.chmod(0o644)
            with self.assertRaises(ProfileError):
                load_profile(path)

    def test_symlink_and_bad_urls_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.write_text('do not overwrite')
            path = Path(directory) / 'link'
            path.symlink_to(target)
            with self.assertRaises(ProfileError):
                save_profile(path, profile())
            with self.assertRaises(OSError):
                load_profile(path)
            self.assertEqual(target.read_text(), 'do not overwrite')
            fifo = Path(directory) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ProfileError):
                load_profile(fifo)
            with self.assertRaises(ProfileError):
                load_profile(Path(directory))
        for url in ['http://remote.example/v1', 'https://user:password@example.com/v1',
                    'https://example.com/v1?api_key=anything', 'https://example.com/#fragment', 'file:///tmp/model']:
            with self.subTest(url=url), self.assertRaises(ProfileError):
                validate_profile(dict(profile(), base_url=url))
        self.assertEqual(validate_profile(dict(profile(), base_url='http://127.0.0.1:11434/v1')).model, 'user-selected-model')

    def test_cli_missing_status_has_no_network_or_config_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch('sys.argv', ['sts', 'model-status']), contextlib.redirect_stdout(io.StringIO()) as output:
                    main()
                self.assertFalse(json.loads(output.getvalue())['configured'])
                self.assertFalse(Path('.sts').exists())
            finally:
                os.chdir(previous)

    def test_configure_cli_refuses_noninteractive_secret_input(self):
        with patch('sys.argv', ['sts', 'configure-model']), patch('sys.stdin.isatty', return_value=False), \
                patch('getpass.getpass') as password, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 1)
            password.assert_not_called()
