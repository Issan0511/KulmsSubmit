from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from kulms_submit.KulmsClient import KulmsError
from kulms_submit.KulmsWorkflow import Workflow, CATEGORIES, PageRequirements
from test_kulms_submission import FakeClient, Response, COURSE, ASSIGNMENT, ENDPOINT, VIEW, form, receipt, TEXT


def pdf_bytes(text='problem 1: show the calculation'):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), text)
    data = doc.tobytes()
    doc.close()
    return data


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.answer = self.root / 'answer.txt'
        self.answer.write_text('Calculation: 1 + 1 = 2', encoding='utf-8')
        self.instructions = 'Give your calculation.'
        self.requirement_bytes = pdf_bytes()
        self.old_answer = None
        self.fail_post = False
        self.writes = []
        self.clients = []
        self.factory_calls = 0
        owner = self

        class Client:
            def __init__(self):
                self.posted = False
                self.text = ''
            def courses(self):
                return [{'id': COURSE, 'title': 'Course', 'sitePages': [{'tools': [
                    {'id': 'tool-1', 'toolId': 'sakai.assignment.grades'}]}]}]
            def assignments(self, course):
                a = {'id': ASSIGNMENT, 'context': COURSE, 'title': 'Report',
                     'instructions': '<p>' + owner.instructions + '</p>', 'anonymousGrading': False,
                     'submissionType': 'TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION',
                     'attachments': [{'name': 'problem.pdf', 'url': 'https://lms.gakusei.kyoto-u.ac.jp/access/content/problem.pdf'}]}
                if self.posted:
                    a['submissions'] = [receipt(self.text)]
                elif owner.old_answer:
                    a['submissions'] = [receipt(attachments=[owner.old_answer], timestamp=100)]
                return [a]
            def _open(self, url, data=None, **kwargs):
                if url.endswith('/problem.pdf'):
                    return Response(owner.requirement_bytes, url)
                if url.endswith('/old.pdf'):
                    return Response(pdf_bytes('retained answer'), url)
                if data is None:
                    return Response('<div class="textPanel">' + owner.instructions + '</div>' + form(), url)
                owner.writes.append(deepcopy(data))
                if data['option'] == 'confirm':
                    self.text = data[TEXT]
                    return Response(form('csrf-2', confirm=True), url)
                if data['option'] == 'post':
                    if owner.fail_post:
                        raise TimeoutError('connection failed')
                    self.posted = True
                    return Response('Submitted', url)
                raise AssertionError(data)

        @contextmanager
        def factory():
            owner.factory_calls += 1
            c = Client(); owner.clients.append(c)
            yield c
        self.flow = Workflow(self.root / 'state', client_factory=factory)
        self.flow.set_profile('Test Student', '123', 'B3')

    def prepare(self):
        return self.flow.prepare(COURSE, ASSIGNMENT, text_file=str(self.answer))

    def review(self, job, status='pass'):
        for item in job['artifacts']:
            if item['role'] != 'backup':
                for page in range(1, item['pages'] + 1):
                    metadata, png = self.flow.read_artifact(job['id'], item['id'], page)
                    if item['kind'] == 'pdf':
                        self.assertTrue(png.startswith(b'\x89PNG'))
        check = {'status': status, 'requirement': 'Show calculation', 'source': 'problem.pdf page 1',
                 'evidence': 'inline.html: Calculation: 1 + 1 = 2'}
        return self.flow.record_review(job['id'], job['fingerprint'],
                    {k: dict(check) for k in CATEGORIES}, [dict(check)], 'All requirements checked.')

    def test_read_prepare_review_submit_and_duplicate_use_receipt(self):
        job = self.prepare()
        self.assertEqual(self.writes, [])
        self.assertEqual(job['state'], 'prepared')
        self.assertEqual(job['requirements']['page_instruction_panels'], ['Give your calculation.'])
        self.review(job)
        result = self.flow.submit(job['id'], completion_signal='This answer is finished; submit it.')
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.writes), 2)
        calls = self.factory_calls
        again = self.flow.submit(job['id'], completion_signal='Submit it.')
        self.assertTrue(again['already_submitted'])
        self.assertEqual(self.factory_calls, calls)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.flow.status(job['id'])['state'], 'submitted')

    def test_no_review_or_uncertain_review_cannot_send(self):
        job = self.prepare()
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal='Submit')
        self.review(job, status='uncertain')
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal='Submit')
        self.assertEqual(self.writes, [])

    def test_pass_requires_all_pages_and_real_check_records(self):
        job = self.prepare()
        check = {'status': 'pass', 'requirement': 'test', 'source': 'instructions', 'evidence': 'test answer'}
        with self.assertRaisesRegex(KulmsError, '未確認'):
            self.flow.record_review(job['id'], job['fingerprint'], {k: check for k in CATEGORIES}, [check], 'Checked')
        with self.assertRaises(KulmsError):
            self.flow.record_review(job['id'], job['fingerprint'], {'filename': check}, [check], 'Checked')
        with self.assertRaises(KulmsError):
            self.flow.record_review(job['id'], 'wrong', {k: check for k in CATEGORIES}, [check], 'Checked')

    def test_changes_block_before_any_mutation(self):
        for change in ('source', 'snapshot', 'instructions', 'attachment', 'profile'):
            with self.subTest(change=change):
                job = self.prepare(); self.review(job)
                _, stored = self.flow.load(job['id'])
                if change == 'source': self.answer.write_text('changed')
                if change == 'snapshot': Path(stored['options']['text_file']).write_text('changed')
                if change == 'instructions': self.instructions += ' and prove it'
                if change == 'attachment': self.requirement_bytes = pdf_bytes('different problem')
                if change == 'profile': self.flow.set_profile('Changed Student', '321', 'B4')
                with self.assertRaises(KulmsError):
                    self.flow.submit(job['id'], completion_signal='Submit')
                self.assertEqual(self.writes, [])
                # Avoid reusing a deliberately corrupted snapshot in later cases.
                self.answer.write_text('next answer ' + change)

    def test_exact_prepare_reuses_review_without_extra_model_calls(self):
        first = self.prepare(); self.review(first)
        second = self.prepare()
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['reused'])
        self.assertEqual(second['state'], 'reviewed')
        self.assertEqual(self.writes, [])

    def test_changed_cached_snapshot_is_not_reused(self):
        first = self.prepare(); self.review(first)
        _, stored = self.flow.load(first['id'])
        Path(stored['options']['text_file']).write_text('edited snapshot')
        second = self.prepare()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(second['state'], 'prepared')

    def test_post_failure_is_persistent_uncertain_and_never_retried(self):
        job = self.prepare(); self.review(job); self.fail_post = True
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal='Submit')
        self.assertEqual(self.flow.status(job['id'])['state'], 'uncertain')
        calls, writes = self.factory_calls, len(self.writes)
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal='Submit')
        self.assertEqual((calls, writes), (self.factory_calls, len(self.writes)))

    def test_retained_answers_are_included_and_resubmit_required(self):
        self.old_answer = {'name': 'old.pdf', 'ref': '/content/old', 'size': 200,
                          'url': 'https://lms.gakusei.kyoto-u.ac.jp/access/content/old.pdf'}
        job = self.prepare()
        self.assertIn('existing_answer', [a['role'] for a in job['artifacts']])
        self.review(job)
        with self.assertRaisesRegex(KulmsError, '再提出'):
            self.flow.submit(job['id'], completion_signal='Submit')
        self.assertEqual(self.writes, [])

    def test_lock_and_empty_completion_signal_fail_without_network(self):
        job = self.prepare(); self.review(job)
        calls = self.factory_calls
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal=' ')
        path, _ = self.flow.load(job['id']); (path / '.lock').touch()
        with self.assertRaises(KulmsError): self.flow.submit(job['id'], completion_signal='Submit')
        self.assertEqual(self.factory_calls, calls)

    def test_copies_are_renamed_without_changing_original(self):
        source = self.root / 'original.pdf'; data = pdf_bytes('answer'); source.write_bytes(data)
        job = self.flow.prepare(COURSE, ASSIGNMENT, files=[str(source)], output_names=['report.pdf'])
        self.assertEqual(job['summary']['files'][0]['name'], 'report.pdf')
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(self.writes, [])

    def test_outside_names_and_job_paths_are_rejected(self):
        for name in ('../escape.pdf', 'bad\nfile.pdf', '/tmp/leak.pdf'):
            with self.assertRaises(KulmsError):
                self.flow.prepare(COURSE, ASSIGNMENT, files=[str(self.answer)], output_names=[name])
        with self.assertRaises(KulmsError): self.flow.status('../../escape')

    def test_parser_omits_tokens_scripts_and_hidden_fields(self):
        p = PageRequirements('<div class="textPanel"><p>Rule</p><script>secret()</script></div>'
                             '<input type="hidden" value="csrf-secret">')
        self.assertEqual(p.panels, ['Rule'])


if __name__ == '__main__':
    unittest.main()
