import contextlib
import getpass
import io
import json
import sys
import types
import unittest
import warnings
from unittest.mock import Mock, patch

from kulms_submit import KulmsOTP as otp
from kulms_submit import KulmsOTPCLI as cli


# Published RFC 6238 fixture, never an actual account credential.
SEED = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


class MemoryBackend:
    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[service, account] = value

    def delete_password(self, service, account):
        del self.items[service, account]


class CurrentCodeTests(unittest.TestCase):
    def test_expiring_code_waits_and_rechecks_time(self):
        sleeper = Mock()
        result = otp.current_code(otp.TOTPConfig(SEED), clock=Mock(side_effect=[59, 60.25]), sleep=sleeper)
        sleeper.assert_called_once_with(1)
        self.assertEqual(result, {'code': otp.generate_totp(otp.TOTPConfig(SEED), 60.25),
                                  'valid_until': 90, 'remaining_seconds': 29.75})

    def test_fresh_or_no_wait_code_uses_one_timestamp(self):
        for now, minimum in ((30, 5), (59.5, 0), (55, 5)):
            with self.subTest(now=now):
                sleeper, clock = Mock(), Mock(return_value=now)
                result = otp.current_code(otp.TOTPConfig(SEED), minimum, clock=clock, sleep=sleeper)
                sleeper.assert_not_called()
                clock.assert_called_once_with()
                self.assertEqual(result['valid_until'], 60)
                self.assertEqual(result['remaining_seconds'], 60 - now)

    def test_invalid_minimum_or_unstable_clock_does_not_loop_forever(self):
        for minimum in (-1, 30, 31, float('inf'), float('nan'), True, '5'):
            with self.subTest(minimum=minimum), self.assertRaises(otp.OTPError):
                otp.current_code(otp.TOTPConfig(SEED), minimum)
        sleeper = Mock()
        with self.assertRaises(otp.OTPError):
            otp.current_code(otp.TOTPConfig(SEED), clock=lambda: 59, sleep=sleeper)
        self.assertEqual(sleeper.call_count, 2)

    def test_default_validity_supports_short_periods(self):
        for period in (1, 2, 5, 10, 30):
            config = otp.TOTPConfig(SEED, period=period)
            with self.subTest(period=period):
                result = otp.current_code(config, clock=lambda: 60)
                self.assertEqual(result['remaining_seconds'], period)

    def test_wait_does_not_skip_a_narrow_valid_window(self):
        now = [29.99]
        def advance(seconds):
            now[0] += seconds
        result = otp.current_code(otp.TOTPConfig(SEED), 29.99, clock=lambda: now[0], sleep=advance)
        self.assertEqual(result['valid_until'], 60)
        self.assertEqual(result['remaining_seconds'], 30)


class BackendSelectionTests(unittest.TestCase):
    def backend(self, module, name, **attrs):
        return type(name, (), {'__module__': module, 'priority': 1, **attrs})()

    def select(self, backend):
        with patch.dict(sys.modules, {'keyring': types.SimpleNamespace(get_keyring=lambda: backend)}):
            return otp._os_keyring()

    def test_concrete_os_backend_and_secure_chain(self):
        for module, name in otp._OS_BACKENDS:
            backend = self.backend(module, name)
            self.assertIs(self.select(backend), backend)
        first = self.backend('keyring.backends.SecretService', 'Keyring')
        second = self.backend('keyring.backends.kwallet', 'DBusKeyring')
        chain = self.backend('keyring.backends.chainer', 'ChainerBackend', backends=[first, second])
        self.assertIs(self.select(chain), first)

    def test_plaintext_null_remote_or_mixed_chain_rejected_before_access(self):
        unsafe = self.backend('keyrings.alt.file', 'PlaintextKeyring')
        good = self.backend('keyring.backends.SecretService', 'Keyring')
        candidates = [unsafe, self.backend('keyring.backends.null', 'Keyring'),
                      self.backend('remote_store', 'Keyring'),
                      self.backend('keyring.backends.chainer', 'ChainerBackend', backends=[good, unsafe]),
                      self.backend('keyring.backends.chainer', 'ChainerBackend', backends=[]),
                      self.backend('keyring.backends.SecretService', 'Keyring', priority=0)]
        for backend in candidates:
            with self.subTest(backend=type(backend)), self.assertRaises(otp.OTPError):
                self.select(backend)

    def test_missing_dependency_or_backend_errors_are_actionable_and_redacted(self):
        with patch.dict(sys.modules, {'keyring': None}), self.assertRaisesRegex(otp.OTPError, 'otp'):
            otp._os_keyring()
        fake = types.SimpleNamespace(get_keyring=Mock(side_effect=RuntimeError(SEED)))
        with patch.dict(sys.modules, {'keyring': fake}), self.assertRaises(otp.OTPError) as caught:
            otp._os_keyring()
        self.assertNotIn(SEED, str(caught.exception))


