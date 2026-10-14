import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock


class VoiceJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path("cogs/voice.py").read_text(encoding="utf-8"))
        voice = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Voice")
        methods = [node for node in voice.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {"__init__", "connect_voice", "join_voice", "leave", "next", "play"}]
        for method in methods:
            method.decorator_list = []
        cls = ast.ClassDef(name="Voice", bases=[], keywords=[], body=methods, decorator_list=[])
        self.dependencies = Mock(return_value=None)
        self.namespace: dict[str, Any] = {
            "asyncio": asyncio, "Optional": Optional, "logger": Mock(), "queue": [],
            "Choice": list, "search_yt": Mock(return_value=(None, None, None)),
            "get_act": Mock(return_value="game"), "voice_dependency_problem": self.dependencies,
            "discord": SimpleNamespace(Interaction=object, VoiceClient=object, TextChannel=object,
                                       __version__="2.7.1", Status=SimpleNamespace(idle="idle"), Game=Mock()),
            "commands": SimpleNamespace(Bot=object),
            "receive_client_class": Mock(return_value=object),
            "MicrophoneSession": Mock(return_value=SimpleNamespace(start=Mock(), close=Mock(), closed=False)),
            "Any": Any,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                     "voice-join", "exec"), self.namespace)
        self.bot = SimpleNamespace(change_presence=AsyncMock())
        self.cog = self.namespace["Voice"](self.bot)
        self.channel = SimpleNamespace(id=10, mention="<#10>", connect=AsyncMock(),
                                       permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True)))
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.voice = SimpleNamespace(channel=self.channel, is_connected=Mock(return_value=True),
                                     disconnect=AsyncMock(), play=Mock(), guild=self.guild, listen=Mock())
        self.interaction = SimpleNamespace(
            guild=self.guild, user=SimpleNamespace(voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )

        async def connect(**kwargs: Any) -> Any:
            await asyncio.sleep(0)
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect

    async def test_join_connects_once_without_music(self) -> None:
        await self.cog.join_voice(self.interaction)
        self.channel.connect.assert_awaited_once_with(timeout=20, reconnect=False, self_deaf=False, cls=object)
        self.voice.play.assert_not_called()
        self.assertEqual(self.namespace["queue"], [])
        self.interaction.followup.send.assert_awaited_once_with("OK")

    async def test_concurrent_joins_reuse_same_connection(self) -> None:
        first, second = await asyncio.gather(
            self.cog.connect_voice(self.interaction), self.cog.connect_voice(self.interaction)
        )
        self.assertIs(first, second)
        self.channel.connect.assert_awaited_once()

    async def test_connection_failure_stops_and_cleans_partial_connection(self) -> None:
        async def fail(**kwargs: Any) -> Any:
            self.guild.voice_client = self.voice
            raise TimeoutError("voice handshake timed out")

        self.channel.connect.side_effect = fail
        self.assertIsNone(await self.cog.connect_voice(self.interaction))
        self.channel.connect.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.namespace["logger"].exception.assert_called_once()

    async def test_missing_dependency_prevents_handshake(self) -> None:
        self.dependencies.return_value = "missing davey"
        self.assertIsNone(await self.cog.connect_voice(self.interaction))
        self.channel.connect.assert_not_awaited()

    async def test_existing_other_channel_is_not_moved(self) -> None:
        self.guild.voice_client = self.voice
        self.voice.channel = SimpleNamespace(id=20, mention="<#20>")
        self.assertIsNone(await self.cog.connect_voice(self.interaction))
        self.channel.connect.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()

    async def test_leave_can_disconnect_standalone_join(self) -> None:
        self.guild.voice_client = self.voice
        self.assertIsNone(self.cog.vc)
        await self.cog.leave(self.interaction)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        await self.cog.next(SimpleNamespace())

    async def test_song_reuses_standalone_voice_connection(self) -> None:
        self.guild.voice_client = self.voice
        self.assertIsNone(self.cog.vc)
        await self.cog.play(self.interaction, "song", SimpleNamespace(value=0))
        self.assertIs(self.cog.vc, self.voice)
        self.channel.connect.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()
        self.namespace["search_yt"].assert_called_once_with("song")

    async def test_song_does_not_use_stale_cached_connection(self) -> None:
        stale = SimpleNamespace()
        self.cog.vc = stale
        self.guild.voice_client = self.voice
        await self.cog.play(self.interaction, "song", SimpleNamespace(value=0))
        self.assertIs(self.cog.vc, self.voice)
        self.channel.connect.assert_not_awaited()

    async def test_song_in_other_channel_does_not_search_or_move(self) -> None:
        self.guild.voice_client = self.voice
        self.voice.channel = SimpleNamespace(id=20, mention="<#20>")
        await self.cog.play(self.interaction, "song", SimpleNamespace(value=0))
        self.namespace["search_yt"].assert_not_called()
        self.channel.connect.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()


class VoiceDependencyTests(unittest.TestCase):
    def test_old_library_and_missing_dave_are_reported(self) -> None:
        tree = ast.parse(Path("cogs/voice.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "voice_dependency_problem")
        discord = SimpleNamespace(version_info=SimpleNamespace(major=2, minor=4, micro=0), __version__="2.4.0")
        find_spec = Mock(return_value=None)
        namespace = {"discord": discord, "Optional": Optional,
                     "importlib": SimpleNamespace(util=SimpleNamespace(find_spec=find_spec))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "voice-dependencies", "exec"), namespace)
        self.assertIn("2.4.0", namespace["voice_dependency_problem"]())
        discord.version_info = SimpleNamespace(major=2, minor=7, micro=1)
        self.assertIn("davey", namespace["voice_dependency_problem"]())
        find_spec.return_value = object()
        self.assertIsNone(namespace["voice_dependency_problem"]())


if __name__ == "__main__":
    unittest.main()
