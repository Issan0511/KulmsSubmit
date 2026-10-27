"""Sakai's student submission forms, with a read-only preparation default.

Use one client/session for a submission: Sakai keeps the currently selected
assignment and attachments in that session. Never retry a write automatically.
"""

from dataclasses import dataclass, field
import hashlib
from html import escape
from html.parser import HTMLParser
from http.client import HTTPException
import mimetypes
from pathlib import Path
import re
import secrets
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .KulmsClient import LMS, KulmsError, _checked_url, _site_segment


_EVENT = 'eventSubmit_doRead_add_submission_form'
_TEXT = 'Assignment.view_submission_text'
_TOKEN = 'sakai_csrf_token'
_LIMIT = 64 * 1024 * 1024
_TEXT_LIMIT = 1024 * 1024
_TYPES = {'TEXT_ONLY_ASSIGNMENT_SUBMISSION', 'TEXT_AND_ATTACHMENT_ASSIGNMENT_SUBMISSION',
          'ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION', 'SINGLE_ATTACHMENT_SUBMISSION'}


class _Forms(HTMLParser):
    def __init__(self, body, url):
        super().__init__()
        self.body = body  # Memory only; contains CSRF fields. Never persist this HTML.
        self.url = url
        self.forms = []
        self.links = set()
        self.frames = []
        self.anchors = []
        self.extra_confirmation = False
        self.current = None
        self.textarea = None
        self.anchor = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get('name') == 'submissionConfirmCheck':
            self.extra_confirmation = True
        if tag == 'form':
            self.current = {'action': a.get('action', ''), 'method': a.get('method', 'get').lower(),
                            'enctype': a.get('enctype', '').lower(), 'fields': []}
            self.forms.append(self.current)
        elif tag in ('input', 'textarea', 'select', 'button') and self.current is not None and a.get('name'):
            a['tag'] = tag
            self.current['fields'].append(a)
            if tag == 'textarea':
                a['value'] = ''
                self.textarea = a
        if tag == 'a' and a.get('href'):
            self.links.add(urljoin(self.url, a['href']))
            self.anchor = {**a, 'href': urljoin(self.url, a['href']), 'text': ''}
            self.anchors.append(self.anchor)
        if tag in ('iframe', 'frame') and a.get('src'):
            self.frames.append(urljoin(self.url, a['src']))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None
        elif tag == 'textarea':
            self.textarea = None
        elif tag == 'a':
            self.anchor = None

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea['value'] += data
        if self.anchor is not None:
            self.anchor['text'] += data

    def submission_form(self, endpoint, assignment_id, *, button, pending_file_upload=False):
        candidates = [f for f in self.forms if any(x['name'] == _EVENT for x in f['fields'])]
        if len(candidates) != 1:
            raise KulmsError('課題の提出フォームを一意に確認できませんでした。')
        form = candidates[0]
        action = urljoin(self.url, form['action'])
        _checked_url(action, {urlsplit(LMS).hostname})
        if action != endpoint or form['method'] != 'post':
            raise KulmsError('提出フォームの送信先が想定と異なります。')
        by_name = {x['name']: x for x in form['fields']}
        if not all(name in by_name for name in (_TOKEN, _EVENT, 'option', 'assignmentId')):
            raise KulmsError('提出フォームに必要な項目がありません。')
        if not by_name[_TOKEN].get('value'):
            raise KulmsError('課題のCSRFトークンを確認できませんでした。')
        # KULMS currently renders the unresolved literal $assignmentReference
        # in this field. Preserve it; the tool session selects the assignment.
        identity = by_name['assignmentId'].get('value', '')
        reference = '/assignment/a/' + endpoint.split('/site/', 1)[1].split('/tool/', 1)[0] + '/' + assignment_id
        if identity not in (assignment_id, reference, '$assignmentReference', ''):
            raise KulmsError('フォームの課題IDが指定した課題と一致しません。')
        disabled_for_upload = (pending_file_upload and button == 'confirm'
                               and 'upload' in by_name and 'disabled' not in by_name['upload'])
        if button not in by_name or ('disabled' in by_name[button] and not disabled_for_upload):
            raise KulmsError('提出操作が利用できません。期限・提出条件をブラウザで確認してください。')
        if self.extra_confirmation or any(
                x['name'] in ('submit_on_behalf_of', 'selectedGroups', 'originalGroup')
                or x.get('tag') == 'select'
                or (x.get('type') == 'checkbox' and 'checked' not in x
                    and ('required' in x or 'agreement' in x['name'] or 'pledge' in x['name'].lower()))
                for x in form['fields']):
            raise KulmsError('グループ選択・同意などの追加操作が必要です。ブラウザで確認してください。')
        return form


