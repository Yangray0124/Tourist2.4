import ast
import asyncio
import io
import sys
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

from gemini_live import (
    LIVE_MODEL, LIVE_SAMPLE_RATE, LiveReply, LiveVoiceError,
    collect_live_reply, generate_live_reply, pcm_to_wav,
)


class FakeSession:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def receive(self) -> AsyncIterator[Any]:
        for event in self.events:
            yield event


def event(pcm: bytes = b"", transcript: str = "", complete: bool = False) -> Any:
    return SimpleNamespace(server_content=SimpleNamespace(
        interrupted=False, turn_complete=complete,
        output_transcription=SimpleNamespace(text=transcript),
        model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(
            data=pcm, mime_type="audio/pcm;rate=24000"))]),
    ))


class LiveAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_chunks_and_transcript_are_preserved(self) -> None:
        answer = await collect_live_reply(FakeSession([
            event(b"\x01\x00", "你"), event(b"\x02\x00", "好", True),
        ]))
        self.assertEqual(answer.pcm, b"\x01\x00\x02\x00")
        self.assertEqual(answer.transcript, "你好")
        with wave.open(io.BytesIO(pcm_to_wav(answer.pcm)), "rb") as audio:
            self.assertEqual(audio.getframerate(), 24000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.readframes(2), answer.pcm)

    async def test_empty_or_incomplete_audio_is_rejected(self) -> None:
        for events in ([event(complete=True)], [event(b"\x01\x00")]):
            with self.assertRaises(ValueError):
                await collect_live_reply(FakeSession(events))

    async def test_audio_limit_is_enforced(self) -> None:
        with patch("gemini_live.LIVE_MAX_AUDIO_BYTES", 2):
            with self.assertRaises(ValueError):
                await collect_live_reply(FakeSession([event(b"\x00\x00\x00\x00", complete=True)]))

    async def test_sdk_request_decodes_image_and_closes_session(self) -> None:
        session = FakeSession([event(b"\x00\x00", "你好", True)])
        session.send_client_content = AsyncMock()
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        connect = Mock(return_value=session_context)
        client_context = AsyncMock()
        client_context.__aenter__.return_value = SimpleNamespace(live=SimpleNamespace(connect=connect))
        constructor = Mock(return_value=SimpleNamespace(aio=client_context))
        part = SimpleNamespace(from_text=Mock(), from_bytes=Mock())
        sdk = SimpleNamespace(Client=constructor, types=SimpleNamespace(Part=part, Content=Mock()))
        payload = {
            "systemInstruction": {"parts": [{"text": "You are Tourist"}]},
            "contents": [{"role": "user", "parts": [{"text": "hello"},
                         {"inline_data": {"data": "YWJj", "mime_type": "image/png"}}]}],
            "generationConfig": {"temperature": 1.6},
        }
        with patch.dict(sys.modules, {"google": SimpleNamespace(genai=sdk), "google.genai": sdk}):
            answer = await generate_live_reply(payload, "test-key")
        self.assertEqual(answer.transcript, "你好")
        part.from_bytes.assert_called_once_with(data=b"abc", mime_type="image/png")
        self.assertEqual(connect.call_args.kwargs["model"], LIVE_MODEL)
        config = connect.call_args.kwargs["config"]
        self.assertEqual(config["response_modalities"], ["AUDIO"])
        self.assertEqual(config["temperature"], 1.6)
        self.assertEqual(config["thinking_config"], {"thinking_level": "minimal"})
        self.assertEqual(config["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"], "Fenrir")
        self.assertIn("英文或其他語言", config["system_instruction"])
        self.assertNotIn("請用中文", config["system_instruction"])
        self.assertIn("Tourist", config["system_instruction"])
        session.send_client_content.assert_awaited_once()
        session_context.__aexit__.assert_awaited_once()
        client_context.__aexit__.assert_awaited_once()

    async def test_sdk_error_is_sanitized(self) -> None:
        sdk = SimpleNamespace(Client=Mock(side_effect=RuntimeError("sensitive test-key URL")),
                              types=SimpleNamespace(Part=Mock(), Content=Mock()))
        with patch.dict(sys.modules, {"google": SimpleNamespace(genai=sdk), "google.genai": sdk}):
            with self.assertRaises(LiveVoiceError) as error:
                await generate_live_reply({"contents": []}, "test-key")
        self.assertEqual(str(error.exception), "RuntimeError")


class DiscordVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        chat = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Chat")
        method = next(node for node in chat.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "speak_with_live")
        self.source = Mock()
        self.live = AsyncMock(return_value=LiveReply(b"\x00\x00" * 240, "你好"))
        self.log = Mock()
        self.voice = SimpleNamespace(
            is_connected=Mock(return_value=True), is_playing=Mock(return_value=False),
            is_paused=Mock(return_value=False), source=None, stop=Mock(),
        )
        self.guild = SimpleNamespace(id=1, voice_client=self.voice)
        self.message = SimpleNamespace(guild=self.guild, channel=SimpleNamespace(id=10),
                                       id=200, created_at=datetime.now(timezone.utc))
        self.cog = SimpleNamespace(
            voice_reply_locks={}, cut_and_reply=AsyncMock(),
            chat_memory=SimpleNamespace(record_voice_reply=Mock()),
        )
        self.namespace: dict[str, Any] = {
            "discord": SimpleNamespace(Message=object, AudioSource=object, HTTPException=RuntimeError,
                                       FFmpegOpusAudio=Mock(return_value=self.source)),
            "Any": Any, "Optional": __import__("typing").Optional, "asyncio": asyncio,
            "generate_live_reply": self.live, "gemini_api_key": "test-key",
            "LIVE_MODEL": LIVE_MODEL, "LIVE_SAMPLE_RATE": LIVE_SAMPLE_RATE,
            "DEFAULT_LIVE_VOICE": "Fenrir",
            "LiveVoiceError": LiveVoiceError, "pcm_to_wav": pcm_to_wav,
            "io": io, "sys": sys, "log_model_attempt": self.log,
            "logger": Mock(),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), "discord-voice", "exec"), self.namespace)

    async def test_connected_idle_voice_plays_without_text(self) -> None:
        def play(source: Any, *, after: Any) -> None:
            self.voice.source = source
            after(None)

        self.voice.play = Mock(side_effect=play)
        result = await self.namespace["speak_with_live"](self.cog, self.message, {})
        self.assertTrue(result)
        self.voice.play.assert_called_once()
        self.cog.cut_and_reply.assert_not_awaited()
        self.live.assert_awaited_once_with({}, "test-key", "Fenrir")
        self.cog.chat_memory.record_voice_reply.assert_called_once_with(
            200, 10, self.message.created_at.isoformat(), "你好"
        )
        self.log.assert_called_once_with(LIVE_MODEL, True, "voice playback complete", "voice")
        self.voice.stop.assert_not_called()

    async def test_music_is_not_interrupted(self) -> None:
        self.voice.is_playing.return_value = True
        self.assertFalse(await self.namespace["speak_with_live"](self.cog, self.message, {}))
        self.live.assert_not_awaited()
        self.voice.stop.assert_not_called()

    async def test_failed_live_allows_text_fallback(self) -> None:
        self.live.side_effect = LiveVoiceError("HTTP 503")
        self.assertFalse(await self.namespace["speak_with_live"](self.cog, self.message, {}))
        self.log.assert_called_once_with(LIVE_MODEL, False, "HTTP 503", "voice")


if __name__ == "__main__":
    unittest.main()
