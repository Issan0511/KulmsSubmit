"""Coordinate OTP time steps across CLI and MCP processes without storing codes."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import time

from .KulmsOTP import OTPError, current_code


def _gate_path():
    if sys.platform == 'darwin':
        root = Path.home() / 'Library/Application Support'
    elif sys.platform == 'win32':
        root = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    return root / 'KULMS Submit' / 'otp-replay.sqlite3'


def claim_otp(config, *, path=None, clock=None, sleep=None, generator=None):
    """Reserve a time step before attempting the POST, including uncertain failures.

    Only an expiry timestamp is persisted. A new gate skips the current time step,
    which might already have been used by a previous version or another client.
    """
    path = Path(path or _gate_path())
    clock = clock or time.time
    sleep = sleep or time.sleep
    generator = generator or current_code
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        while True:
            with closing(sqlite3.connect(path, timeout=45, isolation_level=None)) as db:
                db.execute('PRAGMA busy_timeout=45000')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS otp_slot (id INTEGER PRIMARY KEY CHECK(id=1), valid_until REAL NOT NULL)')
                row = db.execute('SELECT valid_until FROM otp_slot WHERE id=1').fetchone()
                now = clock()
                if row is None:
                    until = (int(now // config.period) + 1) * config.period
                    db.execute('INSERT INTO otp_slot(id, valid_until) VALUES(1, ?)', (until,))
                else:
                    until = row[0]
                if until > now:
                    wait = until - now
                    if wait > config.period * 2:
                        raise OTPError('OTPの時刻が戻っています。PCの時計を確認してください。')
                    db.commit()
                else:
                    value = generator(config, min(12, config.period / 2), clock=clock, sleep=sleep)
                    if value['valid_until'] <= clock():
                        raise OTPError('OTPの有効期限を確認できません。PCの時計を確認してください。')
                    db.execute('UPDATE otp_slot SET valid_until=? WHERE id=1', (value['valid_until'],))
                    db.commit()
                    return value['code']
            sleep(wait + .01)
    except OTPError:
        raise
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        raise OTPError('OTPの重複防止情報を保存できません。保存先を確認してください。') from None