class StoreAndCLITests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        patcher = patch.object(otp, '_os_keyring', return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = otp.OTPStore()

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_store_roundtrip_replace_delete(self):
        self.assertIsNone(self.store.load())
        config = otp.TOTPConfig(SEED, 'SHA256', 8, 60)
        self.store.save(config)
        self.assertEqual(self.store.load(), config)
        with self.assertRaises(otp.OTPError):
            self.store.save(otp.TOTPConfig(SEED))
        self.store.save(otp.TOTPConfig(SEED), replace=True)
        self.assertEqual(self.store.load().period, 30)
        self.assertTrue(self.store.remove())
        self.assertFalse(self.store.remove())

    def test_corrupt_store_and_failed_reads_do_not_expose_secrets(self):
        for value in ('invalid ' + SEED, '[]', '{}',
                      json.dumps({'version': 1, 'secret': SEED, 'algorithm': 'SHA1', 'digits': True, 'period': 30})):
            self.backend.items[otp._SERVICE, otp._ACCOUNT] = value
            with self.assertRaises(otp.OTPError) as caught:
                self.store.load()
            self.assertNotIn(SEED, str(caught.exception))
        self.backend.get_password = Mock(side_effect=RuntimeError(SEED))
        result, out, err = self.run_cli(['status'])
        self.assertEqual((result, out), (1, ''))
        self.assertNotIn(SEED, err)

    def test_failed_or_silent_store_writes_are_not_reported_successful(self):
        for effect in (RuntimeError(SEED), None):
            self.backend.set_password = Mock(side_effect=effect)
            with self.assertRaises(otp.OTPError) as caught:
                self.store.save(otp.TOTPConfig(SEED))
            self.assertNotIn(SEED, str(caught.exception))
        self.backend.items[otp._SERVICE, otp._ACCOUNT] = SEED
        self.backend.delete_password = Mock()
        with self.assertRaises(otp.OTPError):
            self.store.remove()

    def test_cli_setup_status_code_remove(self):
        result, out, err = self.run_cli(['status', '--json'])
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(out)['configured'])
        with patch.object(cli, '_secret_prompt', return_value=SEED) as prompt:
            result, out, err = self.run_cli(['setup'])
            self.assertEqual((result, err), (0, ''))
            prompt.assert_called_once()
        self.assertNotIn(SEED, out)
        result, out, err = self.run_cli(['status', '--json'])
        self.assertEqual(result, 0)
        status = json.loads(out)
        self.assertTrue(status['configured'])
        self.assertEqual(status['period'], 30)
        self.assertNotIn('secret', out)
        self.assertNotIn('code', status)
        with patch.object(cli, 'current_code', side_effect=lambda cfg, minimum:
                          otp.current_code(cfg, minimum, clock=lambda: 59)):
            result, out, err = self.run_cli(['code', '--min-validity', '0'])
            self.assertEqual((result, out, err), (0, '287082\n', ''))
            result, out, err = self.run_cli(['code', '--json', '--min-validity', '0'])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(out), {'code': '287082', 'valid_until': 60, 'remaining_seconds': 1})
        self.assertEqual(self.run_cli(['remove'])[0], 0)
        self.assertIsNone(self.store.load())
        result, out, err = self.run_cli(['code'])
        self.assertEqual((result, out), (1, ''))
        self.assertIn('setup', err)

    def test_existing_setup_does_not_prompt_without_replace(self):
        self.store.save(otp.TOTPConfig(SEED))
        with patch.object(cli, '_secret_prompt', return_value=SEED) as prompt:
            result, out, err = self.run_cli(['setup'])
            self.assertEqual(result, 1)
            prompt.assert_not_called()
            result, out, err = self.run_cli(['setup', '--replace'])
            self.assertEqual(result, 0)
            prompt.assert_called_once()

    def test_no_echo_fallback_or_secret_in_parser_errors(self):
        def unprotected_prompt(*args, **kwargs):
            warnings.warn('echo would be enabled', getpass.GetPassWarning)
            self.fail('must not fall back to echoed input')
        with patch.object(getpass, 'getpass', side_effect=unprotected_prompt):
            result, out, err = self.run_cli(['setup'])
            self.assertEqual((result, out), (1, ''))
            self.assertIsNone(self.store.load())
        for args in (['setup', '--secret', SEED], ['code', '--min-validity', SEED], [SEED]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                cli.main(args)
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn(SEED, stdout.getvalue() + stderr.getvalue())

    def test_cancelled_setup_does_not_write(self):
        with patch.object(cli, '_secret_prompt', side_effect=KeyboardInterrupt):
            result, out, err = self.run_cli(['setup'])
        self.assertEqual((result, out), (130, ''))
        self.assertIsNone(self.store.load())


if __name__ == '__main__':
    unittest.main()
