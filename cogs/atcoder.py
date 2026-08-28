import discord
from discord.ext import commands
from discord.ext import tasks
from discord import app_commands
from discord.app_commands import Choice
import requests
import random
import time
import re
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Any

# AtCoder 追蹤相關資料結構
ac_queue = []  # 佇列: { "function": async_func, "interaction": channel_or_interaction, "params": {} }
ac_focus_list = []  # 追蹤中名單: [ { "ID": handle, "remain": sec, "channel": channel } ]
last_submission_id = {}  # 紀錄每個 handle 看到的最新 submission id
ac_focus_CD = 20  # 每 20 秒檢查一次

# 快取 Kenkoooo 題目與難度資料
problems_cache: List[Dict[str, Any]] = []
problem_models_cache: Dict[str, Dict[str, Any]] = {}
# (contest_id, problem_id) -> 該比賽中的題號；避免同一題被重複收錄到不同比賽時題號被覆蓋
contest_problem_index_cache: Dict[tuple, str] = {}
cache_last_updated = 0
contest_name_cache: Dict[str, str] = {}


def get_rating_color_emoji(rating: int) -> str:
    """根據 AtCoder 分數回傳對應的顏色 emoji 與段位名稱"""
    if rating < 400:
        return "⚪ 灰 (Gray)"
    elif rating < 800:
        return "🟤 茶 (Brown)"
    elif rating < 1200:
        return "🟢 綠 (Green)"
    elif rating < 1600:
        return "🌐 水/青 (Cyan)"
    elif rating < 2000:
        return "🔵 藍 (Blue)"
    elif rating < 2400:
        return "🟡 黃 (Yellow)"
    elif rating < 2800:
        return "🟠 橙 (Orange)"
    else:
        return "🔴 紅 (Red)"


def format_verdict(verdict: str) -> str:
    """將 AtCoder verdict 代碼轉為友善字串與 emoji"""
    verdict_map = {
        "AC": "Accepted 🟢",
        "WA": "Wrong Answer ❌",
        "TLE": "Time Limit Exceeded ⏱️",
        "MLE": "Memory Limit Exceeded 💾",
        "RE": "Runtime Error ⚠️",
        "CE": "Compilation Error 🔨",
        "OLE": "Output Limit Exceeded 📜",
        "IE": "Internal Error 💥",
        "Q": "In Queue ⏳",
        "WJ": "Waiting for Judge ⏳",
        "WR": "Waiting for Rejudge ⏳"
    }
    return verdict_map.get(verdict, verdict)


def load_problems_data():
    """載入或更新 Kenkoooo 題目、題號對應與難度資料"""
    global problems_cache, problem_models_cache, contest_problem_index_cache, cache_last_updated
    now = time.time()
    # 每天只重新抓取一次 (86400 秒)
    if (
        not problems_cache
        or not contest_problem_index_cache
        or (now - cache_last_updated > 86400)
    ):
        try:
            p_res = requests.get("https://kenkoooo.com/atcoder/resources/problems.json", timeout=10)
            if p_res.status_code == 200:
                problems_cache = p_res.json()

            # 題號不能只用 problem_id 判斷。
            # 同一題可能被收錄在 ABC、Daily Training 等不同 contest，題號會不同。
            cp_res = requests.get(
                "https://kenkoooo.com/atcoder/resources/contest-problem.json",
                timeout=10
            )
            if cp_res.status_code == 200:
                contest_problem_index_cache = {
                    (item["contest_id"], item["problem_id"]): item["problem_index"]
                    for item in cp_res.json()
                    if item.get("contest_id")
                    and item.get("problem_id")
                    and item.get("problem_index")
                }

            m_res = requests.get("https://kenkoooo.com/atcoder/resources/problem-models.json", timeout=10)
            if m_res.status_code == 200:
                problem_models_cache = m_res.json()

            cache_last_updated = now
        except Exception as e:
            print(f"[AtCoder] 載入題目快取失敗: {e}")


