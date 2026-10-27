"""SpenNotes loopback client; public JSON/PDF contract only."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

def default_connection_file():
    explicit = os.environ.get('BEAMERPLUS_MCP_FILE')
    if explicit:
        return Path(explicit)
    if sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    elif sys.platform == 'win32':
        root = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    return root / 'BeamerPlus Notes' / 'mcp-connection.json'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NotebookClient:
    def __init__(self, connection_file=None):
        self.connection_file = Path(connection_file) if connection_file else default_connection_file()

    def call(self, operation, **arguments):
        try:
            connection = json.loads(self.connection_file.read_text())
            url, token = connection['url'], connection['token']
            if not isinstance(url, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/rpc', url):
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{32,100}', token):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('BeamerPlusを起動してください。接続ファイルが見つからないか、形式が正しくありません。') from exc
        request = Request(url, data=json.dumps({'operation': operation, 'arguments': arguments}, ensure_ascii=False).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=12) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get('error')
            except (ValueError, AttributeError):
                message = None
            raise ValueError(message or 'BeamerPlusへの接続に失敗しました。起動し直して接続を確認してください。') from exc
        except (URLError, OSError, ValueError) as exc:
            raise ValueError('BeamerPlusに接続できません。起動しているか確認してください。') from exc
        if result.get('error') is not None:
            raise ValueError(result['error'])
        return result
