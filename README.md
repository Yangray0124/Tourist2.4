

###### 2024/06/18
**Tourist 1.0 發布**
> Tourist發布


###### 2025/01/11
**Tourist 2.0 發布**
> 新增指令、音樂功能

###### 2025/01/30
**Tourist 2.1 發布**
> 新增循環播放、處理播放一次跳過兩首的bug

###### 2025/02/08
**Tourist 2.2 發布**
> 新增關注功能、隨機推薦題目

> 使用隊列管理cf操作

> 參數list都改為dict

###### 2025/03/07
**Tourist 2.3 發布**
> 新增查看圖片功能，字數達到discord上限自動分割

###### 2025/07/02
> 新增拯救皮卡丘！

###### 2025/07/14
> 修改db，拯救皮卡丘每個頻道獨立！

###### 2025/12/28
**Tourist 2.4 發布**
> 改成2.5-flash-lite 修了一些bug!

###### 2025/12/28
> 增加BrawlStars關注和pdf轉成圖片！

## AI 聊天與頻道記憶（2026/10/14）

- 先進入伺服器的語音頻道，執行 `/加語音`，Tourist 會加入但不播音樂，成功只回覆 `OK`；然後在文字頻道 tag Tourist 即可語音問答，使用 `/離開` 結束。已在同一語音頻道時沿用連線，不自動跨頻道移動。加入時檢查 Connect／Speak、discord.py 版本與 DAVE 依賴，20 秒 timeout、不自動重連；Terminal 的 `[Voice]` 紀錄包含語音狀態、成功／失敗與 WebSocket code。
- 語音套件要求 `discord.py[voice]>=2.7.1,<3`，包含 davey；在 bot 實際 Python 環境執行 `python -m pip install -U -r requirements.txt`，再重啟 bot 同步 slash commands。可用 `python -m discord --version` 檢查語音依賴。`/唱歌` 的 YouTube 搜尋／播放流程依使用者要求暫不處理；反覆進出原因尚未在故障主機確認。

- Tourist 已連線至該伺服器的語音頻道且未播放／暫停音樂時，tag 問答優先使用 `gemini-3.1-flash-live-preview` 依使用者指定語言及活動情境產生語音（可使用英文），思考量設為 `minimal`，成功時只播放語音、不傳送文字轉錄。Live 走獨立 API，不加入原本 7 個文字／摘要模型池；失敗、忙碌或未連線時退回原文字流程。已停用 1% 自動回覆。
- 音色固定為 Fenrir；只能修改 `gemini_live.py` 的 `DEFAULT_LIVE_VOICE` Python 常數並重啟。沒有 /音色 指令、環境變數或資料庫音色覆寫。
- 語音問答只在 tag 時讀同一文字頻道最近 20 則，不收聽使用者麥克風、不自動加入／切換語音頻道。已連線語音模式下，問候、身分詢問等 tag 也交給 Live 回答；原本音樂不會被中斷。
- 更新依賴 `pip install -r requirements.txt`（新增 `google-genai`），主機需要 FFmpeg；Windows 使用 `bin/ffmpeg.exe`，Linux 使用 PATH 的 `ffmpeg`。Live 收取單次完整語音後播放，timeout 60 秒、原始 PCM 上限 8 MiB；輸出用 FFmpeg 編碼為 Opus。需具備語音頻道 Connect／Speak 權限，API key 也須能使用 Live 模型。

- 已停用未 tag 訊息的 1% AI 自動回覆；AI 問答只在 tag 時觸發。原本關鍵字貼圖／表情互動仍保留。

- 每次文字／圖片問答隨機排序 7 個模型：`gemini-2.5-flash`、`gemini-2.5-flash-lite`、`gemini-3-flash-preview`、`gemini-3.5-flash-lite`、`gemini-3.1-flash-lite`、`gemma-4-26b-a4b-it`、`gemma-4-31b-it`。任何 HTTP 錯誤、網路錯誤、逾時或無有效回答時換下一個，各模型每次最多嘗試一次；成功即停止，全部失敗才通知使用者。每次請求 timeout 60 秒。
- Terminal 印出台灣時間（+08:00）、用途（chat／memory）、模型名稱、成功／失敗及 HTTP 狀態或錯誤類別，不印 API key。記憶摘要也使用此模型池，JSON 格式無效時會換模型。文字／圖片問答 temperature 為 1.6。
- 只有 tag Tourist 並進入 AI 問答時，才讀取及保存這次的最近訊息至 `data/chat_memory.sqlite3`；其中包含沒 tag 的人類對話及之前 Tourist 的回覆。不逐則背景保存、不定時讀取歷史。頻道與討論串各自獨立；其他機器人的訊息不納入 AI 記憶。
- 被 tag 且進入 AI 問答流程時，從 Discord 讀取同頻道最近 20 則訊息，按時間排列，最多 24,000 字元；近期附件只提供檔名，當前訊息的第一張圖片會另外傳給模型。
- 同一次 AI 問答觸發該頻道的摘要檢查，保留最新 20 則；較舊的未摘要訊息累積至少 30 則時，每批最多 100 則交給模型整理。摘要只讀取每則前 2,000 字元，但原文會完整用 zlib 壓縮歸檔。模型選出的重要訊息另保留完整原文及檢索索引。摘要失敗時保留原始資料，等下次 tag 時重試。
- 回答前以 SQLite FTS5／BM25 檢索同頻道摘要、重要原文與尚未摘要的原文；中文使用雙字切詞。最多取 6 筆、10,000 字元，與近期內容一併提供給模型，不把整份歷史放進 prompt。這是關鍵字檢索，尚未使用 embedding，對同義詞的匹配能力有限。
- 記憶只累積每次 tag 時取得的最近 20 則快照；兩次 tag 間若超過 20 則，較早訊息可能不會保存。不自動匯入所有既有歷史。資料庫重啟後保留且被 Git 忽略，換機需另行備份／搬移。壓縮歸檔目前作還原備份，不直接參與全文檢索；重要性由模型判斷，可能遺漏。
- 保存的是收到當下的快照，目前不同步 Discord 訊息編輯／刪除，不會自動清除舊資料。僅保存文字與附件檔名，不備份附件檔案。

部署時確保 Developer Portal 的 **Message Content Intent** 已啟用，並授予 Tourist 頻道的 **View Channel** 和 **Read Message History** 權限。程式已使用 `discord.Intents.all()`。摘要也會消耗 Gemini 額度；切換模型不保證避開帳號／專案共用限制。

驗證：`python -m unittest discover -s tests -v`；語法檢查：`python -m py_compile chat_memory.py gemini_live.py cogs/chat.py`。
- 語音也使用同文字頻道的近期 20 則、相關記憶及過往語音回覆；成功播放後把語音轉錄私下保存到 SQLite `voice_replies`，不貼到聊天室。下次 tag 時納入背景，並可檢索較舊語音內容，頻道／討論串仍隔離。這讓英聽、連續問答等活動可延續上一個語音問題；不收聽使用者麥克風，仍以文字 tag 回答或指示。
