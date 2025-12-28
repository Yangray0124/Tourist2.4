import base64

import discord
import time
from discord.ext import commands
from discord.ext import tasks
import requests
import random
from discord import app_commands
from discord.app_commands import Choice
from bs4 import BeautifulSoup
from typing import Optional
from keys import gemini_api_key
import shutil

hd = {'Content-Type': 'application/json'}
js = {
    "contents": [
        {
            "parts": [
                {
                    "text": "pikachu"
                }
            ],
            "role": "user"
        }
    ],
    "safetySettings": [
        {
            "category": "HARM_CATEGORY_HARASSMENT",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_HATE_SPEECH",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
            "threshold": "BLOCK_NONE"
        }
    ],
    "generationConfig": {
        "temperature": "0.8",
        "topP": "0.95",
        "topK": "50",
        "candidateCount": "1",
        "maxOutputTokens": "4096",
    }
}
js_image = {
    "contents": [
        {
            "parts": [
                {
                    "inline_data": {
                        "mime_type": "image/jpg",
                        "data": ""
                    }
                },
                {
                    "text": "pikachu"
                }
            ],
            "role": "user"
        }
    ],
    "safetySettings": [
        {
            "category": "HARM_CATEGORY_HARASSMENT",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_HATE_SPEECH",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "threshold": "BLOCK_NONE"
        },
        {
            "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
            "threshold": "BLOCK_NONE"
        }
    ],
    "generationConfig": {
        "temperature": "0.8",
        "topP": "0.95",
        "topK": "50",
        "candidateCount": "1",
        "maxOutputTokens": "8192",
    }
}
prompt = "請用繁體中文回答！\n\n"
cf_focus_CD = 20

gemini_model = "v1/models/gemini-2.5-flash-lite"
gemini_image_model = "v1/models/gemini-2.5-flash-lite"


def check(msg):
    for i in range(len(msg)):
        now = msg[i]
        # print(now, '\u4e00', '\u9fa5',  '\u4e00' <= now <= '\u9fa5')
        if '\u4e00' <= now <= '\u9fa5':
            return False
    return True


def get_hour_and_min(sec):
    h = sec // 3600
    sec -= h * 3600
    m = sec // 60
    if m == 0:
        return f"{h} hr"
    else:
        return f"{h} hr {m} min"


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return encoded_string


cf_queue = []  # {function, interaction, params{} }
cf_focus_list = []  # {ID, remain, channel }
last_submission_id = {}


