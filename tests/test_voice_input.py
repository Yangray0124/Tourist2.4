import ast
import asyncio
import base64
import copy
import io
import json
import struct
import sys
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from voice_input import Utterance, UtteranceBuffer, microphone_wav, receive_client_class

FRAME = struct.pack("<hh", 3000, 3000) * 960


class AudioBufferTests(unittest.TestCase):
    def test_speakers_are_not_mixed_and_pause_ends_a_turn(self) -> None:
        buffer = UtteranceBuffer()
        for index in range(20):
            buffer.feed(1, "Alice", FRAME, index * 0.02)
            buffer.feed(2, "Bob", FRAME, index * 0.02)
        self.assertEqual(buffer.ready(0.5), [])
        turns = buffer.ready(1.5)
        self.assertEqual([turn.user_id for turn in turns], [1, 2])
        self.assertTrue(all(turn.pcm == FRAME * 20 for turn in turns))
        self.assertEqual(buffer.ready(2), [])

    def test_silence_and_short_noise_are_discarded(self) -> None:
        buffer = UtteranceBuffer()
        buffer.feed(1, "Alice", b"\0" * 3840, 0)
        self.assertEqual(buffer.speakers, {})
        buffer.feed(1, "Alice", FRAME, 0)
        self.assertEqual(buffer.ready(2), [])

    def test_wav_has_discord_input_format(self) -> None:
        with wave.open(io.BytesIO(microphone_wav(FRAME)), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (48000, 2, 2))
            self.assertEqual(wav.readframes(960), FRAME)

    def test_continuous_speech_has_a_memory_limit(self) -> None:
        buffer = UtteranceBuffer()
        with patch("voice_input.MAX_UTTERANCE_SECONDS", 1):
            for index in range(60):
                buffer.feed(1, "Alice", FRAME, index * 0.02)
            turns = buffer.ready(1.2)
        self.assertEqual(len(turns), 1)
        self.assertLessEqual(len(turns[0].pcm), 192000)


class DaveReceiveTests(unittest.TestCase):
    def setUp(self) -> None:
        class Decoder:
            def _decode_packet(self, packet: Any) -> tuple[Any, bytes]:
                return packet, b"original"

        self.decoder_class = Decoder
        self.session = SimpleNamespace(ready=True, decrypt=Mock(return_value=b"opus"))
        self.voice = SimpleNamespace(
            _connection=SimpleNamespace(dave_protocol_version=1, dave_session=self.session),
            _get_id_from_ssrc=Mock(return_value=22), guild=SimpleNamespace(id=1),
        )
        self.decoder = Decoder()
        self.decoder.sink = SimpleNamespace(voice_client=self.voice)
        self.decoder._decoder = SimpleNamespace(decode=Mock(return_value=b"pcm"))
        self.packet = SimpleNamespace(ssrc=123, decrypted_data=b"encrypted")
        modules = {
            "discord.ext": SimpleNamespace(voice_recv=SimpleNamespace(VoiceRecvClient=object)),
            "discord.ext.voice_recv.opus": SimpleNamespace(PacketDecoder=Decoder),
            "discord.opus": SimpleNamespace(Decoder=Mock()),
            "davey": SimpleNamespace(MediaType=SimpleNamespace(audio="audio")),
        }
        with patch.dict(sys.modules, modules), patch("voice_input.version", return_value="0.5.2a179"):
            receive_client_class()

    def test_decrypts_once_using_actual_discord_user_id(self) -> None:
        packet, pcm = self.decoder._decode_packet(self.packet)
        self.assertIs(packet, self.packet)
        self.assertEqual(pcm, b"pcm")
        self.session.decrypt.assert_called_once_with(22, "audio", b"encrypted")
        self.decoder._decoder.decode.assert_called_once_with(b"opus", fec=False)

    def test_unknown_speaker_cannot_enter_opus_decoder(self) -> None:
        self.voice._get_id_from_ssrc.return_value = None
        self.assertEqual(self.decoder._decode_packet(self.packet)[1], b"\0" * 3840)
        self.session.decrypt.assert_not_called()
        self.decoder._decoder.decode.assert_not_called()

    def test_unready_dave_drops_audio_instead_of_decoding_ciphertext(self) -> None:
        self.session.ready = False
        self.decoder._decode_packet(self.packet)
        self.session.decrypt.assert_not_called()
        self.decoder._decoder.decode.assert_not_called()

    def test_non_dave_uses_original_receiver(self) -> None:
        self.voice._connection.dave_protocol_version = 0
        self.assertEqual(self.decoder._decode_packet(self.packet)[1], b"original")


class MicrophoneDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path("cogs/chat.py").read_text(encoding="utf-8"))
        chat = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Chat")
        method = next(node for node in chat.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "answer_microphone")
        self.model = Mock(return_value=object())
        self.result = {"transcript": "Next question, please", "should_reply": True}
        self.namespace: dict[str, Any] = {
            "Any": Any, "Utterance": Utterance, "asyncio": asyncio, "base64": base64,
            "json": json, "copy": copy, "microphone_wav": microphone_wav,
            "generate_chat": self.model, "gemini_text": Mock(side_effect=lambda response: json.dumps(self.result)),
            "js": {"contents": [{"parts": [{"text": ""}]}]}, "logger": Mock(),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), "microphone-decision", "exec"), self.namespace)
        self.memory = SimpleNamespace(room_history=Mock(return_value="Tourist: What time is it?"),
                                      record_room_turn=Mock())
        self.cog = SimpleNamespace(chat_memory=self.memory, voice_current_text={},
                                   speak_with_live=AsyncMock(return_value=True))
        self.message = SimpleNamespace(channel=SimpleNamespace(id=30), guild=SimpleNamespace(id=1),
                                       created_at=datetime.now(timezone.utc))

    async def test_many_people_can_address_tourist_without_wake_word(self) -> None:
        await self.namespace["answer_microphone"](self.cog, self.message, Utterance(22, "Alice", FRAME * 20))
        self.cog.speak_with_live.assert_awaited_once()
        payload = self.cog.speak_with_live.call_args.args[1]
        text = payload["contents"][0]["parts"][0]["text"]
        self.assertIn("Alice (22)", text)
        self.assertIn("What time is it?", text)
        self.assertIn("Next question", text)
        self.memory.room_history.assert_called_once_with(30)

    async def test_side_conversation_is_saved_but_does_not_trigger_speech(self) -> None:
        self.result["should_reply"] = False
        await self.namespace["answer_microphone"](self.cog, self.message, Utterance(23, "Bob", FRAME * 20))
        self.cog.speak_with_live.assert_not_awaited()
        self.memory.record_room_turn.assert_called_once()
        self.assertEqual(self.memory.record_room_turn.call_args.args[1], "Bob (23)")


if __name__ == "__main__":
    unittest.main()
