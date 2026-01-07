import asyncio
import os
import re
import discord
import time
from discord.ext import commands
import requests
import random
from yt_dlp import YoutubeDL
from discord import app_commands
from typing import Optional
from discord.app_commands import Choice
from bs4 import BeautifulSoup
from googleapiclient.discovery import build  # google-api-python-client
from keys import yt_api_key
import platform

import logging

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

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vc = None
        self.owner = None

    def safe_after(self, e, channel):
        # 使用 asyncio.create_task() 安全地執行異步函數
        asyncio.create_task(self.next(channel))

    async def next(self, channel: discord.TextChannel):
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
    async def play(self, interaction: discord.Interaction, 關鍵字或網址: str, 循環播放: Choice[int]):
        print("try to play: "+關鍵字或網址)
        # print(os.listdir("."))
        # print(os.listdir("bin/"))
        await interaction.response.defer()

        if not interaction.user.voice:
            await interaction.followup.send("你先進語音")
            return

        if self.vc is None:
            try:
                self.vc = await interaction.user.voice.channel.connect()
                # await interaction.channel.send(f"我來了")
            except discord.ClientException as e:
                await interaction.followup.send(f"無法連接到語音頻道：{e}\n @Liquan 修一下")
                return
            except Exception as e:
                await interaction.followup.send(f"發生未知錯誤：{e}\n @Liquan 修一下")
                return

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
    async def leave(self, interaction: discord.Interaction):

        await interaction.response.defer()

        if self.vc is None:
            await interaction.followup.send("?")
            return
        #
        # if interaction.user != self.owner:
        #     await interaction.response.send_message("你目前不能控制Tourist唷！")
        #     return

        self.owner = None
        await self.vc.disconnect()
        self.vc = None
        queue.clear()
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
        self.vc.stop()
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