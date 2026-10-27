import contextlib
import getpass
import io
import json
import unittest
import warnings
from unittest.mock import Mock, patch

from kulms_submit import KulmsLogin as login
from kulms_submit import KulmsOTP as otp


# Deliberately fake account data; never contact the university from these tests.
USERNAME = 'test-kulms-account'
PASSWORD = ' example-fake-password-with-spaces '


class MemoryBackend:
    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[service, account] = value

    def delete_password(self, service, account):
        del self.items[service, account]


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        for module in (login, otp):
            patcher = patch.object(module, '_os_keyring', return_value=self.backend)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = login.LoginStore()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = login.main(args)
        return result, out.getvalue(), err.getvalue()

    def assert_redacted(self, text):
        self.assertNotIn(USERNAME, text)
        self.assertNotIn(PASSWORD, text)

    def test_store_roundtrip_preserves_password_and_otp(self):
        otp_store = otp.OTPStore()
        otp_config = otp.TOTPConfig('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ')
        otp_store.save(otp_config)
        credentials = login.LoginCredentials('  ' + USERNAME + '  ', PASSWORD)
        self.assert_redacted(repr(credentials))
        self.store.save(credentials)
        self.assertEqual(self.store.load(), credentials)
        self.assertEqual(self.store.load().password, PASSWORD)
        with self.assertRaises(login.LoginError):
            self.store.save(credentials)
        self.store.save(login.LoginCredentials(USERNAME, 'replacement'), replace=True)
        self.assertEqual(self.store.load().password, 'replacement')
        self.assertTrue(self.store.remove())
        self.assertFalse(self.store.remove())
        self.assertEqual(otp_store.load(), otp_config)

    def test_setup_status_and_remove_hide_credentials(self):
        with patch.object(login, '_hidden_input', side_effect=[USERNAME, PASSWORD, PASSWORD]):
            result, out, err = self.run_cli(['setup'])
        self.assertEqual((result, err), (0, ''))
        self.assert_redacted(out)
        result, out, err = self.run_cli(['status', '--json'])
        self.assertEqual((result, err), (0, ''))
        status = json.loads(out)
        self.assertTrue(status['login_configured'])
        self.assertFalse(status['otp_configured'])
        self.assert_redacted(out)
        self.assertEqual(self.run_cli(['remove'])[0], 0)
        self.assertIsNone(self.store.load())

    def test_confirmation_mismatch_cancel_and_no_echo_do_not_save(self):
        for answers in ([USERNAME, PASSWORD, 'mismatch'], [KeyboardInterrupt]):
            with patch.object(login, '_hidden_input', side_effect=answers):
                result, out, err = self.run_cli(['setup'])
            self.assertIn(result, (1, 130))
            self.assertEqual(out, '')
            self.assert_redacted(err)
            self.assertIsNone(self.store.load())
        def fallback(*args):
            warnings.warn('unsafe fallback', getpass.GetPassWarning)
            self.fail('must not fall back to echoed input')
        with patch.object(getpass, 'getpass', side_effect=fallback):
            self.assertEqual(self.run_cli(['setup'])[0], 1)
        self.assertIsNone(self.store.load())

    def test_existing_setup_requires_replace_before_prompt(self):
        self.store.save(login.LoginCredentials(USERNAME, PASSWORD))
        with patch.object(login, '_hidden_input') as prompt:
            self.assertEqual(self.run_cli(['setup'])[0], 1)
            prompt.assert_not_called()
        with patch.object(login, '_hidden_input', side_effect=[USERNAME, PASSWORD, PASSWORD]):
            self.assertEqual(self.run_cli(['setup', '--replace'])[0], 0)

    def test_invalid_inputs_and_corrupt_or_locked_keyring_are_redacted(self):
        for username, password in (('', PASSWORD), ('has space', PASSWORD), (USERNAME, ''),
                                   (USERNAME, 'line\nbreak'), (None, PASSWORD), (USERNAME, None)):
            with self.assertRaises(login.LoginError) as caught:
                login.LoginCredentials(username, password)
            self.assert_redacted(str(caught.exception))
        for value in ('invalid ' + PASSWORD, '[]', '{}', json.dumps({'version': True, 'username': USERNAME, 'password': PASSWORD})):
            self.backend.items[login._SERVICE, login._ACCOUNT] = value
            with self.assertRaises(login.LoginError) as caught:
                self.store.load()
            self.assert_redacted(str(caught.exception))
        self.backend.get_password = Mock(side_effect=RuntimeError(PASSWORD))
        result, out, err = self.run_cli(['status'])
        self.assertEqual((result, out), (1, ''))
        self.assert_redacted(err)

    def test_failed_or_silent_writes_and_deletes_are_detected(self):
        for effect in (RuntimeError(PASSWORD), None):
            self.backend.set_password = Mock(side_effect=effect)
            with self.assertRaises(login.LoginError) as caught:
                self.store.save(login.LoginCredentials(USERNAME, PASSWORD))
            self.assert_redacted(str(caught.exception))
        self.backend.items[login._SERVICE, login._ACCOUNT] = PASSWORD
        self.backend.delete_password = Mock()
        with self.assertRaises(login.LoginError):
            self.store.remove()

    def test_arguments_never_echo_accidentally_supplied_password(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            login.main(['setup', '--password', PASSWORD])
        self.assertEqual(caught.exception.code, 2)
        self.assert_redacted(out.getvalue() + err.getvalue())


if __name__ == '__main__':
    unittest.main()
