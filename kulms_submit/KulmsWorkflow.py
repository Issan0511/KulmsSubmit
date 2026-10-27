"""Review-bound submissions. Deterministic work is local; the MCP caller reviews meaning.

No model/API calls or background polling. Reviews are bound to assignment sources,
student profile, original sources, and the precise outgoing snapshot. Writes use
one fresh LMS session and persist an uncertain marker before the first mutation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlsplit
import uuid

from .KulmsClient import KulmsClient, KulmsError, LMS, _checked_url
from .KulmsSubmission import submit_assignment, _previous, _payload
from .KulmsSources import snapshot_note, render_note

LIMIT = 64 * 1024 * 1024
CATEGORIES = {'filename', 'identity', 'coverage', 'readability'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def filename(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or len(value.encode()) > 240
            or any(c in value for c in '/\\') or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise KulmsError('ファイル名にパス・制御文字を含めず、240バイト以内で指定してください。')
    return value


def read_bytes(path, limit=LIMIT):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise KulmsError('ファイルが空か、処理上限を超えています。')
        return data
    except OSError:
        raise KulmsError('提出元ファイルを読み取れません。') from None


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PageRequirements(HTMLParser):
    """Extract actual Sakai instruction panels, never hidden tokens or scripts."""
    def __init__(self, body):
        super().__init__()
        self.depth = 0
        self.capture = None
        self.suppressed = 0
        self.parts = []
        self.panels = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'textarea'):
            self.suppressed += 1
        if tag == 'div':
            self.depth += 1
            if self.capture is None and 'textPanel' in a.get('class', '').split():
                self.capture = self.depth
                self.parts = []
        if self.capture is not None and tag in ('p', 'br', 'li', 'tr', 'div'):
            self.parts.append('\n')
        if self.capture is not None and tag in ('a', 'img'):
            value = a.get('href') if tag == 'a' else a.get('src')
            if value:
                self.parts.append(' [' + value + '] ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'textarea'):
            self.suppressed = max(0, self.suppressed - 1)
        if tag == 'div':
            if self.capture == self.depth:
                self.panels.append(re.sub(r'\s+', ' ', ''.join(self.parts)).strip())
                self.capture = None
            self.depth -= 1

    def handle_data(self, text):
        if self.capture is not None and not self.suppressed:
            self.parts.append(text)


class References(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.urls = set(); self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ('a', 'img'):
            value = dict(attrs).get('href' if tag == 'a' else 'src')
            if value:
                self.urls.add(urljoin(LMS, value))


def assignment_requirements(course, assignment, page):
    keys = ('id', 'context', 'title', 'instructions', 'submissionType', 'anonymousGrading',
            'openTimeString', 'dueTimeString', 'closeTimeString', 'dropDeadTimeString',
            'allowResubmission', 'allPurposeItemText', 'modelAnswerText', 'timeLastModified')
    result = {k: assignment.get(k) for k in keys}
    result.update(course_title=course.get('title'), page_url=page.url,
                  page_instruction_panels=PageRequirements(page.body).panels,
                  attachments=assignment.get('attachments') or [])
    # API HTML is retained as data; nothing here is executed as an instruction.
    result['linked_sources'] = sorted(References(str(assignment.get('instructions') or '')).urls)
    return result


def download(client, url):
    _checked_url(url, {urlsplit(LMS).hostname})
    if not urlsplit(url).path.startswith('/access/content/'):
        raise KulmsError('要件・既存答案の添付URLを確認できません。')
    try:
        with client._open(url) as response:
            if urlsplit(response.url).path != urlsplit(url).path:
                raise KulmsError('添付の代わりに別ページが返されました。')
            data = response.read(LIMIT + 1)
    except OSError:
        raise KulmsError('添付の取得中に通信が途切れました。') from None
    if not data or len(data) > LIMIT:
        raise KulmsError('添付が空か、64MiBを超えています。')
    return data


def describe(data, name):
    result = {'name': name, 'bytes': len(data), 'sha256': sha(data)}
    if data.startswith(b'%PDF-'):
        import fitz
        try:
            with fitz.open(stream=data, filetype='pdf') as doc:
                if doc.needs_pass or doc.page_count < 1 or doc.page_count > 200:
                    raise ValueError()
                result.update(kind='pdf', pages=doc.page_count)
        except Exception:
            raise KulmsError('PDFのページを読み取れないか、暗号化・200ページ超過です。') from None
    elif Path(name).suffix.lower() in ('.txt', '.md', '.html', '.htm', '.csv', '.tex', '.py'):
        try:
            data.decode('utf-8-sig')
        except UnicodeError:
            raise KulmsError('テキストファイルをUTF-8で読めません。') from None
        result.update(kind='text', pages=1)
    else:
        import fitz
        try:
            with fitz.open(stream=data) as image:
                if image.is_pdf or image.page_count != 1:
                    raise ValueError()
                image[0].get_pixmap(matrix=fitz.Matrix(.1, .1))
            result.update(kind='image', pages=1)
        except Exception:
            result.update(kind='unsupported', pages=1)
    return result


def default_state_dir():
    import sys
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    elif sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    return base / 'KULMS Submit'


class Workflow:
    def __init__(self, root=None, client_factory=KulmsClient.from_keyring):
        self.root = Path(root or os.environ.get('KULMS_HOME') or os.environ.get('BEAMERPLUS_KULMS_HOME') or default_state_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.factory = client_factory

    def profile(self):
        path = self.root / 'profile.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def set_profile(self, name, student_id, affiliation):
        values = {'name': name, 'student_id': student_id, 'affiliation': affiliation}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 160 or any(ord(c) < 32 for c in v) for v in values.values()):
            raise KulmsError('氏名・学籍番号・所属を短い文字列で指定してください。')
        write_json(self.root / 'profile.json', values)
        return values

    def load(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise KulmsError('提出準備IDが正しくありません。')
        path = self.root / job_id
        try:
            return path, json.loads((path / 'job.json').read_text())
        except (OSError, ValueError):
            raise KulmsError('提出準備が見つかりません。') from None

    def _snapshot_intact(self, job):
        try:
            options = job['options']
            uploads, text = _payload(options['files'], options['text_file'], options['html_file'])
            return ([u.metadata() for u in uploads] == job['binding']['files']
                    and (text is None or text == job['binding']['text'])
                    and all(sha(read_bytes(a['path'])) == a['sha256'] for a in job['artifacts']))
        except (KulmsError, KeyError, TypeError):
            return False

    @contextmanager
    def locked(self, job_id):
        path, _ = self.load(job_id)
        lock = path / '.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise KulmsError('この提出の処理中、または前回が中断されました。重複送信を停止しました。') from None
        try:
            os.close(fd)
            yield self.load(job_id)
        finally:
            lock.unlink()

    def _remote(self, client, assignment, requirements, replacements):
        resources = []
        listed = list(requirements['attachments'])
        urls = {a.get('url') for a in listed}
        for url in requirements['linked_sources']:
            if url not in urls and url.startswith(LMS + '/access/content/'):
                listed.append({'name': Path(urlsplit(url).path).name or 'linked-source', 'url': url})
        entries = [('requirement', a) for a in listed]
        entries += [('backup' if a.get('ref') in replacements else 'existing_answer', a)
                    for a in _previous(assignment).get('submittedAttachments') or []]
        if len(entries) > 30:
            raise KulmsError('参照添付が多すぎます。30件以内の課題に対応しています。')
        total = 0
        for role, a in entries:
            data = download(client, a.get('url', ''))
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise KulmsError('課題・既存答案の添付の合計が128MiBを超えています。')
            resources.append((role, a, data))
        return resources

    def _binding(self, course, assignment, page, uploads, text, summary, remote):
        return {'requirements': assignment_requirements(course, assignment, page),
                'previous': {k: _previous(assignment).get(k) for k in
                             ('id', 'submitted', 'dateSubmittedEpochSeconds', 'submittedText', 'submittedAttachments', 'canSubmit')},
                'files': [u.metadata() for u in uploads], 'text': text,
                'replace_attachments': summary['replace_attachments'],
                'remote': [{'role': role, 'url': a.get('url'), 'sha256': sha(data)} for role, a, data in remote],
                'profile': self.profile()}

    def prepare(self, course_id, assignment_id, *, files=(), output_names=(), text_file=None,
                html_file=None, note_id=None, note_filename=None, page_url=None, replace_attachments=()):
        if len(output_names) not in (0, len(files)):
            raise KulmsError('output_namesはfilesと同じ個数で指定してください。')
        job_id = uuid.uuid4().hex
        path = self.root / job_id
        path.mkdir(mode=0o700)
        (path / 'answers').mkdir(mode=0o700)
        (path / 'sources').mkdir(mode=0o700)
        originals, outgoing, artifacts = [], [], []
        def artifact(data, name, role):
            identifier = str(len(artifacts))
            target = path / 'sources' / identifier
            target.write_bytes(data)
            target.chmod(0o600)
            item = dict(describe(data, name), id=identifier, role=role, path=str(target))
            artifacts.append(item)
        def answer(data, name):
            name = filename(name)
            target = path / 'answers' / name
            if target.exists():
                raise KulmsError('提出ファイル名が重複しています。')
            target.write_bytes(data); target.chmod(0o600)
            outgoing.append(str(target))
            artifact(data, name, 'answer')
        for i, source in enumerate(files):
            source = Path(source).resolve()
            data = read_bytes(source)
            originals.append({'kind': 'file', 'path': str(source), 'sha256': sha(data)})
            answer(data, output_names[i] if output_names else source.name)
        if note_id is not None:
            note, original = snapshot_note(note_id)
            originals.append(original)
            cached = self.root / ('note-' + original['sha256'] + '.pdf')
            if not cached.exists():
                snapshot = path / 'note.json'; write_json(snapshot, note)
                render_note(snapshot, cached)
                cached.chmod(0o600)
            answer(read_bytes(cached), filename(note_filename or note['title'] + '.pdf'))
        text_path = html_path = None
        if text_file is not None or html_file is not None:
            if text_file is not None and html_file is not None:
                raise KulmsError('text_fileとhtml_fileは併用できません。')
            source = Path(text_file if text_file is not None else html_file).resolve()
            data = read_bytes(source, 1024 * 1024)
            originals.append({'kind': 'file', 'path': str(source), 'sha256': sha(data)})
            target = path / ('inline.txt' if text_file is not None else 'inline.html')
            target.write_bytes(data); target.chmod(0o600)
            if text_file is not None: text_path = str(target)
            else: html_path = str(target)
        options = dict(files=outgoing, text_file=text_path, html_file=html_path,
                       page_url=page_url, replace_attachments=list(replace_attachments))
        captured = {}
        with self.factory() as client:
            def capture(course, assignment, page, uploads, text, summary):
                requirements = assignment_requirements(course, assignment, page)
                remote = self._remote(client, assignment, requirements, set(replace_attachments))
                for role, a, data in remote:
                    artifact(data, a.get('name') or 'attachment', role)
                if text:
                    artifact(text.encode(), 'inline.html', 'answer')
                captured['binding'] = self._binding(course, assignment, page, uploads, text, summary, remote)
            summary = submit_assignment(client, course_id, assignment_id, **options, review_guard=capture)
        fingerprint = digest({'binding': captured['binding'], 'originals': originals})
        # Reuse the exact review and receipt. No re-review, OCR, or rendering for
        # identical saved sources and identical assignment/submission state.
        for old in self.root.glob('*/job.json'):
            previous = json.loads(old.read_text())
            if (previous.get('fingerprint') == fingerprint
                    and previous.get('state') not in ('uncertain', 'submitting')
                    and self._snapshot_intact(previous)):
                import shutil
                shutil.rmtree(path)
                return {**self.status(previous['id']), 'reused': True}
        job = {'version': 1, 'id': job_id, 'state': 'prepared', 'created_at': now(),
               'course_id': course_id, 'assignment_id': assignment_id, 'options': options,
               'originals': originals, 'binding': captured['binding'], 'fingerprint': fingerprint,
               'artifacts': artifacts, 'viewed': {}, 'summary': summary, 'review': None}
        write_json(path / 'job.json', job)
        return self.status(job_id)

    def status(self, job_id):
        _, job = self.load(job_id)
        return {k: job[k] for k in ('id', 'state', 'fingerprint', 'summary', 'review')} | {
            'requirements': job['binding']['requirements'], 'profile': job['binding']['profile'],
            'artifacts': [{k: v for k, v in a.items() if k != 'path'} for a in job['artifacts']],
            'viewed': job['viewed'], 'receipt': job.get('receipt'), 'error': job.get('error')}

    def read_artifact(self, job_id, artifact_id, page=1):
        with self.locked(job_id) as (path, job):
            item = next((a for a in job['artifacts'] if a['id'] == artifact_id), None)
            if item is None or type(page) is not int or not 1 <= page <= item['pages']:
                raise KulmsError('資料IDまたはページ番号が正しくありません。')
            data = read_bytes(item['path'])
            if sha(data) != item['sha256']:
                raise KulmsError('確認用ファイルが変更されています。prepareからやり直してください。')
            image, text = None, ''
            if item['kind'] in ('pdf', 'image'):
                import fitz
                with fitz.open(stream=data, filetype='pdf' if item['kind'] == 'pdf' else None) as doc:
                    p = doc[page - 1]
                    text = p.get_text()
                    scale = min(1600 / max(p.rect.width, p.rect.height), 2)
                    image = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
            elif item['kind'] == 'text':
                text = data.decode('utf-8-sig')
                if len(text) > 50000:
                    raise KulmsError('本文が長すぎます。確認対象を5万文字以内に分けてください。')
            else:
                raise KulmsError('この形式の内容確認には未対応です。PDFまたはUTF-8テキストを用意してください。')
            seen = job['viewed'].setdefault(artifact_id, [])
            if page not in seen: seen.append(page)
            write_json(path / 'job.json', job)
            return {'artifact_id': artifact_id, 'page': page, 'pages': item['pages'], 'text': text,
                    'role': item['role'], 'name': item['name'], 'sha256': item['sha256']}, image

    def record_review(self, job_id, fingerprint, checks, requirements, summary):
        """Persist the caller's evidence-based semantic review, not a boolean self-check."""
        with self.locked(job_id) as (path, job):
            if job['state'] not in ('prepared', 'reviewed', 'blocked') or fingerprint != job['fingerprint']:
                raise KulmsError('この版にはレビューを登録できません。最新の提出準備を確認してください。')
            if (not isinstance(checks, dict) or set(checks) != CATEGORIES
                    or not isinstance(requirements, list) or not 1 <= len(requirements) <= 200):
                raise KulmsError('filename・identity・coverage・readabilityと、要件ごとの確認を記録してください。')
            items = list(checks.values()) + list(requirements)
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 5000:
                raise KulmsError('確認結果の要約を記録してください。')
            for item in items:
                if (not isinstance(item, dict) or item.get('status') not in ('pass', 'blocked', 'uncertain')
                        or not all(isinstance(item.get(k), str) and 0 < len(item[k].strip()) <= 5000
                                   for k in ('requirement', 'source', 'evidence'))):
                    raise KulmsError('各確認にstatus、requirement、source、evidenceが必要です。')
            missing = [a['name'] for a in job['artifacts'] if a['role'] != 'backup'
                       and set(job['viewed'].get(a['id'], [])) != set(range(1, a['pages'] + 1))]
            passed = all(item['status'] == 'pass' for item in items)
            if passed and missing:
                raise KulmsError('未確認の資料・答案ページがあります: ' + ', '.join(missing))
            review = {'checked_at': now(), 'fingerprint': fingerprint, 'checks': checks,
                      'requirements': requirements, 'summary': summary}
            job.update(review=review, state='reviewed' if passed else 'blocked')
            write_json(path / 'job.json', job)
        return self.status(job_id)

    def _originals_unchanged(self, job):
        for original in job['originals']:
            if original['kind'] == 'note':
                _, current = snapshot_note(original['note_id'], path=original['path'])
                actual = current['sha256']
            else:
                actual = sha(read_bytes(original['path']))
            if actual != original['sha256']:
                raise KulmsError('確認後に元ノート・答案が変更されています。再度prepareとレビューが必要です。')

    def submit(self, job_id, *, completion_signal, resubmit=False):
        if not isinstance(completion_signal, str) or not completion_signal.strip() or len(completion_signal) > 2000:
            raise KulmsError('本人が完成・提出を指示した発言をcompletion_signalに指定してください。')
        with self.locked(job_id) as (path, job):
            if job['state'] == 'submitted':
                return dict(job['receipt'], already_submitted=True)
            if job['state'] != 'reviewed':
                raise KulmsError('要件・答案の確認が完了していないか、前回の提出結果が不明です。')
            if job['summary']['requires_resubmit_flag'] and not resubmit:
                raise KulmsError('提出済みです。本人の再提出指示がある場合だけresubmitを指定してください。')
            self._originals_unchanged(job)
            started = False
            try:
                with self.factory() as client:
                    def guard(course, assignment, page, uploads, text, summary):
                        nonlocal started
                        requirements = assignment_requirements(course, assignment, page)
                        remote = self._remote(client, assignment, requirements, set(job['options']['replace_attachments']))
                        binding = self._binding(course, assignment, page, uploads, text, summary, remote)
                        if binding != job['binding']:
                            raise KulmsError('課題要件・既存提出・提出ファイル・プロフィールが確認時から変わりました。再確認が必要です。')
                        self._originals_unchanged(job)
                        job.update(state='submitting', completion_signal=completion_signal, started_at=now())
                        write_json(path / 'job.json', job)
                        started = True
                    receipt = submit_assignment(client, job['course_id'], job['assignment_id'],
                                **job['options'], execute=True, resubmit=resubmit, review_guard=guard)
                job.update(state='submitted', receipt=receipt, finished_at=now())
                write_json(path / 'job.json', job)
                return receipt
            except Exception:
                if started:
                    job.update(state='uncertain', error='提出処理開始後に完了を確認できませんでした。LMSを確認し、自動再送しないでください。')
                    write_json(path / 'job.json', job)
                raise
