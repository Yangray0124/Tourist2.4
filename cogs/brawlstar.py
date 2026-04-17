import discord
from discord.ext import commands
from discord.ext import tasks
from discord import app_commands
from discord.app_commands import Choice
import requests
import json
import os
from keys import brawl_stars_api_key

# 儲存目前正在追蹤的列表
bs_focus_list = []
# 紀錄每個玩家最新的一場對戰時間
last_battle_time = {}

bs_focus_CD = 30 
HISTORY_FILE = "bs_history.json"

# API 請求的 Headers
BS_HEADERS = {'Authorization': f'Bearer {brawl_stars_api_key}', 'Accept': 'application/json'}

def format_name(name):
    if not name: return "Unknown"
    return name.title()

def save_to_history(tag, name):
    """將玩家存入歷史紀錄檔案"""
    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
    
    history[tag] = name # 以 tag 為 key 確保不重複
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=4, ensure_ascii=False)

def get_history():
    """讀取歷史紀錄"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

class BattleDetailView(discord.ui.View):
    """詳細戰報按鈕"""
    def __init__(self, battle_data: dict, player_tag: str):
        super().__init__(timeout=None)
        self.battle_data = battle_data
        self.player_tag = player_tag

    @discord.ui.button(label="📊 查看詳細戰報", style=discord.ButtonStyle.primary)
    async def show_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        battle = self.battle_data.get("battle", {})
        event = self.battle_data.get("event", {})
        mode = format_name(event.get("mode", battle.get("mode", "Unknown")))
        map_name = event.get("map", "Unknown Map")
        
        embed = discord.Embed(title=f"📝 Battle Detail: {mode} - {map_name}", color=0x3498db)
        
        sp = battle.get("starPlayer")
        if sp:
            sp_brawler = format_name(sp.get('brawler', {}).get('name', ''))
            embed.add_field(name="🌟 Star Player", value=f"**{sp.get('name')}** ({sp_brawler})", inline=False)

        if "teams" in battle:
            player_team_idx = 0
            for i, team in enumerate(battle["teams"]):
                for p in team:
                    if p.get("tag", "").replace("#", "") == self.player_tag:
                        player_team_idx = i
            for i, team in enumerate(battle["teams"]):
                team_label = "🔵 Friendly Team" if i == player_team_idx else "🔴 Enemy Team"
                info = "\n".join([f"• {p.get('name')} ({format_name(p.get('brawler', {}).get('name', ''))}) 🏆{p.get('brawler', {}).get('trophies', '?')}" for p in team])
                embed.add_field(name=team_label, value=info, inline=False)
        elif "players" in battle:
            info = "\n".join([f"• {p.get('name')} ({format_name(p.get('brawler', {}).get('name', ''))}) 🏆{p.get('brawler', {}).get('trophies', '?')}" for p in battle["players"]])
            embed.add_field(name="👥 Players", value=info[:1024], inline=False)
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

class HistorySelect(discord.ui.Select):
    """常用玩家下拉選單"""
    def __init__(self, history, cog):
        self.cog = cog
        options = [
            discord.SelectOption(label=name, description=f"Tag: #{tag}", value=tag)
            for tag, name in list(history.items())[-25:] # 最多顯示最近25位
        ]
        super().__init__(placeholder="選擇要關注的玩家...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.start_focus(interaction, self.values[0], 3600)

class HistoryView(discord.ui.View):
    def __init__(self, history, cog):
        super().__init__()
        self.add_item(HistorySelect(history, cog))

class BrawlStar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bs_focus_update.start()

    def cog_unload(self): 
        self.bs_focus_update.cancel()

    async def start_focus(self, interaction, tag, sec):
        """核心關注邏輯"""
        clean_tag = tag.replace("#", "").upper()
        try:
            req = requests.get(f"https://api.brawlstars.com/v1/players/%23{clean_tag}", headers=BS_HEADERS, timeout=5)
            
            if req.status_code != 200:
                print(f"DEBUG [Setup Failed]: {clean_tag} Status {req.status_code}")
                await interaction.followup.send("❌ 標籤錯誤或 API 連線失敗。")
                return
                
            p_data = req.json()
            p_name = p_data.get("name", "Unknown")
            save_to_history(clean_tag, p_name)

            b_req = requests.get(f"https://api.brawlstars.com/v1/players/%23{clean_tag}/battlelog", headers=BS_HEADERS, timeout=5)
            
            # 初始設定時依然印出一次，確認連線成功
            print(f"DEBUG [Initial Focus]: {p_name} (#{clean_tag})")

            if b_req.status_code == 200:
                items = b_req.json().get("items", [])
                if items: last_battle_time[clean_tag] = items[0].get("battleTime")

            # 🛑 跨頻道檢查：同時比對標籤與頻道 ID
            for p in bs_focus_list:
                if p["tag"] == clean_tag and p["channel"].id == interaction.channel.id:
                    p["remain"], p["name"] = sec, p_name
                    await interaction.followup.send(f"✅ 已更新本頻道的追蹤：**{p_name}** (#{clean_tag})。")
                    return
            
            bs_focus_list.append({"tag": clean_tag, "name": p_name, "remain": sec, "channel": interaction.channel})
            await interaction.followup.send(f"✅ 成功追蹤 **{p_name}** (#{clean_tag})！")
        except Exception as e: 
            print(f"DEBUG [Setup Error]: {e}")
            await interaction.followup.send("❌ 連線發生錯誤。")

    @tasks.loop(seconds=bs_focus_CD)
    async def bs_focus_update(self):
        del_tmp = []
        for p in bs_focus_list:
            tag, name, channel = p["tag"], p["name"], p["channel"]
            p["remain"] -= bs_focus_CD
            if p["remain"] <= 0:
                try:
                    await channel.send(f"⌛ **{name}** (#{tag}) 的追蹤時間結束！")
                except:
                    pass # 若無法發送訊息，直接略過以防報錯
                del_tmp.append(p)
                continue

            try:
                req = requests.get(f"https://api.brawlstars.com/v1/players/%23{tag}/battlelog", headers=BS_HEADERS, timeout=10)
                if req.status_code != 200: continue
                
                res = req.json()
                items = res.get("items", [])
                if not items: continue

                new_battles = []
                for b in items:
                    bt = b.get("battleTime")
                    if last_battle_time.get(tag) and bt > last_battle_time[tag]: 
                        new_battles.append(b)
                    else: break
                
                # --- 只有當有「新對戰」時才印出內容 ---
                if new_battles:
                    print(f"DEBUG [New Battle Found]: {name} (#{tag}) for Channel: {channel.name if hasattr(channel, 'name') else channel.id}")
                    print(json.dumps(res, indent=4, ensure_ascii=False))

                if items: last_battle_time[tag] = items[0].get("battleTime")
                new_battles.reverse()

                for b in new_battles:
                    battle_info = b.get("battle", {})
                    event_info = b.get("event", {})
                    mode = format_name(event_info.get("mode", battle_info.get("mode", "unknown")))
                    map_name = event_info.get("map", "Unknown Map")
                    
                    used_brawler = "Unknown"
                    participants = []
                    if "teams" in battle_info:
                        for t in battle_info["teams"]: participants.extend(t)
                    elif "players" in battle_info:
                        participants = battle_info["players"]
                    
                    for pl in participants:
                        if pl.get("tag", "").replace("#", "") == tag:
                            used_brawler = format_name(pl.get("brawler", {}).get("name", ""))
                            break

                    t_change = battle_info.get("trophyChange", 0)
                    t_str = f"{'+' if t_change > 0 else ''}{t_change}"
                    
                    if "rank" in battle_info:
                        res_str = f"獲得了 🎖️ **第 {battle_info['rank']} 名**"
                    else:
                        r = battle_info.get("result", "draw")
                        if r == "victory": res_str = "獲得了 ✨ **獲勝！**"
                        elif r == "defeat": res_str = "獲得了 💀 **戰敗**"
                        else: res_str = "獲得了 🤝 **平手**"

                    msg = f"🎮 **{name}** 使用了 🔫 **{used_brawler}** 在 ⚔️ **{mode}** 的 🗺️ **{map_name}** {res_str} ({t_str} 🏆)"
                    
                    # 🛑 權限攔截保護：發生 403 錯誤時移除該頻道的追蹤任務
                    try:
                        await channel.send(msg, view=BattleDetailView(b, tag))
                    except discord.Forbidden:
                        print(f"❌ 權限不足 (403): 無法在頻道 {channel.id} 發送。已自動取消該頻道的追蹤。")
                        if p not in del_tmp:
                            del_tmp.append(p)
                        break
                    except Exception as send_err:
                        print(f"發送訊息失敗: {send_err}")

            except Exception as e: 
                print(f"BS Loop Error: {e}")

        # 清除過期或無權限的追蹤
        for p in del_tmp:
            if p in bs_focus_list: bs_focus_list.remove(p)

    @app_commands.command(name="bs_關注", description="追蹤玩家即時戰績")
    @app_commands.describe(tag="玩家標籤(不需#)", 時間="關注時間")
    @app_commands.choices(時間=[
        Choice(name="10分鐘", value=600), 
        Choice(name="1hr", value=3600), 
        Choice(name="2hr", value=3600*2)
    ])
    async def bs_focus(self, interaction: discord.Interaction, tag: str, 時間: Choice[int]):
        await interaction.response.defer()
        await self.start_focus(interaction, tag, 時間.value)

    @app_commands.command(name="bs_常用清單", description="從關注過的玩家中快速選擇")
    async def bs_history_list(self, interaction: discord.Interaction):
        history = get_history()
        if not history:
            await interaction.response.send_message("目前常用清單是空的，請先使用 `/bs_關注` 追蹤玩家！")
            return
        
        await interaction.response.send_message("請選擇一位玩家開始關注（預設 1 小時）：", view=HistoryView(history, self))

    @app_commands.command(name="bs_關注列表", description="查看追蹤中的玩家")
    async def bs_focus_list_cmd(self, interaction: discord.Interaction):
        if not bs_focus_list:
            await interaction.response.send_message("目前沒有追蹤任何玩家。")
            return
        msg = "🔍 **目前正在追蹤的荒野亂鬥玩家：**\n" + "\n".join([f"- **{p['name']}** (#{p['tag']}) [剩餘 {p['remain']//60} 分鐘]" for p in bs_focus_list])
        await interaction.response.send_message(msg)

async def setup(bot: commands.Bot): 
    await bot.add_cog(BrawlStar(bot))