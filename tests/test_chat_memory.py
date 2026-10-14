import ast
import json
import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from chat_memory import ChatMemory


class MemoryTests(unittest.TestCase):
    def test_private_voice_context_is_persistent_and_channel_scoped(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            store = ChatMemory(Path(folder) / "memory.sqlite3")
            store.record_voice_reply(1, 10, "today", "What time does the train leave?")
            reopened = ChatMemory(store.path)
            self.assertIn("train leave", reopened.voice_history(10, 2))
            self.assertEqual(reopened.voice_history(20, 2), "")
            self.assertEqual(reopened.voice_history(10, 1), "")
            self.assertIn("train leave", reopened.search(10, "train", 2))
            self.assertEqual(reopened.search(20, "train", 2), "")

    def test_archive_retrieval_and_channel_isolation(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            store = ChatMemory(Path(folder) / "memory.sqlite3")
            for index in range(1, 121):
                store.record(index, 10, "Alice (1)", "2026-10-14", f"週六聚餐餐廳地址 {index}")
            batch = store.pending_batches()[0]
            self.assertEqual(len(batch), 100)
            store.archive(batch, "Alice 約定週六聚餐，餐廳地址 1", {1})
            reopened = ChatMemory(store.path)
            found = reopened.search(10, "聚餐地址", 151)
            self.assertIn("[summary]", found)
            self.assertIn("[original]", found)
            self.assertEqual(reopened.search(20, "聚餐地址", 151), "")
            self.assertEqual(reopened.search(10, "聚餐地址", 1), "")
            with reopened.connect() as db:
                packed = db.execute("SELECT original FROM archives").fetchone()[0]
                self.assertEqual(json.loads(zlib.decompress(packed)), batch)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 21)
                self.assertEqual(db.execute("SELECT content FROM messages WHERE id=1").fetchone()[0],
                                 "週六聚餐餐廳地址 1")
            self.assertEqual(reopened.pending_batches(), [])
            # Fetching an overlapping Discord snapshot must not resurrect archived messages.
            reopened.record(2, 10, "Alice (1)", "2026-10-14", "週六聚餐餐廳地址 2")
            with reopened.connect() as db:
                self.assertIsNone(db.execute("SELECT id FROM messages WHERE id=2").fetchone())
            self.assertEqual(reopened.pending_batches(20), [])

    def test_failed_summary_keeps_originals(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            store = ChatMemory(Path(folder) / "memory.sqlite3")
            store.record(1, 10, "Alice", "today", "original")
            with store.connect() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM messages")]
            with self.assertRaises(ValueError):
                store.archive(rows, "", set())
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT content FROM messages").fetchone()[0], "original")
            self.assertEqual(store.search(10, "<@123> !!!", 100), "")


class ModelFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        # Load helpers without Discord or local credentials.
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        names = {"generate_chat", "gemini_text", "ModelPoolExhausted"}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 and node.name in names]
        pool = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "GEMINI_MODELS"
                            for target in node.targets))
        self.models = ast.literal_eval(pool)
        self.post = Mock()
        self.log = Mock()
        self.shuffle = Mock(side_effect=lambda pool: pool.reverse())
        self.namespace: dict[str, Any] = {
            "requests": SimpleNamespace(post=self.post, Response=object, RequestException=OSError),
            "Any": Any, "hd": {"Content-Type": "application/json"},
            "gemini_api_key": "test-key", "GEMINI_MODELS": self.models,
            "random": SimpleNamespace(shuffle=self.shuffle), "json": json,
            "log_model_attempt": self.log,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "chat-helper", "exec"), self.namespace)

    def response(self, status: int = 200, text: str = "answer") -> Mock:
        response = Mock(status_code=status)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return response

    def test_http_failures_try_next_then_stop_on_success(self) -> None:
        self.post.side_effect = [self.response(503), self.response(429), self.response()]
        payload = {"contents": ["question"]}
        self.assertEqual(self.namespace["generate_chat"](payload).status_code, 200)
        self.shuffle.assert_called_once()
        self.assertEqual(self.post.call_count, 3)
        for call, model in zip(self.post.call_args_list, reversed(self.models)):
            self.assertIn(model, call.args[0])
            self.assertEqual(call.kwargs["json"], payload)
            self.assertNotIn("test-key", call.args[0])
            self.assertEqual(call.kwargs["timeout"], 60)
        self.assertEqual([call.args[1] for call in self.log.call_args_list], [False, False, True])

    def test_all_failures_try_each_model_once(self) -> None:
        self.post.return_value = self.response(400)
        with self.assertRaises(self.namespace["ModelPoolExhausted"]):
            self.namespace["generate_chat"]({})
        self.assertEqual(self.post.call_count, 7)
        self.assertEqual(len({call.args[0] for call in self.post.call_args_list}), 7)
        self.assertEqual(self.log.call_count, 7)
        self.assertTrue(all(not call.args[1] for call in self.log.call_args_list))

    def test_timeout_and_empty_answer_do_not_stop_pool(self) -> None:
        self.post.side_effect = [TimeoutError("test timeout"), self.response(text=""), self.response()]
        self.namespace["generate_chat"]({})
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.log.call_args_list[0].args[2], "TimeoutError")
        self.assertEqual(self.log.call_args_list[1].args[2], "HTTP 200 / ValueError")

    def test_invalid_json_summary_tries_next_model(self) -> None:
        self.post.side_effect = [self.response(text="not json"),
                                self.response(text='{"summary":"remember", "important_ids":[]}')]
        payload = {"generationConfig": {"responseMimeType": "application/json", "responseSchema": {
            "required": ["summary", "important_ids"], "properties": {
                "summary": {"type": "STRING"}, "important_ids": {"type": "ARRAY"}}}}}
        self.namespace["generate_chat"](payload, "memory")
        self.assertEqual(self.post.call_count, 2)
        self.assertTrue(all(call.args[3] == "memory" for call in self.log.call_args_list))

    def test_terminal_log_has_taiwan_time_model_and_result(self) -> None:
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "log_model_attempt")
        namespace = {"datetime": datetime, "timezone": timezone, "timedelta": timedelta}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "chat-log", "exec"), namespace)
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["log_model_attempt"]("gemini-2.5-flash", False, "HTTP 503", "chat")
            namespace["log_model_attempt"]("gemma-4-31b-it", True, "HTTP 200", "memory")
        lines = output.getvalue().splitlines()
        self.assertRegex(lines[0], r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+08:00\]")
        self.assertIn("gemini-2.5-flash 失敗 (HTTP 503)", lines[0])
        self.assertIn("[memory] gemma-4-31b-it 成功 (HTTP 200)", lines[1])


if __name__ == "__main__":
    unittest.main()
