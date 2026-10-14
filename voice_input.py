"""Microphone utterance buffering and DAVE compatibility for the pinned receiver."""

import io
import logging
import struct
import time
import wave
from importlib.metadata import version
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)
PCM_BYTES_PER_SECOND = 48000 * 2 * 2
SILENCE_SECONDS = 1.0
MAX_UTTERANCE_SECONDS = 20
MIN_VOICED_BYTES = int(PCM_BYTES_PER_SECOND * 0.15)


@dataclass
class Utterance:
    user_id: int
    name: str
    pcm: bytes


@dataclass
class PendingSpeech:
    user_id: int
    name: str
    last_voice: float
    data: bytearray = field(default_factory=bytearray)
    voiced_bytes: int = 0


class UtteranceBuffer:
    def __init__(self) -> None:
        self.speakers: dict[int, PendingSpeech] = {}

    def feed(self, user_id: int, name: str, pcm: bytes, now: float) -> None:
        if not pcm or len(pcm) % 4:
            return
        samples = [value[0] for value in struct.iter_unpack("<h", pcm)]
        voiced = sum(value * value for value in samples) / len(samples) >= 200 ** 2
        state = self.speakers.get(user_id)
        if state is None:
            if not voiced or len(self.speakers) >= 32:
                return
            state = self.speakers[user_id] = PendingSpeech(user_id, name, now)
        # Bound memory even if the poll task falls behind.
        if len(state.data) + len(pcm) > MAX_UTTERANCE_SECONDS * PCM_BYTES_PER_SECOND:
            return
        state.data.extend(pcm)
        if voiced:
            state.last_voice = now
            state.voiced_bytes += len(pcm)

    def ready(self, now: float) -> list[Utterance]:
        result: list[Utterance] = []
        for user_id, state in list(self.speakers.items()):
            full = len(state.data) >= MAX_UTTERANCE_SECONDS * PCM_BYTES_PER_SECOND - 3840
            if not full and now - state.last_voice < SILENCE_SECONDS:
                continue
            del self.speakers[user_id]
            if state.voiced_bytes >= MIN_VOICED_BYTES:
                result.append(Utterance(user_id, state.name, bytes(state.data)))
        return result

    def clear(self) -> None:
        self.speakers.clear()


def microphone_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 4:
        raise ValueError("Invalid stereo microphone PCM")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(pcm)
    return output.getvalue()


def receive_client_class() -> Any:
    """Use the receiver's transport handling and discord.py's existing DAVE session."""
    from discord.ext import voice_recv
    from discord.ext.voice_recv.opus import PacketDecoder
    from discord.opus import Decoder
    from davey import MediaType

    if version("discord-ext-voice-recv") != "0.5.2a179":
        raise RuntimeError("Microphone receive requires discord-ext-voice-recv==0.5.2a179")
    # Fail during /加語音 instead of waiting for the first packet to discover missing libopus.
    probe = Decoder()
    del probe
    if getattr(PacketDecoder, "_tourist_dave_patch", False):
        return voice_recv.VoiceRecvClient
    original = PacketDecoder._decode_packet
    last_error: dict[int, float] = {}

    def decode(self: Any, packet: Any) -> tuple[Any, bytes]:
        voice = self.sink.voice_client
        connection = voice._connection
        if not connection.dave_protocol_version:
            return original(self, packet)
        # Lost packets use PLC, never attempt to decrypt the look-ahead packet twice.
        if not packet:
            return packet, self._decoder.decode(None, fec=False)
        user_id = voice._get_id_from_ssrc(packet.ssrc)
        if user_id is None or not packet.decrypted_data:
            return packet, b"\0" * 3840
        data = bytes(packet.decrypted_data)
        if data == b"\xf8\xff\xfe":
            return packet, self._decoder.decode(data, fec=False)
        session = connection.dave_session
        if session is None or not session.ready:
            return packet, b"\0" * 3840
        try:
            decoded = session.decrypt(user_id, MediaType.audio, data)
            return packet, self._decoder.decode(decoded, fec=False)
        except Exception as error:
            now = time.monotonic()
            if now - last_error.get(voice.guild.id, -10.0) >= 5:
                logger.warning("[VoiceInput] DAVE/Opus receive failed guild=%s (%s)",
                               voice.guild.id, type(error).__name__)
                last_error[voice.guild.id] = now
            return packet, b"\0" * 3840

    PacketDecoder._decode_packet = decode
    PacketDecoder._tourist_dave_patch = True
    return voice_recv.VoiceRecvClient