def _values(form):
    result = {}
    for item in form['fields']:
        kind = item.get('type', 'text').lower()
        if 'disabled' in item or kind in ('file', 'button', 'submit', 'reset') or item['tag'] == 'button':
            continue
        if kind in ('checkbox', 'radio') and 'checked' not in item:
            continue
        result[item['name']] = item.get('value', '')
    return result


@dataclass(frozen=True)
class _Upload:
    name: str
    data: bytes = field(repr=False)

    def metadata(self):
        return {'name': self.name, 'bytes': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest()}


def _payload(files, text_file, html_file):
    if text_file is not None and html_file is not None:
        raise KulmsError('本文はtext-fileかhtml-fileのどちらか一方を指定してください。')
    if not files and text_file is None and html_file is None:
        raise KulmsError('提出するfile、text-file、html-fileのいずれかを指定してください。')
    if len(files) > 5:
        raise KulmsError('添付ファイルは一度に5個まで指定してください。')
    uploads = []
    total = 0
    try:
        for path in files:
            path = Path(path)
            if not path.is_file() or any(ord(c) < 32 or ord(c) == 127 for c in path.name):
                raise KulmsError('添付には通常のファイルと、改行などを含まないファイル名を指定してください。')
            with path.open('rb') as stream:
                data = stream.read(_LIMIT - total + 1)
            total += len(data)
            if not data or total > _LIMIT:
                raise KulmsError('空ファイルは提出できません。添付の合計上限は64MiBです。')
            uploads.append(_Upload(path.name, data))
        if len({u.name for u in uploads}) != len(uploads):
            raise KulmsError('同じファイル名の添付は一度に指定できません。')
        text = None
        source = text_file if text_file is not None else html_file
        if source is not None:
            path = Path(source)
            if not path.is_file():
                raise KulmsError('本文にはUTF-8の通常のファイルを指定してください。')
            with path.open('rb') as stream:
                data = stream.read(_TEXT_LIMIT + 1)
            if len(data) > _TEXT_LIMIT:
                raise KulmsError('本文の上限はUTF-8で1MiBです。')
            text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
            if not text.strip() or '\x00' in text:
                raise KulmsError('空の本文やNUL文字を含む本文は提出できません。')
            if text_file is not None:
                text = '<p>' + escape(text).replace('\n', '<br />\n') + '</p>'
        return uploads, text
    except (OSError, UnicodeError):
        raise KulmsError('答案ファイルを読み取れません。保存先とUTF-8の文字コードを確認してください。') from None


def _multipart(values, upload):
    boundary = 'BeamerPlus' + secrets.token_hex(24)
    pieces = []
    for name, value in values.items():
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', name):
            raise KulmsError('想定と異なるフォーム項目名のため停止しました。')
        pieces.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                       str(value).encode('utf-8'), b'\r\n'])
    filename = upload.name.replace('\\', '\\\\').replace('"', '\\"')
    mime = mimetypes.guess_type(upload.name)[0] or 'application/octet-stream'
    pieces.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode('utf-8'),
                   upload.data, f'\r\n--{boundary}--\r\n'.encode()])
    return b''.join(pieces), 'multipart/form-data; boundary=' + boundary


