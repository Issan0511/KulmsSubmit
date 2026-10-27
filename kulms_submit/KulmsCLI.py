"""Course, assignment, assessment, and resource commands for kulms."""

import json
import re
import sys

from .KulmsClient import KulmsClient
from .KulmsLogin import LoginError
from .KulmsOTP import OTPError
from .KulmsSubmission import submit_assignment


def add_read_commands(commands):
    for name, help_text in (
            ('login', '保存済みのID・パスワードとOTPでLMSへのログインを確認'),
            ('courses', '自分のコース一覧とコースIDを取得'),
            ('assignments', '自分に割り当てられた課題一覧を取得'),
            ('tests', '指定コースで公開されているテストの一覧を取得'),
            ('materials', '指定コースの教材一覧と教材IDを取得'),
            ('download', '教材IDを指定してファイルを保存')):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--json', action='store_true', help='APIの各項目をJSONで出力')
        if name in ('assignments', 'tests', 'materials', 'download'):
            command.add_argument('--course', required=name != 'assignments', metavar='COURSE_ID',
                                 help='coursesに表示されるコースID')
        if name == 'download':
            command.add_argument('--resource', required=True, metavar='RESOURCE_ID',
                                 help='materialsに表示される教材ID')
            command.add_argument('--output', required=True, metavar='FILE',
                                 help='保存先ファイル（既存ファイルは上書きしません）')
    submit = commands.add_parser('submit', help='答案の送信内容を確認（execute指定時だけアップロード・提出）')
    submit.add_argument('--course', required=True, metavar='COURSE_ID')
    submit.add_argument('--assignment', required=True, metavar='ASSIGNMENT_ID')
    submit.add_argument('--page', metavar='URL', help='課題ツールのURL（省略時はコースから検出）')
    submit.add_argument('--file', action='append', default=[], metavar='FILE', help='添付する答案（最大5個、繰り返し指定可）')
    text = submit.add_mutually_exclusive_group()
    text.add_argument('--text-file', metavar='FILE', help='インライン本文のUTF-8テキスト')
    text.add_argument('--html-file', metavar='FILE', help='インライン本文のUTF-8 HTML')
    submit.add_argument('--execute', action='store_true', help='本人の提出指示を受け、アップロード・下書き保存・最終提出を実行')
    submit.add_argument('--resubmit', action='store_true', help='本人から指示された既存答案の再提出を許可')
    submit.add_argument('--replace-attachment', action='append', default=[], metavar='REF',
                        help='削除して差し替える既存添付のref（ローカル保存・照合後に指定）')
    submit.add_argument('--json', action='store_true')


def _cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value if value is not None else ''))


def _display(command, rows):
    fields = {
        'courses': ('title', 'id'),
        'assignments': ('title', 'id', 'context', 'dueTimeString', 'status'),
        'tests': ('title', 'id'),
        'materials': ('title', 'id', 'type'),
    }[command]
    print('\t'.join(fields))
    for row in rows:
        print('\t'.join(_cell(row.get(key)) for key in fields))
    print(f'{len(rows)}件', file=sys.stderr)


def run_read_command(args):
    try:
        with KulmsClient.from_keyring() as client:
            if args.command == 'login':
                client.login()
                result = {'lms_authenticated': client.authenticated}
            elif args.command == 'courses':
                result = client.courses()
            elif args.command == 'assignments':
                result = client.assignments(args.course)
            elif args.command == 'tests':
                result = client.tests(args.course)
            elif args.command == 'materials':
                result = client.materials(args.course)
            elif args.command == 'submit':
                result = submit_assignment(client, args.course, args.assignment, files=args.file,
                                           text_file=args.text_file, html_file=args.html_file,
                                           page_url=args.page, execute=args.execute, resubmit=args.resubmit,
                                           replace_attachments=args.replace_attachment)
            else:
                result = client.download(args.course, args.resource, args.output)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == 'login':
            print('保存済みのID・パスワードとOTPでLMSへのログインに成功しました。')
        elif args.command == 'download':
            print(f"{result['path']} に保存しました（{result['bytes']} bytes）。")
        elif args.command == 'submit':
            print(_cell(result['course_title']) + ' / ' + _cell(result['assignment_title']))
            for upload in result['files']:
                print(f"添付: {_cell(upload['name'])} ({upload['bytes']} bytes, SHA256 {upload['sha256']})")
            print(f"本文: {result['text_characters']}文字（" + ('指定内容に更新' if result['text_changed'] else '既存内容を保持') + '）')
            if result['dry_run']:
                print('送信前の確認です。本人の提出指示を受けてexecuteを指定すると提出します。')
                if result['requires_resubmit_flag']:
                    print('提出済みの課題です。再提出にはresubmitも指定してください。')
            else:
                print('提出済み・本文・添付の内容を確認しました。受付時刻: ' + _cell(result['submitted_at']))
        else:
            _display(args.command, result)
        return 0
    except (LoginError, OTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
