"""Connections must close at request exit, while commits and rollbacks survive.

Python 3.13 first exposed this gap through ResourceWarning in the full runtime.
The tracker holds references deliberately: passing cannot depend on garbage
collection, and these regressions also catch the defect on Python 3.12.
"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'server'))
import agent_auth
import agent_memory
import router


REAL_CONNECT = sqlite3.connect


class TrackedConnection(sqlite3.Connection):
    explicitly_closed = False

    def close(self):
        super().close()
        self.explicitly_closed = True


class SetupFailureConnection(TrackedConnection):
    def execute(self, sql, *args, **kwargs):
        if sql.startswith('PRAGMA foreign_keys'):
            raise sqlite3.OperationalError('intentional connection setup failure')
        return super().execute(sql, *args, **kwargs)


class SQLiteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ict_sqlite_lifecycle_')
        self.root = Path(self.tmp.name)
        self.connections = []
        self.connection_class = TrackedConnection

        def connect(*args, **kwargs):
            kwargs['factory'] = self.connection_class
            connection = REAL_CONNECT(*args, **kwargs)
            self.connections.append(connection)
            return connection

        self.connect_patch = patch.object(sqlite3, 'connect', side_effect=connect)
        self.connect_patch.start()

    def tearDown(self):
        self.connect_patch.stop()
        for connection in self.connections:
            if not connection.explicitly_closed:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
        self.tmp.cleanup()

    def assert_all_closed(self):
        self.assertTrue(self.connections, 'the exercise must actually open SQLite')
        self.assertEqual(sum(not c.explicitly_closed for c in self.connections), 0,
                         'connections must close without waiting for garbage collection')

    def test_auth_reads_writes_sessions_and_early_returns_close_immediately(self):
        auth = agent_auth.Auth(self.root / 'auth.db')
        self.assert_all_closed()
        user = auth.ensure_user('fixture_user')
        self.assertEqual(auth.ensure_user('fixture_user'), user)
        token = auth.issue_token(user)
        self.assertEqual(auth.resolve(token)['user_id'], user)
        self.assertIsNone(auth.resolve('invalid-token'))
        session = auth.create_session(user)
        self.assertEqual(auth.resolve_session(session)['user_id'], user)
        self.assertIsNone(auth.resolve_session('missing-session'))
        self.assertTrue(auth.revoke_session(session))
        self.assertIsNone(auth.resolve_session(session))
        self.assertIsNone(auth.get_user('missing-user'))
        self.assertFalse(auth.authenticate('missing-user', 'not-a-real-password')['ok'])
        self.assertEqual(auth.stats()['users'], 1)
        self.assert_all_closed()

    def test_memory_data_commits_and_operations_close_immediately(self):
        memory = agent_memory.Memory(db_path=self.root / 'memory.db')
        user = memory.ensure_user('fixture_user')
        conversation = memory.new_conversation(user, 'fixture')
        message = memory.append(conversation, 'user', 'preserved original')
        memory.save_summary(conversation, 'unverified summary', message)
        memory.set_pref(user, 'fixture_key', {'period': '2026-05'})
        self.assertEqual(memory.load(conversation)[0]['content'], 'preserved original')
        self.assertEqual(memory.get_context(conversation)['summary'], 'unverified summary')
        self.assertEqual(memory.get_pref(user, 'fixture_key'), {'period': '2026-05'})
        self.assertEqual(memory.get_context(999999), {})
        self.assertFalse(memory.delete_conversation(user, 999999))
        self.assertEqual(memory.stats()['messages'], 1)
        self.assert_all_closed()

    def test_body_failure_rolls_back_auth_and_memory_then_closes(self):
        for name, make in (('auth', lambda: agent_auth.Auth(self.root / 'auth.db')),
                           ('memory', lambda: agent_memory.Memory(db_path=self.root / 'memory.db'))):
            with self.subTest(store=name):
                store = make()
                with self.assertRaisesRegex(RuntimeError, 'intentional rollback'):
                    with store._conn() as connection:
                        connection.execute("INSERT INTO users(external_id,name) VALUES(?,?)",
                                           ('must_rollback', 'fixture'))
                        raise RuntimeError('intentional rollback')
                self.assert_all_closed()
                with store._conn() as connection:
                    count = connection.execute("SELECT count(*) FROM users WHERE external_id='must_rollback'").fetchone()[0]
                self.assertEqual(count, 0)
                self.assert_all_closed()
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def test_connection_setup_failure_also_closes(self):
        self.connection_class = SetupFailureConnection
        for make in (lambda: agent_auth.Auth(self.root / 'auth.db'),
                     lambda: agent_memory.Memory(db_path=self.root / 'memory.db')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'connection setup failure'):
                make()
            self.assert_all_closed()

    def test_router_readonly_success_and_failure_close_connection(self):
        database = self.root / 'knowledge.db'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE example(value TEXT)')
            connection.execute('INSERT INTO example VALUES(?)', ('preserved',))
        with patch.object(router, '_db_path', return_value=database):
            with router._connect() as connection:
                self.assertEqual(connection.execute('SELECT value FROM example').fetchone()[0], 'preserved')
            self.assert_all_closed()
            with self.assertRaises(sqlite3.OperationalError):
                with router._connect() as connection:
                    connection.execute("UPDATE example SET value='forbidden'")
            self.assert_all_closed()
            with self.assertRaises(RuntimeError):
                with router._connect():
                    raise RuntimeError('intentional request failure')
            self.assert_all_closed()

    def test_repeated_authenticated_http_requests_leave_no_live_sqlite_connection(self):
        from fastapi.testclient import TestClient
        import app as web
        import agent_runtime

        with patch.dict(os.environ, {'LLM_WIKI_DATA': str(self.root)}), \
             patch.object(agent_runtime.AgentSession, '_ensure_llm', side_effect=AssertionError('no model in lifecycle test')):
            auth = agent_auth.Auth()
            user = auth.ensure_user('sqlite_lifecycle_fixture')
            headers = {'Authorization': 'Bearer ' + auth.issue_token(user)}
            with TestClient(web.app, raise_server_exceptions=False) as client:
                for _ in range(5):
                    for endpoint in ('/api/auth/me', '/api/conversations', '/api/chat/history'):
                        response = client.get(endpoint, headers=headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assert_all_closed()
                created = client.post('/api/conversations', headers=headers, json={'title': 'resource fixture'})
                self.assertEqual(created.status_code, 200, created.text)
                self.assert_all_closed()
                denied = client.get('/api/conversations/999999', headers=headers)
                self.assertEqual(denied.status_code, 404, denied.text)
                self.assert_all_closed()
            self.assertGreater(len(self.connections), 100)
            self.assert_all_closed()


if __name__ == '__main__':
    unittest.main()
