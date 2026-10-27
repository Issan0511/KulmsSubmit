"""Local TOTP generation and OS-keyring storage for the KULMS CLI.

No browser, network, notebook or Qt dependency. Secrets stay in the OS keyring;
only callers explicitly requesting a code receive a short-lived OTP.
"""

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit


class OTPError(ValueError):
    """An error safe to display without exposing the supplied secret."""


def _normalize_secret(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096 or not value.isascii():
        raise OTPError('Base32形式の秘密鍵を入力してください。')
    compact = ''.join(value.split()).upper()
    if not re.fullmatch(r'[A-Z2-7]+={0,6}', compact):
        raise OTPError('Base32形式の秘密鍵を入力してください。')
    unpadded = compact.rstrip('=')
    padded = unpadded + '=' * (-len(unpadded) % 8)
    if '=' in compact and compact != padded:
        raise OTPError('Base32のパディングが正しくありません。')
    try:
        decoded = base64.b32decode(padded)
    except (binascii.Error, ValueError):
        raise OTPError('Base32形式の秘密鍵を入力してください。') from None
    if not decoded or base64.b32encode(decoded).decode().rstrip('=') != unpadded:
        raise OTPError('Base32形式の秘密鍵を入力してください。')
    return unpadded


@dataclass(frozen=True)
class TOTPConfig:
    secret: str = field(repr=False)
    algorithm: str = 'SHA1'
    digits: int = 6
    period: int = 30

    def __post_init__(self):
        object.__setattr__(self, 'secret', _normalize_secret(self.secret))
        if not isinstance(self.algorithm, str) or self.algorithm.upper() not in ('SHA1', 'SHA256', 'SHA512'):
            raise OTPError('TOTPのハッシュ方式はSHA1・SHA256・SHA512に対応しています。')
        object.__setattr__(self, 'algorithm', self.algorithm.upper())
        if type(self.digits) is not int or self.digits not in (6, 8):
            raise OTPError('TOTPの桁数は6または8にしてください。')
        if type(self.period) is not int or not 1 <= self.period <= 300:
            raise OTPError('TOTPの周期は1〜300秒にしてください。')

    def public_settings(self) -> dict:
        return {'algorithm': self.algorithm, 'digits': self.digits, 'period': self.period}


def parse_totp(value: str) -> TOTPConfig:
    """Accept a Base32 seed or an authenticator QR's otpauth://totp URI."""
    if not isinstance(value, str) or len(value) > 8192:
        raise OTPError('Base32の秘密鍵またはotpauth://totp/形式を入力してください。')
    value = value.strip()
    if not value.lower().startswith('otpauth:'):
        return TOTPConfig(value)
    try:
        if re.search(r'\s|%(?![0-9A-Fa-f]{2})', value) or '#' in value:
            raise ValueError
        uri = urlsplit(value)
        if uri.scheme != 'otpauth' or uri.netloc != 'totp' or not uri.path.lstrip('/'):
            raise ValueError
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=8)
        params = dict(pairs)
        allowed = {'secret', 'algorithm', 'digits', 'period', 'issuer'}
        if (len(params) != len(pairs) or not params.keys() <= allowed
                or 'secret' not in params or any(not v.strip() for v in params.values())):
            raise ValueError
        label = unquote(uri.path[1:], encoding='utf-8', errors='strict')
        if not label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError
        if ':' in label and 'issuer' in params and label.split(':', 1)[0] != params['issuer']:
            raise ValueError
        for name in ('digits', 'period'):
            if name in params and not re.fullmatch(r'[0-9]+', params[name]):
                raise ValueError
        return TOTPConfig(params['secret'], params.get('algorithm', 'SHA1'),
                          int(params.get('digits', '6')), int(params.get('period', '30')))
    except (ValueError, UnicodeError):
        raise OTPError('TOTP URIが不正、または未対応の設定です。Base32の秘密鍵も入力できます。') from None


def _counter(config: TOTPConfig, timestamp: float) -> int:
    try:
        if isinstance(timestamp, bool) or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError
        counter = math.floor(timestamp / config.period)
        if not 0 <= counter < 2**64:
            raise ValueError
        return counter
    except (ValueError, TypeError, OverflowError):
        raise OTPError('OTP生成に使う時刻が有効範囲外です。PCの時計を確認してください。') from None


def generate_totp(config: TOTPConfig, timestamp: float) -> str:
    """RFC 6238 with T0=0; timestamp is Unix time in seconds (UTC)."""
    counter = _counter(config, timestamp)
    secret = base64.b32decode(config.secret + '=' * (-len(config.secret) % 8))
    digest = hmac.new(secret, counter.to_bytes(8, 'big'),
                      getattr(hashlib, config.algorithm.lower())).digest()
    offset = digest[-1] & 0x0f
    number = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff
    return str(number % (10**config.digits)).zfill(config.digits)