class Chat(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cf_clock.start()
        self.cf_add_focus.start()

    # @commands.command()
    # async def Hello(self, ctx: commands.Context):
    #     await ctx.send("你好")

    @app_commands.command(name="說你好", description="說你好")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("你好")

    @app_commands.command(name="版本", description="Tourist2.3")
    async def version(self, interaction: discord.Interaction):
        await interaction.response.send_message(">>> 版本： **Tourist2.4**\n"
                                                "更新日期： 2025/12/29\n"
                                                "才藝： 智能聊天、cf功能、唱歌\n"
                                                "贊助商： 郭老師贊助機器！\n"
                                                "OpenSource： https://github.com/Yangray0124/Tourist2.4.git")

    @tasks.loop(seconds=3)
    async def cf_clock(self):
        # print("clock")
        if len(cf_queue) == 0:
            return
        function, interaction, params = cf_queue[0]["function"], cf_queue[0]["interaction"], cf_queue[0]["params"]
        cf_queue.pop(0)
        await function(interaction, params)
        return

    @tasks.loop(seconds=cf_focus_CD)
    async def cf_add_focus(self):
        del_tmp = []
        for p in cf_focus_list:
            # print(p["ID"])
            cf_queue.append({"function": self.cf_focus_update,
                             "interaction": p["channel"],
                             "params": {"ID": p["ID"]} })
            p["remain"] -= cf_focus_CD

            if p["remain"] <= 0:
                # print(f"{p['ID']} deleted")
                channel = p["channel"]
                await channel.send(f"**{p['ID']}** 關注結束")
                del_tmp.append(p)
        for p in del_tmp:
            cf_focus_list.remove(p)

    async def test(self, interaction: discord.Interaction, params: dict):
        await interaction.channel.send("test")
        return

    async def cf_rank(self, interaction: discord.Interaction, params: dict):
        print("cf_rank")
        msg = "以下是目前排行榜：\n"
        rank = requests.get("https://codeforces.com/api/user.ratedList?activeOnly=true&includeRetired=false")
        if rank.status_code == 200:
            res = rank.json()["result"]
            msg += "```\n"
            for i in range(10):
                msg += "{0:>2}. {1:<17} {2}\n".format(i + 1, res[i]["handle"], res[i]["rating"])
            msg += "```"
            await interaction.followup.send(msg)
        else:
            await interaction.followup.send("CF好像壞掉了... 請燒等")

    async def cf_contest(self, interaction: discord.Interaction, params: dict):
        print("cf_contest")
        contests = requests.get("https://codeforces.com/api/contest.list?gym=false")
        if contests.status_code == 200:
            res = contests.json()["result"]
            l = []
            for i in range(10, -1, -1):
                if res[i]["phase"] == "BEFORE":
                    l.append(res[i])
            cnt = min(3, len(l))
            msg = ""
            for i in range(cnt):
                NAME = l[i]["name"]
                TIME = l[i]["startTimeSeconds"] + 3600 * 8
                ACTIME = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(TIME))
                HMS = get_hour_and_min(l[i]["durationSeconds"])
                msg += "{0:<60}  | {1:<23} // {2}".format(NAME, ACTIME, HMS)
                msg += '\n'
            await interaction.followup.send(
                f"- 以下是最近的{cnt}場比賽：\n" + "```\n{0}```".format(msg) + "[點我報名]({0})".format(
                    "https://codeforces.com/contests"))
        else:
            await interaction.followup.send("CF好像壞掉了... 請燒等")

    async def cf_user_score(self, interaction: discord.Interaction, params: dict):
        print("cf_user_score")
        ID = params["ID"]
        info = requests.get(
            f"https://codeforces.com/api/user.info?handles={ID}&checkHistoricHandles=false")
        if info.status_code != 200:
            await interaction.followup.send("查不到捏")
            return
        res = info.json()["result"]
        if len(res) == 0:
            await interaction.followup.send("他還沒參加過比賽喔")
            return
        if ID == "tourist":
            await interaction.followup.send(f'## {ID}\n- 目前分數：{res[0]["rating"]}\n- 最高分數：{res[-1]["maxRating"]}:v:')
        else:
            await interaction.followup.send(f'## {ID}\n- 目前分數：{res[0]["rating"]}\n- 最高分數：{res[-1]["maxRating"]}')

    async def cf_user_contest(self, interaction: discord.Interaction, params: dict):
        print("cf_user_contest")
        ID = params["ID"]
        contest_kw = params["contest_kw"]
        info = requests.get(
            f"https://codeforces.com/api/user.info?handles={ID}&checkHistoricHandles=false")
        if info.status_code != 200:
            await interaction.followup.send("查不到捏")
            return
        contests = requests.get("https://codeforces.com/api/contest.list?gym=false")
        if contests.status_code != 200:
            await interaction.followup.send("CF好像壞掉了... 請燒等")
            return

        res = contests.json()["result"]
        l = []
        for i in res:
            if contest_kw in i["name"] and i["phase"] == "FINISHED":
                l.append(i)
        if len(l) == 0:
            await interaction.followup.send("找不到符合的比賽")
            return
        if len(l) > 3:
            await interaction.followup.send("太多筆了懶得看 包欠")
            return

        ok = False
        for i in l:
            contestid = i["id"]
            endtime = i["startTimeSeconds"] + i["durationSeconds"]
            subs = requests.get(
                f"https://codeforces.com/api/contest.status?contestId={contestid}&handle={ID}&from=1&count=500")
            res = subs.json()["result"]
            if len(res) == 0:
                continue
            ok = True
            msg = f"## {ID}\n### {i['name']}\n"
            in_game = set()
            after_game = set()
            # ng = {}
            for j in res:
                pid = j["problem"]["index"]
                in_time = (j["creationTimeSeconds"] < endtime)
                if j["verdict"] == "OK":
                    if in_time:
                        in_game.add(pid)
                    else:
                        after_game.add(pid)
            if len(in_game) > 0:
                in_game = list(in_game)
                in_game.sort()
                msg += "- 賽中解出 : "
                for j in range(len(in_game)):
                    if j > 0:
                        msg += ", "
                    msg += "**" + in_game[j] + "**"
            if len(after_game) > 0:
                after_game = list(after_game)
                after_game.sort()
                msg += "- 賽後解出 : "
                for j in range(len(after_game)):
                    if j > 0:
                        msg += ", "
                    msg += "**" + after_game[j] + "**"

            rating = requests.get(f"https://codeforces.com/api/user.rating?handle={ID}")
            res = rating.json()["result"]
            for j in res:
                if j["contestId"] == contestid:
                    await interaction.followup.send(msg + '\n' +
                                                    f"> 排名 ： {j['rank']}　　　分數 ： ***{j['oldRating']} --> {j['newRating']}***")
                    break

    async def cf_focus_setup(self, interaction: discord.Interaction, params: dict):
        print("cf_focus_setup")
        ID = params["ID"]
        sec = params["sec"]
        info = requests.get(f"https://codeforces.com/api/user.status?handle={ID}&from=1&count=10")
        if info.status_code != 200:
            await interaction.followup.send("查不到這個人捏")
            return
        info = info.json()
        for p in cf_focus_list:
            if p["ID"] == ID:
                p["remain"] = sec
                await interaction.followup.send(f"繼續關注 **{ID}** 成功，持續{sec//3600}小時")
                return
        cf_focus_list.append({"ID": ID, "remain": sec, "channel": interaction.channel})
        last_submission_id[ID] = info["result"][0]["id"]
        await interaction.followup.send(f"關注 **{ID}** 成功，持續{sec//3600}小時")

    async def cf_focus_update(self, channel: discord.TextChannel, params: dict):
        ID = params["ID"]

        try:
            # 加上 timeout 避免卡死，並檢查狀態碼
            response = requests.get(f"https://codeforces.com/api/user.status?handle={ID}&from=1&count=10", timeout=5)

            if response.status_code != 200:
                print(f"CF API Error: {response.status_code}")
                return  # 這次抓失敗，直接跳過，等下次迴圈再試

            info = response.json()
        except Exception as e:
            print(f"Fetch failed for {ID}: {e}")
            return  # 發生任何錯誤(連線失敗、解析失敗)都跳過

        # info = requests.get(f"https://codeforces.com/api/user.status?handle={ID}&from=1&count=10").json()
        l = []  # {problem_id, problem_idx, problem_name, problem_verdict}
        for i in range(10):
            if info["result"][i]["id"] == last_submission_id[ID]:
                break
            l.append({"problem_id": info["result"][i]["id"],
                      "problem_idx": info["result"][i]["problem"]["index"],
                      "problem_name": info["result"][i]["problem"]["name"],
                      "problem_verdict": info["result"][i]["verdict"]})
        for i in range(len(l)):
            if l[i]["problem_verdict"] != "TESTING":
                last_submission_id[ID] = l[i]["problem_id"]
                break
        for i in range(len(l)-1, -1, -1):
            verdict = l[i]["problem_verdict"]
            if verdict == "TESTING":
                continue
            if verdict == "OK":
                verdict = "Accepted"
            await channel.send(f" **{ID}** 提交了 **{l[i]['problem_idx']} - {l[i]['problem_name']}** ，結果是 ***{verdict.title()}*** ！")

    async def cf_get_random_problem(self, interaction: discord.Interaction, params: dict):
        print("cf_get_random_problem")
        l, r = params["L"], params["R"]
        # print(l, r)
        info = requests.get("https://codeforces.com/api/problemset.problems")
        if info.status_code != 200:
            await interaction.followup.send("cf好像出了問題！")
            return
        problems = info.json()["result"]["problems"]
        ls = []
        for p in problems:
            if "rating" in p and l <= p["rating"] <= r:
                ls.append({"contest_id": p["contestId"], "idx": p["index"], "name": p["name"]})

        if len(ls) == 0:
            await interaction.followup.send("找不到符合難度的題目！")
            return
        rand = random.randint(0, len(ls)-1)
        await interaction.followup.send(f"好的， [ **{ls[rand]['name']}** ](https://codeforces.com/contest/{ls[rand]['contest_id']}/problem/{ls[rand]['idx']})")

    async def cut_and_reply(self, message:discord.Message, res:str):
        msc = message.channel
        replies = []
        tmpL = 0
        for i in range(len(res)):
            if res[i] == '\n' and i - tmpL >= 1500:
                replies.append(res[tmpL:i + 1])
                tmpL = i + 1
        if tmpL < len(res) - 1:
            replies.append(res[tmpL:len(res)])
        await message.reply(replies[0])
        for i in range(1, len(replies)):
            await msc.send(replies[i])
        print("cut:", f"len={len(replies)}")

    @app_commands.command(name="cf", description="查詢CodeForces的...")
    @app_commands.describe(選擇="選擇功能")
    @app_commands.choices(
        選擇=[
            Choice(name="排行榜", value="rank"),
            Choice(name="最近的比賽", value="contests"),
        ]
    )
    async def cf(self, interaction: discord.Interaction, 選擇: Choice[str]):
        # print("append")
        await interaction.response.defer()
        if 選擇.name == "排行榜":
            cf_queue.append({"function": self.cf_rank, "interaction": interaction, "params": {}})
        elif 選擇.name == "最近的比賽":
            cf_queue.append({"function": self.cf_contest, "interaction": interaction, "params": {}})

    @app_commands.command(name="cf查分", description="查詢CodeForces玩家的分數、比賽表現")
    @app_commands.describe(id="玩家名稱", 比賽關鍵字="(可選)查詢比賽表現") # id不可以大寫!!!!  :(
    async def cf_user(self, interaction: discord.Interaction, id: str, 比賽關鍵字: Optional[str] = None):
        # print("append")
        await interaction.response.defer()
        if 比賽關鍵字 is None:
            cf_queue.append({"function": self.cf_user_score, "interaction": interaction, "params": {"ID": id}})
        else:
            cf_queue.append({"function": self.cf_user_contest, "interaction": interaction, "params": {"ID": id, "contest_kw": 比賽關鍵字}})

    @app_commands.command(name="關注", description="追蹤玩家CodeForces表現")
    @app_commands.describe(id="玩家名稱", 時間="關注時間")  # id不可以大寫!!!!  :(
    @app_commands.choices(
        時間=[
            Choice(name="2min(測試)", value=120),
            Choice(name="1hr", value=3600),
            Choice(name="2hr", value=3600*2),
            Choice(name="3hr", value=3600*3),
        ]
    )
    async def cf_focus(self, interaction: discord.Interaction, id: str, 時間: Choice[int]):
        # print("append")
        await interaction.response.defer()
        cf_queue.append({"function": self.cf_focus_setup, "interaction": interaction, "params": {"ID": id.lower(), "sec": 時間.value}})

    @app_commands.command(name="關注列表", description="目前追蹤的列表")
    async def cf_focus_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if len(cf_focus_list) == 0:
            await interaction.followup.send("目前沒有關注任何玩家")
            return
        msg = "目前關注： "
        for i in range(len(cf_focus_list)):
            if i > 0:
                msg += ", "
            msg += f"**{cf_focus_list[i]['ID']}**"
        await interaction.followup.send(msg)

    @app_commands.command(name="隨機一題", description="隨機一題CodeForces題目！")
    @app_commands.describe(l="最低難度", r="最高難度")
    async def cf_random_problem(self, interaction: discord.Interaction, l: int, r:int):
        if l > r:
            l, r = r, l
        await interaction.response.defer()
        cf_queue.append({"function": self.cf_get_random_problem, "interaction": interaction, "params": {"L": l, "R": r}})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        msc = message.channel

        # if "笑" in message.content:
        #     await message.add_reaction('\N{smiling face with open mouth and tightly-closed eyes}')
        #     await message.add_reaction('\N{clapping hands sign}')

        if "晚安" in message.content:
            await message.add_reaction('\N{sleeping symbol}')
            await message.add_reaction('\N{last quarter moon with face}')

        if ("趴" in message.content or "啪" in message.content) and "沒了" in message.content:
            print("啪 沒了")
            await msc.send(file=discord.File("img/pa.jpg"))

        if "聽聽看" in message.content and ("說" in message.content or "講" in message.content) and (
                "什麼" in message.content or "甚麼" in message.content):
            print("聽聽看")
            await msc.send(file=discord.File("img/chill.jpg"))

        if "櫻桃" in message.content:
            print("櫻桃")
            await msc.send(file=discord.File("img/owl.jpg"))

        if "真的沒差" in message.content:
            print("真的沒差")
            await msc.send(file=discord.File("img/fork.jpg"))

        if f"<@{self.bot.application_id}>" in message.content:
            if message.content.strip() == f"<@{self.bot.application_id}>" and len(message.attachments) == 0:
                await message.reply("怎樣")

            elif "是誰" in message.content or "你誰" in message.content:
                await msc.send("我是tourist")
                await message.add_reaction('\N{waVing hand sign}')

            elif "打招呼" in message.content:
                await msc.send("早安!!")

            elif '去' in message.content:
                game = ""
                m = message.content.find('去')
                if "去玩" in message.content:
                    game = message.content[m + 2:]
                else:
                    game = message.content[m + 1:]
                if random.random() > 0.25:
                    if random.random() > 0.666:
                        await message.reply("好的")
                    elif random.random() > 0.5:
                        await message.reply("好啦")
                    else:
                        await message.reply(":ok:")
                    pen = open("status.txt", 'w')
                    pen.write(game)
                    await self.bot.change_presence(status=discord.Status.idle, activity=discord.Game(game))
                    pen.close()
                else:
                    await message.reply("不要")

            else:
                if len(message.attachments)>0:
                    url = message.attachments[0].url
                    image = requests.get(url, stream=True)
                    try:
                        with open("pikachu.jpg", "wb") as out:
                            shutil.copyfileobj(image.raw, out)
                        print("pikachu.jpg saved")
                        L = message.content.find(">") + 2
                        msg = message.content[L:]
                        js_image["contents"][0]["parts"][1]["text"] = prompt + msg
                        print("text(with image): ", js_image["contents"][0]["parts"][1]["text"])
                        js_image["contents"][0]["parts"][0]["inline_data"]["data"] = encode_image("pikachu.jpg")
                        google = requests.post(
                            f"https://generativelanguage.googleapis.com/{gemini_image_model}:generateContent?key={gemini_api_key}",
                            headers=hd, json=js_image)

                        if google.status_code == 200:
                            await self.cut_and_reply(message, google.json()["candidates"][0]["content"]["parts"][0]["text"].replace("Gemini", "Tourist"))

                        else:
                            await message.reply("Tourist壞掉了，請檢查模型的版本")
                            print("Tourist壞掉了！")
                            print("Error: ", google.json()["error"]["message"])
                            print(f"https://generativelanguage.googleapis.com/v1/models?key={gemini_api_key}")
                            print(f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_api_key}")
                        return
                    except Exception as e:
                        print("failed:", e)

                if random.random() > 0.99:
                    await message.reply("聽不懂辣")
                else:
                    L = message.content.find(">") + 2
                    msg = message.content[L:]
                    
                    js["contents"][0]["parts"][0]["text"] = prompt + msg

                    print("text: ", js["contents"][0]["parts"][0]["text"])
                    google = requests.post(
                        f"https://generativelanguage.googleapis.com/{gemini_model}:generateContent?key={gemini_api_key}",
                        headers=hd, json=js)
                    if google.status_code == 200:
                        await self.cut_and_reply(message, google.json()["candidates"][0]["content"]["parts"][0]["text"].replace("Gemini", "Tourist"))

                    else:
                        await message.reply("Tourist壞掉了，請檢查模型的版本")
                        print("Tourist壞掉了！")
                        print("Error: ", google.json()["error"]["message"])
                        print(f"https://generativelanguage.googleapis.com/v1/models?key={gemini_api_key}")
                        print(f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_api_key}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Chat(bot))