def get_contest_name(contest_id: str) -> str:
    """取得 AtCoder 比賽完整名稱，優先使用 Kenkoooo contests.json 並快取。"""
    if not contest_id:
        return "Unknown Contest"

    if contest_id in contest_name_cache:
        return contest_name_cache[contest_id]

    contest_name = contest_id.upper()

    try:
        res = requests.get(
            "https://kenkoooo.com/atcoder/resources/contests.json",
            timeout=10
        )
        if res.status_code == 200:
            for contest in res.json():
                cid = contest.get("id")
                title = contest.get("title")
                if cid and title:
                    contest_name_cache[cid] = title

            if contest_id in contest_name_cache:
                return contest_name_cache[contest_id]
    except Exception as e:
        print(f"[AtCoder] 載入比賽名稱資料失敗: {e}")

    # contests.json 尚未收錄或暫時不可用時，再直接從 AtCoder 比賽頁取得名稱
    try:
        res = requests.get(f"https://atcoder.jp/contests/{contest_id}", timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
                if title.endswith(" - AtCoder"):
                    title = title[:-10].strip()
                if title:
                    contest_name = title
    except Exception as e:
        print(f"[AtCoder] 取得比賽名稱失敗 ({contest_id}): {e}")

    contest_name_cache[contest_id] = contest_name
    return contest_name


class AtCoderSubmissionView(discord.ui.View):
    """AtCoder 提交通知的詳細資訊按鈕。"""

    def __init__(self, player: str, submission: dict):
        super().__init__(timeout=86400)  # 按鈕保留 24 小時
        self.player = player
        self.submission = submission

    @discord.ui.button(
        label="查看詳細資訊",
        emoji="📊",
        style=discord.ButtonStyle.primary
    )
    async def show_detail(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        sub = self.submission
        verdict = sub.get("verdict", "")

        # Embed 左側顏色依評測結果區分
        if verdict == "AC":
            color = discord.Color.green()
        elif verdict in {"WA", "RE", "CE", "IE"}:
            color = discord.Color.red()
        elif verdict in {"TLE", "MLE", "OLE"}:
            color = discord.Color.orange()
        else:
            color = discord.Color.blue()

        contest_name = sub.get("contest_name", sub["contest_id"].upper())
        contest_url = f"https://atcoder.jp/contests/{sub['contest_id']}"

        embed = discord.Embed(
            title="📊 AtCoder 提交詳細資訊",
            description=(
                f"🏆 **[{contest_name}]({contest_url})**\n"
                f"**{sub['problem_title']}**"
            ),
            color=color
        )

        embed.add_field(
            name="👤 Player",
            value=f"[{self.player}](https://atcoder.jp/users/{self.player})",
            inline=False
        )
        embed.add_field(
            name="📌 Verdict",
            value=format_verdict(verdict),
            inline=True
        )
        embed.add_field(
            name="💻 Language",
            value=sub.get("language") or "Unknown",
            inline=True
        )
        embed.add_field(
            name="🎯 Score",
            value=f"{sub['point_str']} pt",
            inline=True
        )

        execution_time = sub.get("execution_time")
        embed.add_field(
            name="⏱ Execution Time",
            value=f"{execution_time} ms" if execution_time is not None else "-",
            inline=True
        )

        code_length = sub.get("code_length")
        embed.add_field(
            name="📦 Code Length",
            value=f"{code_length} Bytes" if code_length is not None else "-",
            inline=True
        )

        difficulty = sub.get("difficulty")
        if difficulty is not None:
            try:
                difficulty_text = str(int(round(float(difficulty))))
            except (TypeError, ValueError):
                difficulty_text = str(difficulty)

            embed.add_field(
                name="⭐ Difficulty",
                value=difficulty_text,
                inline=True
            )

        epoch_second = sub.get("epoch_second")
        if epoch_second:
            embed.add_field(
                name="🕒 Submitted",
                value=f"<t:{int(epoch_second)}:R>",
                inline=True
            )

        embed.add_field(
            name="📝 Problem",
            value=f"[{sub['problem_title']}]({sub['problem_url']})",
            inline=False
        )
        embed.add_field(
            name="🔗 Submission",
            value=f"[前往 AtCoder 查看完整提交]({sub['submission_url']})",
            inline=False
        )

        embed.set_footer(
            text=f"AtCoder • {sub['contest_id'].upper()} • Submission #{sub['submission_id']}"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


class AtCoder(commands.Cog):
    """AtCoder 相關指令與追蹤模組"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ac_clock.start()
        self.ac_add_focus.start()

    def cog_unload(self):
        self.ac_clock.cancel()
        self.ac_add_focus.cancel()

    # ================= 佇列處理與定期檢查 =================

    @tasks.loop(seconds=3)
    async def ac_clock(self):
        """每 3 秒從佇列中取出一個任務執行，避免 rate limit"""
        if len(ac_queue) == 0:
            return

        current_task = ac_queue.pop(0)
        function = current_task["function"]
        interaction = current_task["interaction"]
        params = current_task["params"]

        try:
            await function(interaction, params)
        except Exception as e:
            print(f"[AtCoder] 執行 {function.__name__} 時發生錯誤: {e}")

    @tasks.loop(seconds=ac_focus_CD)
    async def ac_add_focus(self):
        """每 ac_focus_CD (20秒) 遍歷追蹤中的玩家，加入佇列進行檢查"""
        del_tmp = []
        for p in ac_focus_list:
            ac_queue.append({
                "function": self.ac_focus_update,
                "interaction": p["channel"],
                "params": {"ID": p["ID"]}
            })
            p["remain"] -= ac_focus_CD

            if p["remain"] <= 0:
                channel = p["channel"]
                try:
                    await channel.send(f"🏁 **{p['ID']}** 的 AtCoder 關注結束")
                except Exception as e:
                    print(f"[AtCoder] 發送結束訊息失敗: {e}")
                del_tmp.append(p)

        for p in del_tmp:
            if p in ac_focus_list:
                ac_focus_list.remove(p)

    # ================= 內部邏輯處理函式 =================

    async def ac_focus_setup(self, interaction: discord.Interaction, params: dict):
        """開始關注某位玩家"""
        ID = params["ID"]
        sec = params["sec"]

        # 1. 檢查該用戶在 AtCoder 是否存在
        try:
            user_check = requests.get(f"https://atcoder.jp/users/{ID}", timeout=5)
            if user_check.status_code != 200:
                await interaction.followup.send(f"❌ 查不到 AtCoder 玩家 **{ID}**，請確認 ID 是否正確！")
                return
        except Exception as e:
            await interaction.followup.send("⚠️ 連線 AtCoder 失敗，請稍後再試！")
            return

        # 2. 如果已經在關注名單，延長時間
        for p in ac_focus_list:
            if p["ID"].lower() == ID.lower():
                p["remain"] = sec
                p["channel"] = interaction.channel
                time_str = f"{sec//3600} 小時" if sec >= 3600 else f"{sec//60} 分鐘"
                await interaction.followup.send(f"🔄 繼續關注 **{ID}** 成功，持續時間設定為 **{time_str}**")
                return

        # 3. 取得該玩家最新的提交 ID 作為起始基準點
        try:
            # 抓取最近 30 天內的提交，找出最新的 submission ID
            recent_time = int(time.time()) - 86400 * 30
            sub_res = requests.get(
                f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={ID}&from_second={recent_time}",
                timeout=8
            )
            if sub_res.status_code == 200:
                submissions = sub_res.json()
                if submissions:
                    last_submission_id[ID] = max(sub["id"] for sub in submissions)
                else:
                    # 若近 30 天無提交，從全部紀錄中找最後一筆
                    all_sub_res = requests.get(
                        f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={ID}&from_second=0",
                        timeout=8
                    )
                    if all_sub_res.status_code == 200 and all_sub_res.json():
                        last_submission_id[ID] = max(sub["id"] for sub in all_sub_res.json())
                    else:
                        last_submission_id[ID] = 0
            else:
                last_submission_id[ID] = 0
        except Exception as e:
            print(f"[AtCoder] 取得 {ID} 歷史提交失敗: {e}")
            last_submission_id[ID] = 0

        # 加入關注名單
        ac_focus_list.append({
            "ID": ID,
            "remain": sec,
            "channel": interaction.channel,
            "start_time": int(time.time()) - 3600  # 記錄開始時間
        })

        time_str = f"{sec//3600} 小時" if sec >= 3600 else f"{sec//60} 分鐘"
        await interaction.followup.send(f"🎯 成功關注 **{ID}**，持續時間：**{time_str}**！")

    async def ac_focus_update(self, channel: discord.TextChannel, params: dict):
        """定期檢查是否有新提交並發送通知"""
        ID = params["ID"]
        last_id = last_submission_id.get(ID, 0)

        # 找出該玩家關注的起始時間，避免漏掉任何剛同步進來的提交
        user_focus = next((p for p in ac_focus_list if p["ID"].lower() == ID.lower()), None)
        from_second = user_focus["start_time"] if user_focus else int(time.time()) - 86400

        try:
            sub_res = requests.get(
                f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={ID}&from_second={from_second}",
                timeout=8
            )
            if sub_res.status_code != 200:
                return

            submissions = sub_res.json()
            if not submissions:
                return

        except Exception as e:
            print(f"[AtCoder] 取得 {ID} 提交紀錄失敗: {e}")
            return

        # 確保問題快取存在，以便找尋題目名稱
        load_problems_data()
        prob_dict = {p["id"]: p for p in problems_cache} if problems_cache else {}

        # 篩選比上次紀錄更新的提交，並按 ID 升冪排序
        new_subs = [s for s in submissions if s["id"] > last_id]
        new_subs.sort(key=lambda s: s["id"])

        for sub in new_subs:
            sub_id = sub["id"]
            verdict = sub.get("result", "")

            # 如果還在評判中 (WJ, WR, Q)，暫不更新該題的最後 ID，等評測完畢再通知
            if verdict in ["WJ", "WR", "Q", ""]:
                continue

            # 更新最新看到的 submission ID
            last_submission_id[ID] = max(last_submission_id.get(ID, 0), sub_id)

            prob_id = sub.get("problem_id", "")
            contest_id = sub.get("contest_id", "")
            point = sub.get("point", 0.0)
            point_str = f"{int(point)}" if point.is_integer() else f"{point}"

            # 尋找題目名稱與「這一場比賽中的」題號。
            # 不能只用 problem_id -> problem_index，因為同一題可能在不同 contest 中有不同題號。
            prob_info = prob_dict.get(prob_id, {})
            prob_index = contest_problem_index_cache.get((contest_id, prob_id), "")

            # contest-problem.json 暫時抓不到時，只在 problems.json 的 contest 也吻合時才 fallback，
            # 避免把別場比賽（例如 Daily Training）的 I 題誤套到 ABC340 的 F 題。
            if not prob_index and prob_info.get("contest_id") == contest_id:
                prob_index = prob_info.get("problem_index", "")

            prob_name = prob_info.get("name", prob_id)
            if prob_index:
                prob_title = f"{prob_index} - {prob_name}"
            else:
                prob_title = prob_name

            verdict_text = format_verdict(verdict)
            problem_url = f"https://atcoder.jp/contests/{contest_id}/tasks/{prob_id}"
            submission_url = f"https://atcoder.jp/contests/{contest_id}/submissions/{sub_id}"

            msg = (
                f"📢 **{ID}** 提交了 **{prob_title}** [{contest_id.upper()}]，"
                f"結果是 ***{verdict_text}***（{point_str} pt）！"
            )

            # 詳細卡片所需資訊；原本通知文字 msg 保持不變
            model_info = problem_models_cache.get(prob_id, {})
            detail_data = {
                "submission_id": sub_id,
                "contest_id": contest_id,
                "contest_name": get_contest_name(contest_id),
                "problem_title": prob_title,
                "verdict": verdict,
                "language": sub.get("language", "Unknown"),
                "point_str": point_str,
                "execution_time": sub.get("execution_time"),
                "code_length": sub.get("length"),
                "difficulty": model_info.get("difficulty"),
                "epoch_second": sub.get("epoch_second"),
                "problem_url": problem_url,
                "submission_url": submission_url
            }
            view = AtCoderSubmissionView(ID, detail_data)

            try:
                await channel.send(msg, view=view)
            except discord.Forbidden:
                print(f"[AtCoder] 權限不足: 無法在頻道 {channel.id} 發送訊息")
                break
            except Exception as send_err:
                print(f"[AtCoder] 發送訊息失敗: {send_err}")

    async def ac_rank(self, interaction: discord.Interaction, params: dict):
        """取得 AtCoder 全球排行榜 Top 10"""
        try:
            res = requests.get("https://atcoder.jp/ranking", timeout=8)
            if res.status_code != 200:
                await interaction.followup.send("❌ 連線 AtCoder 排行榜失敗，請稍後再試！")
                return

            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table", class_="table")
            if not table:
                await interaction.followup.send("❌ 解析 AtCoder 排行榜失敗！")
                return

            rows = table.find("tbody").find_all("tr")
            msg = "🏆 **AtCoder 全球排行榜 Top 10**\n```\n"
            msg += f"{'排名':<4} {'選手名稱':<20} {'Rating':<6}\n"
            msg += "-" * 34 + "\n"

            for i, row in enumerate(rows[:10]):
                tds = row.find_all("td")
                if len(tds) >= 4:
                    rank_num = tds[0].text.strip()
                    user_handle = tds[1].text.strip().split("\n")[0]
                    rating = tds[3].text.strip()
                    msg += f"{rank_num:<4} {user_handle:<20} {rating:<6}\n"

            msg += "```\n🔗 [查看完整排行榜](https://atcoder.jp/ranking)"
            await interaction.followup.send(msg)

        except Exception as e:
            print(f"[AtCoder] 取得排行榜失敗: {e}")
            await interaction.followup.send("⚠️ 查詢排行榜時發生錯誤！")

    async def ac_contest(self, interaction: discord.Interaction, params: dict):
        """取得 AtCoder 即將舉行的比賽"""
        try:
            res = requests.get("https://atcoder.jp/contests/", timeout=8)
            if res.status_code != 200:
                await interaction.followup.send("❌ 連線 AtCoder 比賽列表失敗，請稍後再試！")
                return

            soup = BeautifulSoup(res.text, "html.parser")
            upcoming_div = soup.find(id="contest-table-upcoming")
            if not upcoming_div:
                await interaction.followup.send("📅 目前沒有即將舉行的 AtCoder 比賽！")
                return

            rows = upcoming_div.find("tbody").find_all("tr")
            if not rows:
                await interaction.followup.send("📅 目前沒有即將舉行的 AtCoder 比賽！")
                return

            msg = "📅 **即將舉行的 AtCoder 比賽：**\n\n"
            count = min(4, len(rows))

            for row in rows[:count]:
                tds = row.find_all("td")
                if len(tds) >= 4:
                    time_raw = tds[0].text.strip()  # 例如 2026-08-29 21:00:00+0900
                    contest_a = tds[1].find("a")
                    contest_name = contest_a.text.strip() if contest_a else tds[1].text.strip()
                    contest_href = contest_a["href"] if contest_a and "href" in contest_a.attrs else ""
                    if contest_href.startswith("/"):
                        contest_url = f"https://atcoder.jp{contest_href}"
                    else:
                        contest_url = contest_href

                    duration = tds[2].text.strip()
                    rated_range = tds[3].text.strip()

                    msg += f"🔹 [**{contest_name}**]({contest_url})\n"
                    msg += f"　- ⏰ 開始時間：`{time_raw}`\n"
                    msg += f"　- ⏳ 比賽時長：`{duration}` ｜ 🎯 Rated 範圍：`{rated_range}`\n\n"

            msg += "🔗 [點我前往 AtCoder 比賽專區報名](https://atcoder.jp/contests/)"
            await interaction.followup.send(msg)

        except Exception as e:
            print(f"[AtCoder] 取得比賽列表失敗: {e}")
            await interaction.followup.send("⚠️ 查詢比賽列表時發生錯誤！")

    async def ac_user_score(self, interaction: discord.Interaction, params: dict):
        """查詢 AtCoder 玩家個人分數與狀態"""
        ID = params["ID"]
        try:
            profile_res = requests.get(f"https://atcoder.jp/users/{ID}", timeout=8)
            if profile_res.status_code != 200:
                await interaction.followup.send(f"❌ 查不到 AtCoder 玩家 **{ID}**！")
                return

            soup = BeautifulSoup(profile_res.text, "html.parser")
            th_tags = soup.find_all("th")
            data = {}
            for th in th_tags:
                nxt = th.find_next_sibling()
                if nxt:
                    data[th.text.strip()] = nxt.text.strip()

            current_rating_str = data.get("Rating", "Unrated")
            highest_rating_raw = data.get("Highest Rating", "Unrated")
            highest_rating_str = highest_rating_raw.split("\n")[0] if highest_rating_raw else "Unrated"
            rank_str = data.get("Rank", "無")
            rated_matches = data.get("Rated Matches", "0")
            affiliation = data.get("Affiliation", "無")
            country = data.get("Country/Region", "無")

            # 計算 emoji
            try:
                rating_val = int(re.search(r'\d+', current_rating_str).group())
                tier_emoji = get_rating_color_emoji(rating_val)
            except Exception:
                tier_emoji = "Unrated"

            msg = (
                f"## 👤 AtCoder 玩家：[{ID}](https://atcoder.jp/users/{ID})\n"
                f"- **目前分數 (Rating)**：{current_rating_str} ({tier_emoji})\n"
                f"- **最高分數 (Highest)**：{highest_rating_str}\n"
                f"- **全球排名 (Rank)**：{rank_str}\n"
                f"- **參加場次 (Rated Matches)**：{rated_matches}\n"
            )
            if affiliation and affiliation != "無":
                msg += f"- **所屬機構 (Affiliation)**：{affiliation}\n"
            if country and country != "無":
                msg += f"- **國家/地區**：{country}\n"

            await interaction.followup.send(msg)

        except Exception as e:
            print(f"[AtCoder] 查分失敗: {e}")
            await interaction.followup.send("⚠️ 查詢玩家分數時發生錯誤！")

    async def ac_user_contest(self, interaction: discord.Interaction, params: dict):
        """查詢 AtCoder 玩家在特定比賽中的表現"""
        ID = params["ID"]
        kw = params["contest_kw"].lower()

        try:
            history_res = requests.get(f"https://atcoder.jp/users/{ID}/history/json", timeout=8)
            if history_res.status_code != 200:
                await interaction.followup.send(f"❌ 查不到玩家 **{ID}** 的比賽歷史紀錄！")
                return

            history = history_res.json()
            if not history:
                await interaction.followup.send(f"**{ID}** 還沒有參加過任何 Rated 比賽喔！")
                return

            matched = [
                h for h in history
                if kw in h.get("ContestName", "").lower() or kw in h.get("ContestScreenName", "").lower()
            ]

            if not matched:
                await interaction.followup.send(f"找不到玩家 **{ID}** 參加過包含關鍵字 `{kw}` 的比賽！")
                return

            if len(matched) > 5:
                matched = matched[-5:]  # 最多顯示最新的 5 筆

            msg = f"## 🏆 {ID} 的比賽表現\n\n"
            for c in matched:
                c_name = c.get("ContestName", "Unknown Contest")
                screen_name = c.get("ContestScreenName", "")
                contest_code = screen_name.split(".")[0] if "." in screen_name else screen_name
                c_url = f"https://atcoder.jp/contests/{contest_code}" if contest_code else "https://atcoder.jp/contests"

                place = c.get("Place", "無")
                perf = c.get("Performance", "無")
                old_r = c.get("OldRating", 0)
                new_r = c.get("NewRating", 0)
                diff = new_r - old_r
                diff_str = f"+{diff}" if diff > 0 else f"{diff}"

                msg += (
                    f"### [**{c_name}**]({c_url})\n"
                    f"> 🎖️ **排名**：第 {place} 名\n"
                    f"> ⚡ **表現分 (Performance)**：{perf}\n"
                    f"> 📈 **Rating 變化**：***{old_r} ➔ {new_r}*** ({diff_str})\n\n"
                )

            await interaction.followup.send(msg)

        except Exception as e:
            print(f"[AtCoder] 查詢比賽表現失敗: {e}")
            await interaction.followup.send("⚠️ 查詢比賽表現時發生錯誤！")

    async def ac_get_random_problem(self, interaction: discord.Interaction, params: dict):
        """隨機抽取一題 AtCoder 題目"""
        l = params["L"]
        r = params["R"]
        cat = params.get("category", "ALL")

        load_problems_data()
        if not problems_cache:
            await interaction.followup.send("❌ 題目資料庫載入中或連線異常，請稍後再試！")
            return

        matched_problems = []
        for p in problems_cache:
            pid = p["id"]
            cid = p.get("contest_id", "")
            
            # 分類篩選
            if cat == "ABC" and not cid.lower().startswith("abc"):
                continue
            elif cat == "ARC" and not cid.lower().startswith("arc"):
                continue
            elif cat == "AGC" and not cid.lower().startswith("agc"):
                continue

            model = problem_models_cache.get(pid, {})
            diff = model.get("difficulty")

            # 若有指定難度，只篩選有 difficulty 數值的題目
            if diff is not None:
                if l <= diff <= r:
                    matched_problems.append((p, diff))
            elif l == 0 and r == 4000:
                # 預設範圍且無難度標註時也納入
                matched_problems.append((p, None))

        if not matched_problems:
            await interaction.followup.send(f"❌ 找不到難度在 {l} ~ {r} 之間的題目！")
            return

        chosen, diff_val = random.choice(matched_problems)
        contest_id = chosen.get("contest_id", "")
        prob_id = chosen["id"]
        prob_index = chosen.get("problem_index", "")
        prob_name = chosen.get("name", prob_id)
        url = f"https://atcoder.jp/contests/{contest_id}/tasks/{prob_id}"

        diff_str = f"難度：`{diff_val}`" if diff_val is not None else "難度：`未評估`"
        emoji = get_rating_color_emoji(diff_val) if diff_val is not None else "⚪"

        await interaction.followup.send(
            f"🎲 **隨機 AtCoder 題目推薦**：\n"
            f"### [**{prob_index}. {prob_name}**]({url})\n"
            f"- 所屬比賽：`{contest_id.upper()}`\n"
            f"- {diff_str} ({emoji})"
        )

    # ================= Slash 指令註冊 =================

    @app_commands.command(name="ac", description="查詢 AtCoder 的排行榜或近期比賽")
    @app_commands.describe(選擇="選擇功能")
    @app_commands.choices(
        選擇=[
            Choice(name="排行榜", value="rank"),
            Choice(name="最近的比賽", value="contests"),
        ]
    )
    async def ac_cmd(self, interaction: discord.Interaction, 選擇: Choice[str]):
        await interaction.response.defer()
        if 選擇.value == "rank":
            ac_queue.append({"function": self.ac_rank, "interaction": interaction, "params": {}})
        elif 選擇.value == "contests":
            ac_queue.append({"function": self.ac_contest, "interaction": interaction, "params": {}})

    @app_commands.command(name="ac查分", description="查詢 AtCoder 玩家的分數、段位或比賽表現")
    @app_commands.describe(id="AtCoder 使用者名稱", 比賽關鍵字="(可選) 查詢特定比賽表現，如 abc350")
    async def ac_user_cmd(self, interaction: discord.Interaction, id: str, 比賽關鍵字: Optional[str] = None):
        await interaction.response.defer()
        if 比賽關鍵字 is None:
            ac_queue.append({"function": self.ac_user_score, "interaction": interaction, "params": {"ID": id}})
        else:
            ac_queue.append({
                "function": self.ac_user_contest,
                "interaction": interaction,
                "params": {"ID": id, "contest_kw": 比賽關鍵字}
            })

    @app_commands.command(name="ac關注", description="追蹤玩家 AtCoder 的即時提交表現")
    @app_commands.describe(id="AtCoder 使用者名稱", 時間="關注時間")
    @app_commands.choices(
        時間=[
            Choice(name="2min(測試)", value=120),
            Choice(name="10分鐘", value=600),
            Choice(name="1hr", value=3600),
            Choice(name="2hr", value=3600 * 2),
            Choice(name="3hr", value=3600 * 3),
            Choice(name="1天", value=3600 * 24),
        ]
    )
    async def ac_focus_cmd(self, interaction: discord.Interaction, id: str, 時間: Choice[int]):
        await interaction.response.defer()
        ac_queue.append({
            "function": self.ac_focus_setup,
            "interaction": interaction,
            "params": {"ID": id, "sec": 時間.value}
        })

    @app_commands.command(name="ac取消關注", description="取消對某位 AtCoder 玩家的關注")
    @app_commands.describe(id="AtCoder 使用者名稱")
    async def ac_unfocus_cmd(self, interaction: discord.Interaction, id: str):
        await interaction.response.defer()
        target = None
        for p in ac_focus_list:
            if p["ID"].lower() == id.lower():
                target = p
                break

        if target:
            ac_focus_list.remove(target)
            await interaction.followup.send(f"🛑 已取消對 **{id}** 的 AtCoder 關注！")
        else:
            await interaction.followup.send(f"目前沒有在關注 **{id}** 喔！")

    @app_commands.command(name="ac關注列表", description="查看目前追蹤中的 AtCoder 玩家")
    async def ac_focus_list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not ac_focus_list:
            await interaction.followup.send("目前沒有關注任何 AtCoder 玩家。")
            return

        msg = "🔍 **目前正在關注的 AtCoder 玩家：**\n"
        for p in ac_focus_list:
            rem_min = max(1, p['remain'] // 60)
            msg += f"- **{p['ID']}**（剩餘約 `{rem_min}` 分鐘）於 <#{p['channel'].id}>\n"

        await interaction.followup.send(msg)

    @app_commands.command(name="ac隨機一題", description="隨機抽出一題 AtCoder 題目！")
    @app_commands.describe(
        最低難度="最低難度 (預設 0)",
        最高難度="最高難度 (預設 4000)",
        分類="比賽分類 (ABC/ARC/AGC/全部)"
    )
    @app_commands.choices(
        分類=[
            Choice(name="全部 (All)", value="ALL"),
            Choice(name="AtCoder Beginner Contest (ABC)", value="ABC"),
            Choice(name="AtCoder Regular Contest (ARC)", value="ARC"),
            Choice(name="AtCoder Grand Contest (AGC)", value="AGC"),
        ]
    )
    async def ac_random_problem_cmd(
        self,
        interaction: discord.Interaction,
        最低難度: Optional[int] = 0,
        最高難度: Optional[int] = 4000,
        分類: Optional[Choice[str]] = None
    ):
        await interaction.response.defer()
        l = 最低難度 if 最低難度 is not None else 0
        r = 最高難度 if 最高難度 is not None else 4000
        if l > r:
            l, r = r, l
        cat = 分類.value if 分類 else "ALL"

        ac_queue.append({
            "function": self.ac_get_random_problem,
            "interaction": interaction,
            "params": {"L": l, "R": r, "category": cat}
        })


async def setup(bot: commands.Bot):
    await bot.add_cog(AtCoder(bot))