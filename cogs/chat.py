import base64
import os
import sys
import re
import io
import fitz  # PyMuPDF 的套件名稱是 fitz
import asyncio

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
import uuid
import json
import mimetypes
import html
from pathlib import Path
from urllib.parse import quote
from aiohttp import web

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


# ==================== Tourist 自架檔案上傳 / 下載 ====================
# 對外公開網址，例如：
#   https://files.example.com
#   https://xxxx.trycloudflare.com
# 預設直接使用目前 Tourist AWS 對外可連的 15.152.171.240:5173。
UPLOAD_BASE_URL = os.getenv("UPLOAD_BASE_URL", "http://15.152.171.240:5173").rstrip("/")
UPLOAD_BIND_HOST = os.getenv("UPLOAD_BIND_HOST", "0.0.0.0")
UPLOAD_PORT = int(os.getenv("UPLOAD_PORT", "5173"))
UPLOAD_ROOT = Path(os.getenv("UPLOAD_ROOT", "./uploaded_files"))
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))  # 預設 2GB
UPLOAD_PAGE_TOKEN_TTL = 15 * 60  # 上傳頁面連結 15 分鐘失效
UPLOAD_CLEANUP_INTERVAL_MINUTES = 5
DISCORD_INLINE_FILE_LIMIT = 20 * 1024 * 1024  # 2026/08 起一般使用者上限為 20MB；實際 bot API 若較低會自動 fallback
DISCORD_PREVIEW_BATCH_LIMIT = 20 * 1024 * 1024  # 每則 PDF 預覽訊息保守控制在 20MB 內
DISCORD_MAX_ATTACHMENTS_PER_MESSAGE = 10  # Discord API 每則訊息最多 10 個附件

# 預設只在 Linux（AWS）啟用上傳網站；Windows 本機不開任何 upload port。
# 若之後真的需要手動覆寫，可設定環境變數 UPLOAD_ENABLED=1 或 0。
_default_upload_enabled = sys.platform.startswith("linux")
_env_upload_enabled = os.getenv("UPLOAD_ENABLED")
if _env_upload_enabled is None:
    UPLOAD_ENABLED = _default_upload_enabled
else:
    UPLOAD_ENABLED = _env_upload_enabled.strip().lower() in {"1", "true", "yes", "on"}

