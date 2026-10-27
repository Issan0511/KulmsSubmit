"""Store KULMS login credentials locally without putting them in shell history."""

import argparse
from dataclasses import dataclass, field
import getpass
import json
import re
import sys
import warnings

from .KulmsOTP import OTPError, OTPStore, _os_keyring


class LoginError(ValueError):
    """Safe to display: never includes a credential or backend error text."""


@dataclass(frozen=True)
class LoginCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.username, str):
            raise LoginError('ECS-IDまたはSPS-IDを入力してください。')
        username = self.username.strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', username):
            raise LoginError('ECS-IDまたはSPS-IDを入力してください。')
        object.__setattr__(self, 'username', username)
        if (not isinstance(self.password, str) or not 1 <= len(self.password) <= 4096
                or any(c in self.password for c in '\r\n\x00')):
            raise LoginError('パスワードが空、長すぎる、または改行を含んでいます。')


_SERVICE = 'beamerplus.kulms.login'
_ACCOUNT = 'credentials'


class LoginStore:
    def __init__(self):
        self._backend = _os_keyring()

    @property
    def backend_name(self) -> str:
        return type(self._backend).__module__ + '.' + type(self._backend).__name__

    def _read(self):
        try:
            return self._backend.get_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise LoginError('ログイン情報を読み取れません。端末のキーストアを解除してください。') from None

    def load(self) -> LoginCredentials | None:
        raw = self._read()
        if raw is None:
            return None
        try:
            if not isinstance(raw, str) or len(raw) > 20000:
                raise ValueError
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'username', 'password'}
                    or type(data['version']) is not int or data['version'] != 1):
                raise ValueError
            return LoginCredentials(data['username'], data['password'])
        except (ValueError, TypeError):
            raise LoginError('保存したログイン情報を読み取れません。setup --replaceで登録し直してください。') from None

    def save(self, credentials: LoginCredentials, *, replace: bool = False):
        if self._read() is not None and not replace:
            raise LoginError('ログイン情報は登録済みです。置き換える場合はsetup --replaceを指定してください。')
        encoded = json.dumps({'version': 1, 'username': credentials.username,
                              'password': credentials.password}, ensure_ascii=False)
        try:
            self._backend.set_password(_SERVICE, _ACCOUNT, encoded)
        except Exception:
            raise LoginError('ログイン情報をキーストアへ保存できませんでした。') from None
        if self._read() != encoded:
            raise LoginError('ログイン情報の保存を確認できませんでした。')

    def remove(self) -> bool:
        if self._read() is None:
            return False
        try:
            self._backend.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            raise LoginError('ログイン情報をキーストアから削除できませんでした。') from None
        if self._read() is not None:
            raise LoginError('ログイン情報の削除を確認できませんでした。')
        return True


def _hidden_input(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError, OSError):
            raise LoginError('非表示入力ができません。ご自身の対話ターミナルでsetupを実行してください。') from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, '引数が正しくありません。IDとパスワードはsetupの非表示入力欄へ入力してください。\n')


def main(argv=None) -> int:
    from .KulmsCLI import add_read_commands, run_read_command

    parser = _Parser(description='KULMSの認証情報管理・自動ログインと課題・テスト・教材の取得。')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    setup = commands.add_parser('setup', help='IDとパスワードを非表示入力して保存')
    setup.add_argument('--replace', action='store_true', help='保存済みのログイン情報を置き換える')
    status = commands.add_parser('status', help='ID・パスワード・OTPの値を表示せず登録状態を確認')
    status.add_argument('--json', action='store_true', help='登録状態をJSONで出力')
    commands.add_parser('remove', help='このCLIに保存したID・パスワードだけを削除')
    add_read_commands(commands)
    args = parser.parse_args(argv)
    try:
        if args.command not in ('setup', 'status', 'remove'):
            return run_read_command(args)
        store = LoginStore()
        if args.command == 'setup':
            if not args.replace and store.load() is not None:
                raise LoginError('ログイン情報は登録済みです。置き換える場合はsetup --replaceを指定してください。')
            username = _hidden_input('京大のECS-ID / SPS-ID（入力は非表示）: ')
            password = _hidden_input('パスワード（入力は非表示）: ')
            if password != _hidden_input('パスワード（確認・入力は非表示）: '):
                raise LoginError('確認用パスワードが一致しません。保存していません。')
            store.save(LoginCredentials(username, password), replace=args.replace)
            print('IDとパスワードをOSのキーストアに保存しました。大学側の設定は変更していません。')
        elif args.command == 'status':
            result = {'login_configured': store.load() is not None,
                      'otp_configured': OTPStore().load() is not None,
                      'backend': store.backend_name}
            if args.json:
                print(json.dumps(result))
            else:
                print('ID・パスワード: ' + ('登録済み' if result['login_configured'] else '未登録'))
                print('OTP: ' + ('登録済み' if result['otp_configured'] else '未登録'))
        else:
            print('保存したIDとパスワードを削除しました。' if store.remove() else 'ログイン情報は未登録です。')
        return 0
    except (LoginError, OTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('中止しました。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