def current_code(config: TOTPConfig, min_validity: float | None = None, *,
                 clock=time.time, sleep=time.sleep) -> dict:
    """Wait past a nearly expired time step, then return a code and its expiry."""
    if min_validity is None:
        min_validity = min(5, config.period / 2)
    if (type(min_validity) not in (int, float) or not math.isfinite(min_validity)
            or not 0 <= min_validity < config.period):
        raise OTPError('最低有効秒数は0以上、TOTPの周期未満にしてください。')
    for attempt in range(3):
        now = clock()
        counter = _counter(config, now)
        valid_until = (counter + 1) * config.period
        remaining = valid_until - now
        if remaining >= min_validity:
            return {'code': generate_totp(config, now), 'valid_until': valid_until,
                    'remaining_seconds': math.floor(remaining * 1000) / 1000}
        if attempt < 2:
            sleep(remaining)
    raise OTPError('時計の変化により有効時間を確保できませんでした。再実行してください。')


_SERVICE = 'beamerplus.kulms.otp'
_ACCOUNT = 'totp'
_OS_BACKENDS = {
    ('keyring.backends.SecretService', 'Keyring'),
    ('keyring.backends.kwallet', 'DBusKeyring'),
    ('keyring.backends.kwallet', 'DBusKeyringKWallet4'),
    ('keyring.backends.macOS', 'Keyring'),
    ('keyring.backends.Windows', 'WinVaultKeyring'),
}


def _os_keyring():
    try:
        import keyring
    except ImportError:
        raise OTPError("OTP用の依存関係が必要です: .venv/bin/python -m pip install -e '.[otp]'") from None
    try:
        backend = keyring.get_keyring()
        identity = (type(backend).__module__, type(backend).__name__)
        if identity == ('keyring.backends.chainer', 'ChainerBackend'):
            candidates = list(backend.backends)
            # Never delegate to a chain that could read from a plaintext/remote store.
            if not candidates or any((type(b).__module__, type(b).__name__) not in _OS_BACKENDS
                                     for b in candidates):
                raise ValueError
            backend = candidates[0]
        if (type(backend).__module__, type(backend).__name__) not in _OS_BACKENDS:
            raise ValueError
        if backend.priority <= 0:
            raise ValueError
        return backend
    except Exception:
        raise OTPError('OSのキーストアが利用できません。Secret Service・KWallet・Keychain・'
                       'Windows Credential Lockerを有効にしてください。平文ファイルには保存しません。') from None


class OTPStore:
    """Use a dedicated OS-keyring item; never include it in notebook backups."""

    def __init__(self):
        self._backend = _os_keyring()

    @property
    def backend_name(self) -> str:
        return type(self._backend).__module__ + '.' + type(self._backend).__name__

    def _read(self):
        try:
            return self._backend.get_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise OTPError('キーストアを読み取れません。端末でロックを解除して再実行してください。') from None

    def load(self) -> TOTPConfig | None:
        raw = self._read()
        if raw is None:
            return None
        try:
            if not isinstance(raw, str) or len(raw) > 8192:
                raise ValueError
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'secret', 'algorithm', 'digits', 'period'}
                    or type(data['version']) is not int or data['version'] != 1):
                raise ValueError
            return TOTPConfig(data['secret'], data['algorithm'], data['digits'], data['period'])
        except (ValueError, TypeError):
            raise OTPError('保存されたOTP設定を読み取れません。setup --replaceで登録し直してください。') from None

    def save(self, config: TOTPConfig, *, replace: bool = False):
        if self._read() is not None and not replace:
            raise OTPError('OTPは登録済みです。置き換える場合はsetup --replaceを指定してください。')
        encoded = json.dumps({'version': 1, 'secret': config.secret, **config.public_settings()})
        try:
            self._backend.set_password(_SERVICE, _ACCOUNT, encoded)
        except Exception:
            raise OTPError('OTPをキーストアへ保存できませんでした。端末のキーストアを確認してください。') from None
        if self._read() != encoded:
            raise OTPError('OTPの保存を確認できませんでした。端末のキーストアを確認してください。')

    def remove(self) -> bool:
        if self._read() is None:
            return False
        try:
            self._backend.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise OTPError('OTPをキーストアから削除できませんでした。') from None
        if self._read() is not None:
            raise OTPError('OTPの削除を確認できませんでした。')
        return True