if UPLOAD_ENABLED:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
# ===================================================================


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

        # 新增：Tourist 自架上傳服務。與原本功能完全獨立。
        self.upload_sessions = {}
        self.upload_web_runner = None
        self.upload_web_site = None
        self.upload_server_task = None

        if UPLOAD_ENABLED:
            self.upload_server_task = asyncio.create_task(self._start_upload_server())
            self.upload_cleanup.start()
            print(f"[Upload] enabled on {sys.platform}; preparing port {UPLOAD_PORT}")
        else:
            print(f"[Upload] disabled on {sys.platform}; web server will NOT start")

    # @commands.command()
    # async def Hello(self, ctx: commands.Context):
    #     await ctx.send("你好")

    @app_commands.command(name="說你好", description="說你好")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("你好")

    @app_commands.command(name="版本", description="Tourist2.4")
    async def version(self, interaction: discord.Interaction):
        await interaction.response.send_message(">>> 版本： **Tourist2.4**\n"
                                                "更新日期： 2026/04/19\n"
                                                "才藝： 智能聊天、cf功能、唱歌\n"
                                                "贊助商： 郭老師贊助機器！\n"
                                                "OpenSource： https://github.com/Yangray0124/Tourist2.4.git")

    @tasks.loop(seconds=3)
    async def cf_clock(self):
        # print("clock")
        if len(cf_queue) == 0:
            return
        
        # 先取出任務並移除
        current_task = cf_queue.pop(0)
        function, interaction, params = current_task["function"], current_task["interaction"], current_task["params"]
        
        # 加上 try...except 安全網，保證迴圈絕對不會死
        try:
            await function(interaction, params)
        except Exception as e:
            print(f"執行 {function.__name__} 時發生錯誤: {e}")
            
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

    # ==================== Tourist 自架檔案上傳功能 ====================

    async def _start_upload_server(self):
        """啟動內建 aiohttp server，提供上傳頁面與檔案下載。"""
        await self.bot.wait_until_ready()

        app = web.Application(client_max_size=UPLOAD_MAX_BYTES)
        app.router.add_get("/upload/{token}", self._upload_page)
        app.router.add_post("/upload/{token}", self._upload_receive)
        app.router.add_get("/files/{file_id}/{filename:.*}", self._download_file)
        app.router.add_get("/health", self._upload_health)

        self.upload_web_runner = web.AppRunner(app)
        await self.upload_web_runner.setup()
        self.upload_web_site = web.TCPSite(
            self.upload_web_runner,
            UPLOAD_BIND_HOST,
            UPLOAD_PORT,
        )

        try:
            await self.upload_web_site.start()
            print(
                f"[Upload] server started: {UPLOAD_BIND_HOST}:{UPLOAD_PORT} "
                f"public={UPLOAD_BASE_URL}"
            )
        except OSError as e:
            print(f"[Upload] 無法啟動上傳 server: {e}")

    async def _upload_health(self, request: web.Request):
        return web.json_response({"ok": True})

    @staticmethod
    def _safe_upload_filename(filename: str) -> str:
        filename = os.path.basename(filename or "upload.bin")
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ")
        if not filename:
            filename = "upload.bin"

        # Windows path 與瀏覽器處理都比較穩，保留副檔名並限制長度。
        stem, ext = os.path.splitext(filename)
        if len(filename) > 180:
            filename = stem[: max(1, 180 - len(ext))] + ext
        return filename

    async def _upload_page(self, request: web.Request):
        token = request.match_info["token"]
        session = self.upload_sessions.get(token)
        now = time.time()

        if not session or session["page_expires_at"] <= now:
            self.upload_sessions.pop(token, None)
            return web.Response(
                text="<h2>這個上傳連結已失效。</h2>",
                content_type="text/html",
                status=410,
            )

        retention_text = session["retention_text"]
        max_gb = UPLOAD_MAX_BYTES / (1024 ** 3)
        page_html = f"""<!doctype html>
<html lang=\"zh-Hant\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>Tourist File Upload</title>
<style>
  * {{ box-sizing: border-box; }}
  :root {{
    color-scheme: light;
    --bg: #f2efe7;
    --card: #fffdf8;
    --ink: #262621;
    --muted: #777064;
    --line: #d9d3c5;
    --soft: #f7f3e9;
    --accent: #2e302b;
  }}
  body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
    font-family: system-ui, -apple-system, \"Segoe UI\", sans-serif;
    background:
      radial-gradient(circle at 20% 10%, rgba(255,255,255,.9), transparent 34%),
      var(--bg);
    color: var(--ink);
  }}
  .card {{
    width: min(700px, 100%);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 24px;
    padding: 30px;
    box-shadow: 0 20px 70px rgba(55, 48, 36, .10);
  }}
  .eyebrow {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
    color: var(--muted);
    font-size: 13px;
    letter-spacing: .04em;
  }}
  h1 {{ margin: 0; font-size: clamp(26px, 5vw, 34px); letter-spacing: -.03em; }}
  .sub {{ margin: 10px 0 24px; color: var(--muted); line-height: 1.65; }}
  .drop {{
    border: 1.5px dashed #aaa291;
    border-radius: 18px;
    padding: 34px 20px;
    text-align: center;
    background: var(--soft);
    transition: .18s ease;
  }}
  .drop:hover {{ border-color: #777064; transform: translateY(-1px); }}
  input[type=file] {{ max-width: 100%; font: inherit; }}
  .file-info {{
    min-height: 22px;
    margin-top: 12px;
    color: var(--muted);
    font-size: 14px;
    overflow-wrap: anywhere;
  }}
  button {{
    margin-top: 18px;
    width: 100%;
    border: 0;
    border-radius: 14px;
    padding: 14px 18px;
    font-size: 16px;
    font-weight: 750;
    background: var(--accent);
    color: white;
    cursor: pointer;
    transition: .16s ease;
  }}
  button:hover:not(:disabled) {{ transform: translateY(-1px); opacity: .94; }}
  button:disabled {{ cursor: default; opacity: .55; }}
  .progress-wrap {{
    display: none;
    margin-top: 20px;
    padding: 16px;
    border: 1px solid var(--line);
    background: #fff;
    border-radius: 16px;
  }}
  .progress-head {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
    margin-bottom: 10px;
  }}
  .progress-title {{ font-weight: 720; }}
  .progress-percent {{ font-variant-numeric: tabular-nums; color: var(--muted); }}
  .track {{
    height: 10px;
    overflow: hidden;
    background: #ebe7dd;
    border-radius: 999px;
  }}
  .bar {{
    width: 0%;
    height: 100%;
    background: #3d4039;
    border-radius: inherit;
    transition: width .12s linear;
  }}
  .progress-detail {{
    margin-top: 9px;
    color: var(--muted);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }}
  .error {{ color: #a33b32; }}
  .meta {{ margin-top: 18px; font-size: 13px; color: var(--muted); line-height: 1.65; }}
</style>
</head>
<body>
  <main class=\"card\">
    <div class=\"eyebrow\">TOURIST · FILE UPLOAD</div>
    <h1>📤 上傳檔案</h1>
    <div class=\"sub\">選擇 PDF、圖片、影片或其他檔案。大檔案上傳時會顯示即時進度。</div>

    <form id=\"uploadForm\" method=\"post\" enctype=\"multipart/form-data\">
      <div class=\"drop\">
        <input id=\"fileInput\" type=\"file\" name=\"file\" required>
        <div id=\"fileInfo\" class=\"file-info\">📎 尚未選擇檔案</div>
      </div>
      <button id=\"submitButton\" type=\"submit\">🚀 開始上傳</button>
    </form>

    <section id=\"progressWrap\" class=\"progress-wrap\">
      <div class=\"progress-head\">
        <div id=\"progressTitle\" class=\"progress-title\">正在上傳…</div>
        <div id=\"progressPercent\" class=\"progress-percent\">0%</div>
      </div>
      <div class=\"track\"><div id=\"progressBar\" class=\"bar\"></div></div>
      <div id=\"progressDetail\" class=\"progress-detail\">準備中…</div>
    </section>

    <div class=\"meta\">
      保存時間：{html.escape(retention_text)} · 單檔上限：約 {max_gb:.1f} GB<br>
      此頁面為一次性上傳連結，上傳成功後即失效。
    </div>
  </main>

<script>
  const form = document.getElementById('uploadForm');
  const fileInput = document.getElementById('fileInput');
  const fileInfo = document.getElementById('fileInfo');
  const button = document.getElementById('submitButton');
  const wrap = document.getElementById('progressWrap');
  const title = document.getElementById('progressTitle');
  const percent = document.getElementById('progressPercent');
  const bar = document.getElementById('progressBar');
  const detail = document.getElementById('progressDetail');

  function formatBytes(bytes) {{
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, i);
    return `${{value.toFixed(i >= 2 ? 2 : 1)}} ${{units[i]}}`;
  }}

  fileInput.addEventListener('change', () => {{
    const file = fileInput.files && fileInput.files[0];
    fileInfo.textContent = file ? `📎 ${{file.name}} · ${{formatBytes(file.size)}}` : '📎 尚未選擇檔案';
  }});

  form.addEventListener('submit', (event) => {{
    event.preventDefault();
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;

    button.disabled = true;
    button.textContent = '⬆️ 上傳中…';
    fileInput.disabled = true;
    wrap.style.display = 'block';
    title.textContent = '⬆️ 正在上傳…';
    title.classList.remove('error');
    percent.textContent = '0%';
    bar.style.width = '0%';
    detail.textContent = '正在建立連線…';

    const data = new FormData(form);
    // fileInput disabled 後不會被 FormData(form) 收入，因此手動補回檔案。
    data.set('file', file, file.name);

    const xhr = new XMLHttpRequest();
    const startedAt = performance.now();

    xhr.open('POST', window.location.href, true);

    xhr.upload.onprogress = (e) => {{
      if (!e.lengthComputable) {{
        detail.textContent = `${{formatBytes(e.loaded)}} 已上傳`;
        return;
      }}

      const p = Math.min(100, (e.loaded / e.total) * 100);
      const elapsed = Math.max((performance.now() - startedAt) / 1000, 0.1);
      const speed = e.loaded / elapsed;

      percent.textContent = `${{p.toFixed(p < 10 ? 1 : 0)}}%`;
      bar.style.width = `${{p}}%`;
      detail.textContent = `${{formatBytes(e.loaded)}} / ${{formatBytes(e.total)}} · ${{formatBytes(speed)}}/s`;

      if (p >= 100) {{
        title.textContent = '💾 檔案已傳送，正在完成儲存…';
        detail.textContent = '請稍候，不要關閉此頁面。';
      }}
    }};

    xhr.onload = () => {{
      if (xhr.status >= 200 && xhr.status < 300) {{
        document.open();
        document.write(xhr.responseText);
        document.close();
        return;
      }}

      title.textContent = '上傳失敗';
      title.classList.add('error');
      percent.textContent = '';
      detail.textContent = xhr.responseText || `伺服器回傳錯誤 ${{xhr.status}}`;
      button.disabled = false;
      button.textContent = '重新上傳';
      fileInput.disabled = false;
    }};

    xhr.onerror = () => {{
      title.textContent = '連線中斷';
      title.classList.add('error');
      percent.textContent = '';
      detail.textContent = '無法連線到伺服器，請確認網路後再試一次。';
      button.disabled = false;
      button.textContent = '重新上傳';
      fileInput.disabled = false;
    }};

    xhr.send(data);
  }});
</script>
</body>
</html>"""
        return web.Response(text=page_html, content_type="text/html")
    async def _upload_receive(self, request: web.Request):
        token = request.match_info["token"]
        session = self.upload_sessions.get(token)
        now = time.time()

        if not session or session["page_expires_at"] <= now:
            self.upload_sessions.pop(token, None)
            return web.Response(text="上傳連結已失效。", status=410)

        try:
            reader = await request.multipart()
            file_field = None

            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "file":
                    file_field = part
                    break

            if file_field is None or not file_field.filename:
                return web.Response(text="沒有收到檔案。", status=400)

            original_filename = file_field.filename
            filename = self._safe_upload_filename(original_filename)
            file_id = uuid.uuid4().hex
            file_dir = UPLOAD_ROOT / file_id
            file_dir.mkdir(parents=True, exist_ok=False)
            file_path = file_dir / filename

            total_size = 0
            with open(file_path, "wb") as f:
                while True:
                    chunk = await file_field.read_chunk(size=1024 * 1024)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > UPLOAD_MAX_BYTES:
                        f.close()
                        shutil.rmtree(file_dir, ignore_errors=True)
                        return web.Response(text="檔案超過大小限制。", status=413)
                    f.write(chunk)

            created_at = int(time.time())
            expires_at = created_at + session["retention_seconds"]
            mime_type = (
                file_field.headers.get("Content-Type")
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )

            metadata = {
                "file_id": file_id,
                "filename": filename,
                "original_filename": original_filename,
                "size": total_size,
                "mime_type": mime_type,
                "created_at": created_at,
                "expires_at": expires_at,
                "channel_id": session["channel_id"],
                "guild_id": session.get("guild_id"),
                "requester_id": session["requester_id"],
            }
            (file_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # 一次性 token：只允許成功上傳一個檔案。
            self.upload_sessions.pop(token, None)

            # 回 Discord 的工作不阻塞瀏覽器回應。
            asyncio.create_task(self._announce_uploaded_file(metadata, file_path))

            download_url = f"{UPLOAD_BASE_URL}/files/{file_id}/{quote(filename)}"
            size = total_size
            if size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            elif size < 1024 * 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.2f} MB"
            else:
                size_str = f"{size / (1024 ** 3):.2f} GB"

            success_html = f"""<!doctype html>
<html lang=\"zh-Hant\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>Upload complete</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
    font-family: system-ui, -apple-system, \"Segoe UI\", sans-serif;
    background: #f2efe7;
    color: #262621;
  }}
  .card {{
    width: min(620px, 100%);
    padding: 34px;
    border: 1px solid #d9d3c5;
    border-radius: 24px;
    background: #fffdf8;
    box-shadow: 0 20px 70px rgba(55, 48, 36, .10);
    text-align: center;
  }}
  .check {{
    width: 62px;
    height: 62px;
    display: grid;
    place-items: center;
    margin: 0 auto 18px;
    border-radius: 50%;
    background: #e6eee4;
    font-size: 30px;
  }}
  h1 {{ margin: 0; font-size: 30px; letter-spacing: -.03em; }}
  .sub {{ margin: 10px 0 22px; color: #746e63; line-height: 1.6; }}
  .file {{
    padding: 15px 16px;
    border: 1px solid #e0dbcf;
    border-radius: 15px;
    background: #f8f5ed;
    text-align: left;
    overflow-wrap: anywhere;
  }}
  .name {{ font-weight: 720; }}
  .size {{ margin-top: 4px; color: #81796c; font-size: 13px; }}
  .download {{
    display: block;
    margin-top: 18px;
    padding: 14px 18px;
    border-radius: 14px;
    background: #2e302b;
    color: white;
    text-decoration: none;
    font-weight: 750;
  }}
  .hint {{ margin-top: 16px; color: #81796c; font-size: 13px; }}
</style>
</head>
<body>
  <main class=\"card\">
    <div class=\"check\">✅</div>
    <h1>上傳完成</h1>
    <div class=\"sub\">檔案已儲存，並正在回傳到 Discord。</div>
    <div class=\"file\">
      <div class=\"name\">{html.escape(filename)}</div>
      <div class=\"size\">{html.escape(size_str)}</div>
    </div>
    <a class=\"download\" href=\"{html.escape(download_url)}\">⬇️ 下載原檔</a>
    <div class=\"hint\">你現在可以關閉這個頁面。</div>
  </main>
</body>
</html>"""
            return web.Response(text=success_html, content_type="text/html")

        except Exception as e:
            print(f"[Upload] 接收檔案失敗: {e}")
            return web.Response(text="上傳失敗，請稍後再試。", status=500)
    async def _download_file(self, request: web.Request):
        file_id = request.match_info["file_id"]
        requested_filename = request.match_info.get("filename", "")

        # UUID hex only，避免 path traversal。
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise web.HTTPNotFound()

        file_dir = UPLOAD_ROOT / file_id
        metadata_path = file_dir / "metadata.json"
        if not metadata_path.exists():
            raise web.HTTPNotFound()

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            raise web.HTTPNotFound()

        if int(metadata.get("expires_at", 0)) <= int(time.time()):
            shutil.rmtree(file_dir, ignore_errors=True)
            raise web.HTTPGone(text="這個檔案已經過期。")

        filename = metadata.get("filename")
        if not filename or requested_filename != filename:
            raise web.HTTPNotFound()

        file_path = file_dir / filename
        if not file_path.exists():
            raise web.HTTPNotFound()

        return web.FileResponse(
            path=file_path,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    async def _announce_uploaded_file(self, metadata: dict, file_path: Path):
        """把上傳結果送回原 Discord channel，並建立 thread。"""
        try:
            channel = self.bot.get_channel(metadata["channel_id"])
            if channel is None:
                channel = await self.bot.fetch_channel(metadata["channel_id"])

            filename = metadata["filename"]
            file_id = metadata["file_id"]
            expires_at = metadata["expires_at"]
            size = metadata["size"]
            download_url = f"{UPLOAD_BASE_URL}/files/{file_id}/{quote(filename)}"

            if size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            elif size < 1024 * 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.2f} MB"
            else:
                size_str = f"{size / (1024 ** 3):.2f} GB"

            # 主頻道只保留最重要的兩件事：檔名與直接下載。
            # 檔名刻意不做超連結，避免同一張卡片出現兩個下載入口。
            embed = discord.Embed(
                title="✅ 檔案上傳完成",
                description=(
                    f"**{filename}**\n\n"
                    f"**[點我直接下載]({download_url})**"
                ),
                color=discord.Color.green(),
            )

            sent = await channel.send(embed=embed)

            thread_name = filename if len(filename) <= 70 else filename[:67] + "..."
            thread = await sent.create_thread(
                name=f"📎 {thread_name}",
                auto_archive_duration=60,
            )

            # 詳細資訊集中在討論串內；這裡不再另外放下載超連結。
            thread_embed = discord.Embed(
                title="📋 檔案資訊",
                description=f"**{filename}**",
                color=discord.Color.blue(),
            )
            thread_embed.add_field(name="📦 大小", value=size_str, inline=True)
            thread_embed.add_field(
                name="🧩 類型",
                value=metadata.get("mime_type") or "application/octet-stream",
                inline=True,
            )
            thread_embed.add_field(
                name="👤 上傳者",
                value=f"<@{metadata.get('requester_id')}>",
                inline=True,
            )
            thread_embed.add_field(
                name="🕒 上傳時間",
                value=f"<t:{int(metadata.get('created_at', 0))}:F>",
                inline=True,
            )
            thread_embed.add_field(
                name="⏳ 保留至",
                value=f"<t:{expires_at}:F>  ·  <t:{expires_at}:R>",
                inline=False,
            )
            thread_embed.set_footer(text=f"Tourist File Storage • {file_id[:8]}")
            await thread.send(embed=thread_embed)

            # 20MB 以內先嘗試保留一份 Discord 原檔。
            # Discord 的 bot/API 實際限制可能依伺服器而較低，因此失敗時自動退回自架下載連結。
            if size <= DISCORD_INLINE_FILE_LIMIT:
                try:
                    await thread.send(
                        content="📥 **原檔案：**",
                        file=discord.File(str(file_path), filename=filename),
                    )
                except discord.HTTPException as e:
                    print(f"[Upload] Discord 原檔附件被拒絕 ({filename}): {e}")
                    await thread.send(
                        "📦 Discord 無法直接附加這個原檔，請使用上方下載連結。"
                    )
            else:
                inline_mb = DISCORD_INLINE_FILE_LIMIT // (1024 * 1024)
                await thread.send(
                    f"📦 原檔超過 {inline_mb} MB，不另外附加到 Discord，請使用上方下載連結。"
                )

            # PDF 才做每頁截圖；圖片、影片、其他檔案完全不轉換。
            if filename.lower().endswith(".pdf"):
                await self._preview_local_pdf(thread, file_path, filename)

        except Exception as e:
            print(f"[Upload] Discord 回傳失敗: {e}")

    async def _preview_local_pdf(self, thread, file_path: Path, filename: str):
        """沿用原本 PDF -> PNG 分頁預覽行為。"""
        doc = None
        try:
            doc = fitz.open(str(file_path))

            if len(doc) > 1000:
                await thread.send(
                    f"這份 PDF 有 {len(doc)} 頁，超過 1000 頁限制，不進行分頁預覽。"
                )
                return

            await thread.send(f"📄 總共 {len(doc)} 頁，開始產生 PDF 分頁預覽...")

            batch_files = []
            batch_size = 0
            batch_start_page = 1

            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                img_size = len(img_bytes)

                if len(batch_files) == DISCORD_MAX_ATTACHMENTS_PER_MESSAGE or (batch_size + img_size) > DISCORD_PREVIEW_BATCH_LIMIT:
                    if batch_files:
                        end_page = page_num
                        content_msg = (
                            f"第 {batch_start_page} - {end_page} 頁"
                            if batch_start_page != end_page
                            else f"第 {batch_start_page} 頁"
                        )
                        await thread.send(content=content_msg, files=batch_files)
                        await asyncio.sleep(1)

                    batch_files = []
                    batch_size = 0
                    batch_start_page = page_num + 1

                batch_files.append(
                    discord.File(
                        fp=io.BytesIO(img_bytes),
                        filename=f"page_{page_num + 1}.png",
                    )
                )
                batch_size += img_size

            if batch_files:
                end_page = len(doc)
                content_msg = (
                    f"第 {batch_start_page} - {end_page} 頁"
                    if batch_start_page != end_page
                    else f"第 {batch_start_page} 頁"
                )
                await thread.send(content=content_msg, files=batch_files)

            await thread.send("✅ PDF 分頁預覽完成。")

        except Exception as e:
            print(f"[Upload] PDF 預覽失敗 {filename}: {e}")
            await thread.send("❌ PDF 分頁預覽失敗，但原始下載連結仍可使用。")
        finally:
            if doc is not None:
                doc.close()

    @tasks.loop(minutes=UPLOAD_CLEANUP_INTERVAL_MINUTES)
    async def upload_cleanup(self):
        """固定掃描過期 upload token 與本機檔案。"""
        now = int(time.time())

        # 清除還沒使用、已經過期的上傳頁 token。
        for token, session in list(self.upload_sessions.items()):
            if session["page_expires_at"] <= now:
                self.upload_sessions.pop(token, None)

        if not UPLOAD_ROOT.exists():
            return

        removed = 0
        for file_dir in UPLOAD_ROOT.iterdir():
            if not file_dir.is_dir():
                continue

            metadata_path = file_dir / "metadata.json"
            if not metadata_path.exists():
                continue

            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if int(metadata.get("expires_at", 0)) <= now:
                    shutil.rmtree(file_dir, ignore_errors=True)
                    removed += 1
            except Exception as e:
                print(f"[Upload] cleanup 讀取 metadata 失敗 {file_dir}: {e}")

        if removed:
            print(f"[Upload] cleanup removed {removed} expired file(s)")

    @upload_cleanup.before_loop
    async def before_upload_cleanup(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="上傳檔案", description="產生一個 Tourist 檔案上傳頁面")
    @app_commands.describe(保留時間="檔案與下載連結要保留多久")
    @app_commands.choices(
        保留時間=[
            Choice(name="1 小時", value=3600),
            Choice(name="6 小時", value=3600 * 6),
            Choice(name="1 天", value=3600 * 24),
            Choice(name="3 天", value=3600 * 24 * 3),
            Choice(name="7 天", value=3600 * 24 * 7),
            Choice(name="30 天", value=3600 * 24 * 30),
        ]
    )
    async def upload_file_command(
        self,
        interaction: discord.Interaction,
        保留時間: Choice[int],
    ):
        if not UPLOAD_ENABLED:
            await interaction.response.send_message(
                "📤 檔案上傳功能目前只在 AWS / Linux 主機啟用；這台 Windows 本機不會開上傳網站。",
                ephemeral=True,
            )
            return

        token = uuid.uuid4().hex + uuid.uuid4().hex
        retention_map = {
            3600: "1 小時",
            3600 * 6: "6 小時",
            3600 * 24: "1 天",
            3600 * 24 * 3: "3 天",
            3600 * 24 * 7: "7 天",
            3600 * 24 * 30: "30 天",
        }

        self.upload_sessions[token] = {
            "channel_id": interaction.channel_id,
            "guild_id": interaction.guild_id,
            "requester_id": interaction.user.id,
            "retention_seconds": 保留時間.value,
            "retention_text": retention_map.get(保留時間.value, f"{保留時間.value} 秒"),
            "page_expires_at": int(time.time()) + UPLOAD_PAGE_TOKEN_TTL,
        }

        upload_url = f"{UPLOAD_BASE_URL}/upload/{token}"
        page_expires_at = self.upload_sessions[token]["page_expires_at"]

        # 指令回覆只凸顯「打開上傳頁面」，其他細節不塞在主卡片。
        embed = discord.Embed(
            title="📤 上傳檔案",
            description=f"**[🌐 點我打開上傳頁面]({upload_url})**",
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text=(
                f"一次性連結 • {self.upload_sessions[token]['retention_text']}後自動刪除"
            )
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    # =================================================================

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
        current_rating = res[0].get("rating", "Unrated")
        max_rating = res[-1].get("maxRating", "Unrated")

        if ID == "tourist":
            await interaction.followup.send(f'## {ID}\n- 目前分數：{current_rating}\n- 最高分數：{max_rating} :v:')
        else:
            await interaction.followup.send(f'## {ID}\n- 目前分數：{current_rating}\n- 最高分數：{max_rating}')

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
            subs = requests.get(f"https://codeforces.com/api/contest.status?contestId={contestid}&handle={ID}&from=1&count=500", timeout=5)
            if subs.status_code != 200:
                continue # 抓不到提交紀錄就跳過這個比賽
            res = subs.json().get("result", [])

            # 下面的 rating 也是一樣：
            rating = requests.get(f"https://codeforces.com/api/user.rating?handle={ID}", timeout=5)
            if rating.status_code == 200:
                res = rating.json().get("result", [])
                # 接下來再跑 for j in res: ...
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
        
        try:
            # 加上 timeout 保護
            info_req = requests.get(f"https://codeforces.com/api/user.status?handle={ID}&from=1&count=10", timeout=5)
            if info_req.status_code != 200:
                await interaction.followup.send("查不到這個人捏")
                return
            
            # 只轉一次 json
            info = info_req.json()
            
        except Exception as e:
            await interaction.followup.send("CF API 連線異常，請稍後再試！")
            return

        for p in cf_focus_list:
            if p["ID"] == ID:
                p["remain"] = sec
                await interaction.followup.send(f"繼續關注 **{ID}** 成功，持續{sec//3600}小時")
                return
                
        # 確認有紀錄再加入關注列表與賦值
        if not info.get("result"):
            await interaction.followup.send(f"**{ID}** 還沒有任何提交紀錄，無法關注喔！")
            return
            
        cf_focus_list.append({"ID": ID, "remain": sec, "channel": interaction.channel})
        last_submission_id[ID] = info["result"][0]["id"]
        
        await interaction.followup.send(f"關注 **{ID}** 成功，持續{sec//3600}小時")

    async def cf_focus_update(self, channel: discord.TextChannel, params: dict):
        ID = params["ID"]

        try:
            response = requests.get(f"https://codeforces.com/api/user.status?handle={ID}&from=1&count=10", timeout=5)

            if response.status_code != 200:
                print(f"CF API Error: {response.status_code}")
                return

            info = response.json()
            
            # 確保 API 真的是成功回傳，且有 result 欄位
            if info.get("status") != "OK" or "result" not in info:
                print(f"CF API 狀態異常: {info.get('comment', 'Unknown Error')}")
                return

        except Exception as e:
            print(f"Fetch failed for {ID}: {e}")
            return  

        l = []  
        # 取得實際回傳的筆數，最多 10 筆 (避免總提交次數不到 10 次的人報錯)
        count = min(10, len(info["result"]))
        
        for i in range(count):
            if info["result"][i]["id"] == last_submission_id.get(ID):
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

    @app_commands.command(name="查看空間", description="查看目前下載的音樂佔用了多少空間")
    async def check_storage(self, interaction: discord.Interaction):
        await interaction.response.defer()
        folder = "./downloads"

        if not os.path.exists(folder):
            await interaction.followup.send("目前沒有下載任何檔案 (0 MB)。")
            return

        total_size = 0
        file_count = 0

        # 算出資料夾總大小
        for dirpath, dirnames, filenames in os.walk(folder):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                # 跳過連結檔，只算實體檔案
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
                    file_count += 1

        # 換算單位 (Bytes -> MB -> GB)
        if total_size < 1024 * 1024 * 1024:
            size_str = f"{total_size / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"

        await interaction.followup.send(f"📂 **快取狀態**\n- 檔案數量：{file_count} 首\n- 佔用空間：{size_str}")

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

    @app_commands.command(name="猜拳", description="剪刀石頭布！")
    @app_commands.allowed_installs(guilds=True, users=True) # 允許伺服器與用戶安裝
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def paper_scissors_stone(self, interaction: discord.Interaction):
        await interaction.response.defer()

        m = random.choice(["剪刀 :v: ", "石頭 :fist: ", "布 :raised_hand_with_fingers_splayed: "])
        await interaction.followup.send(f"{m}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return
        
        # ------------------- PDF 擷取與預覽功能 -------------------
        # 判斷訊息中是否包含特定關鍵字
        if "檔案上傳完成" in message.content and "點我直接下載" in message.content:
            filename_match = re.search(r'檔名:\s*`([^`]+)`', message.content)
            url_match = re.search(r'\[點我直接下載\]\((https?://[^\s\)]+)\)', message.content)
            
            if not url_match:
                url_match = re.search(r'(https?://storage\.to/[^\s\)]+)', message.content)

            if filename_match and url_match:
                filename = filename_match.group(1)
                url = url_match.group(1)

                if filename.lower().endswith('.pdf'):
                    try:
                        await message.add_reaction('⏳')

                        # 使用 to_thread 避免下載時卡死整個機器人
                        req = await asyncio.to_thread(requests.get, url, timeout=20)
                        if req.status_code == 200:
                            pdf_bytes = req.content
                            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                            
                            if len(doc) <= 1000:
                                thread_name = filename if len(filename) <= 50 else filename[:47] + "..."
                                thread = await message.create_thread(
                                    name=f"📄 {thread_name} 預覽",
                                    auto_archive_duration=60
                                )
                                
                                await thread.send(f"總共 {len(doc)} 頁，開始轉換為圖片...")
                                
                                # ---------- 新增：小於 10MB 就先傳送 PDF 原檔 ----------
                                if len(pdf_bytes) < 10 * 1024 * 1024:
                                    pdf_file = discord.File(fp=io.BytesIO(pdf_bytes), filename=filename)
                                    await thread.send(content="📥 **提供原檔案下載：**", file=pdf_file)
                                # --------------------------------------------------------

                                # 批次轉換並傳送，讓使用者可以左右滑動 (最多10張/限制10MB)
                                batch_files = []
                                batch_size = 0
                                batch_start_page = 1
                                
                                for page_num in range(len(doc)):
                                    page = doc.load_page(page_num)
                                    pix = page.get_pixmap(dpi=150)
                                    img_bytes = pix.tobytes("png")
                                    img_size = len(img_bytes)
                                    
                                    # 檢查：如果加入這張圖片會超過 10 個檔案，或是總大小超過 9.5MB (留點緩衝給 10MB 限制)
                                    if len(batch_files) == 10 or (batch_size + img_size) > 9.5 * 1024 * 1024:
                                        if batch_files:
                                            # 結算並傳送當前批次
                                            end_page = page_num
                                            content_msg = f"第 {batch_start_page} - {end_page} 頁" if batch_start_page != end_page else f"第 {batch_start_page} 頁"
                                            await thread.send(content=content_msg, files=batch_files)
                                            await asyncio.sleep(1) # 防刷頻冷卻
                                            
                                        # 清空並重置批次，準備裝下一批
                                        batch_files = []
                                        batch_size = 0
                                        batch_start_page = page_num + 1
                                    
                                    # 將當前頁面加入批次
                                    file = discord.File(fp=io.BytesIO(img_bytes), filename=f"page_{page_num + 1}.png")
                                    batch_files.append(file)
                                    batch_size += img_size
                                
                                # 迴圈結束後，把最後剩下還沒傳送的批次傳出去
                                if batch_files:
                                    end_page = len(doc)
                                    content_msg = f"第 {batch_start_page} - {end_page} 頁" if batch_start_page != end_page else f"第 {batch_start_page} 頁"
                                    await thread.send(content=content_msg, files=batch_files)
                                
                                await message.add_reaction('✅')
                            else:
                                print(f"[{filename}] 頁數超過 1000 頁 ({len(doc)} 頁)，不進行轉換。")
                                await message.reply(f"這份 PDF 有 {len(doc)} 頁，超過 1000 頁的限制，太多啦！！")
                                
                            doc.close()
                    except Exception as e:
                        print(f"處理 PDF 發生錯誤: {e}")
                        await message.add_reaction('❌')
        # --------------------------------------------------------

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
