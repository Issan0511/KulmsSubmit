"""Authenticated access to Kyoto University's Sakai APIs.

Credentials and the session cookie stay in memory. Assignment writes are
implemented separately in KulmsSubmission, behind an explicit execute option.
"""

import hashlib
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .KulmsLogin import LoginCredentials, LoginError, LoginStore
from .KulmsOTP import OTPStore, TOTPConfig
from .KulmsOTPGate import claim_otp


LMS = 'https://lms.gakusei.kyoto-u.ac.jp'
AUTH = 'https://auth.iimc.kyoto-u.ac.jp'
_LMS_HOST = urlsplit(LMS).hostname
_AUTH_HOST = urlsplit(AUTH).hostname
_JSON_LIMIT = 16 * 1024 * 1024
_DOWNLOAD_LIMIT = 512 * 1024 * 1024


class KulmsError(LoginError):
    """A displayable error that never contains authentication response data."""


class KulmsSessionExpired(KulmsError):
    """A confirmed expired session, not a generic authorization failure."""


def _checked_url(url, hosts):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname in hosts
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.fragment
                 and not any(c in url for c in '\r\n\x00'))
    except ValueError:
        valid = False
    if not valid:
        raise KulmsError('大学の認証・LMS以外への通信先を検出したため停止しました。')
    return url


