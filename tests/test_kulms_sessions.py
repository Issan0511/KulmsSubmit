import asyncio
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kulms_submit.KulmsClient import KulmsClient, KulmsSessionExpired
from kulms_submit.KulmsLogin import LoginCredentials
from kulms_submit.KulmsMCP import create_server
from kulms_submit.KulmsOTP import TOTPConfig
from kulms_submit.KulmsOTPGate import claim_otp
from kulms_submit.KulmsSession import SessionPool
from kulms_submit.KulmsWorkflow import Workflow

CONFIG = TOTPConfig('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', period=1)


def deterministic_code(config, min_validity, *, clock, sleep):
    now = clock()
    end = (int(now // config.period) + 1) * config.period
    if end - now < min_validity:
        sleep(end - now + .01)
        now = clock()
        end = (int(now // config.period) + 1) * config.period
    return {'code': str(int(now // config.period)), 'valid_until': end}


def claim_in_process(path, queue):
    try:
        queue.put(('ok', claim_otp(CONFIG, path=path, generator=deterministic_code)))
    except Exception as exc:
        queue.put(('error', str(exc)))


class SessionTests(unittest.TestCase):
    def test_mcp_tools_reuse_one_client_and_pool_closes_it(self):
        client = Mock()
        client.courses.return_value = [{'id':'course', 'title':'Course'}]
        client.assignments.return_value = [{'id':'assignment', 'title':'Exercise'}]
        factory = Mock(return_value=client)
        pool = SessionPool(factory)
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(directory, workflow=Workflow(directory, client_factory=pool))
            async def exercise():
                courses = await server.call_tool('list_kulms_courses', {})
                assignments = await server.call_tool('list_kulms_assignments', {'course_id':'course'})
                return courses, assignments
            courses, assignments = asyncio.run(exercise())
            self.assertIn('course', str(courses))
            self.assertIn('assignment', str(assignments))
            self.assertEqual(factory.call_count, 1)
            client.close.assert_not_called()
        pool.close()
        client.close.assert_called_once()

    def test_parallel_operations_are_serialized_and_expiry_discards_client(self):
        active = 0
        maximum = 0
        pool = SessionPool(lambda: Mock())
        def operation(_):
            nonlocal active, maximum
            with pool() as client:
                active += 1
                maximum = max(maximum, active)
                time.sleep(.01)
                active -= 1
                return client
        with ThreadPoolExecutor(max_workers=4) as executor:
            clients = list(executor.map(operation, range(4)))
        self.assertEqual(maximum, 1)
        self.assertTrue(all(c is clients[0] for c in clients))
        with self.assertRaises(KulmsSessionExpired):
            with pool(): raise KulmsSessionExpired('expired')
        clients[0].close.assert_called_once()
        with pool() as fresh: self.assertIsNot(fresh, clients[0])
        pool.close()

    def test_expired_get_reauthenticates_once(self):
        import io
        client = KulmsClient(LoginCredentials('fake','fake'), CONFIG)
        response=io.BytesIO(b'{"site_collection":[]}')
        response.url='https://lms.gakusei.kyoto-u.ac.jp/direct/site.json'
        response.headers={'Content-Type':'application/json'}
        with patch.object(client,'login') as login, patch.object(client,'close') as close, \
             patch.object(client,'_open',side_effect=[KulmsSessionExpired('expired'),response]) as send:
            self.assertEqual(client.courses(), [])
            self.assertEqual(login.call_count, 2)
            close.assert_called_once()
            self.assertEqual(send.call_count, 2)

    def test_login_page_returned_for_get_triggers_one_reauthentication(self):
        import io
        client = KulmsClient(LoginCredentials('fake','fake'), CONFIG)
        expired=io.BytesIO(b'<html>login</html>')
        expired.url='https://lms.gakusei.kyoto-u.ac.jp/portal/login'
        expired.headers={'Content-Type':'text/html'}
        valid=io.BytesIO(b'{"site_collection":[]}')
        valid.url='https://lms.gakusei.kyoto-u.ac.jp/direct/site.json'
        valid.headers={'Content-Type':'application/json'}
        with patch.object(client,'login') as login, patch.object(client,'close') as close, \
             patch.object(client,'_open',side_effect=[expired,valid]):
            self.assertEqual(client.courses(), [])
            self.assertEqual(login.call_count,2)
            close.assert_called_once()

    def test_auth_redirect_survives_network_wrapper(self):
        client = KulmsClient(LoginCredentials('fake','fake'), CONFIG)
        client._opener.open = Mock(side_effect=KulmsSessionExpired('expired'))
        with self.assertRaises(KulmsSessionExpired):
            client._open('https://lms.gakusei.kyoto-u.ac.jp/direct/site.json')

    def test_other_errors_do_not_trigger_retry(self):
        client = KulmsClient(LoginCredentials('fake','fake'), CONFIG)
        with patch.object(client,'login'), patch.object(client,'_open',side_effect=ValueError('failed')) as send:
            with self.assertRaises(ValueError): client.courses()
            send.assert_called_once()


class OTPGateTests(unittest.TestCase):
    def test_multiple_processes_never_claim_same_time_step(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'gate.sqlite3')
            ctx=multiprocessing.get_context('spawn')
            queue=ctx.Queue()
            workers=[ctx.Process(target=claim_in_process,args=(path,queue)) for _ in range(2)]
            for process in workers: process.start()
            results=[queue.get(timeout=10) for _ in workers]
            for process in workers:
                process.join(5)
                self.assertEqual(process.exitcode,0)
            self.assertTrue(all(status=='ok' for status,_ in results), results)
            self.assertNotEqual(results[0][1],results[1][1])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(otp_slot)')], ['id','valid_until'])
            if os.name=='posix': self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
