import asyncio
import importlib.util
import os
import re
import discord
import time
from discord.ext import commands
import requests
import random
from yt_dlp import YoutubeDL
from discord import app_commands
from typing import Any, Optional
from microphone_session import MicrophoneSession
from voice_input import receive_client_class
from discord.app_commands import Choice
from bs4 import BeautifulSoup
from googleapiclient.discovery import build  # google-api-python-client
from keys import yt_api_key
import platform

import logging
logger = logging.getLogger(__name__)


def voice_dependency_problem() -> Optional[str]:
    version = discord.version_info
    if (version.major, version.minor, version.micro) < (2, 7, 1):
        return f"discord.py {discord.__version__} 太舊，請更新 discord.py[voice]>=2.7.1。"
    if importlib.util.find_spec("davey") is None:
        return "缺少 DAVE 語音依賴 davey，請更新 discord.py[voice]。"
    if importlib.util.find_spec("discord.ext.voice_recv") is None:
        return "缺少麥克風接收依賴 discord-ext-voice-recv，請更新 requirements.txt。"
    return None

# 設定日誌等級為 DEBUG，並寫入到檔案
# logging.basicConfig(
#     level=logging.DEBUG,
#     filename='bot_debug.log',
#     filemode='w',
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
# )

yt_dlp_options = {
    # 'format': 'best/bestaudio/worstaudio/worst',
    'format': 'bestaudio/best',
    'outtmpl': './downloads/%(id)s.%(ext)s',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    # 'outtmpl': 'downloads/%(title)s.%(ext)s',
    # 'postprocessors': [{
    #     'key': 'FFmpegExtractAudio',
    #     'preferredcodec': 'mp3',
    #     'preferredquality': '128',
    # }],
    # 'http_headers': {
    # 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/91.0',
    # 'Referer': 'https://www.bilibili.com/',
    # 'Origin': 'https://www.bilibili.com'
    # },
    'cookiefile': 'cookies.txt'
}

# FFMPEG_OPTIONS = {'options': '-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"',
    'options': '-vn'
}
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"
youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, developerKey=yt_api_key)

if platform.system() == "Windows":
    executable_path = "bin\\ffmpeg.exe"
else:
    executable_path = "ffmpeg"

queue = []  # {music_url, title, yt_url, loop?}

def get_act():
    pen = open("status.txt", 'r', encoding="UTF-8")
    game = pen.readline()
    pen.close()
    return game

def create_audio_source(audio_url: str):
    if os.path.exists(audio_url):
        print(f"[播放模式] 讀取本地硬碟檔案：{audio_url}")
        # 本地檔案不需要 reconnect 參數，也不需要 headers，只要 -vn
        return discord.FFmpegOpusAudio(
            audio_url,
            before_options="-vn",
            options="-vn",
            executable=executable_path
        )
    else:
        # 網路串流模式 (給 m4a 用的)
        print(f"[播放模式] 讀取網路串流")

        return discord.FFmpegOpusAudio(
            audio_url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
            executable=executable_path
        )


def search_yt(url: str):

    if not url.startswith("http"):
        res = youtube.search().list(
            q=url,
            part="id",
            maxResults=1,
            type="video"
        ).execute()
        # print(res)
        if res is None:
            return None, "Error1", None
        # print(res["items"][0])
        url = "https://www.youtube.com/watch?v=" + res["items"][0]["id"]["videoId"]


    with YoutubeDL(yt_dlp_options) as ydl:
        try:
            info = ydl.extract_info(url, download=False)

            # 【關鍵判斷】是否為 m3u8 (HLS) 格式？
            download_url = info.get('url', '')
            protocol = info.get('protocol', '')
            is_m3u8 = 'm3u8' in download_url or 'm3u8' in protocol

            if is_m3u8:
                print(f"⚠️ 偵測到 m3u8 (HLS) 格式！啟動下載模式以避免 403... ({info['title']})")

                # 重新執行一次，這次開啟 download=True
                # 因為設定了 outtmpl，它會自動下載到 ./downloads 資料夾
                info = ydl.extract_info(url, download=True)

                # 取得下載後的「硬碟路徑」
                filename = ydl.prepare_filename(info)

                # 回傳：本地路徑, 標題, 原始網址, (本地檔不需要headers)
                return filename, info["title"], url

            else:
                print(f"✅ 偵測到一般格式 (m4a/webm)，使用直接串流。")
                # 不是 m3u8，就照舊回傳網址和 headers
                return info["url"], info["title"], url

        except Exception as e:
            print(f"Download/Info Error: {e}")
            return None, "Error2", None

    # print(info["url"])
    # thumbnail = info["thumbnail"]
    # return info["url"], info["title"], url