def _site_segment(site_id):
    if (not isinstance(site_id, str) or site_id in ('.', '..')
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', site_id)):
        raise KulmsError('coursesで確認したコースIDを指定してください。')
    return quote(site_id, safe='')


class _Redirects(HTTPRedirectHandler):
    def __init__(self):
        self.hosts = {_LMS_HOST, _AUTH_HOST}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.hosts == {_LMS_HOST} and urlsplit(newurl).hostname == _AUTH_HOST:
            raise KulmsSessionExpired('認証期限が切れました。')
        _checked_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Page(HTMLParser):
    def __init__(self, body, url, refresh=''):
        super().__init__()
        self.url = url
        self.forms = []
        self.links = []
        self.redirects = []
        self._form = None
        self._select = None
        self._link = None
        self.feed(body)
        self._refresh(refresh)
        for match in re.finditer(
                r'''(?:location(?:\.href)?\s*=|location\.(?:replace|assign)\()\s*["']([^"']+)["']''', body):
            self._redirect(match[1])
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                for value in payload.values():
                    if isinstance(value, str) and value.startswith(('https://', '/')):
                        self._redirect(value)
        except ValueError:
            pass
        # The FIDO page appends a literal query parameter to its method picker.
        # Extract that literal; never execute the university's JavaScript.
        suffix = re.search(r'''back_link_select\s*\+\s*["']([^"']+)["']''', body)
        if suffix:
            for link in self.links:
                if link.get('id') == 'back_link_select' and suffix[1] not in link['href']:
                    link['href'] += suffix[1]

    def _refresh(self, value):
        match = re.search(r'url\s*=\s*(.+)', value, re.I)
        if match:
            self._redirect(match[1].strip().strip('\"\''))

    def _redirect(self, value):
        target = urljoin(self.url, value)
        if target not in self.redirects:
            self.redirects.append(target)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh':
            self._refresh(attrs.get('content', ''))
        elif tag == 'form':
            self._form = {'action': attrs.get('action', ''),
                          'method': attrs.get('method', 'get').lower(),
                          'fields': [], 'selects': []}
            self.forms.append(self._form)
        elif tag == 'input' and self._form is not None:
            if attrs.get('name') and 'disabled' not in attrs:
                self._form['fields'].append(attrs)
        elif tag == 'select' and self._form is not None:
            self._select = {'name': attrs.get('name'), 'options': []}
            self._form['selects'].append(self._select)
        elif tag == 'option' and self._select is not None:
            self._select['options'].append(attrs.get('value'))
        elif tag == 'a' and attrs.get('href'):
            self._link = dict(attrs, text='')
            self.links.append(self._link)

    def handle_endtag(self, tag):
        if tag == 'form':
            self._form = None
        elif tag == 'select':
            self._select = None
        elif tag == 'a':
            self._link = None

    def handle_data(self, data):
        if self._link is not None:
            self._link['text'] += data


def _has_fields(form, *names):
    return set(names) <= {item['name'] for item in form['fields']}


class KulmsClient:
    def __init__(self, credentials: LoginCredentials, otp: TOTPConfig):
        self._credentials = credentials
        self._otp = otp
        self._cookies = CookieJar()
        self._redirects = _Redirects()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies), self._redirects)
        self.authenticated = False

    @classmethod
    def from_keyring(cls):
        credentials = LoginStore().load()
        otp = OTPStore().load()
        if credentials is None or otp is None:
            raise KulmsError('ID・パスワードとOTPをsetupで登録してください。')
        return cls(credentials, otp)

    def close(self):
        self._cookies.clear()
        self.authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _open(self, url, data=None, *, authentication=False, referer=None, content_type=None):
        hosts = {_LMS_HOST, _AUTH_HOST} if authentication else {_LMS_HOST}
        _checked_url(url, hosts)
        self._redirects.hosts = hosts
        headers = {'User-Agent': 'BeamerPlus KULMS/1.0', 'Cache-Control': 'no-store'}
        if referer:
            headers['Referer'] = referer
        if data is not None:
            headers['Content-Type'] = content_type or 'application/x-www-form-urlencoded'
            headers['Origin'] = AUTH if urlsplit(url).hostname == _AUTH_HOST else LMS
        encoded = data if isinstance(data, bytes) else urlencode(data).encode() if data is not None else None
        request = Request(url, data=encoded,
                          headers=headers)
        try:
            response = self._opener.open(request, timeout=30)
        except KulmsSessionExpired:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401 and not authentication:
                raise KulmsSessionExpired('認証期限が切れました。') from None
            if status in (401, 403):
                raise KulmsError('認証期限切れ、またはこの情報へのアクセス権がありません。') from None
            raise KulmsError(f'LMSからHTTP {status}が返されました。') from None
        except (URLError, OSError, HTTPException, ValueError):
            raise KulmsError('大学のサーバーに接続できませんでした。') from None
        try:
            _checked_url(response.url, hosts)
        except KulmsError:
            response.close()
            raise
        return response

    def _page(self, url, data=None, *, referer=None):
        try:
            with self._open(url, data, authentication=True, referer=referer) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise KulmsError('認証画面の応答が大きすぎます。')
                return _Page(body.decode('utf-8', errors='replace'), response.url,
                             response.headers.get('Refresh', ''))
        except (URLError, OSError, HTTPException):
            raise KulmsError('認証画面の読み取り中に通信が途切れました。') from None

    def _submit(self, page, form, updates, expected_url):
        action = urljoin(page.url, form['action'])
        if form['method'] != 'post' or action != expected_url:
            raise KulmsError('想定と異なる認証フォームのため停止しました。')
        values = {}
        for item in form['fields']:
            kind = item.get('type', 'text').lower()
            # Do not change the user's remembered authentication method.
            if item['name'] in ('remember', 'saveauthmethod', 'save_user_check'):
                continue
            if kind in ('submit', 'button', 'file', 'reset', 'checkbox', 'radio'):
                continue
            values[item['name']] = item.get('value', '')
        values.update(updates)
        return self._page(action, values, referer=page.url)

    def login(self):
        if self.authenticated:
            return
        page = self._page(LMS + '/portal/login')
        form = next((f for f in page.forms if _has_fields(f, 'username', 'password')), None)
        if form is None or urlsplit(page.url).hostname != _AUTH_HOST:
            raise KulmsError('京大のログインフォームを確認できませんでした。')
        page = self._submit(page, form, {'username': self._credentials.username,
                                        'password': self._credentials.password},
                            AUTH + '/pub/login.cgi')
        otp_sent = False
        for _ in range(12):
            parsed = urlsplit(page.url)
            if (parsed.hostname == _LMS_HOST and parsed.path.startswith('/portal')
                    and any('/logout' in urlsplit(link['href']).path
                            or 'ログアウト' in link['text'] for link in page.links)):
                self.authenticated = True
                return
            if parsed.hostname != _AUTH_HOST:
                raise KulmsError('LMSへのログイン完了を確認できませんでした。')
            if parsed.path == '/pub/login.cgi' and any(_has_fields(f, 'username', 'password') for f in page.forms):
                raise KulmsError('ID・パスワードが受け付けられませんでした。')
            if parsed.path == '/pub/otplogin.cgi':
                form = next((f for f in page.forms if any(
                    x['name'] == 'password' and x.get('type') == 'number'
                    for x in f['fields'])), None)
                if form is not None:
                    if otp_sent:
                        raise KulmsError('OTPが受け付けられませんでした。大学側の登録とPCの時計を確認してください。')
                    code = claim_otp(self._otp)
                    page = self._submit(page, form, {'password': code}, AUTH + '/pub/otplogin.cgi')
                    otp_sent = True
                    continue
                if not otp_sent:
                    raise KulmsError('OTPの入力フォームを確認できませんでした。')
            if parsed.path == '/pub/u2flogin.cgi':
                link = next((x for x in page.links if x.get('id') == 'back_link_select'), None)
                if link is None:
                    raise KulmsError('認証方式をOTPへ切り替えられませんでした。')
                target = urljoin(page.url, link['href'])
                if urlsplit(target).path != '/user/authselect.php':
                    raise KulmsError('認証方式の選択先が想定と異なります。')
                page = self._page(target, referer=page.url)
                continue
            picker = next((f for f in page.forms if any(
                s['name'] == 'auth' and 'otplogin' in s['options'] for s in f['selects'])), None)
            if picker is not None:
                page = self._submit(page, picker, {'auth': 'otplogin'}, AUTH + '/user/authselect.php')
                continue
            saml = next((f for f in page.forms if _has_fields(f, 'SAMLResponse')), None)
            if saml is not None and otp_sent:
                page = self._submit(page, saml, {}, LMS + '/Shibboleth.sso/SAML2/POST')
                continue
            if len(page.redirects) == 1:
                target = page.redirects[0]
                if (urlsplit(target).hostname != _AUTH_HOST
                        or urlsplit(target).path not in ('/saml/saml2/idp/SSOService.php', '/user/authselect.php',
                                                       '/pub/u2flogin.cgi', '/pub/otplogin.cgi')):
                    raise KulmsError('想定と異なる認証の遷移先のため停止しました。')
                page = self._page(target, referer=page.url)
                continue
            raise KulmsError('京大の認証画面が想定と異なり、処理を続けられませんでした。')
        raise KulmsError('認証の画面遷移が多すぎるため停止しました。')

    def _json(self, path, parameters=None):
        # GET only: one fresh authentication on a confirmed expiry. No write is retried.
        for attempt in range(2):
            self.login()
            url = LMS + path
            if parameters:
                url += '?' + urlencode(parameters)
            try:
                with self._open(url) as response:
                    if urlsplit(response.url).path in ('/portal/login', '/Shibboleth.sso/Login'):
                        raise KulmsSessionExpired('認証期限が切れました。')
                    if (urlsplit(response.url).path != path
                            or 'json' not in response.headers.get('Content-Type', '').lower()):
                        raise KulmsError('JSONの代わりにログイン画面などが返されました。')
                    body = response.read(_JSON_LIMIT + 1)
            except KulmsSessionExpired:
                self.close()
                if attempt == 0:
                    continue
                raise
            except (URLError, OSError, HTTPException):
                raise KulmsError('APIの読み取り中に通信が途切れました。') from None
            if len(body) > _JSON_LIMIT:
                raise KulmsError('APIの応答が大きすぎます。コースを指定してください。')
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                raise KulmsError('APIのJSONを読み取れませんでした。') from None

    def _collection(self, path, name, *, paginate=False):
        result = []
        seen = set()
        for start in range(0, 10000, 50):
            payload = self._json(path, {'_limit': 50, '_start': start} if paginate else None)
            rows = payload.get(name + '_collection') if isinstance(payload, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise KulmsError('APIの一覧データが想定と異なります。')
            for row in rows:
                identity = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if identity in seen:
                    raise KulmsError('APIが同じデータを繰り返しました。ページ送りを確認してください。')
                seen.add(identity)
                result.append(row)
            if not paginate or len(rows) < 50:
                return result
        raise KulmsError('一覧の上限に達しました。コースを指定してください。')

    def courses(self):
        return self._collection('/direct/site.json', 'site', paginate=True)

    def assignments(self, course=None):
        suffix = 'site/' + _site_segment(course) if course is not None else 'my'
        return self._collection('/direct/assignment/' + suffix + '.json', 'assignment', paginate=True)

    def tests(self, course):
        return self._collection('/direct/sam_pub/context/' + _site_segment(course) + '.json', 'sam_pub')

    def materials(self, course):
        rows = self._collection('/direct/content/site/' + _site_segment(course) + '.json', 'content')
        for item in rows:
            # Content has no id, and entityReference is just "/content" for
            # every item. The resource URL is its stable, unique identifier.
            identity = item.get('id') or item.get('url')
            if not isinstance(identity, str) or not identity:
                raise KulmsError('教材の参照IDを確認できませんでした。')
            item['id'] = identity
        return rows

    def download(self, course, resource_id, output):
        resource = next((item for item in self.materials(course)
                         if item.get('id') == resource_id), None)
        if resource is None:
            raise KulmsError('指定した教材IDがこのコースの教材一覧にありません。')
        if resource.get('type') in ('collection', 'text/url'):
            raise KulmsError('フォルダや外部リンクにはファイルのダウンロードを使えません。')
        url = resource.get('url')
        if not isinstance(url, str):
            raise KulmsError('教材のダウンロードURLを確認できません。')
        _checked_url(url, {_LMS_HOST})
        if not urlsplit(url).path.startswith('/access/content/'):
            raise KulmsError('教材のダウンロード先が想定と異なります。')
        output = Path(output)
        digest = hashlib.sha256()
        count = 0
        created = False
        try:
            with self._open(url) as response:
                if not urlsplit(response.url).path.startswith('/access/content/'):
                    raise KulmsError('教材の代わりに別の画面が返されました。')
                with output.open('xb') as stream:
                    created = True
                    while chunk := response.read(128 * 1024):
                        count += len(chunk)
                        if count > _DOWNLOAD_LIMIT:
                            raise KulmsError('教材が512MiBを超えるため停止しました。')
                        stream.write(chunk)
                        digest.update(chunk)
        except BaseException as exc:
            if created:
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, FileExistsError):
                raise KulmsError('保存先が既に存在します。別のファイル名を指定してください。') from None
            if isinstance(exc, (OSError, URLError, HTTPException)):
                raise KulmsError('教材を保存できませんでした。通信と保存先を確認してください。') from None
            raise
        return {'path': str(output.resolve()), 'bytes': count, 'sha256': digest.hexdigest()}
