import base64
from dataclasses import FrozenInstanceError
import math
import unittest
from urllib.parse import urlencode

from kulms_submit.KulmsOTP import OTPError, TOTPConfig, generate_totp, parse_totp


# Public interoperability fixtures, never account credentials:
# https://www.rfc-editor.org/rfc/rfc6238#appendix-A
# https://www.rfc-editor.org/rfc/rfc6238#appendix-B
RFC_SECRETS = {
    'SHA1': b'12345678901234567890',
    'SHA256': b'12345678901234567890123456789012',
    'SHA512': b'1234567890123456789012345678901234567890123456789012345678901234',
}
RFC_VECTORS = (
    (59, '94287082', '46119246', '90693936'),
    (1111111109, '07081804', '68084774', '25091201'),
    (1111111111, '14050471', '67062674', '99943326'),
    (1234567890, '89005924', '91819424', '93441116'),
    (2000000000, '69279037', '90698825', '38618901'),
    (20000000000, '65353130', '77737706', '47863826'),
)


def encoded_secret(algorithm='SHA1'):
    return base64.b32encode(RFC_SECRETS[algorithm]).decode('ascii').rstrip('=')


class TOTPGenerationTests(unittest.TestCase):
    def test_rfc_6238_vectors_for_all_algorithms_including_post_2038(self):
        for algorithm_index, algorithm in enumerate(RFC_SECRETS, start=1):
            config = TOTPConfig(encoded_secret(algorithm), algorithm=algorithm, digits=8)
            for row in RFC_VECTORS:
                with self.subTest(algorithm=algorithm, timestamp=row[0]):
                    self.assertEqual(generate_totp(config, row[0]), row[algorithm_index])

    def test_default_six_digits_preserve_leading_and_trailing_zeroes(self):
        config = TOTPConfig(encoded_secret())
        self.assertEqual((config.algorithm, config.digits, config.period), ('SHA1', 6, 30))
        for timestamp, expected in ((59, '287082'), (1111111111, '050471'),
                                    (1234567890, '005924'), (20000000000, '353130')):
            with self.subTest(timestamp=timestamp):
                code = generate_totp(config, timestamp)
                self.assertIsInstance(code, str)
                self.assertEqual(code, expected)

    def test_fractional_timestamp_changes_only_at_period_boundary(self):
        config = TOTPConfig(encoded_secret())
        for timestamp in (1111111080, 1111111109, 1111111109.999):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(generate_totp(config, timestamp), '081804')
        for timestamp in (1111111110, 1111111111, 1111111139.999):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(generate_totp(config, timestamp), '050471')

    def test_epoch_and_custom_periods(self):
        config = TOTPConfig(encoded_secret())
        self.assertEqual(generate_totp(config, 0), generate_totp(config, 29.999))
        for period in (1, 60, 300):
            with self.subTest(period=period):
                custom = TOTPConfig(encoded_secret(), period=period)
                self.assertEqual(generate_totp(custom, period), '287082')
                self.assertEqual(generate_totp(custom, 2 * period - 0.001), '287082')

    def test_invalid_time_and_counter_overflow_raise_otp_error(self):
        config = TOTPConfig(encoded_secret())
        for timestamp in (-1, -0.001, math.nan, math.inf, -math.inf,
                          float(1 << 64) * config.period, 1e300):
            with self.subTest(timestamp=timestamp), self.assertRaises(OTPError):
                generate_totp(config, timestamp)


