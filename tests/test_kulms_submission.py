from email import policy
from email.parser import BytesParser
from html import escape
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from kulms_submit.KulmsClient import LMS, KulmsError
from kulms_submit.KulmsSubmission import submit_assignment


COURSE = 'course-1'
ASSIGNMENT = 'assignment-1'
PATH = '/portal/site/course-1/tool/tool-1'
ENDPOINT = LMS + PATH + '?panel=Main'
VIEW = LMS + PATH + '?' + urlencode({'assignmentReference': '/assignment/a/course-1/assignment-1',
                                    'panel': 'Main', 'sakai_action': 'doView_submission'})
FILE_URL = LMS + '/access/content/attachment/course-1/Assignments/fake/答案.pdf'
CONTENT = b'%PDF-fake-answer\x00\xff'
TEXT = 'Assignment.view_submission_text'
EVENT = 'eventSubmit_doRead_add_submission_form'


def upload_url(token):
    return ENDPOINT + '&sakai_action=doAttachUpload&sakai_csrf_token=' + token


def form(token='csrf-1', *, text='', files=True, confirm=False, link=False,
         action=ENDPOINT, assignment='$assignmentReference', extra='', disabled=False):
    body = f'<form method="post" enctype="multipart/form-data" action="{escape(action)}">'
    for name, value in ((EVENT, 'x'), ('option', 'cancel'), ('assignmentId', assignment),
                        ('sakai_csrf_token', token), ('currentAttachment', ''),
                        ('ckeditor-autosave-entity-id', ASSIGNMENT)):
        body += f'<input type="hidden" name="{name}" value="{escape(value)}">'
    if confirm:
        body += '<input type="button" name="post"' + (' disabled' if disabled else '') + '>'
    else:
        body += f'<textarea name="{TEXT}">{escape(text)}</textarea>'
        if files:
            body += '<input type="file" name="upload" onchange="this.form.action=\'' + escape(upload_url(token)) + '\'">'
        body += '<input type="button" name="confirm"' + (' disabled' if disabled else '') + '>'
    body += extra + '</form>'
    if link:
        body += f'<a href="{FILE_URL}">答案.pdf</a>'
    return body


class Response(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body.encode('utf-8') if isinstance(body, str) else body)
        self.url = url


class FakeClient:
    def __init__(self, steps, previous=None, receipt=None, submission_type='TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION'):
        self.steps = list(steps)
        self.requests = []
        self.assignment = {'id': ASSIGNMENT, 'context': COURSE, 'title': 'Fake assignment',
                           'submissionType': submission_type, 'submissions': [previous] if previous else []}
        after = {**self.assignment, 'submissions': [receipt]} if receipt else self.assignment
        self.assignments = Mock(side_effect=[[self.assignment], [after]])
        self.courses = Mock(return_value=[{'id': COURSE, 'title': 'Fake course',
                                          'sitePages': [{'tools': [{'id': 'tool-1', 'toolId': 'sakai.assignment.grades'}]}]}])

    def _open(self, url, data=None, **kwargs):
        self.requests.append((url, data, kwargs))
        expected, body = self.steps.pop(0)
        if expected != url:
            raise AssertionError('unexpected request URL')
        if isinstance(body, Exception):
            raise body
        return Response(body, url)


