import discord
from discord.ext import commands
from discord.ext import tasks
from discord import app_commands
from discord.app_commands import Choice
import requests
import urllib.parse
from keys import brawl_stars_api_key

# 儲存關注列表： {"tag": 玩家標籤, "remain": 剩餘秒數, "channel": 發送訊息的頻道}
bs_focus_list = []
# 紀錄每個玩家最新的一場對戰時間 (battleTime)，避免重複發送
last_battle_time = {}

bs_focus_CD = 30  # 每 30 秒檢查一次

# API 請求的 Headers，帶入你的 API Token
BS_HEADERS = {
    'Authorization': f'Bearer {brawl_stars_api_key}',
    'Accept': 'application/json'
}

class BrawlStar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bs_focus_update.start()

    def cog_unload(self):
        self.bs_focus_update.cancel()

    def format_tag(self, tag: str) -> str:
        """格式化玩家標籤，確保開頭沒有 # 且為大寫，因為 API 網址需要轉碼"""
        return tag.replace("#", "").upper()

    @tasks.loop(seconds=bs_focus_CD)
    async def bs_focus_update(self):
        del_tmp = []
        for p in bs_focus_list:
            tag = p["tag"]
            channel = p["channel"]
            p["remain"] -= bs_focus_CD

            if p["remain"] <= 0:
                await channel.send(f"**#{tag}** 的荒野亂鬥關注時間結束！")
                del_tmp.append(p)
                continue

            # 呼叫 Battlelog API (%23 是 # 的 URL 編碼)
            try:
                url = f"https://api.brawlstars.com/v1/players/%23{tag}/battlelog"
                req = requests.get(url, headers=BS_HEADERS, timeout=10)
                
                if req.status_code != 200:
                    print(f"BS API Error for {tag}: {req.status_code}")
                    continue

                res = req.json()
                if "items" not in res or len(res["items"]) == 0:
                    continue

                # API 回傳的 items 是由新到舊排列
                new_battles = []
                for battle_item in res["items"]:
                    b_time = battle_item.get("battleTime")
                    # 如果時間大於上一次紀錄的時間，代表是新的對戰 (字串可以直接比大小)
                    if last_battle_time.get(tag) and b_time > last_battle_time[tag]:
                        new_battles.append(battle_item)
                    else:
                        break # 遇到舊的紀錄就可以停止檢查了
                
                # 更新最新對戰時間
                if len(res["items"]) > 0:
                    last_battle_time[tag] = res["items"][0].get("battleTime")

                # 反轉陣列，讓發送訊息時從舊的到最新的發出
                new_battles.reverse()

                for b in new_battles:
                    battle_info = b.get("battle", {})
                    mode = battle_info.get("mode", "unknown").capitalize()
                    result = battle_info.get("result", "draw") # victory, defeat, draw
                    trophy_change = battle_info.get("trophyChange", 0)
                    
                    # 翻譯結果成中文與表情符號
                    if result == "victory":
                        result_str = "🏆 **獲勝**"
                        trophy_str = f"+{trophy_change}" if trophy_change else "+0"
                    elif result == "defeat":
                        result_str = "💀 **戰敗**"
                        trophy_str = f"{trophy_change}" if trophy_change else "-0"
                    else:
                        result_str = "🤝 **平手**"
                        trophy_str = "0"

                    msg = f"🎮 **#{tag}** 剛完成了一場對戰！\n"
                    msg += f"> 模式：**{mode}**\n"
                    msg += f"> 結果：{result_str} ({trophy_str} 獎盃)"

                    await channel.send(msg)

            except Exception as e:
                print(f"處理荒野亂鬥 {tag} 戰績時發生錯誤: {e}")

        # 移除到期的關注
        for p in del_tmp:
            if p in bs_focus_list:
                bs_focus_list.remove(p)


    @app_commands.command(name="bs_關注", description="追蹤荒野亂鬥玩家的即時戰績")
    @app_commands.describe(tag="玩家標籤(不需包含#)", 時間="關注時間")
    @app_commands.choices(
        時間=[
            Choice(name="10分鐘(測試)", value=600),
            Choice(name="1hr", value=3600),
            Choice(name="2hr", value=3600*2),
        ]
    )
    async def bs_focus(self, interaction: discord.Interaction, tag: str, 時間: Choice[int]):
        await interaction.response.defer()
        clean_tag = self.format_tag(tag)
        sec = 時間.value

        try:
            # 先打一次 Profile API 確認玩家是否存在
            profile_url = f"https://api.brawlstars.com/v1/players/%23{clean_tag}"
            req = requests.get(profile_url, headers=BS_HEADERS, timeout=5)
            
            if req.status_code == 404:
                await interaction.followup.send(f"找不到標籤為 **#{clean_tag}** 的玩家，請確認輸入是否正確！")
                return
            elif req.status_code == 403:
                print(f"BS API Permission Denied for tag {clean_tag}: {req.status_code}")
                await interaction.followup.send("API Token 權限拒絕！請檢查 Token 是否過期或 IP 未在白名單內。")
                return
            elif req.status_code != 200:
                await interaction.followup.send(f"BS API 連線異常 (代碼: {req.status_code})，請稍後再試！")
                return

            player_name = req.json().get("name", "Unknown")

            # 抓取第一筆對戰時間作為初始值
            battle_url = f"https://api.brawlstars.com/v1/players/%23{clean_tag}/battlelog"
            b_req = requests.get(battle_url, headers=BS_HEADERS, timeout=5)
            if b_req.status_code == 200:
                b_res = b_req.json()
                if "items" in b_res and len(b_res["items"]) > 0:
                    last_battle_time[clean_tag] = b_res["items"][0].get("battleTime")
                else:
                    last_battle_time[clean_tag] = "20000101T000000.000Z" # 假時間
            
        except Exception as e:
            await interaction.followup.send("連線發生錯誤，請稍後再試！")
            print(f"BS Setup Error: {e}")
            return

        # 檢查是否已經在關注清單中
        for p in bs_focus_list:
            if p["tag"] == clean_tag:
                p["remain"] = sec
                p["channel"] = interaction.channel
                await interaction.followup.send(f"已更新關注！繼續追蹤 **{player_name} (#{clean_tag})** 的戰績，持續 {sec//60} 分鐘。")
                return

        # 加入清單
        bs_focus_list.append({
            "tag": clean_tag, 
            "remain": sec, 
            "channel": interaction.channel
        })
        await interaction.followup.send(f"✅ 成功關注 **{player_name} (#{clean_tag})**！接下來的 {sec//60} 分鐘內若有對戰將即時通報。")


    @app_commands.command(name="bs_關注列表", description="查看目前追蹤荒野亂鬥的玩家")
    async def bs_focus_list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if len(bs_focus_list) == 0:
            await interaction.followup.send("目前沒有關注任何荒野亂鬥玩家。")
            return
        
        msg = "目前正在關注的荒野亂鬥玩家：\n"
        for p in bs_focus_list:
            mins = p['remain'] // 60
            msg += f"- **#{p['tag']}** (剩餘 {mins} 分鐘)\n"
            
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(BrawlStar(bot))