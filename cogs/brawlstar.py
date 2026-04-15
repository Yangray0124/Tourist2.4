import discord
from discord.ext import commands
from discord.ext import tasks
from discord import app_commands
from discord.app_commands import Choice
import requests
from keys import bs_api_key

# 儲存關注列表： {"tag": 玩家標籤, "name": 玩家名稱, "remain": 剩餘秒數, "channel": 發送訊息的頻道}
bs_focus_list = []
# 紀錄每個玩家最新的一場對戰時間 (battleTime)，避免重複發送
last_battle_time = {}

bs_focus_CD = 30  # 每 30 秒檢查一次

# API 請求的 Headers
BS_HEADERS = {
    'Authorization': f'Bearer {bs_api_key}',
    'Accept': 'application/json'
}

class BattleDetailView(discord.ui.View):
    """用於顯示詳細戰報的互動按鈕"""
    def __init__(self, battle_data: dict, player_tag: str):
        super().__init__(timeout=None) # timeout=None 代表按鈕不會失效
        self.battle_data = battle_data
        self.player_tag = player_tag

    @discord.ui.button(label="查看詳細戰報", style=discord.ButtonStyle.primary, emoji="📊")
    async def show_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.create_embed()
        # ephemeral=True 讓這則詳細資訊只有點擊的人看得到，不會洗版
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def create_embed(self) -> discord.Embed:
        battle = self.battle_data.get("battle", {})
        event = self.battle_data.get("event", {})
        
        mode = event.get("mode", battle.get("mode", "Unknown")).capitalize()
        map_name = event.get("map", "未知地圖")
        
        embed = discord.Embed(
            title=f"📊 詳細戰報：{mode} - {map_name}", 
            color=discord.Color.blue()
        )
        
        # 抓取 MVP (Star Player)
        sp = battle.get("starPlayer")
        if sp:
            sp_name = sp.get("name", "Unknown")
            sp_brawler = sp.get("brawler", {}).get("name", "Unknown")
            embed.add_field(name="🌟 MVP (Star Player)", value=f"**{sp_name}** ({sp_brawler})", inline=False)
            
        # 處理 3v3 模式的隊伍資訊
        if "teams" in battle:
            teams = battle["teams"]
            # 尋找被追蹤玩家所在的隊伍索引
            player_team_idx = 0
            for i, team in enumerate(teams):
                for p in team:
                    if p.get("tag", "").replace("#", "") == self.player_tag:
                        player_team_idx = i
                        break
            
            for i, team in enumerate(teams):
                team_name = "🔵 友方隊伍" if i == player_team_idx else "🔴 敵方隊伍"
                team_info = ""
                for p in team:
                    p_name = p.get("name", "Unknown")
                    b_name = p.get("brawler", {}).get("name", "Unknown")
                    b_trophies = p.get("brawler", {}).get("trophies", "?")
                    team_info += f"• **{p_name}** - {b_name} (🏆 {b_trophies})\n"
                
                embed.add_field(name=team_name, value=team_info if team_info else "無資料", inline=False)
                
        # 處理荒野生死鬥 (Showdown) 等單人/雙人模式的玩家列表
        elif "players" in battle:
            players = battle["players"]
            info = ""
            for p in players:
                p_name = p.get("name", "Unknown")
                b_name = p.get("brawler", {}).get("name", "Unknown")
                b_trophies = p.get("brawler", {}).get("trophies", "?")
                info += f"• **{p_name}** - {b_name} (🏆 {b_trophies})\n"
            
            # 若人數過多可能超出 Embed 限制，這裡取前 10 名
            if len(info) > 1024:
                info = info[:1000] + "...\n(顯示部分玩家)"
            embed.add_field(name="👥 玩家列表", value=info if info else "無資料", inline=False)
            
        return embed


