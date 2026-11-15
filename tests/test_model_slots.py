"""Synthetic concurrency and ASGI disconnect regressions; no model is called."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))
import app as web
from fastapi import HTTPException
from starlette.requests import ClientDisconnect


class ModelSlotTests(unittest.TestCase):
    def setUp(self):
        self.limits = patch.multiple(web, MODEL_MAX_PER_USER=1,
                                     MODEL_MAX_TOTAL=4, MODEL_RATE_PER_MINUTE=1000)
        self.limits.start()
        web._MODEL_ACTIVE.clear()
        web._MODEL_REQUESTS.clear()

    def tearDown(self):
        self.limits.stop()
        web._MODEL_ACTIVE.clear()
        web._MODEL_REQUESTS.clear()

    def acquire(self, name="fixture_alice"):
        return web._acquire_model_slot({"external_id": name})

    def test_serial_reuse_and_idempotent_release(self):
        for _ in range(20):
            release = self.acquire()
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 1)
            release()
            release()
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)

    def test_same_account_race_admits_exactly_one(self):
        barrier = threading.Barrier(2)
        def attempt():
            barrier.wait(timeout=5)
            try:
                return self.acquire()
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 429)
                self.assertEqual(exc.headers["Retry-After"], "5")
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            acquired = list(executor.map(lambda _: attempt(), range(2)))
        releases = [item for item in acquired if item is not None]
        self.assertEqual(len(releases), 1)
        self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 1)
        releases[0]()

    def test_accounts_are_isolated_and_global_limit_is_enforced(self):
        releases = [self.acquire("fixture_" + str(i)) for i in range(4)]
        try:
            self.assertEqual(sum(web._MODEL_ACTIVE.values()), 4)
            with self.assertRaises(HTTPException) as caught:
                self.acquire("fixture_fifth")
            self.assertEqual(caught.exception.status_code, 429)
            releases[0]()
            release = self.acquire("fixture_fifth")
            self.assertEqual(sum(web._MODEL_ACTIVE.values()), 4)
            release()
            self.assertEqual(web._MODEL_ACTIVE["fixture_1"], 1)
        finally:
            for release in releases:
                release()

    def test_rejected_concurrency_does_not_consume_rate_budget(self):
        release = self.acquire()
        try:
            with self.assertRaises(HTTPException):
                self.acquire()
            self.assertEqual(len(web._MODEL_REQUESTS["fixture_alice"]), 1)
        finally:
            release()

    def test_rate_limit_survives_release_and_expires(self):
        with patch.object(web, "MODEL_RATE_PER_MINUTE", 2), patch.object(web.time, "monotonic", return_value=100):
            self.acquire()()
            self.acquire()()
            with self.assertRaises(HTTPException) as caught:
                self.acquire()
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.headers["Retry-After"], "60")
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)
        with patch.object(web.time, "monotonic", return_value=161):
            self.acquire()()

    def test_context_releases_after_success_exception_and_baseexception(self):
        with web._model_work({"external_id": "fixture_alice"}):
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 1)
        self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)
        for error in (RuntimeError("fixture failure"), asyncio.CancelledError()):
            with self.assertRaises(type(error)):
                with web._model_work({"external_id": "fixture_alice"}):
                    raise error
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)

    def test_task_cancellation_releases_context(self):
        async def scenario():
            entered = asyncio.Event()
            async def work():
                with web._model_work({"external_id": "fixture_alice"}):
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(work())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(scenario())
        self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)

    def test_sse_disconnect_before_and_after_first_chunk_releases_both_routes(self):
        async def invoke(response, fail_at, error):
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                if message["type"] == fail_at:
                    raise error
            with self.assertRaises((ClientDisconnect, asyncio.CancelledError)):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        fake = types.SimpleNamespace(chat=lambda question: iter([{"type": "token", "text": "fixture"}]))
        with tempfile.TemporaryDirectory(prefix="ict_model_slot_") as directory, \
                patch.object(web, "MODEL_MAX_TOTAL", 32), \
                patch.object(web, "_session", return_value=fake), \
                patch.object(web, "_ws", return_value=Path(directory)):
            for index, route in enumerate((lambda: web.chat(web.ChatIn(question="fixture")), web.control_ingest)):
                for fail_at in ("http.response.start", "http.response.body"):
                    for error in (OSError("fixture disconnect"), asyncio.CancelledError()):
                        with self.subTest(route=route, fail_at=fail_at, error=type(error).__name__):
                            key = f"fixture_{index}_{fail_at}_{type(error).__name__}"
                            with patch.object(web, "_whoami", return_value={"external_id": key}):
                                response = route()
                            self.assertEqual(web._MODEL_ACTIVE[key], 1)
                            asyncio.run(invoke(response, fail_at, error))
                            self.assertEqual(web._MODEL_ACTIVE[key], 0)

    def test_chat_stream_success_and_worker_exception_release(self):
        async def invoke(response):
            messages = []
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                messages.append(message)
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            return b"".join(message.get("body", b"") for message in messages)
        fake = types.SimpleNamespace(chat=lambda question: iter([{"type": "token", "text": "fixture"}]))
        with patch.object(web, "_whoami", return_value={"external_id": "fixture_alice"}):
            for session, expected in ((fake, b"fixture"), (RuntimeError("fixture failure"), b"error")):
                with self.subTest(session=type(session).__name__):
                    kwargs = {"side_effect": session} if isinstance(session, Exception) else {"return_value": session}
                    with patch.object(web, "_session", **kwargs):
                        body = asyncio.run(invoke(web.chat(web.ChatIn(question="fixture"))))
                    self.assertIn(expected, body)
                    self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)

    def test_ingest_disconnect_closes_started_worker_before_releasing(self):
        calls = []
        process = types.SimpleNamespace(stdout=iter(["fixture worker output\n"]),
            poll=lambda: None, terminate=lambda: calls.append("terminate"),
            wait=lambda **kwargs: calls.append("wait"), kill=lambda: calls.append("kill"))
        async def invoke(response):
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                if b'"type": "log"' in message.get("body", b""):
                    raise OSError("fixture disconnect after worker start")
            with self.assertRaises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        with tempfile.TemporaryDirectory(prefix="ict_model_worker_") as directory, \
                patch.object(web, "_whoami", return_value={"external_id": "fixture_alice"}), \
                patch.object(web, "_ws", return_value=Path(directory)), \
                patch.object(web.subprocess, "Popen", return_value=process):
            asyncio.run(invoke(web.control_ingest()))
        self.assertEqual(calls, ["terminate", "wait"])
        self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)


if __name__ == "__main__":
    unittest.main()
