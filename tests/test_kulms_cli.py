import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from kulms_submit.KulmsClient import KulmsError
from kulms_submit.KulmsLogin import main


class ReadCLITests(unittest.TestCase):
    def run_cli(self, args, client):
        out, err = io.StringIO(), io.StringIO()
        with patch('kulms_submit.KulmsCLI.KulmsClient.from_keyring', return_value=client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def client(self):
        client = MagicMock()
        client.__enter__.return_value = client
        return client

    def test_json_commands_return_records_without_prose(self):
        commands = [(['courses'], 'courses', ()),
                    (['assignments'], 'assignments', (None,)),
                    (['assignments', '--course', 'course-1'], 'assignments', ('course-1',)),
                    (['tests', '--course', 'course-1'], 'tests', ('course-1',)),
                    (['materials', '--course', 'course-1'], 'materials', ('course-1',))]
        for arguments, method, parameters in commands:
            with self.subTest(arguments=arguments):
                client = self.client()
                records = [{'id': 'item-1', 'title': 'テスト教材'}]
                getattr(client, method).return_value = records
                code, out, err = self.run_cli(arguments + ['--json'], client)
                self.assertEqual((code, err), (0, ''))
                self.assertEqual(json.loads(out), records)
                getattr(client, method).assert_called_once_with(*parameters)
                client.__exit__.assert_called_once()

    def test_login_download_and_readable_table(self):
        client = self.client()
        client.authenticated = True
        code, out, err = self.run_cli(['login', '--json'], client)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out), {'lms_authenticated': True})
        client.login.assert_called_once_with()
        client.download.return_value = {'path': '/tmp/fake.pdf', 'bytes': 10, 'sha256': 'fake'}
        args = ['download', '--course', 'course-1', '--resource', '/content/group/course-1/fake.pdf',
                '--output', '/tmp/fake.pdf', '--json']
        self.assertEqual(self.run_cli(args, client)[0], 0)
        client.download.assert_called_once_with('course-1', '/content/group/course-1/fake.pdf', '/tmp/fake.pdf')
        client.courses.return_value = [{'title': 'Title\x1b[2J\nnew line', 'id': 'course-1'}]
        code, out, err = self.run_cli(['courses'], client)
        self.assertEqual(code, 0)
        self.assertNotIn('\x1b', out)
        self.assertIn('course-1', out)
        self.assertEqual(err, '1件\n')

    def test_authentication_failure_does_not_report_success(self):
        client = self.client()
        client.login.side_effect = KulmsError('認証できませんでした。')
        code, out, err = self.run_cli(['login', '--json'], client)
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(err, '認証できませんでした。\n')
        client.__exit__.assert_called_once()

    def test_submission_is_dry_run_unless_execute_is_explicit(self):
        client = self.client()
        arguments = ['submit', '--course', 'course-1', '--assignment', 'assignment-1',
                     '--file', '/tmp/answer.pdf', '--json']
        for flags, execute, resubmit in (([], False, False), (['--execute'], True, False),
                                         (['--execute', '--resubmit'], True, True)):
            result = {'dry_run': not execute, 'submitted': execute}
            with patch('kulms_submit.KulmsCLI.submit_assignment', return_value=result) as submit:
                code, out, err = self.run_cli(arguments + flags, client)
            self.assertEqual((code, err), (0, ''))
            self.assertEqual(json.loads(out), result)
            submit.assert_called_once_with(client, 'course-1', 'assignment-1', files=['/tmp/answer.pdf'],
                                           text_file=None, html_file=None, page_url=None,
                                           execute=execute, resubmit=resubmit, replace_attachments=[])


if __name__ == '__main__':
    unittest.main()
