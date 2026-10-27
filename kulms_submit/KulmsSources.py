"""Saved notebook sources and isolated PDF rendering for the KULMS MCP."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re
import tempfile

from .KulmsClient import KulmsError
from .NotebookClient import NotebookClient, default_connection_file


def note_digest(note):
    pages = note.get('pages') or [note]
    content = {'id': note['id'], 'title': note['title'], 'pages': [
        {k: p.get(k) for k in ('id', 'strokes', 'texts', 'paper', 'aspect')} for p in pages]}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def store_path():
    explicit = os.environ.get('BEAMERPLUS_NOTEBOOK_PATH')
    if explicit: return Path(explicit)
    root = default_connection_file().parent
    return root / 'library' if (root / 'library/index.json').exists() else root / 'notebooks.json'


def read_notes(path=None):
    try:
        source = Path(path or store_path()).resolve()
        index = source / 'index.json' if source.is_dir() else source
        data = json.loads(index.read_text())
        if data.get('version') == 2:
            migration = data.get('migration')
            if migration:
                legacy = Path(migration['source'])
                if legacy.exists() and hashlib.sha256(legacy.read_bytes()).hexdigest() != migration['sha256']:
                    raise KulmsError('移行後に旧ノートが更新されています。ノートアプリで変更を統合してください。')
            notes = []
            for item in data['notes']:
                relative = Path(item['path'])
                if relative.is_absolute(): raise ValueError()
                target = (index.parent / relative).resolve()
                if not target.is_relative_to(index.parent) or target.name != 'note.json': raise ValueError()
                expected = item['sha256']
                if not re.fullmatch(r'[0-9a-f]{64}', expected): raise ValueError()
                raw = target.read_bytes() if target.exists() else b''
                if hashlib.sha256(raw).hexdigest() != expected:
                    raw = (target.parent / '.history' / (expected + '.json')).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected: raise ValueError()
                note = json.loads(raw)
                if note['id'] != item['id']: raise ValueError()
                notes.append(note)
        elif data.get('version') == 1:
            notes = data['notes']
        else: raise ValueError()
        return source, [n for n in notes if not n.get('deletedAt')]
    except (OSError, ValueError, TypeError, KeyError):
        raise KulmsError('保存済みノートを読み取れません。BEAMERPLUS_NOTEBOOK_PATHを確認してください。') from None


def list_sources():
    path, notes = read_notes()
    return {'store_path': str(path), 'notes': [dict(note_id=n['id'], title=n['title'], course=n.get('course', '未分類'),
            pages=len(n.get('pages') or [n]), content_hash=note_digest(n)) for n in notes]}


def snapshot_note(note_id, *, path=None, sync=True):
    # If the editor is connected, flush its completed ink before reading disk.
    # Never fall back to stale disk data when the running editor rejects this.
    if sync and default_connection_file().exists():
        try:
            result = NotebookClient().call('save_note_snapshot', note_id=note_id)
            path = result['store_path']
        except (ValueError, KeyError):
            raise KulmsError('ノートの最新保存を確認できません。BeamerPlusを更新後に再起動し、ペンを離してください。') from None
    source, notes = read_notes(path)
    matches = [n for n in notes if n.get('id') == note_id]
    if len(matches) != 1:
        raise KulmsError('提出元ノートが見つかりません。list_submission_sourcesで選び直してください。')
    note = matches[0]
    return note, {'kind': 'note', 'path': str(source), 'note_id': note_id, 'sha256': note_digest(note)}


def render_note(snapshot, output):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    output = Path(output)
    fd, temporary = tempfile.mkstemp(dir=output.parent, suffix='.pdf')
    os.close(fd)
    try:
        result = subprocess.run(export_command() + [str(snapshot), temporary],
                                env=env, capture_output=True, timeout=90)
        if result.returncode != 0 or not Path(temporary).read_bytes().startswith(b'%PDF-'):
            raise KulmsError('ノートのPDF化に失敗しました。元ノートは変更していません。')
        os.replace(temporary, output)
    except subprocess.TimeoutExpired:
        raise KulmsError('ノートのPDF化が時間内に完了しませんでした。') from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)



def export_command():
    """Optional external notebook exporter; no import or runtime dependency on its GUI."""
    import shutil
    python = os.environ.get('SPENNOTES_PYTHON')
    if python:
        return [python, '-m', 'beamerplus.NotebookSources']
    command = shutil.which('beamerplus-export')
    if command: return [command]
    raise KulmsError('ノートPDF化にはSpenNotesのbeamerplus-export、またはSPENNOTES_PYTHONが必要です。既存PDFはそのまま使えます。')
