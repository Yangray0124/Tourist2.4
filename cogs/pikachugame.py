import os.path
import random

import discord
import numpy
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
import cv2
import numpy as np
import requests
import sqlite3
import json


events = [None for _ in range(91)]

events[3], events[6],   events[12],  events[13], events[18], events[21],  events[25], events[26], events[33],  events[39] =\
"Lapras",  "Passimian", "Exeggutor", "Koko",     "Cosmog",   "Passimian", "Lapras",   "Lele",     "Passimian", "Jigglypuff"

events[43], events[45], events[46], events[55], events[56], events[57],  events[70],   events[77],   events[78],   events[79] =\
"Popplio",  "Snorlax",  "Snorlax",  "Snorlax",  "Snorlax",  "Passimian", "Turtonator", "Jigglypuff", "Jigglypuff", "Jigglypuff"

events[83], events[86], events[87], events[89],  events[90] =\
"Pidgeot", "Rayquaza",  "Bewear",   "Dragonite", "Pikachu"

pos_xy = [(0, 0) for _ in range(91)]

for i in range(1, 91):
    if (i-1) % 20 + 1 <= 10:
        pos_xy[i] = ( -50 + 170*((i-1)%10+1), 990 - 106*2*(i//20) )
    else:
        pos_xy[i] = ( -50 + 170*(10-(i-1)%10), 884 - 106*2*((i-1)//20) )


def render(bottom: numpy.ndarray, top: numpy.ndarray, x, y):
    fh, fw = top.shape[:2]
    roi = bottom[y:(y + fh), x:(x + fw)]

    if top.shape[-1] == 4:  # 有alpha
        f_rgb = top[:, :, :3]
        alpha = top[:, :, 3] / 255.0
    else:
        f_rgb = top
        alpha = np.ones((fh, fw), dtype=np.float32)

    roi = roi.astype(float)
    f_rgb = f_rgb.astype(float)
    alpha_inv = 1.0 - alpha

    foreground_part = alpha[:, :, np.newaxis] * f_rgb
    background_part = alpha_inv[:, :, np.newaxis] * roi
    blended = cv2.add(foreground_part, background_part)
    bottom[y:(y + fh), x:(x + fw)] = blended.astype(np.uint8)
    return bottom


def turn_now(channel: discord.TextChannel):
    turns = {}
    with open("pika_playing.json", 'r', encoding="UTF-8") as f:
        if f is not None:
            turns = json.load(f)

    if f"channel_{channel.id}" not in turns:
        now = 0
    else:
        now = turns[f"channel_{channel.id}"]
    now = int(now)
    return now


def turn_write(channel: discord.TextChannel, strr: str):
    turns = {}
    with open("pika_playing.json", 'r', encoding="UTF-8") as f:
        if f is not None:
            turns = json.load(f)
    turns[f"channel_{channel.id}"] = strr

    with open("pika_playing.json", "w", encoding="UTF-8") as f:
        json.dump(turns, f, indent=4, ensure_ascii=False)


class Pikachugame(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.con = sqlite3.connect('pika.db')
        self.cursor = self.con.cursor()
        # self.cursor.execute('''
        #         CREATE TABLE IF NOT EXISTS users (
        #             id INTEGER PRIMARY KEY,
        #             name TEXT NOT NULL,
        #             pos INTEGER NOT NULL,
        #             turn INTEGER NOT NULL,
        #             sleep BOOLEAN DEFAULT 0,
        #             boost BOOLEAN DEFAULT 0
        #         )''')
        # self.con.commit()
        # print("創建 users table (pika.db)")

    def __del__(self):
        self.con.close()

    def people_count(self, channel: discord.TextChannel):
        self.cursor.execute(f"SELECT COUNT(*) FROM channel_{channel.id};")
        return self.cursor.fetchone()[0]

    def find_player_by_turn(self, channel: discord.TextChannel, turn):
        self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id} WHERE turn = ?;", (turn,))
        return self.cursor.fetchone()

    async def show_map(self, channel: discord.TextChannel):
        self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id};")
        players = self.cursor.fetchall()
        # print(players)

        mp = cv2.imread("img/map.png")

        r = [False for _ in range(0, 91)]
        for player in players:
            # print(player)
            pos = player[2]
            if r[pos]:
                continue
            r[pos] = True

            if pos == 0:
                players_now = [player_now for player_now in players if player_now[2] == pos]
                y = 990 - 30*(len(players_now)-1)
                for player_now in players_now:
                    avatar = cv2.imread(f"img/user_avatar/{player_now[1]}.png", cv2.IMREAD_UNCHANGED)
                    mp = render(mp, avatar, 20, y)
                    y += 30
            else:
                (x, y) = pos_xy[pos]
                players_now = [player_now for player_now in players if player_now[2] == pos]
                x += 20*(len(players_now)-1)
                for player_now in players_now:
                    avatar = cv2.imread(f"img/user_avatar/{player_now[1]}.png", cv2.IMREAD_UNCHANGED)
                    mp = render(mp, avatar, x, y)
                    x -= 20

        cv2.imwrite("img/newmap.png", mp)
        await channel.send(file=discord.File("img/newmap.png"))

    async def gogo(self, channel: discord.TextChannel , player, des):
        if events[des] == "Lapras":
            if des == 3:
                await channel.send(f"<@{player[0]}> 遇到了**拉普拉斯**，載著<@{player[0]}>游到了**17**！")
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET pos = 17, boost = 0 "
                                    "WHERE id = ?;", (player[0],))
                self.con.commit()
                await self.show_map(channel)
            if des == 25:
                await channel.send(f"<@{player[0]}> 遇到了**拉普拉斯**，載著<@{player[0]}>游到了**37**！")
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET pos = 37, boost = 0 "
                                    "WHERE id = ?;", (player[0],))
                self.con.commit()
                await self.show_map(channel)

        if events[des] == "Passimian":
            ls = [6, 21, 33, 57]
            ls.remove(des)
            new_des = ls[random.randint(0, 2)]
            await channel.send(f"<@{player[0]}> 遇到了**投擲猴**，將<@{player[0]}>丟給了其中一個同伴！")
            await channel.send(f"<@{player[0]}> 被丟到了 **{new_des}**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, boost = 0 "
                                "WHERE id = ?;", (new_des, player[0]))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Exeggutor":
            await channel.send(f"<@{player[0]}> 遇到了**阿羅拉椰蛋樹**，並且爬著他到了**52**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 52, boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Koko":
            await channel.send(f"<@{player[0]}> 遇到了守護神**卡璞•鳴鳴**，他給了<@{player[0]}>一個電Z純晶！")
            await channel.send(f"受到守護神的加持，下一輪<@{player[0]}>的移動步數變為兩倍！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, boost = 1 "
                                "WHERE id = ?;", (des, player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Cosmog":
            await channel.send(f"<@{player[0]}> 遇到了小星雲**科斯莫古**！")
            new_des = random.randint(1, 60)
            await channel.send(f"小星雲將<@{player[0]}>瞬間移動傳送到了**{new_des}**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, boost = 0 "
                                "WHERE id = ?;", (new_des, player[0]))
            self.con.commit()
            await self.show_map(channel)
            if events[new_des] is not None:
                await self.gogo(channel, player, new_des)

        if events[des] == "Lele":
            await channel.send(f"<@{player[0]}> 遇到了守護神**卡璞•蝶蝶**，被戲耍了一番！")
            await channel.send(f"<@{player[0]}> 回到了**11**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 11,  boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Jigglypuff":
            await channel.send(f"<@{player[0]}> 遇到了在唱歌的**胖丁**，於是睡著了！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, sleep = 1, boost = 0 "
                                "WHERE id = ?;", (des, player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Popplio":
            await channel.send(f"<@{player[0]}> 遇到了**球球海獅**，他吹泡泡將<@{player[0]}>吹到了**62**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 62,  boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Snorlax":
            await channel.send(f"<@{player[0]}> 撞到了正在睡覺的**卡比獸**而暈倒了！")
            await channel.send(f"<@{player[0]}> 接下來兩回合都會在睡眠狀態！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, sleep = 2,  boost = 0 "
                                "WHERE id = ?;", (des, player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Turtonator":
            await channel.send(f"<@{player[0]}> 撞到了**爆焰龜獸**的刺而噴飛了！")
            await channel.send(f"<@{player[0]}> 回到了**30**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 30,  boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Pidgeot":
            await channel.send(f"<@{player[0]}> 遇到了在空中的**大比鳥**！")
            await channel.send(f"<@{player[0]}> 被吹飛了20格，回到了**63**！")
            if random.randint(0,100) > 50:
                await channel.send(f"<@{player[0]}> 被吹暈了！下回合無法行動。")
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET pos = 63 ,sleep = 1 , boost = 0 "
                                    "WHERE id = ?;", (player[0],))
                self.con.commit()
                await self.show_map(channel)
            else:
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET pos = 63 , boost = 0 "
                                    "WHERE id = ?;", (player[0],))
                self.con.commit()
                await self.show_map(channel)

        if events[des] == "Rayquaza":
            await channel.send(f"<@{player[0]}> 遇到了**烈空坐**，滑到了**66**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 66 , boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Bewear":
            await channel.send(f"# 熊！！！！！")
            await channel.send(file=discord.File("img/bewear.jpg"))
            await channel.send(f"<@{player[0]}> 被**穿著熊**抱回了原點！")
            await channel.send(f"好討厭的感覺啊！！！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 0 , boost = 0 "
                                "WHERE id = ?;", (player[0],))
            self.con.commit()
            await self.show_map(channel)

        if events[des] == "Dragonite":
            await channel.send(f"<@{player[0]}> 遇到**快龍**了！")
            rand = random.randint(1, 4)
            new_des = des - 10*rand
            await channel.send(f"<@{player[0]}> 被**快龍**揍飛了{10*rand}格，回到**{new_des}**！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ? , boost = 0 "
                                "WHERE id = ?;", (new_des, player[0]))
            self.con.commit()
            await self.show_map(channel)
            if events[new_des] is not None:
                await self.gogo(channel, player, new_des)

        if events[des] == "Pikachu":
            await channel.send(f"<@{player[0]}> 終於見到**皮卡丘**了！")
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = 90 , boost = 0 "
                                "WHERE id = ?;", (player[0],))

            await channel.send(file=discord.File("img/pikapika.jpg"))
            await self.show_map(channel)
            await channel.send("**遊戲結束！** /加入 來參加下一輪遊戲")
            with open('pika_playing.txt', 'w') as f:
                f.write("0")
            self.cursor.execute(f"DELETE FROM channel_{channel.id};")
            self.con.commit()

    async def create_table(self, channel: discord.TextChannel):
        try:
            self.cursor.execute(f'''
                            CREATE TABLE IF NOT EXISTS channel_{channel.id} (
                                id INTEGER PRIMARY KEY,
                                name TEXT NOT NULL,
                                pos INTEGER NOT NULL,
                                turn INTEGER NOT NULL,
                                sleep BOOLEAN DEFAULT 0,
                                boost BOOLEAN DEFAULT 0
                            )''')
            self.con.commit()
            # print(f"table channel_{channel.id} created/checked!")
        except Exception as e:
            print(e)

    @app_commands.command(name="db測試", description="test")
    async def dbtest(self, interaction: discord.Interaction):

        await interaction.response.send_message(interaction.channel.id, ephemeral=True)
        await self.create_table(interaction.channel)

    @app_commands.command(name="加入_拯救皮卡丘", description="加入遊戲")
    async def pika_join(self, interaction: discord.Interaction):

        await interaction.response.defer()
        channel = interaction.channel
        name = interaction.user.name
        id = interaction.user.id

        if not os.path.exists(f"img/user_avatar/{name}.png"):
            url = interaction.user.display_avatar.replace(format="png", size=128).url
            head = requests.get(url)
            nparr = np.frombuffer(head.content, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            print("decode")

            if img is not None:
                print(f"成功從 URL 下載並解碼圖像，原始尺寸：{img.shape[:2]}")
                height, width = img.shape[:2]
                radius = min(height, width) // 2
                mask = np.zeros((height, width), dtype=np.uint8)
                cv2.circle(mask, (width // 2, height // 2), radius, 255, -1)

                if img.shape[-1] == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2RGBA)
                elif img.shape[-1] == 1:  # 灰度圖
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGBA)

                img[:, :, 3] = cv2.bitwise_and(img[:, :, 3], mask)
                img = cv2.resize(img, (70, 70), interpolation=cv2.INTER_LANCZOS4)

                cv2.imwrite(f"img/user_avatar/{name}.png", img)

        await self.create_table(channel)

        count = self.people_count(channel)
        if count >= 6:
            await interaction.followup.send("已經滿人了唷！")

        self.cursor.execute(f"SELECT COUNT(*) FROM channel_{channel.id} WHERE id = ?", (id,))
        if self.cursor.fetchone()[0] > 0:
            await interaction.followup.send(f"{interaction.user.mention} 己經在遊戲裡囉！")
            return

        self.cursor.execute(f'''INSERT INTO channel_{channel.id} (id, name, pos, turn, sleep, boost) VALUES (?, ?, ?, ?, ?, ?);''', (id, name, 0, count+1, 0, 0))

        print(name, "加入拯救皮卡丘！")
        self.con.commit()
        await interaction.followup.send(f"{interaction.user.mention} 加入拯救皮卡丘！")
        await self.show_map(channel)

    @app_commands.command(name="開始_拯救皮卡丘", description="開始遊戲")
    async def pika_start(self, interaction: discord.Interaction):

        await interaction.response.defer()
        channel = interaction.channel

        await self.create_table(channel)

        if turn_now(channel) != 0:
            await interaction.followup.send("遊戲已經開始了唷！")
            return

        if self.people_count(channel) < 2:
            await interaction.followup.send("人數還不夠唷！")
            return

        self.cursor.execute(f"SELECT COUNT(*) FROM channel_{channel.id} WHERE id = ?", (interaction.user.id,))
        if self.cursor.fetchone()[0] == 0:
            await interaction.followup.send(f"{interaction.user.mention} 不在遊戲裡喔！")
            return

        turn_write(channel, "1")

        await interaction.followup.send("遊戲開始！")

        await self.show_map(channel)

        ls = [0, 0, 0, 0, 0, 0, 0]

        self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id};")
        players = self.cursor.fetchall()
        # print(players)
        for player in players:
            ls[ player[3] ] = player[0]  # ls[turn] = id
        # print(ls)
        msg = "順序： "
        for i in range(1,7):
            if ls[i] == 0:
                break
            msg += f"<@{ls[i]}>({i}), "

        await interaction.channel.send(msg)
        await interaction.channel.send(f"先從<@{ls[1]}>開始， /移動 來丟骰子前進！")

    @app_commands.command(name="移動", description="丟出一個骰子")
    async def pika_move(self, interaction: discord.Interaction):

        await interaction.response.defer()
        channel = interaction.channel

        await self.create_table(channel)

        if turn_now(channel) == 0:
            await interaction.followup.send("遊戲還沒開始唷！")
            return

        player_now = self.find_player_by_turn(channel, turn_now(channel))  # id, name, pos, turn, sleep, boost
        # print(player_now)

        if player_now[0] != interaction.user.id:
            await interaction.followup.send(f"現在輪到 <@{player_now[0]}> 唷！")
            return

        step = random.randint(1, 6)

        if player_now[5] == 1:
            des = player_now[2] + step*2
            if des > 90:
                des = 180 - des
            await interaction.followup.send(f"骰子的結果是**{step}**，由於受到卡璞•鳴鳴的加持，移動了**{step*2}步**，來到**{des}**！  ({player_now[2]}->{des})")

        else:
            des = player_now[2] + step
            if des > 90:
                des = 180 - des
            await interaction.followup.send(f"骰子的結果是**{step}**，來到**{des}**！  ({player_now[2]}->{des})")

        if events[des] is None:
            self.cursor.execute(f"UPDATE channel_{channel.id} "
                                "SET pos = ?, boost = 0 "
                                "WHERE id = ?;", (des, player_now[0]))
            self.con.commit()
            await self.show_map(channel)
        else:
            await self.gogo(channel, player_now, des)

        now = turn_now(channel)

        while True:
            now += 1
            if now > self.people_count(channel):
                now = 1

            player_now = self.find_player_by_turn(channel, now)
            if player_now[4] > 0:
                await interaction.channel.send(f"<@{player_now[0]}> 還在睡覺！")
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET sleep = ? "
                                    "WHERE id = ?;", (player_now[4]-1, player_now[0]))
                self.con.commit()
            else:
                await interaction.channel.send(f"輪到 <@{player_now[0]}> 了！")
                turn_write(channel, str(now))
                return

    @app_commands.command(name="查看_拯救皮卡丘", description="目前所有玩家狀態")
    async def pika_status(self, interaction: discord.Interaction):

        await interaction.response.defer()
        channel = interaction.channel

        await self.create_table(channel)
        now = turn_now(channel)

        if now == 0:
            await interaction.followup.send("遊戲還沒開始唷！")
            self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id};")
            players = self.cursor.fetchall()
            msg = "目前加入的玩家： "
            for player in players:
                msg += f"<@{player[0]}>, "
            if len(players) == 0:
                msg += "還沒有人加入！"
            await interaction.followup.send(msg)
            return

        await interaction.followup.send(f"目前輪到： <@{self.find_player_by_turn(channel, now)[0]}>({now}): **{self.find_player_by_turn(channel, now)[2]}**")

        msg = "其他玩家： "
        ls = [0, 0, 0, 0, 0, 0, 0]
        self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id};")
        players = self.cursor.fetchall()
        # print(players)
        for player in players:
            ls[player[3]] = (player[0], player[2])  # ls[turn] = (id, pos)

        for i in range(1,7):
            if ls[i] == 0:
                break
            if i == now:
                continue
            msg += f"<@{ls[i][0]}>({i}): **{ls[i][1]}**, "

        await interaction.channel.send(msg)
        await interaction.channel.send("目前地圖：")
        await self.show_map(channel)

    @app_commands.command(name="離開_拯救皮卡丘", description="離開遊戲")
    async def pika_leave(self, interaction: discord.Interaction):

        await interaction.response.defer()
        channel = interaction.channel
        id = interaction.user.id

        await self.create_table(channel)

        self.cursor.execute(f"SELECT id, name, pos, turn, sleep, boost FROM channel_{channel.id} WHERE id = ?", (id,))
        player = self.cursor.fetchone()
        # print(player)
        if player is None:
            await interaction.followup.send(f"{interaction.user.mention} 不在遊戲裡唷！")
            return

        turn = player[3]
        count = self.people_count(channel)

        try:
            for i in range(turn, self.people_count(channel)+1):
                p = self.find_player_by_turn(channel, i)
                self.cursor.execute(f"UPDATE channel_{channel.id} "
                                    "SET turn = ? "
                                    "WHERE turn = ?;", (i-1, i))
                self.con.commit()

                self.cursor.execute(f"DELETE FROM channel_{channel.id} WHERE id = ?;", (player[0],))
                self.con.commit()

                await interaction.followup.send(f"<@{player[0]}> 離開遊戲！")

                now = turn_now(channel)

                if now >= turn:
                    turn_write(channel, str(now-1))

        except Exception as e:
            await interaction.followup.send(e)
            return

        if self.people_count(channel) == 0 and turn_now(channel) != 0:
            await channel.send("所有人都離開遊戲了，遊戲結束！")
            turn_write(channel, "0")

    @app_commands.command(name="拯救皮卡丘_遊戲介紹", description="玩法說明")
    async def pika_intro(self, interaction: discord.Interaction):

        await interaction.response.defer()
        await interaction.followup.send("## 拯救皮卡丘！\n和大家一起比賽，在90格的寶可夢地圖上朝皮卡丘的方向前進！\n")
        await interaction.channel.send("### 相關指令：\n/加入＿拯救皮卡丘： 加入遊戲，最多6人一起玩\n/開始＿拯救皮卡丘： 開始遊戲，至少需要2人\n/查看＿拯救皮卡丘： 查看目前遊戲狀態")
        await interaction.channel.send("/移動： 在你的回合，丟一顆骰子，向前移動！超過90格會往回走唷！")


'''
    @app_commands.command(name="頭像", description="取得頭像")
    async def head(self, interaction: discord.Interaction):

        await interaction.response.defer()
        name = interaction.user.name

        img = None
        # try:
        #     img = cv2.imread(f"img/user_avatar/{name}.png")
        #     print("read")
        # except:
        url = interaction.user.avatar.url
        head = requests.get(url)
        nparr = np.frombuffer(head.content, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        print("decode")

        if img is not None:
            print(f"成功從 URL 下載並解碼圖像，原始尺寸：{img.shape[:2]}")
            height, width = img.shape[:2]
            radius = min(height, width)//2
            mask = np.zeros((height, width), dtype=np.uint8)
            cv2.circle(mask, (width//2, height//2), radius, 255, -1)

            if img.shape[-1] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2RGBA)
            elif img.shape[-1] == 1:  # 灰度圖
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGBA)

            img[:, :, 3] = cv2.bitwise_and(img[:, :, 3], mask)

            img = cv2.resize(img, (70, 70), interpolation=cv2.INTER_LANCZOS4)

            # # 使用 OpenCV 調整圖像大小
            # resized_img = cv2.resize(img, target_size)
            # print(f"調整後的圖像尺寸：{resized_img.shape[:2]}")

            # 使用 OpenCV 儲存調整大小後的圖像
            cv2.imwrite(f"img/user_avatar/{name}.png", img)

        else:
            print("無法解碼??")

        await interaction.followup.send(file=discord.File(f"img/user_avatar/{name}.png"))
'''


async def setup(bot: commands.Bot):
    await bot.add_cog(Pikachugame(bot))