def _read(client, url, data=None, content_type=None, referer=None):
    try:
        with client._open(url, data, content_type=content_type, referer=referer) as response:
            if urlsplit(response.url).path != urlsplit(url).path:
                raise KulmsError('課題ページの代わりに別の画面が返されました。')
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise KulmsError('課題ページの応答が大きすぎます。')
            return _Forms(body.decode('utf-8', 'replace'), response.url)
    except (URLError, OSError, HTTPException):
        raise KulmsError('課題ページとの通信が途切れました。') from None


def _discover_tool(client, course):
    course_id = course['id']
    tools = [t for page in course.get('sitePages', []) for t in page.get('tools') or []
             if t.get('toolId') == 'sakai.assignment.grades']
    if len(tools) == 1 and re.fullmatch(r'[A-Za-z0-9-]{1,128}', str(tools[0].get('id', ''))):
        return tools[0]['id']
    # KULMS's site API omits tools from SitePage. Read the linked Assignments
    # page and identify its tool from forms/navigation, without posting them.
    pages = [p for p in course.get('sitePages', [])
             if p.get('title', '').strip().casefold() in ('課題', 'assignments', 'assignment')]
    if len(pages) == 1 and re.fullmatch(r'[A-Za-z0-9-]{1,128}', str(pages[0].get('id', ''))):
        url = LMS + '/portal/site/' + course_id + '/page/' + pages[0]['id']
        try:
            with client._open(url) as response:
                if not response.url.startswith(LMS + '/portal/site/' + course_id + '/'):
                    raise KulmsError('課題ページが別コースへ移動しました。')
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise KulmsError('課題ページの応答が大きすぎます。')
                page = _Forms(body.decode('utf-8', 'replace'), response.url)
        except (OSError, URLError, HTTPException):
            raise KulmsError('課題ツールの検出中に通信が途切れました。') from None
        candidates = set()
        pattern = '/portal/site/' + re.escape(course_id) + r'/tool/([A-Za-z0-9-]{1,128})'
        targets = [urljoin(page.url, f['action']) for f in page.forms]
        targets += [a['href'] for a in page.anchors if any(
            a.get(k, '').strip().casefold() in ('課題', 'assignments', 'assignment')
            for k in ('title', 'text', 'aria-label'))]
        for target in targets:
            parsed = urlsplit(target)
            match = re.fullmatch(pattern, parsed.path)
            if parsed.hostname == urlsplit(LMS).hostname and match:
                candidates.add(match[1])
        if not candidates:
            for target in page.frames:
                parsed = urlsplit(target)
                match = re.fullmatch(r'/portal/tool/([A-Za-z0-9-]{1,128})', parsed.path)
                if parsed.hostname == urlsplit(LMS).hostname and match:
                    candidates.add(match[1])
        if len(candidates) == 1:
            return candidates.pop()
    raise KulmsError('課題ツールを一意に確認できません。pageで課題一覧ページのURLを指定してください。')


