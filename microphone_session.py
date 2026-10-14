"""Per-voice-channel conversation manager; raw microphone audio stays in memory."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from voice_input import Utterance, UtteranceBuffer

logger = logging.getLogger(__name__)


class MicrophoneSession:
    def __init__(self, bot: Any, voice: Any) -> None:
        self.bot = bot
        self.voice = voice
        self.loop = asyncio.get_running_loop()
        self.buffer = UtteranceBuffer()
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue(maxsize=8)
        self.tasks: list[asyncio.Task[None]] = []
        self.closed = False
        self.sequence = 0

    def start(self) -> None:
        from discord.ext import voice_recv

        owner = self

        class Sink(voice_recv.AudioSink):
            def __init__(self) -> None:
                super().__init__()

            def wants_opus(self) -> bool:
                return False

            def write(self, user: Any, data: Any) -> None:
                if user is None or user.bot or not data.pcm or owner.closed:
                    return
                owner.loop.call_soon_threadsafe(owner.feed, user.id, user.display_name, bytes(data.pcm))

            def cleanup(self) -> None:
                if not owner.closed:
                    owner.loop.call_soon_threadsafe(owner.close)

        self.voice.listen(Sink(), after=self.receive_finished)
        self.tasks = [asyncio.create_task(self.poll()), asyncio.create_task(self.process())]
        logger.warning("[VoiceInput] listening guild=%s voice_channel=%s", self.voice.guild.id, self.voice.channel.id)

    def receive_finished(self, error: Exception | None) -> None:
        if error is not None:
            logger.error("[VoiceInput] receiver stopped (%s)", type(error).__name__)
        self.loop.call_soon_threadsafe(self.close)

    def feed(self, user_id: int, name: str, pcm: bytes) -> None:
        if not self.closed and not self.music_playing():
            self.buffer.feed(user_id, name, pcm, time.monotonic())

    def music_playing(self) -> bool:
        if self.voice.is_paused():
            return True
        chat = self.bot.get_cog("Chat")
        source = chat.voice_audio_sources.get(self.voice.guild.id) if chat is not None else None
        return self.voice.is_playing() and (source is None or self.voice.source is not source)

    async def poll(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.2)
            if not self.voice.is_connected():
                self.close()
                return
            if self.music_playing():
                self.buffer.clear()
                continue
            for utterance in self.buffer.ready(time.monotonic()):
                try:
                    self.queue.put_nowait(utterance)
                except asyncio.QueueFull:
                    logger.warning("[VoiceInput] conversation queue full; dropping stale audio")

    async def process(self) -> None:
        while not self.closed:
            utterance = await self.queue.get()
            try:
                chat = self.bot.get_cog("Chat")
                if chat is None:
                    logger.error("[VoiceInput] Chat cog not available")
                    continue
                if self.music_playing():
                    continue
                # Generated event IDs are private memory keys, not Discord message links.
                created_at = datetime.now(timezone.utc)
                self.sequence = (self.sequence + 1) % (1 << 22)
                import discord

                message = SimpleNamespace(
                    guild=self.voice.guild, channel=self.voice.channel,
                    id=discord.utils.time_snowflake(created_at) + self.sequence,
                    created_at=created_at,
                    author=self.voice.guild.get_member(utterance.user_id),
                )
                await chat.answer_microphone(message, utterance)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[VoiceInput] microphone turn failed")
            finally:
                self.queue.task_done()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.buffer.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        for task in self.tasks:
            task.cancel()
        if self.voice.is_listening():
            self.voice.stop_listening()
        logger.warning("[VoiceInput] stopped guild=%s voice_channel=%s", self.voice.guild.id, self.voice.channel.id)
