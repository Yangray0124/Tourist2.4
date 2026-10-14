import ast
import asyncio
import copy
import logging
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock
from gemini_live import is_voice_recipient


class ChatTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        chat = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Chat")
        handler = next(node for node in chat.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "on_message")
        handler.decorator_list = []
        self.history = AsyncMock(return_value="tagged history")
        self.roll = Mock(return_value=0.5)
        self.namespace: dict[str, Any] = {
            "discord": SimpleNamespace(Message=object), "asyncio": asyncio,
            "random": SimpleNamespace(random=self.roll), "copy": copy,
            "build_chat_prompt": self.history,
            "prompt": "依使用者指定語言：", "logger": logging.getLogger("test"),
            "js": {"contents": [{"parts": [{"text": ""}]}]},
            "is_voice_recipient": is_voice_recipient,
        }
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "chat-trigger", "exec"), self.namespace)
        self.cog = SimpleNamespace(
            bot=SimpleNamespace(user=SimpleNamespace(id=100), application_id=100),
            chat_memory=SimpleNamespace(search=Mock(return_value="")),
            memory_summary_task=None, compress_chat_memory=AsyncMock(),
            reply_with_gemini=AsyncMock(),
        )
        self.message = SimpleNamespace(
            id=200, content="這個問題怎麼解", clean_content="這個問題怎麼解",
            author=SimpleNamespace(id=1, display_name="Alice", bot=False),
            channel=SimpleNamespace(id=10, send=AsyncMock()),
            attachments=[], mentions=[], reply=AsyncMock(),
            guild=None,
        )

    async def test_untagged_message_never_replies_or_reads_history(self) -> None:
        for roll in (0.0, 0.009, 0.01, 0.999):
            self.roll.return_value = roll
            await self.namespace["on_message"](self.cog, self.message)
        self.cog.reply_with_gemini.assert_not_awaited()
        self.history.assert_not_awaited()
        self.cog.chat_memory.search.assert_not_called()
        self.cog.compress_chat_memory.assert_not_awaited()
        self.roll.assert_not_called()

    async def test_other_bot_never_triggers_automatic_reply(self) -> None:
        self.roll.return_value = 0.0
        self.message.author.bot = True
        await self.namespace["on_message"](self.cog, self.message)
        self.cog.reply_with_gemini.assert_not_awaited()
        self.roll.assert_not_called()

    async def test_tagged_question_reads_history_without_chance_gate(self) -> None:
        self.message.mentions = [self.cog.bot.user]
        self.message.content = "<@!100> 這個問題怎麼解"
        await self.namespace["on_message"](self.cog, self.message)
        self.history.assert_awaited_once_with(self.message, 100, self.cog.chat_memory)
        self.cog.reply_with_gemini.assert_awaited_once()
        self.assertFalse(self.cog.reply_with_gemini.call_args.kwargs["speak"])
        await asyncio.sleep(0)
        self.cog.compress_chat_memory.assert_awaited_once_with(10)

    async def test_tagged_identity_question_in_voice_uses_ai_speech(self) -> None:
        channel = SimpleNamespace(id=30)
        self.message.guild = SimpleNamespace(voice_client=SimpleNamespace(
            is_connected=Mock(return_value=True), channel=channel))
        self.message.author.voice = SimpleNamespace(channel=channel)
        self.message.mentions = [self.cog.bot.user]
        self.message.content = "<@100> 你是誰"
        self.roll.return_value = 0.999
        await self.namespace["on_message"](self.cog, self.message)
        self.history.assert_awaited_once()
        self.cog.reply_with_gemini.assert_awaited_once()
        self.assertTrue(self.cog.reply_with_gemini.call_args.kwargs["speak"])
        self.message.channel.send.assert_not_awaited()
        await asyncio.sleep(0)

    async def test_prompt_contains_recent_text_and_private_spoken_context(self) -> None:
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"persist_chat_snapshot", "build_chat_prompt"}]
        self.message.content = self.message.clean_content = "Repeat the last question in English."
        self.message.created_at = datetime.now(timezone.utc)
        previous = SimpleNamespace(
            id=150, content="Quiz me in English.", clean_content="Quiz me in English.",
            author=self.message.author, channel=self.message.channel,
            attachments=[], created_at=self.message.created_at,
        )

        async def history() -> AsyncIterator[Any]:
            yield previous

        self.message.channel.history = Mock(return_value=history())
        memory = SimpleNamespace(record=Mock(), voice_history=Mock(
            return_value="Tourist 語音：What time does the train leave?"))
        namespace = {
            "discord": SimpleNamespace(Message=object, HTTPException=RuntimeError),
            "ChatMemory": object, "asyncio": asyncio, "logger": Mock(),
            "CHAT_CONTEXT_MESSAGES": 20, "CHAT_CONTEXT_CHARACTERS": 24000,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "chat-context", "exec"), namespace)
        prompt = await namespace["build_chat_prompt"](self.message, 100, memory)
        self.assertIn("Quiz me in English.", prompt)
        self.assertIn("What time does the train leave?", prompt)
        self.assertIn("Repeat the last question in English.", prompt)
        self.message.channel.history.assert_called_once_with(limit=20, before=self.message, oldest_first=False)
        memory.voice_history.assert_called_once_with(10, 200)


if __name__ == "__main__":
    unittest.main()