def _context(client, course_id, assignment_id, page_url, *, has_uploads=False):
    _site_segment(course_id)
    _site_segment(assignment_id)
    assignment = next((a for a in client.assignments(course_id) if a.get('id') == assignment_id), None)
    if assignment is None or assignment.get('context') != course_id:
        raise KulmsError('指定コースにその課題がありません。assignmentsで課題IDを確認してください。')
    if assignment.get('submissionType') not in _TYPES:
        raise KulmsError('この課題の提出形式はCLIで扱えません。')
    course = next((c for c in client.courses() if c.get('id') == course_id), None)
    if course is None:
        raise KulmsError('指定したコースを確認できませんでした。')
    if page_url is None:
        page_url = LMS + '/portal/site/' + course_id + '/tool/' + _discover_tool(client, course)
    _checked_url(page_url, {urlsplit(LMS).hostname})
    parsed = urlsplit(page_url)
    if not re.fullmatch('/portal/site/' + re.escape(course_id) + r'/tool/[A-Za-z0-9-]{1,128}', parsed.path):
        raise KulmsError('pageには指定コースの課題ツールのURLを指定してください。')
    endpoint = LMS + parsed.path + '?panel=Main'
    reference = '/assignment/a/' + course_id + '/' + assignment_id
    url = LMS + parsed.path + '?' + urlencode({'assignmentReference': reference, 'panel': 'Main',
                                             'sakai_action': 'doView_submission'})
    page = _read(client, url)
    previous = _previous(assignment)
    # Attachment-only resubmissions disable Proceed until a file changes.
    # The upload control remains enabled. Do not reject that initial state.
    pending_upload = (has_uploads and previous.get('canSubmit') is True
                      and assignment['submissionType'] in ('ATTACHMENT_ONLY_ASSIGNMENT_SUBMISSION',
                                                          'SINGLE_ATTACHMENT_SUBMISSION'))
    form = page.submission_form(endpoint, assignment_id, button='confirm', pending_file_upload=pending_upload)
    return course, assignment, endpoint, page, form


def _previous(assignment):
    rows = assignment.get('submissions') or []
    if not isinstance(rows, list) or len(rows) > 1 or any(not isinstance(s, dict) for s in rows):
        raise KulmsError('自分の提出情報を一意に確認できませんでした。')
    return rows[0] if rows else {}


def _file_links(page, course_id):
    prefix = LMS + '/access/content/attachment/' + course_id + '/Assignments/'
    return {link for link in page.links if link.startswith(prefix)}


def _upload_url(page, form, endpoint):
    source = next((x for x in form['fields'] if x.get('type') == 'file' and x['name'] == 'upload'), None)
    if source is None:
        raise KulmsError('この課題にはファイルを添付できません。')
    match = re.search(r'''\.action\s*=\s*["']([^"']+)["']''', source.get('onchange', ''))
    if match is None:
        raise KulmsError('ファイル選択欄のアップロード先を確認できませんでした。')
    target = urljoin(page.url, match[1]).split('#', 1)[0]
    _checked_url(target, {urlsplit(LMS).hostname})
    parsed = urlsplit(target)
    query = parse_qs(parsed.query)
    if (parsed.path != urlsplit(endpoint).path
            or query.get('sakai_action') not in (['doAttachUpload'], ['doAttachUploadSingle'])
            or query.get(_TOKEN) != [_values(form)[_TOKEN]]):
        raise KulmsError('添付のアップロード操作またはCSRFトークンが想定と異なります。')
    return target


def _public_endpoint(url):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode({key: values[0] for key, values in query.items()
                                  if key in ('panel', 'sakai_action')}), ''))


def _removal_id(page, form, reference):
    if 'currentAttachment' not in _values(form) or not reference.startswith('/content/attachment/'):
        raise KulmsError('差し替える添付の削除操作を確認できません。')
    expected = unquote(reference[len('/content'):])
    matches = []
    for anchor in page.anchors:
        match = re.search(r'''["']removeAttachment["']\s*,\s*(["'])(.*?)\1''', anchor.get('onclick', ''))
        if match and unquote(match[2]) == expected:
            matches.append(match[2])
    if len(set(matches)) != 1:
        raise KulmsError('指定した添付の削除ボタンを一意に確認できません。')
    return matches[0]


def _matches_file(client, url, upload):
    _checked_url(url, {urlsplit(LMS).hostname})
    try:
        with client._open(url) as response:
            if urlsplit(response.url).path != urlsplit(url).path:
                return False
            digest = hashlib.sha256()
            count = 0
            while chunk := response.read(min(128 * 1024, len(upload.data) + 1 - count)):
                count += len(chunk)
                if count > len(upload.data):
                    return False
                digest.update(chunk)
        return count == len(upload.data) and digest.digest() == hashlib.sha256(upload.data).digest()
    except (URLError, OSError, HTTPException):
        raise KulmsError('アップロード済みファイルの検証中に通信が途切れました。') from None


