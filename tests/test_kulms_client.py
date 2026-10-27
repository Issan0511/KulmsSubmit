import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request

from kulms_submit.KulmsClient import AUTH, LMS, KulmsClient, KulmsError, _Page, _Redirects
from kulms_submit.KulmsLogin import LoginCredentials
from kulms_submit.KulmsOTP import TOTPConfig


# Fake credentials and fixtures only. These tests never access the network.
PASSWORD = 'fake-password-for-client-tests'
OTP = '123456'
SSO = AUTH + '/saml/saml2/idp/SSOService.php'
LOGIN = AUTH + '/pub/login.cgi'
PICKER = AUTH + '/user/authselect.php'
FIDO = AUTH + '/pub/u2flogin.cgi'
TOKEN = AUTH + '/pub/otplogin.cgi'
SAML = LMS + '/Shibboleth.sso/SAML2/POST'


class Response(io.BytesIO):
    def __init__(self, body, url, content_type='text/html'):
        super().__init__(body.encode() if isinstance(body, str) else body)
        self.url = url
        self.headers = {'Content-Type': content_type}


class Opener:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        expected, response = self.steps.pop(0)
        if request.full_url != expected:
            raise AssertionError('unexpected request URL')
        return response


def form(action, fields, extra=''):
    inputs = ''.join(f'<input name="{key}" type="{kind}" value="{value}">'
                     for key, kind, value in fields)
    return f'<form method="post" action="{action}">{inputs}{extra}</form>'


def login_steps(*, rejected=False):
    login_form = form('/pub/login.cgi', [('username', 'text', ''), ('password', 'password', ''),
                                       ('sessid', 'hidden', 'fake-session'), ('save_user_check', 'checkbox', 'yes')])
    picker_form = form('/user/authselect.php', [('remember', 'checkbox', 'yes')],
                       '<select name="auth"><option value="u2flogin">FIDO</option>'
                       '<option value="otplogin">OTP</option></select>')
    otp_form = form('/pub/otplogin.cgi', [('username', 'hidden', 'fake-id'),
                                       ('password', 'number', ''), ('sessid', 'hidden', 'fake-session')])
    saml_form = form(SAML, [('SAMLResponse', 'hidden', 'fake-saml'), ('RelayState', 'hidden', 'fake-relay')])
    refresh = '<meta http-equiv="refresh" content="0; URL=' + SSO + '">'
    steps = [
        (LMS + '/portal/login', Response(login_form, LOGIN)),
        (LOGIN, Response(refresh, LOGIN)),
        (SSO, Response('<script>location.href="' + FIDO + '";</script>', PICKER)),
        (FIDO, Response('<a id="back_link_select" href="/user/authselect.php?back=fake">戻る</a>'
                        '<script>target = back_link_select + "&select=1";</script>', FIDO)),
        (PICKER + '?back=fake&select=1', Response(picker_form, PICKER)),
        (PICKER, Response(otp_form, TOKEN)),
        (TOKEN, Response(otp_form if rejected else refresh, TOKEN)),
    ]
    if not rejected:
        steps += [(SSO, Response(saml_form, SSO)),
                  (SAML, Response('<a href="/portal/logout">ログアウト</a>', LMS + '/portal'))]
    return steps