class TOTPConfigTests(unittest.TestCase):
    def test_base32_case_spacing_and_optional_padding_are_canonicalized(self):
        canonical = encoded_secret('SHA256')
        padded = base64.b32encode(RFC_SECRETS['SHA256']).decode('ascii')
        grouped = ' \t' + '\n'.join(padded.lower()[i:i + 4]
                                    for i in range(0, len(padded), 4)) + ' \r\n'
        for value in (canonical, canonical.lower(), padded, grouped):
            with self.subTest(value=value):
                self.assertEqual(TOTPConfig(value).secret, canonical)
                self.assertEqual(parse_totp(value).secret, canonical)

    def test_malformed_and_ambiguous_base32_are_rejected(self):
        canonical = encoded_secret('SHA256')
        padded = base64.b32encode(RFC_SECRETS['SHA256']).decode('ascii')
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
        # Flip an unused padding bit: permissive decoders map this to the same key.
        bad_pad_bits = canonical[:-1] + alphabet[alphabet.index(canonical[-1]) + 1]
        invalid = ('', ' \n\t', '=', 'A', 'ABC', canonical + '!',
                   canonical[:-1] + '0', canonical[:-1] + '1', canonical[:-1] + '8',
                   canonical[:-1] + '9', canonical[:-1] + '\u00e9',
                   canonical[:4] + '=' + canonical[4:], canonical + '=',
                   padded + '=', bad_pad_bits)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(OTPError):
                    TOTPConfig(value)
                with self.assertRaises(OTPError):
                    parse_totp(value)

    def test_invalid_algorithm_digits_or_period_are_rejected(self):
        invalid_options = (
            {'algorithm': ''}, {'algorithm': 'MD5'}, {'algorithm': 'SHA224'},
            {'digits': 0}, {'digits': 5}, {'digits': 7}, {'digits': 9},
            {'digits': 6.0}, {'digits': '6'}, {'digits': True},
            {'period': 0}, {'period': -1}, {'period': 301},
            {'period': 30.0}, {'period': '30'}, {'period': True},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(OTPError):
                TOTPConfig(encoded_secret(), **options)

    def test_config_is_immutable_and_repr_omits_secret(self):
        config = TOTPConfig(encoded_secret())
        self.assertNotIn(encoded_secret(), repr(config))
        self.assertNotIn(RFC_SECRETS['SHA1'].decode('ascii'), repr(config))
        with self.assertRaises(FrozenInstanceError):
            config.secret = encoded_secret('SHA256')
        with self.assertRaises(FrozenInstanceError):
            config.period = 60
        self.assertTrue(issubclass(OTPError, ValueError))


class TOTPURIParsingTests(unittest.TestCase):
    def test_minimal_uri_has_default_configuration(self):
        config = parse_totp('otpauth://totp/KULMS%3Apublic-test?secret=' + encoded_secret())
        self.assertEqual(config, TOTPConfig(encoded_secret()))

    def test_uri_applies_explicit_parameters_and_percent_decodes_values(self):
        query = urlencode({
            'secret': base64.b32encode(RFC_SECRETS['SHA256']).decode('ascii'),
            'algorithm': 'SHA256', 'digits': '8', 'period': '60',
            'issuer': 'Kyoto University',
        })
        config = parse_totp('otpauth://totp/Kyoto%20University%3Apublic-test?' + query)
        self.assertEqual(config, TOTPConfig(encoded_secret('SHA256'),
                                            algorithm='SHA256', digits=8, period=60))
        self.assertEqual(generate_totp(config, 119), '46119246')

    def test_unsupported_or_malformed_uri_is_rejected(self):
        secret_query = '?secret=' + encoded_secret()
        invalid = (
            'otpauth://hotp/public-test' + secret_query + '&counter=0',
            'https://totp/public-test' + secret_query,
            'otpauth://totp/public-test' + secret_query + '#fragment',
            'otpauth://totp/public-test',
            'otpauth://totp/public-test?issuer=KULMS',
            'otpauth://totp/public-test?secret=',
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OTPError):
                parse_totp(value)

    def test_duplicate_unknown_and_blank_query_values_are_rejected(self):
        prefix = 'otpauth://totp/public-test?secret=' + encoded_secret()
        invalid_suffixes = (
            '&secret=' + encoded_secret(),
            '&secr%65t=' + encoded_secret(),
            '&algorithm=SHA1&algorithm=SHA256',
            '&digits=6&digits=8', '&period=30&period=60',
            '&issuer=KULMS&issuer=Other', '&counter=0', '&unknown=value',
            '&Secret=' + encoded_secret(), '&issuer=', '&algorithm=',
            '&digits=', '&period=', '&issuer', '&=value',
        )
        for suffix in invalid_suffixes:
            with self.subTest(suffix=suffix), self.assertRaises(OTPError):
                parse_totp(prefix + suffix)

    def test_invalid_uri_configuration_is_rejected(self):
        prefix = 'otpauth://totp/public-test?secret=' + encoded_secret()
        for suffix in ('&digits=7', '&digits=6.0', '&digits=abc', '&period=0',
                       '&period=301', '&period=30.5', '&period=abc', '&algorithm=MD5'):
            with self.subTest(suffix=suffix), self.assertRaises(OTPError):
                parse_totp(prefix + suffix)


if __name__ == '__main__':
    unittest.main()