class _Visible(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.parts = []; self.feed(text)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ('br', 'p', 'div', 'li', 'tr', 'td'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'li', 'tr', 'td'):
            self.parts.append(' ')


def _normalized(text):
    return re.sub(r'\s+', ' ', ''.join(_Visible(text or '').parts)).strip()


def submit_assignment(client, course_id, assignment_id, *, files=(), text_file=None,
                      html_file=None, page_url=None, execute=False, resubmit=False,
                      replace_attachments=(), review_guard=None):
    uploads, text = _payload(files, text_file, html_file)
    course, assignment, endpoint, page, form = _context(client, course_id, assignment_id, page_url,
                                                      has_uploads=bool(uploads))
    previous = _previous(assignment)
    old_attachments = previous.get('submittedAttachments') or []
    replacements = set(replace_attachments)
    if len(replacements) != len(replace_attachments) or not replacements <= {a.get('ref') for a in old_attachments}:
        raise KulmsError('差し替える添付の参照IDが既存の提出情報と一致しません。')
    if replacements and not uploads:
        raise KulmsError('添付の差し替えには、新しく提出するfileも指定してください。')
    for reference in replacements:
        _removal_id(page, form, reference)
    had_submission = bool(previous.get('submitted') or previous.get('dateSubmittedEpochSeconds'))
    values = _values(form)
    if text is not None and _TEXT not in values:
        raise KulmsError('この課題には本文を入力できません。')
    if uploads and not any(x.get('type') == 'file' and x['name'] == 'upload' for x in form['fields']):
        raise KulmsError('この課題にはファイルを添付できません。')
    if assignment['submissionType'] == 'SINGLE_ATTACHMENT_SUBMISSION' and (len(uploads) != 1 or previous.get('submittedAttachments')):
        raise KulmsError('単一ファイル形式は新規の1ファイル提出に対応しています。差し替えはブラウザで行ってください。')
    if text is not None:
        values[_TEXT] = text
        if not _normalized(text):
            raise KulmsError('本文が空です。提出する内容を指定してください。')
    upload_target = _upload_url(page, form, endpoint) if uploads else None
    expected_text = values.get(_TEXT, '')
    summary = {'dry_run': not execute, 'course_id': course_id, 'course_title': course.get('title'),
               'assignment_id': assignment_id, 'assignment_title': assignment.get('title'),
               'submission_type': assignment.get('submissionType'), 'endpoint': endpoint,
               'due_at': assignment.get('dueTimeString'), 'close_at': assignment.get('closeTimeString'),
               'requires_resubmit_flag': had_submission, 'existing_status': previous.get('status'),
               'existing_attachments': [a.get('name') for a in previous.get('submittedAttachments') or []],
               'replace_attachments': [a.get('name') for a in old_attachments if a.get('ref') in replacements],
               'files': [u.metadata() for u in uploads], 'text_changed': text is not None,
               'text_characters': len(expected_text), 'csrf_present': True,
               'upload_endpoint': _public_endpoint(upload_target) if upload_target else None}
    # Called after reading the fresh form and snapshotting local bytes, before
    # any LMS mutation. The MCP workflow binds its semantic review to exactly
    # these requirements, existing submission, and outgoing bytes.
    if review_guard is not None:
        review_guard(course, assignment, page, uploads, expected_text, summary)
    if not execute:
        return summary
    if had_submission and not resubmit:
        raise KulmsError('この課題は提出済みです。本人の再提出指示がある場合だけresubmitを指定してください。')
    if previous.get('canSubmit') is False:
        raise KulmsError('LMSがこの課題の提出を許可していません。')
    stage = 'upload'
    try:
        for reference in sorted(replacements):
            stage = 'remove'
            removal = _removal_id(page, form, reference)
            values['option'] = 'removeAttachment'
            values['currentAttachment'] = removal
            page = _read(client, endpoint, values, referer=page.url)
            form = page.submission_form(endpoint, assignment_id, button='confirm',
                                        pending_file_upload=bool(uploads) and previous.get('canSubmit') is True)
            if any(unquote(urlsplit(u).path) == '/access' + unquote(reference)
                   for u in _file_links(page, course_id)):
                raise KulmsError('旧添付がフォームから外れたことを確認できませんでした。')
            values = _values(form)
            if text is not None:
                values[_TEXT] = text
        for upload in uploads:
            stage = 'upload'
            target = _upload_url(page, form, endpoint)
            before_links = _file_links(page, course_id)
            data, mime = _multipart(values, upload)
            page = _read(client, target, data, mime, page.url)
            form = page.submission_form(endpoint, assignment_id, button='confirm')
            new_links = _file_links(page, course_id) - before_links
            if not any(_matches_file(client, link, upload) for link in sorted(new_links)):
                raise KulmsError('添付ファイルの追加と内容を確認できませんでした。')
            values = _values(form)
            if text is not None:
                values[_TEXT] = text
            if _normalized(values.get(_TEXT)) != _normalized(expected_text):
                raise KulmsError('添付後の本文が指定内容と一致しませんでした。')
        stage = 'confirm'
        values['option'] = 'confirm'
        page = _read(client, endpoint, values, referer=page.url)
        form = page.submission_form(endpoint, assignment_id, button='post')
        stage = 'post'
        values = _values(form)
        values['option'] = 'post'
        page = _read(client, endpoint, values, referer=page.url)
        stage = 'verify'
        current = next((a for a in client.assignments(course_id) if a.get('id') == assignment_id), None)
        if current is None:
            raise KulmsError('提出後の課題情報を取得できませんでした。')
        receipt = _previous(current)
        if (receipt.get('submitted') is not True or not receipt.get('dateSubmittedEpochSeconds')
                or receipt['dateSubmittedEpochSeconds'] == previous.get('dateSubmittedEpochSeconds')):
            raise KulmsError('今回の提出済み状態と新しい受付時刻を確認できませんでした。')
        if _normalized(receipt.get('submittedText')) != _normalized(expected_text):
            raise KulmsError('提出された本文が指定内容と一致しませんでした。')
        attachments = receipt.get('submittedAttachments') or []
        old_refs = {a.get('ref') for a in previous.get('submittedAttachments') or []
                    if a.get('ref') and a['ref'] not in replacements and '/InlineSub_' not in a['ref']}
        if not old_refs <= {a.get('ref') for a in attachments}:
            raise KulmsError('以前の添付ファイルの保持を確認できませんでした。')
        if replacements & {a.get('ref') for a in attachments}:
            raise KulmsError('差し替え対象の旧添付が提出情報に残っています。')
        for upload in uploads:
            candidates = [a.get('url') for a in attachments if a.get('name') == upload.name
                          and a.get('size') == len(upload.data) and isinstance(a.get('url'), str)]
            allowed = [u for u in candidates if u.startswith(LMS + '/access/content/attachment/' + course_id + '/Assignments/')]
            if not any(_matches_file(client, u, upload) for u in allowed):
                raise KulmsError('提出された添付ファイルの内容を確認できませんでした。')
        return {**summary, 'submitted': True, 'verified': True, 'submission_id': receipt.get('id'),
                'submitted_at': receipt.get('dateSubmitted'),
                'submitted_epoch_seconds': receipt['dateSubmittedEpochSeconds'], 'status': receipt.get('status')}
    except KulmsError as exc:
        raise KulmsError(f'提出処理の{stage}段階で完了を確認できませんでした。'
                         f'{exc} 添付・下書き・提出が保存された可能性があります。'
                         '自動で再実行せずLMSの提出状態を確認してください。') from None
