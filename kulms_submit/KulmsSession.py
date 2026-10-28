"""One serialized in-memory LMS session for a single MCP server process."""
from contextlib import contextmanager
import threading
from .KulmsClient import KulmsClient, KulmsSessionExpired


class SessionPool:
    def __init__(self, factory=KulmsClient.from_keyring):
        self._factory = factory
        self._client = None
        self._lock = threading.RLock()

    @contextmanager
    def __call__(self):
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            try:
                yield self._client
            except KulmsSessionExpired:
                self._client.close()
                self._client = None
                raise

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
