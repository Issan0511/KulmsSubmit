"""Command-line setup and code generation; never accepts secrets as arguments."""

import argparse
import getpass
import json
import sys
import warnings

from .KulmsOTP import OTPError, OTPStore, current_code, parse_totp


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes unknown arguments, which might be a seed.
        self.print_usage(sys.stderr)
        self.exit(2, '引数が正しくありません。--helpを確認してください。秘密鍵はsetupの非表示入力欄へ入力します。\n')


def _secret_prompt() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass('既存のTOTP秘密鍵（Base32）またはotpauth URI（入力は非表示）: ')
        except (getpass.GetPassWarning, EOFError, OSError):
            raise OTPError('非表示入力ができません。ご自身の対話ターミナルでsetupを実行してください。') from None


def main(argv=None) -> int:
    parser = _Parser(description='KULMS向けローカルTOTP。登録済みの秘密鍵から認証コードを生成します。')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    setup = commands.add_parser('setup', help='秘密鍵を非表示入力してOSのキーストアに保存')
    setup.add_argument('--replace', action='store_true', help='既存のローカル設定を置き換える')
    code = commands.add_parser('code', help='現在の認証コードを1つ出力')
    code.add_argument('--json', action='store_true', help='コード・有効期限（Unix秒）・残り秒数をJSONで出力')
    code.add_argument('--min-validity', type=float, default=None, metavar='SECONDS',
                      help='残り時間がこの秒数未満なら次のコードを待つ（既定: 5秒、短い周期では半周期）')
    status = commands.add_parser('status', help='秘密鍵や認証コードを表示せず登録状態を確認')
    status.add_argument('--json', action='store_true', help='登録状態をJSONで出力')
    commands.add_parser('remove', help='このCLIに保存した秘密鍵を削除（大学側の登録は変更しません）')
    args = parser.parse_args(argv)
    try:
        store = OTPStore()
        if args.command == 'setup':
            if not args.replace and store.load() is not None:
                raise OTPError('OTPは登録済みです。置き換える場合はsetup --replaceを指定してください。')
            config = parse_totp(_secret_prompt())
            store.save(config, replace=args.replace)
            print('OTPをOSのキーストアに保存しました。大学側の認証設定は変更していません。')
        elif args.command == 'remove':
            print('ローカルのOTP設定を削除しました。' if store.remove() else 'OTPは未登録です。')
        else:
            config = store.load()
            if args.command == 'status':
                result = {'configured': config is not None, 'backend': store.backend_name}
                if config is not None:
                    result.update(config.public_settings())
                if args.json:
                    print(json.dumps(result))
                else:
                    print('OTP: ' + ('登録済み' if config else '未登録') + '\n保存先: ' + store.backend_name)
            else:
                if config is None:
                    raise OTPError('OTPは未登録です。ご自身のターミナルでsetupを実行してください。')
                result = current_code(config, args.min_validity)
                print(json.dumps(result) if args.json else result['code'])
        return 0
    except OTPError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('中止しました。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