def receipt(text='', attachments=None, timestamp=200):
    return {'id': 'receipt-1', 'submitted': True, 'dateSubmitted': 'fake submission time',
            'dateSubmittedEpochSeconds': timestamp, 'status': '提出済み', 'submittedText': text,
            'submittedAttachments': attachments or []}


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.pdf = self.directory / '答案.pdf'
        self.pdf.write_bytes(CONTENT)
        self.text = self.directory / 'answer.txt'
        self.text.write_text('a < b\n二行目', encoding='utf-8')

    def submit(self, client, **kwargs):
        return submit_assignment(client, COURSE, ASSIGNMENT, **kwargs)

    def test_dry_run_reads_form_without_upload_save_or_submission(self):
        client = FakeClient([(VIEW, form())])
        plan = self.submit(client, files=[self.pdf])
        self.assertTrue(plan['dry_run'])
        self.assertTrue(plan['csrf_present'])
        self.assertNotIn('csrf-1', str(plan))
        self.assertNotIn('sakai_csrf_token', plan['upload_endpoint'])
        self.assertEqual(plan['files'][0]['bytes'], len(CONTENT))
        self.assertEqual(client.requests[0][0], VIEW)
        self.assertTrue(all(data is None for _, data, _ in client.requests))

    def test_upload_confirm_post_and_hash_verification_use_fresh_csrf(self):
        attachment = {'name': self.pdf.name, 'size': len(CONTENT), 'url': FILE_URL, 'ref': '/content/fake'}
        client = FakeClient([(VIEW, form(text='<p>Keep this</p>')),
                             (upload_url('csrf-1'), form('csrf-2', text='<p>Keep this</p>', link=True)),
                             (FILE_URL, CONTENT), (ENDPOINT, form('csrf-3', confirm=True)),
                             (ENDPOINT, '<p>Submitted</p>'), (FILE_URL, CONTENT)],
                            receipt=receipt('<p>Keep this</p>', [attachment]))
        result = self.submit(client, files=[self.pdf], execute=True)
        self.assertEqual((result['submitted'], result['verified'], result['dry_run']), (True, True, False))
        self.assertEqual(result['submission_id'], 'receipt-1')
        self.assertEqual(client.steps, [])
        content_type = client.requests[1][2]['content_type']
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\n\r\n').encode() + client.requests[1][1])
        parts = {part.get_param('name', header='Content-Disposition'): part for part in message.iter_parts()}
        self.assertEqual(parts['upload'].get_payload(decode=True), CONTENT)
        self.assertEqual(parts['upload'].get_filename(), '答案.pdf')
        self.assertEqual(parts['sakai_csrf_token'].get_payload(decode=True), b'csrf-1')
        self.assertEqual(parts['assignmentId'].get_payload(decode=True), b'$assignmentReference')
        self.assertEqual(client.requests[3][1]['sakai_csrf_token'], 'csrf-2')
        self.assertEqual(client.requests[3][1]['option'], 'confirm')
        self.assertEqual(client.requests[4][1]['sakai_csrf_token'], 'csrf-3')
        self.assertEqual(client.requests[4][1]['option'], 'post')

    def test_inline_submission_escapes_plain_text_and_preserves_line_breaks(self):
        expected = '<p>a &lt; b<br />\n二行目</p>'
        client = FakeClient([(VIEW, form(files=False)),
                             (ENDPOINT, form('csrf-2', confirm=True)),
                             (ENDPOINT, '<p>Submitted</p>')], receipt=receipt(expected),
                            submission_type='TEXT_ONLY_ASSIGNMENT_SUBMISSION')
        result = self.submit(client, text_file=self.text, execute=True)
        self.assertTrue(result['verified'])
        self.assertEqual(client.requests[1][1][TEXT], expected)
        self.assertFalse(any(isinstance(data, bytes) for _, data, _ in client.requests))

    def test_submitted_work_requires_resubmit_and_keeps_old_attachments(self):
        old = {'name': 'old.pdf', 'ref': '/content/old'}
        previous = receipt('<p>old answer</p>', [old], timestamp=100)
        client = FakeClient([(VIEW, form(text='<p>old answer</p>'))], previous=previous)
        with self.assertRaisesRegex(KulmsError, '提出済み'):
            self.submit(client, text_file=self.text, execute=True)
        self.assertTrue(all(data is None for _, data, _ in client.requests))
        client = FakeClient([(VIEW, form()), (ENDPOINT, form(confirm=True)),
                             (ENDPOINT, '<p>Submitted</p>')], previous=previous,
                            receipt=receipt('<p>a &lt; b<br/>二行目</p>', [old]))
        self.assertTrue(self.submit(client, text_file=self.text, execute=True, resubmit=True)['verified'])

    def test_old_receipt_text_mismatch_and_lost_attachments_never_report_success(self):
        old = {'name': 'old.pdf', 'ref': '/content/old'}
        previous = receipt(attachments=[old], timestamp=100)
        for after in (receipt('<p>a &lt; b<br/>二行目</p>', [old], timestamp=100),
                      receipt('<p>wrong answer</p>', [old]), receipt('<p>a &lt; b<br/>二行目</p>')):
            client = FakeClient([(VIEW, form()), (ENDPOINT, form(confirm=True)),
                                 (ENDPOINT, '<p>Submitted</p>')], previous=previous, receipt=after)
            with self.assertRaisesRegex(KulmsError, 'verify段階'):
                self.submit(client, text_file=self.text, execute=True, resubmit=True)
            self.assertEqual(len(client.requests), 3)

    def test_wrong_uploaded_bytes_stop_before_confirm_and_are_not_retried(self):
        client = FakeClient([(VIEW, form()), (upload_url('csrf-1'), form('csrf-2', link=True)),
                             (FILE_URL, b'wrong bytes')])
        with self.assertRaisesRegex(KulmsError, 'upload段階'):
            self.submit(client, files=[self.pdf], execute=True)
        self.assertEqual(len(client.requests), 3)

    def test_lost_post_response_is_uncertain_and_not_retried(self):
        client = FakeClient([(VIEW, form()), (ENDPOINT, form(confirm=True)),
                             (ENDPOINT, TimeoutError('fake connection failure'))])
        with self.assertRaisesRegex(KulmsError, 'post段階'):
            self.submit(client, text_file=self.text, execute=True)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.assignments.call_count, 1)

    def test_cross_course_wrong_assignment_missing_csrf_and_disabled_forms_are_blocked(self):
        cases = [form(action=LMS + '/portal/site/other/tool/tool-1?panel=Main'),
                 form(assignment='wrong-assignment'), form(disabled=True),
                 form().replace('name="sakai_csrf_token"', 'name="not-csrf"'),
                 form(extra='<input name="submit_on_behalf_of" value="another-student">'),
                 form(extra='<input type="checkbox" name="review_service_eula_agreement">'),
                 form(extra='<select name="selectedGroups"></select>')]
        for body in cases:
            with self.subTest(body=body):
                client = FakeClient([(VIEW, body)])
                with self.assertRaises(KulmsError):
                    self.submit(client, files=[self.pdf], execute=True)
                self.assertTrue(all(data is None for _, data, _ in client.requests))

    def test_upload_action_and_token_are_checked_during_dry_run(self):
        for body in (form().replace('doAttachUpload', 'doRemove_attachment'),
                     form().replace('sakai_csrf_token=csrf-1', 'sakai_csrf_token=wrong'),
                     form().replace(upload_url('csrf-1').replace('&', '&amp;'), 'https://example.com/collect')):
            client = FakeClient([(VIEW, body)])
            with self.assertRaises(KulmsError):
                self.submit(client, files=[self.pdf])
            self.assertTrue(all(data is None for _, data, _ in client.requests))

    def test_additional_confirmation_blocks_post_without_accepting_it(self):
        client = FakeClient([(VIEW, form()), (ENDPOINT, form(confirm=True) +
                             '<input name="submissionConfirmCheck" type="checkbox">')])
        with self.assertRaisesRegex(KulmsError, 'confirm段階'):
            self.submit(client, text_file=self.text, execute=True)
        self.assertEqual(len(client.requests), 2)

    def test_invalid_local_payloads_fail_before_network(self):
        cases = ({}, {'files': [self.directory]}, {'files': [self.directory / 'missing.pdf']},
                 {'files': [self.pdf, self.pdf]}, {'text_file': self.pdf},
                 {'text_file': self.text, 'html_file': self.text}, {'files': [self.pdf] * 6})
        for arguments in cases:
            client = FakeClient([])
            with self.assertRaises(KulmsError):
                self.submit(client, **arguments)
            client.assignments.assert_not_called()
            self.assertEqual(client.requests, [])

    def test_tool_discovery_reads_assignments_page_when_api_omits_tools(self):
        page_url = LMS + '/portal/site/course-1/page/page-1'
        client = FakeClient([(page_url, '<form action="' + ENDPOINT + '"></form>'),
                             (VIEW, '<div class="alertMessage">Deadline warning</div>' + form())])
        client.courses.return_value = [{'id': COURSE, 'title': 'Fake course',
                                        'sitePages': [{'id': 'page-1', 'title': '課題'}]}]
        self.assertTrue(self.submit(client, files=[self.pdf])['dry_run'])
        self.assertEqual([url for url, _, _ in client.requests], [page_url, VIEW])
        self.assertTrue(all(data is None for _, data, _ in client.requests))

    def test_explicit_replacement_removes_only_named_attachment_then_uploads(self):
        old_ref = '/content/attachment/course-1/Assignments/old/old.pdf'
        old_url = LMS + '/access' + old_ref
        removal = "ASN.doStudentViewSubmissionAction('addSubmissionForm', 'removeAttachment', '/attachment/course-1/Assignments/old/old.pdf')"
        old_link = '<a href="' + old_url + '" onclick="' + removal + '">old.pdf</a>'
        keep = {'name': 'keep.pdf', 'ref': '/content/keep'}
        previous = {**receipt(attachments=[{'name': 'old.pdf', 'ref': old_ref}, keep], timestamp=100), 'canSubmit': True}
        after = receipt(attachments=[keep, {'name': self.pdf.name, 'ref': '/content/new',
                                           'url': FILE_URL, 'size': len(CONTENT)}])
        client = FakeClient([(VIEW, form(extra=old_link, disabled=True)), (ENDPOINT, form('csrf-2', disabled=True)),
                             (upload_url('csrf-2'), form('csrf-3', link=True)),
                             (FILE_URL, CONTENT), (ENDPOINT, form('csrf-4', confirm=True)),
                             (ENDPOINT, '<p>Submitted</p>'), (FILE_URL, CONTENT)],
                            previous=previous, receipt=after, submission_type='ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION')
        result = self.submit(client, files=[self.pdf], replace_attachments=[old_ref],
                             execute=True, resubmit=True)
        self.assertTrue(result['verified'])
        self.assertEqual(result['replace_attachments'], ['old.pdf'])
        self.assertEqual(client.requests[1][1]['option'], 'removeAttachment')
        self.assertEqual(client.requests[1][1]['currentAttachment'], old_ref[len('/content'):])
        self.assertEqual(client.requests[2][0], upload_url('csrf-2'))
        self.assertEqual(client.steps, [])

    def test_unknown_or_unremovable_attachment_is_not_deleted(self):
        ref = '/content/attachment/course-1/Assignments/old/old.pdf'
        for previous, requested in ((None, ref), (receipt(attachments=[{'ref': ref}]), ref)):
            client = FakeClient([(VIEW, form())], previous=previous)
            with self.assertRaises(KulmsError):
                self.submit(client, files=[self.pdf], replace_attachments=[requested], execute=True, resubmit=True)
            self.assertTrue(all(data is None for _, data, _ in client.requests))


if __name__ == '__main__':
    unittest.main()