class BrawlStar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bs_focus_update.start()

    def cog_unload(self):
        self.bs_focus_update.cancel()

    def format_tag(self, tag: str) -> str:
        return tag.replace("#", "").upper()

    @tasks.loop(seconds=bs_focus_CD)
    async def bs_focus_update(self):
        del_tmp = []
        for p in bs_focus_list:
            tag = p["tag"]
            name = p["name"]
            channel = p["channel"]
            p["remain"] -= bs_focus_CD

            if p["remain"] <= 0:
                await channel.send(f"**{name}** (#{tag}) 的荒野亂鬥關注時間結束！")
                del_tmp.append(p)
                continue

            try:
                url = f"https://api.brawlstars.com/v1/players/%23{tag}/battlelog"
                req = requests.get(url, headers=BS_HEADERS, timeout=10)
                
                if req.status_code != 200:
                    continue

                res = req.json()
                if "items" not in res or len(res["items"]) == 0:
                    continue

                new_battles = []
                for battle_item in res["items"]:
                    b_time = battle_item.get("battleTime")
                    if last_battle_time.get(tag) and b_time > last_battle_time[tag]:
                        new_battles.append(battle_item)
                    else:
                        break 
                
                if len(res["items"]) > 0:
                    last_battle_time[tag] = res["items"][0].get("battleTime")

                new_battles.reverse()

                for b in new_battles:
                    battle_info = b.get("battle", {})
                    event_info = b.get("event", {})
                    
                    mode = event_info.get("mode", battle_info.get("mode", "unknown")).capitalize()
                    map_name = event_info.get("map", "未知地圖")
                    
                    # 判斷勝負與名次
                    if "rank" in battle_info:  # 生死鬥模式
                        rank = battle_info["rank"]
                        result_str = f"🏅 **第 {rank} 名**"
                        trophy_change = battle_info.get("trophyChange", 0)
                        trophy_str = f"{'+' if trophy_change > 0 else ''}{trophy_change}"
                    else:  # 3v3 模式
                        result = battle_info.get("result", "draw")
                        trophy_change = battle_info.get("trophyChange", 0)
                        if result == "victory":
                            result_str = "🏆 **獲勝**"
                            trophy_str = f"+{trophy_change}" if trophy_change else "+0"
                        elif result == "defeat":
                            result_str = "💀 **戰敗**"
                            trophy_str = f"{trophy_change}" if trophy_change else "-0"
                        else:
                            result_str = "🤝 **平手**"
                            trophy_str = "0"

                    # 全新簡潔的播報訊息
                    msg = f"🎮 **{name}** 在 **{mode}** 的 **{map_name}** 獲得了 {result_str}！ ({trophy_str} 獎盃)"

                    # 綁定按鈕 View
                    view = BattleDetailView(b, tag)
                    await channel.send(msg, view=view)

            except Exception as e:
                print(f"處理荒野亂鬥 {tag} 戰績時發生錯誤: {e}")

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
            profile_url = f"https://api.brawlstars.com/v1/players/%23{clean_tag}"
            req = requests.get(profile_url, headers=BS_HEADERS, timeout=5)
            
            if req.status_code == 404:
                await interaction.followup.send(f"找不到標籤為 **#{clean_tag}** 的玩家，請確認輸入是否正確！")
                return
            elif req.status_code == 403:
                await interaction.followup.send("API Token 權限拒絕！請檢查 Token 或是 IP 設定。")
                return
            elif req.status_code != 200:
                await interaction.followup.send(f"BS API 連線異常 (代碼: {req.status_code})，請稍後再試！")
                return

            player_name = req.json().get("name", "Unknown")

            battle_url = f"https://api.brawlstars.com/v1/players/%23{clean_tag}/battlelog"
            b_req = requests.get(battle_url, headers=BS_HEADERS, timeout=5)
            if b_req.status_code == 200:
                b_res = b_req.json()
                if "items" in b_res and len(b_res["items"]) > 0:
                    last_battle_time[clean_tag] = b_res["items"][0].get("battleTime")
                else:
                    last_battle_time[clean_tag] = "20000101T000000.000Z"
            
        except Exception as e:
            await interaction.followup.send("連線發生錯誤，請稍後再試！")
            return

        for p in bs_focus_list:
            if p["tag"] == clean_tag:
                p["remain"] = sec
                p["name"] = player_name
                p["channel"] = interaction.channel
                await interaction.followup.send(f"已更新關注！繼續追蹤 **{player_name}** (#{clean_tag}) 的戰績，持續 {sec//60} 分鐘。")
                return

        bs_focus_list.append({
            "tag": clean_tag, 
            "name": player_name, 
            "remain": sec, 
            "channel": interaction.channel
        })
        await interaction.followup.send(f"✅ 成功關注 **{player_name}** (#{clean_tag})！接下來的 {sec//60} 分鐘內若有對戰將即時通報。")


    @app_commands.command(name="bs_關注列表", description="查看目前追蹤荒野亂鬥的玩家")
    async def bs_focus_list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if len(bs_focus_list) == 0:
            await interaction.followup.send("目前沒有關注任何荒野亂鬥玩家。")
            return
        
        msg = "目前正在關注的荒野亂鬥玩家：\n"
        for p in bs_focus_list:
            mins = p['remain'] // 60
            msg += f"- **{p['name']}** (#{p['tag']}) (剩餘 {mins} 分鐘)\n"
            
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(BrawlStar(bot))