class ClientTests(unittest.TestCase):
    def client(self):
        return KulmsClient(LoginCredentials('fake-id', PASSWORD),
                           TOTPConfig('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'))

    def test_login_from_password_through_fido_picker_otp_and_saml(self):
        client = self.client()
        opener = Opener(login_steps())
        client._opener = opener
        with patch('kulms_submit.KulmsClient.current_code', return_value={'code': OTP}) as generate:
            client.login()
            client.login()
        self.assertTrue(client.authenticated)
        self.assertEqual(len(opener.requests), 9)
        self.assertEqual(generate.call_count, 1)
        password_post = parse_qs(opener.requests[1].data.decode())
        self.assertEqual(password_post['password'], [PASSWORD])
        self.assertNotIn('save_user_check', password_post)
        picker_post = parse_qs(opener.requests[5].data.decode())
        self.assertEqual(picker_post, {'auth': ['otplogin']})
        self.assertEqual(parse_qs(opener.requests[6].data.decode())['password'], [OTP])
        self.assertEqual(parse_qs(opener.requests[8].data.decode())['SAMLResponse'], ['fake-saml'])
        client.close()
        self.assertFalse(client.authenticated)

    def test_rejected_otp_is_not_retried_and_errors_are_redacted(self):
        client = self.client()
        client._opener = Opener(login_steps(rejected=True))
        with patch('kulms_submit.KulmsClient.current_code', return_value={'code': OTP}):
            with self.assertRaises(KulmsError) as caught:
                client.login()
        self.assertIn('OTP', str(caught.exception))
        self.assertNotIn(OTP, str(caught.exception))
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertEqual(len(client._opener.requests), 7)
        self.assertFalse(client.authenticated)

    def test_credentials_and_saml_are_never_posted_to_changed_destinations(self):
        client = self.client()
        for target in ('https://example.com/collect', AUTH + '/user/settings.php',
                       LMS + '/pub/login.cgi'):
            page = _Page(form(target, [('username', 'text', ''), ('password', 'password', '')]), LOGIN)
            with patch.object(client, '_page') as send:
                with self.assertRaises(KulmsError):
                    client._submit(page, page.forms[0], {'password': PASSWORD}, LOGIN)
                send.assert_not_called()
        redirect = _Redirects()
        for target in ('http://auth.iimc.kyoto-u.ac.jp/x', 'https://example.com/x',
                       AUTH + ':444/x', 'https://user:pass@auth.iimc.kyoto-u.ac.jp/x'):
            with self.assertRaises(KulmsError):
                redirect.redirect_request(Request(LOGIN), None, 302, '', {}, target)
        redirect.hosts = {'lms.gakusei.kyoto-u.ac.jp'}
        with self.assertRaises(KulmsError):
            redirect.redirect_request(Request(LMS), None, 302, '', {}, LOGIN)

    def test_courses_are_paginated_and_duplicate_pages_are_rejected(self):
        client = self.client()
        first = [{'id': str(i), 'title': f'Course {i}'} for i in range(50)]
        with patch.object(client, 'login'), patch.object(client, '_json', side_effect=[
                {'site_collection': first}, {'site_collection': [{'id': 'last'}]}]) as fetch:
            self.assertEqual(len(client.courses()), 51)
        self.assertEqual(fetch.call_args_list[0].args, ('/direct/site.json', {'_limit': 50, '_start': 0}))
        self.assertEqual(fetch.call_args_list[1].args[1]['_start'], 50)
        with patch.object(client, '_json', side_effect=[{'site_collection': first}] * 2):
            with self.assertRaises(KulmsError):
                client.courses()

    def test_assignment_test_and_resource_routes_and_invalid_site_ids(self):
        client = self.client()
        with patch.object(client, '_json', side_effect=[{'assignment_collection': []},
                {'assignment_collection': []}, {'sam_pub_collection': []}, {'content_collection': []}]) as fetch:
            self.assertEqual(client.assignments(), [])
            self.assertEqual(client.assignments('course-1'), [])
            self.assertEqual(client.tests('course-1'), [])
            self.assertEqual(client.materials('course-1'), [])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            '/direct/assignment/my.json', '/direct/assignment/site/course-1.json',
            '/direct/sam_pub/context/course-1.json', '/direct/content/site/course-1.json'])
        with patch.object(client, '_json') as fetch:
            for value in ('../x', '.', '..', 'x?y=z', 'x/y', None, ''):
                with self.assertRaises(KulmsError):
                    client.tests(value)
            fetch.assert_not_called()

    def test_materials_use_unique_urls_when_entity_reference_is_generic(self):
        client = self.client()
        resource = {'entityReference': '/content',
                    'url': LMS + '/access/content/group/course-1/lecture.pdf'}
        folder = {'entityReference': '/content', 'type': 'collection',
                  'url': LMS + '/access/content/group/course-1/'}
        with patch.object(client, '_json', return_value={'content_collection': [folder, resource]}):
            rows = client.materials('course-1')
            self.assertEqual(rows[1]['id'], resource['url'])
            self.assertNotEqual(rows[0]['id'], rows[1]['id'])

    def test_json_response_must_match_path_type_and_collection_shape(self):
        client = self.client()
        client.authenticated = True
        for response in (Response('<html>login</html>', LMS + '/portal'),
                         Response('not json', LMS + '/direct/site.json', 'application/json')):
            with patch.object(client, '_open', return_value=response):
                with self.assertRaises(KulmsError):
                    client.courses()
        for payload in ({}, {'site_collection': {}}, {'site_collection': ['bad']}):
            with patch.object(client, '_json', return_value=payload):
                with self.assertRaises(KulmsError):
                    client.courses()
        client._opener.open = lambda *a, **k: (_ for _ in ()).throw(
            HTTPError(LOGIN, 403, PASSWORD, {}, io.BytesIO(PASSWORD.encode())))
        with self.assertRaises(KulmsError) as caught:
            client._open(LMS + '/direct/site.json')
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_download_uses_listed_resource_and_never_overwrites(self):
        client = self.client()
        url = LMS + '/access/content/group/course-1/lecture.pdf'
        resource = {'id': '/group/course-1/lecture.pdf', 'url': url, 'type': 'application/pdf'}
        content = b'%PDF-fake-lecture'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'lecture.pdf'
            with patch.object(client, 'materials', return_value=[resource]), \
                    patch.object(client, '_open', side_effect=lambda *a: Response(content, url)):
                result = client.download('course-1', resource['id'], output)
                self.assertEqual(output.read_bytes(), content)
                self.assertEqual(result['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(result['bytes'], len(content))
                with self.assertRaises(KulmsError):
                    client.download('course-1', resource['id'], output)
                self.assertEqual(output.read_bytes(), content)
            for item in ({**resource, 'url': 'https://example.com/file.pdf'},
                         {**resource, 'url': LMS + '/portal'},
                         {**resource, 'type': 'text/url'}, {**resource, 'type': 'collection'}):
                with patch.object(client, 'materials', return_value=[item]), patch.object(client, '_open') as fetch:
                    with self.assertRaises(KulmsError):
                        client.download('course-1', resource['id'], output)
                    fetch.assert_not_called()
            with patch.object(client, 'materials', return_value=[resource]), patch.object(client, '_open') as fetch:
                with self.assertRaises(KulmsError):
                    client.download('course-1', 'https://example.com/file.pdf', output)
                fetch.assert_not_called()

    def test_partial_download_is_removed_after_failure(self):
        client = self.client()
        url = LMS + '/access/content/group/course-1/lecture.pdf'
        resource = {'id': 'resource', 'url': url, 'type': 'application/pdf'}
        response = Response(b'', url)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'partial.pdf'
            with patch.object(client, 'materials', return_value=[resource]), \
                    patch.object(client, '_open', return_value=response), \
                    patch.object(response, 'read', side_effect=[b'partial', OSError(PASSWORD)]):
                with self.assertRaises(KulmsError) as caught:
                    client.download('course-1', 'resource', output)
            self.assertFalse(output.exists())
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_multipart_transport_keeps_raw_bytes_and_content_type(self):
        client = self.client()
        url = LMS + '/portal/site/course-1/tool/tool-1?panel=Main'
        client._opener = Opener([(url, Response('ok', url))])
        body = b'--fake-boundary\r\n\x00\xff\r\n--fake-boundary--\r\n'
        mime = 'multipart/form-data; boundary=fake-boundary'
        with client._open(url, body, content_type=mime, referer=url):
            pass
        request = client._opener.requests[0]
        self.assertEqual(request.data, body)
        self.assertEqual(request.get_header('Content-type'), mime)
        self.assertEqual(request.get_header('Origin'), LMS)


if __name__ == '__main__':
    unittest.main()