class Voice(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.vc = None
        self.owner = None
        self.connection_lock = asyncio.Lock()
        self.microphone_sessions: dict[int, Any] = {}

    def cog_unload(self) -> None:
        for session in self.microphone_sessions.values():
            session.close()
        self.microphone_sessions.clear()

    async def connect_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        guild = interaction.guild
        state = getattr(interaction.user, "voice", None)
        if guild is None or state is None or state.channel is None:
            await interaction.followup.send("你先進伺服器的語音頻道。")
            return None
        channel = state.channel
        async with self.connection_lock:
            voice = guild.voice_client
            if voice is not None and voice.is_connected():
                if voice.channel.id != channel.id:
                    await interaction.followup.send(f"Tourist 已在 {voice.channel.mention}，請先使用 /離開。")
                    return None
                return voice
            problem = voice_dependency_problem()
            if problem:
                logger.error("[Voice] guild=%s dependency check failed: %s", guild.id, problem)
                await interaction.followup.send(problem + "\n請在 bot 環境執行：python -m pip install -U -r requirements.txt")
                return None
            permissions = channel.permissions_for(guild.me)
            if not permissions.connect or not permissions.speak:
                await interaction.followup.send("Tourist 需要這個頻道的「連線」與「說話」權限。")
                return None
            logger.warning("[Voice] connecting guild=%s channel=%s discord.py=%s reconnect=False",
                           guild.id, channel.id, discord.__version__)
            try:
                if voice is not None:
                    await voice.disconnect(force=True)
                voice = await channel.connect(
                    timeout=20, reconnect=False, self_deaf=False, cls=receive_client_class()
                )
            except Exception as error:
                logger.exception("[Voice] connection failed guild=%s channel=%s code=%s",
                                 guild.id, channel.id, getattr(error, "code", None))
                stale = guild.voice_client
                if stale is not None:
                    try:
                        await stale.disconnect(force=True)
                    except Exception:
                        logger.exception("[Voice] failed to clean up connection guild=%s", guild.id)
                await interaction.followup.send(
                    f"語音連線失敗（{type(error).__name__}，code={getattr(error, 'code', '無')}）。"
                    "已停止重試，請查看 terminal 的 [Voice] 紀錄。"
                )
                return None
            logger.warning("[Voice] connected guild=%s channel=%s", guild.id, channel.id)
            return voice

    @app_commands.command(name="加語音", description="加入你的語音頻道，不播放音樂")
    @app_commands.guild_only()
    async def join_voice(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        voice = await self.connect_voice(interaction)
        if voice is not None:
            if not hasattr(voice, "listen"):
                await interaction.followup.send("目前連線不支援收音，請先 /離開，再 /加語音。")
                return
            session = self.microphone_sessions.get(voice.guild.id)
            if session is None or session.closed:
                session = MicrophoneSession(self.bot, voice)
                try:
                    session.start()
                except Exception:
                    session.close()
                    logger.exception("[VoiceInput] cannot start microphone receiver")
                    await interaction.followup.send("麥克風接收啟動失敗，請查看 terminal 的 [VoiceInput] 紀錄。")
                    return
                self.microphone_sessions[voice.guild.id] = session
            await interaction.followup.send("OK")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if self.bot.user is None or member.id != self.bot.user.id or before.channel == after.channel:
            return
        logger.warning("[Voice] state guild=%s channel=%s -> %s", member.guild.id,
                       getattr(before.channel, "id", None), getattr(after.channel, "id", None))
        if before.channel is not None:
            session = self.microphone_sessions.pop(member.guild.id, None)
            if session is not None:
                session.close()

    def safe_after(self, e, channel):
        # 使用 asyncio.create_task() 安全地執行異步函數
        asyncio.create_task(self.next(channel))

    async def next(self, channel: discord.TextChannel) -> None:
        if not queue or self.vc is None:
            return
        # print("next", len(queue))

        if queue[0]["loop"]: # 循環播放
            queue[0]["music_url"], queue[0]["title"], queue[0]["yt_url"] = search_yt(queue[0]["yt_url"])  # 重抓避免失效
            # _, queue[0]["title"], queue[0]["yt_url"] = search_yt(queue[0]["yt_url"])
            # self.vc.play(discord.FFmpegOpusAudio(queue[0]["music_url"], **FFMPEG_OPTIONS, executable=executable_path),
            #              after=lambda e: asyncio.run_coroutine_threadsafe(self.next(channel), self.bot.loop))
            self.vc.play(
                create_audio_source(queue[0]["music_url"]),
                after=lambda e: asyncio.run_coroutine_threadsafe(self.next(channel), self.bot.loop)
            )
            return

        print("pop")
        queue.pop(0)

        if len(queue) == 0:
            try:
                await channel.send("播完了 :kissing:")
                game = get_act()
                await self.bot.change_presence(status=discord.Status.idle, activity=discord.Game(game))
            except Exception as e:
                print(e)
            return

        # print(queue[0][0])
        queue[0]["music_url"], queue[0]["title"], queue[0]["yt_url"] = search_yt(queue[0]["yt_url"])  # 重抓避免失效
        # print(queue[0][0])

        # self.vc.play(discord.FFmpegOpusAudio(queue[0]["music_url"], **FFMPEG_OPTIONS, executable=executable_path),
        #              after=lambda e: asyncio.run_coroutine_threadsafe(self.next(channel), self.bot.loop))
        self.vc.play(
            create_audio_source(queue[0]["music_url"]),
            after=lambda e: asyncio.run_coroutine_threadsafe(self.next(channel), self.bot.loop)
        )
        await channel.send(f"現在播放： [{queue[0]['title']}]({queue[0]['yt_url']})")
        await self.bot.change_presence(status=discord.Status.idle, activity=discord.Activity(type=discord.ActivityType.listening, name=queue[0]["title"]))

    @app_commands.command(name="唱歌", description="加入語音頻道，將歌曲加到隊列")
    @app_commands.describe(關鍵字或網址="關鍵字或網址", 循環播放="重複播放這首歌")
    @app_commands.choices(
        循環播放=[
            Choice(name="是", value=1),
            Choice(name="否", value=0),
        ]
    )
    async def play(self, interaction: discord.Interaction, 關鍵字或網址: str, 循環播放: Choice[int]) -> None:
        print("try to play: "+關鍵字或網址)
        # print(os.listdir("."))
        # print(os.listdir("bin/"))
        await interaction.response.defer()

        if not interaction.user.voice:
            await interaction.followup.send("你先進語音")
            return

        voice = await self.connect_voice(interaction)
        if voice is None:
            return
        self.vc = voice

        if self.owner is None:
            self.owner = interaction.user
        # await interaction.channel.send("123")
        m_url, m_title, yt_url = search_yt(關鍵字或網址)
        # await interaction.channel.send(m_url)
        print("m_title: ", m_title)

        if not yt_url:
            await interaction.followup.send("好像找不到捏")
            return

        if len(queue) == 0:
            
            queue.append({"music_url": m_url, "title": m_title, "yt_url": yt_url, "loop": 循環播放.value})
            try:
                # self.vc.play(discord.FFmpegOpusAudio(queue[0]["music_url"], **FFMPEG_OPTIONS, executable=executable_path),
                #              after=lambda e: asyncio.run_coroutine_threadsafe(self.next(interaction.channel), self.bot.loop))#asyncio.run(self.next(interaction.channel))
                self.vc.play(
                    create_audio_source(queue[0]["music_url"]),
                    after=lambda e: asyncio.run_coroutine_threadsafe(self.next(interaction.channel), self.bot.loop)
                )
            except Exception as e:
                await interaction.followup.send(e)
                return

            if 循環播放.value:
                await interaction.followup.send(f"好的，循環播放 [{queue[0]['title']}]({queue[0]['yt_url']})")
            else:
                await interaction.followup.send(f"好的，播放 [{queue[0]['title']}]({queue[0]['yt_url']})")

            await self.bot.change_presence(status=discord.Status.idle, activity=discord.Activity(type=discord.ActivityType.listening, name=queue[0]['title']))
            return

        # print(m_title)
        queue.append({"music_url": m_url, "title": m_title, "yt_url": yt_url, "loop": 循環播放.value})
        # await interaction.response.send_message(f"加入隊列成功： [{m_title}]({yt_url})")   # ??
        await interaction.followup.send(f"加入隊列成功： [{m_title}]({yt_url})")

    @app_commands.command(name="離開", description="掰掰")
    async def leave(self, interaction: discord.Interaction) -> None:

        await interaction.response.defer()

        voice = interaction.guild.voice_client if interaction.guild is not None else None
        if interaction.guild is not None:
            session = self.microphone_sessions.pop(interaction.guild.id, None)
            if session is not None:
                session.close()
        if voice is None:
            await interaction.followup.send("?")
            return
        #
        # if interaction.user != self.owner:
        #     await interaction.response.send_message("你目前不能控制Tourist唷！")
        #     return

        if self.vc is voice:
            self.owner = None
            self.vc = None
            queue.clear()
        await voice.disconnect(force=True)
        await interaction.followup.send("掰掰")
        game = get_act()
        await self.bot.change_presence(status=discord.Status.idle, activity=discord.Game(game))

    @app_commands.command(name="跳", description="跳過這首歌曲")
    async def skip(self, interaction: discord.Interaction):

        await interaction.response.defer()

        if self.vc is None:
            await interaction.followup.send("?")
            return
        #
        # if interaction.user != self.owner:
        #     await interaction.response.send_message("你目前不能控制Tourist唷！")
        #     return

        queue[0]["loop"] = False
        getattr(self.vc, "stop_playing", self.vc.stop)()
        # queue.pop(0)

        # if len(queue) == 1:
        #     queue.pop(0)
        #     await interaction.response.send_message("沒歌了 :kissing:")
        #     return

        await interaction.followup.send("成功跳過這首")
        # await self.next(interaction.channel)
        # self.vc.play(discord.FFmpegOpusAudio(queue[0][0], **FFMPEG_OPTIONS, executable=executable_path),
        #              after=lambda e: asyncio.run_coroutine_threadsafe(self.next(interaction.channel), self.bot.loop))#asyncio.run(self.next(interaction.channel))
        # await interaction.channel.send(f"目前播放： [{queue[0][1]}]({queue[0][2]})")

    @app_commands.command(name="目前隊列", description="查看目前歌曲")
    async def see(self, interaction: discord.Interaction):

        await interaction.response.defer()

        if len(queue) == 0:
            await interaction.followup.send("目前隊列是空的！")
            return

        msg = "- 目前隊列：\n" + "```"
        counter = 1
        for s in queue:
            msg += f"{counter:>2}.  "
            if s["loop"]:
                msg += "(循環播放)"
            msg += f"{s['title']}\n"
            counter += 1
        msg += "```"
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
