# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

農場經營遊戲：Discord Bot（主要介面）+ CMD（開發維護）+ PostgreSQL（唯一資料來源）+ FastAPI 即時監控／管理網站。程式註解、訊息與文件皆為繁體中文，新程式碼請沿用。

`discordbot.txt` 是移植到其他電腦的安裝指南（需要的軟體、套件版本、步驟與常見問題；改依賴或部署方式時要同步更新）。`discordbot_原始需求.txt` 是 Discord Bot 改造的完整需求；已實作（`app/discord_bot/`、Migration 0010；0011 為作物時間與每日活動，0012 為賭場），README「Discord Bot」一節是操作與故障排查文件。
尚未在真實 Discord 驗證（需要使用者提供 Bot Token 與測試伺服器），包括 Lavalink 實際語音播放與 DAVE 語音加密；舊玩家清除腳本尚未對開發資料庫執行。

## 協作規範（使用者的固定要求）

- 一律使用繁體中文回覆；指令或需求可能有錯，發現時要指出並說明原因。
- 先看相關程式碼再改，採最小修改；沿用現有架構與風格，不重構無關程式；不猜測 API、檔案或設定。
- 不宣稱未實際執行的測試已通過；回覆時簡要列出修改內容、實際測試結果與尚存的重要風險。
- 刪除資料、移除 Volume、不可逆 Migration 或修改正式環境前，先說明影響並取得使用者確認；修改開發資料庫前先備份。
- 密碼、Token、API Key 不寫入原始碼、設定範例或日誌（`.env` 已 gitignore；管理員密碼只存 scrypt 雜湊）。
- 後端負責授權與驗證（防 IDOR、參數化查詢）；前端防 XSS（只用 `textContent`）。
- 需要瀏覽器驗證時，用另一個 Port（例如 8010；介紹網站用 24445）啟動自己的伺服器（管理網站對測試資料庫：`DATABASE_URL` 設成 `TEST_DATABASE_URL`），**不要關閉使用者在 8000／24444 Port 的伺服器**（現在是 compose 的 `web`／`site` 容器，也不要停止 `bot` 容器）。
  Playwright 不在專案依賴中：需要時在暫存目錄另建 venv 安裝，以 `chromium.launch(channel="msedge")` 使用本機 Edge。
  CSP 禁止 `eval`，Playwright 的 `wait_for_function("字串")` 會失敗，改在 Python 端輪詢。

## 在新電腦上設定（專案由 zip 或 Git 取得時）

需求：Python 3.12+、Docker Desktop（PostgreSQL 16）、Node 18+（只用於前端測試）。

1. `Copy-Item .env.example .env`，填入 `POSTGRES_PASSWORD` 並讓 `DATABASE_URL`／`TEST_DATABASE_URL` 中的密碼一致，`LAVALINK_PASSWORD` 設一組隨機字串；
   `ADMIN_PASSWORD_HASH` 由使用者執行 `python -m app.hash_password` 產生（隱藏輸入，**不要**代替使用者輸入或保存明文密碼；填明文會無法登入）。`.env` 不會隨專案移轉。
   若沿用既有的 `farmgm_farmgm_pgdata` Volume，資料庫密碼必須是當初建立 Volume 時的密碼（可從既有容器的設定取得，不要顯示出來）。
2. `docker compose up -d postgres lavalink yt-cipher`：啟動 PostgreSQL、Lavalink 與 YouTube 簽章服務（`LAVALINK_PASSWORD`／`YT_CIPHER_TOKEN` 未設定時 compose 會直接報錯；網站與 Bot 要等 Migration 完成再啟動）。第一次啟動時 `docker/initdb` 會建立 `farmgm` 與 `farmgm_test`（只在 Volume 為空時執行；若測試資料庫不存在需手動 `CREATE DATABASE farmgm_test`）；Lavalink 每次建立容器都會從網路下載插件。
3. `python -m venv .venv` → `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`。
4. `.\.venv\Scripts\python.exe -m alembic upgrade head`（目前 head 為 0012）。
5. 驗證：`pytest`、`ruff check app tests migrations scripts`、`node --test tests/web/connection.test.js` 都應通過。
6. 若要搬移舊電腦的資料（PowerShell 的 `>` 會破壞二進位檔，一律在容器內產生檔案再 `docker cp`）：
   - 舊電腦：`docker exec farmgm-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -f /tmp/farmgm.dump farmgm'`，再 `docker cp farmgm-postgres-1:/tmp/farmgm.dump .`
   - 新電腦（完成步驟 2 後、先確認目標資料庫，會覆蓋資料）：`docker cp farmgm.dump farmgm-postgres-1:/tmp/farmgm.dump`，
     `docker exec farmgm-postgres-1 sh -c 'pg_restore -U "$POSTGRES_USER" --clean --if-exists -d farmgm /tmp/farmgm.dump'`，
     再執行 `alembic upgrade head` 確認版本。
   - 容器名稱 `farmgm-postgres-1` 取決於專案資料夾名稱（`docker compose ps` 可查）。
   - 舊電腦的開發資料只有測試用玩家；Discord 改造時本來就會清除，通常不需要搬。

## 常用指令（Windows，虛擬環境在 `.venv`）

```powershell
docker compose up -d --build --wait      # 全部在 Linux 容器：PostgreSQL 16（127.0.0.1:5434）、Lavalink 4.2.2（127.0.0.1:2333）、yt-cipher、
                                          # web（8000）、site（24444）、bot；start.bat 同等；--profile music 另啟獨立音樂 Bot
docker compose run --rm web python -m alembic upgrade head   # 在容器內升級 Migration（先備份）
docker compose logs lavalink              # 確認 Loaded 'youtube-plugin…' 與 Lavalink is ready
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.cli     # CMD 遊戲
.\.venv\Scripts\python.exe -m app.main    # 監控網站 http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m app.discord_bot   # Discord Bot：農場＋音樂（需 DISCORD_BOT_TOKEN；音樂需 docker compose 的 lavalink 服務）
.\.venv\Scripts\python.exe -m app.site          # 介紹網站 http://127.0.0.1:24444
.\.venv\Scripts\python.exe scripts/farm_stats.py check   # 排行統計表與交易紀錄一致性（rebuild 重算）

.\.venv\Scripts\python.exe -m pytest                                   # 全部測試（pyproject 預設 -q，看不到總結時加 -o addopts=""）
.\.venv\Scripts\python.exe -m pytest tests/test_admin.py::test_set_gold_and_level
.\.venv\Scripts\python.exe -m ruff check app tests migrations scripts  # line-length 120；不要對整個專案跑 ruff format
node --check app/web/app.js
node --test tests/web/connection.test.js                               # 指定檔案，不要傳目錄
.\.venv\Scripts\python.exe -m alembic check                            # 確認 Model 與 Migration 一致
.\.venv\Scripts\python.exe -m app.hash_password                        # 產生 ADMIN_PASSWORD_HASH
.\.venv\Scripts\python.exe scripts/loadtest.py 1000000                 # 500 玩家負載測試（只對 _test 資料庫，會清空它）
.\.venv\Scripts\python.exe scripts/bot_stresstest.py 200 300           # 面板壓測：玩家數、同時播音樂的伺服器數（清空 _test 資料庫）
.\.venv\Scripts\python.exe scripts/music_stresstest.py --no-network    # 音樂 Bot 端壓測；不加 --no-network 會真的查詢 YouTube，總數要壓低
```

- 設定在 `.env`（gitignored）：`DATABASE_URL`、`TEST_DATABASE_URL`（資料庫名稱必須以 `_test` 結尾，測試會清空遊戲資料表）、`ALLOWED_HOSTS`、`ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH`（scrypt 雜湊，不存明文）、`RANKING_SETTLE_INTERVAL_SECONDS`、`BACKGROUND_TASKS_RUNNER`（web／bot／none）、`APP_ENV`、`DB_POOL_SIZE`／`DB_MAX_OVERFLOW`／`DB_POOL_TIMEOUT`、`DISCORD_BOT_TOKEN`、`DISCORD_APPLICATION_ID`、`DISCORD_DEV_GUILD_ID`、`DISCORD_SYNC_COMMANDS`、`BOT_WORKER_THREADS`、`MUSIC_*`、`LAVALINK_URI`／`LAVALINK_PASSWORD`、`SITE_*`。環境變數優先於 `.env`（`app/config.py`，`extra="ignore"`，舊的 `FFMPEG_PATH` 等殘留設定會被忽略）。設定只在程序啟動時讀取，改 `.env` 後要重啟對應程序／容器。
- 程式啟動時只檢查 DB 連線與 Migration 是否最新，**不會**自動建表。改資料結構一律新增 Alembic Migration（`migrations/versions/000N_*.py`，Migration 內寫死常數，不引用會變動的 app 常數），並在測試資料庫做 upgrade → downgrade -1 → upgrade → `alembic check`。
- 修改開發資料庫前先 `pg_dump` 備份（容器 `farmgm-postgres-1`）；不要執行 `docker compose down -v`。

## 架構

分層：`game/`（純規則，無 DB）→ `repositories/`（SQLAlchemy 查詢）→ `services/`（交易邊界）→ `cli/`、`api/` + `web/`（介面）。

- **音樂與介紹網站**：`app/music/player.py` 移植自 YTDiscordBot（播放邏輯沿用原版、模組層級狀態，`setup(bot, MusicConfig)` 註冊 Hybrid Command；ruff 對 `app/music/*` 放寬 E501／UP045／B904 以便對照原專案）。`FarmBot` 是 `commands.Bot`，農場用 `self.farm`（不可用 `self.commands`，那是 discord.py 的屬性）。音訊由 Lavalink 4.2.2（docker compose 的 `lavalink` 服務，設定 `docker/lavalink/application.yml`，插件 youtube-plugin（暫用 snapshot；B 站與 lavabili-plugin 已停止支援，`/play` 收到 B 站連結直接回覆不支援）；YouTube 簽章由 compose 的 `yt-cipher` 服務經 remoteCipher 解析）解析與播放，Bot 用 wavelink 3.5.2 控制，不需要 ffmpeg／yt-dlp；`connect_lavalink()` 在 `setup_hook` 呼叫，歌曲結束靠 `on_wavelink_track_end`（比對 `track.encoded`，忽略 `replaced`）。語音連線一律用 `vc_busy`／`vc_paused` 等輔助函式判斷，Player 的 play／pause／stop 是 async。設定 `MUSIC_BOT_TOKEN` 時音樂改由獨立程序 `python -m app.music` 執行（另一個 Discord 應用程式，兩程序不可共用 Token），農場 Bot 不載入音樂（`bot.music_embedded`）。一鍵操作（種滿、全部澆水／收成／清除）用 `GameService.plant_all` 等單一交易方法。介紹網站 `app/site/`（`python -m app.site`，Port 24444）只輸出彙總統計（`/api/stats`）與手冊用的遊戲設定（`/api/guide`：啟用中的作物／動物、balance 數值、品質突變、啟用中的活動），不含任何玩家資料；頁面上的手冊數值是 API 失敗時的預設值。壓力測試：`scripts/bot_stresstest.py`、`scripts/music_stresstest.py`。
- **Docker 執行**：`Dockerfile`（python:3.13.18-slim-trixie，非 root 使用者 uid 10001，`.dockerignore` 排除 `.env`）建置共用映像 `farmgm-app`，compose 的 `web`／`site`／`bot`／`music`（profile）以 `x-farmgm-app` 共用設定：`env_file: .env`，但 `DATABASE_URL`（由 `POSTGRES_*` 組成，密碼不編碼）、`LAVALINK_URI`、`APP_HOST`／`SITE_HOST`（0.0.0.0）在 `environment` 覆蓋成容器位址；`init: true` 讓 SIGTERM 正常結束；音樂設定存在 Volume `farmgm_data`。主機上的 `.venv` 只用於測試與開發；容器執行中不要從主機再啟動同一個 Bot Token。
- **Discord 面板**（`app/discord_bot/panels.py`）：預設只註冊 `/farm`（私密互動面板）與 `/help`；`FarmPanel` 每頁有 `_load_<page>`（執行緒中呼叫服務）與 `_build_<page>`（建 Embed 與按鈕／選單），操作經 `act()` 執行後在頂端顯示 ✅／❌；`tests/test_discord_panel.py` 以模擬互動點擊。
  總覽／農田／動物／加工／建造／商店頁附圖：`_load_*` 在執行緒中呼叫 `farm_image`（Pillow，素材與規則在 `app/discord_bot/farm/`，含 `manifest.json` 與 `拼接說明.md`），`_build_*` 用 `attach_image()`，`refresh()` 以 `attachments=self.files` 更新（失敗時退回純文字）。圖片是 4×4 農田，所以 `balance.MAX_PLOTS = 16`（擴建與管理員新增土地都檢查）。完整文字指令在 `DISCORD_ADVANCED_COMMANDS=true` 時才註冊。
- **Discord Bot**（`app/discord_bot/`）：`commands.py` 從互動取得 User／Guild ID（不接受玩家輸入的 ID），以 `SocialService.external_context` 對應群組與玩家，再用 `CommandHandler.execute(args)` 重用 CMD 的服務呼叫與文字輸出（參數以清單傳入，不經字串解析）；新增 Discord 指令時沿用此方式，並讓 `tests/test_discord_bot.py::test_every_command_maps_to_services` 通過。服務在 `runner.ServiceRunner` 的執行緒池執行（不可在事件迴圈直接呼叫），`RetryingService` 只重試 40P01／40001；2 秒內未完成先 `defer`。
- **服務繼承鏈**：`GameService` → `RanchService` → `SocialService` → `CommunityService` → `RankingService` → `AdminService`。CLI 使用 `RankingService`；網站 `app.state.social_service` 是 `AdminService`；`MonitorService` 獨立且所有查詢在 `SET TRANSACTION READ ONLY` 中執行。服務方法只收基本型別，不依賴 CMD／FastAPI／Discord（玩家以 `external_platform`/`external_user_id` 對應，Discord ID 一律字串）。
- **交易與併發**：每個公開服務方法 = 一筆 DB 交易（`self._transaction()`）；改變資產前先以 `farms_repo.get_by_player(..., for_update=True)`（`_locked_farm`）鎖農場列。重複發獎／重複偷菜等由資料庫 UNIQUE／CHECK 約束做最後防線。遊戲規則錯誤皆為 `app.game.errors.GameError` 子類別（訊息可直接給玩家；API 轉成 400／404）。
- **交易紀錄不變式**：所有資產變動都經 `GameService._record()` 寫入 `game_transactions`（含建立農場的初始金幣），因此全部 `gold_delta` 加總 = 全服金幣；`_record()` 同時更新排行用的 `farm_stats`／`farm_daily_stats`（`repositories/farm_stats.py`，不要繞過 `_record` 直接改金幣或販售）；管理員調整用 `admin_adjust`、排名獎勵用 `ranking_reward`。新增事件類型需同時改 `models.EVENT_TYPES`、`rules.EventType`／`EVENT_TYPE_LABELS`、Migration 的 CHECK 約束與 `web/app.js` 的 `EVENT_LABELS`。
- **所屬群組轉移**：`farms.home_group_id` 只能經 `GameService.transfer_home_group`（7 天冷卻、需為目標群組成員）或 `AdminService.admin_transfer_farm`（必填原因，可 `SET LOCAL farmgm.admin_group_transfer = 'on'` 略過冷卻並寫 `admin_actions`）變更；觸發器 `farms_home_group_immutable`（Migration 0010）在資料庫層同樣檢查冷卻，不要移除。
- **任務／競賽計分**：`_record()` 在同一交易中呼叫 `services/activity.on_event`；只有 `context_group_id`（操作所在群組，由 `in_group_context()` 設定）等於農場 `home_group_id`（轉移受冷卻保護，見上）時才計分。
- **時間**：一律注入 `Clock`（測試用 `ManualClock`，起點 2026-01-01 00:00 UTC = 台灣週四 08:00）；DB 存 UTC `timestamptz`，顯示與期間計算用台灣時間（`clock.TAIPEI_TZ`）。土地狀態依時間即時計算（`rules.plot_state`），不靠排程。
- **資料目錄**：作物（含珍稀變種 `variant_code`）與動物／動物產品在 DB（`crop_types`、`animal_types`，有 `active` 停用旗標，代碼不可改、被使用過只能停用）；道具、防守設施、建築、加工配方、加工品在 `game/balance.py`。顯示名稱與合法物品代碼用 `repositories/catalog.py`，不要再寫死作物／動物清單。
- **品質／突變與限時活動**：公式只在 `game/genetics.py`（設定版本存在 `crop_genetics_settings`）與 `game/events.py`（`game_events`，加成在播種／澆水／販售當下決定並記在 `applied_events`）。
- **排名獎勵**：`game/ranking.py`（期間、驗證）+ `services/ranking_service.py`；背景結算 `run_background_settlement`（advisory lock，排名＋競賽）由 `BACKGROUND_TASKS_RUNNER` 指定的網站（`main._auto_settle`，注入 `session_factory` 的測試中不啟動）或 Bot 執行，CLI／Bot 的 `leaderboard groups|rewards` 查詢時補結算，`(rule_id, period_start)` 唯一保證只發一次。
- **排行榜**：`repositories/leaderboard.py` 全部期間讀 `farm_stats`、台灣日期對齊的期間讀 `farm_daily_stats`，只有非標準期間才回退掃描交易紀錄；`tests/test_farm_stats.py` 驗證與直接加總交易紀錄的結果完全相同。
- **網站安全**：`/api/monitor/*` 唯讀；寫入只在 `/api/admin/*`，需管理員登入（HttpOnly cookie，`app/auth.py`，記憶體 session）、`X-Farmgm-Admin: 1` 標頭與同源 Origin；`TrustedHostMiddleware` 只接受本機 Host。前端 `app.js` 一律用 `el()`／`textContent` 建 DOM（不使用 innerHTML）。WebSocket 每數秒推快照；分頁資料只更新目前分頁，且使用者操作中暫停（`userBusy()`），避免表單被重建。
- **管理網站前端**（`app/web/`，原生 JS，無建置步驟）：CSP 為 `default-src 'self'`，不能用 inline script／style 或外部資源（圖示是 `index.html` 內的 SVG sprite `<use href="#i-…">`；JS 設 `element.style` 可以）。側邊欄分頁由 `TAB_META`（標題／說明）、`data-tab`／`data-tab-panel` 與 `TAB_LOADERS`（每頁要載入的函式）三處對應，新增分頁三處都要改；舊網址 `#manage` 由 `TAB_ALIASES` 導向。管理操作一律經 `adminRequest()`（自動把最近點擊的按鈕設為送出中，防重複提交；401 會切回未登入並提示）；重要／危險操作用 `confirmDialog()`（可帶調整前後 `diff` 與必填原因），不要用 `window.confirm／prompt`；結果用 `toast()`／`setNote()`，不要用會被下一次快照清掉的 `showError()`。表單 `label.req` 標示必填，送出前呼叫 `validateForm()`；`details.form-box` 有輸入就標為 dirty（關閉頁面會提醒），成功或重設後 `clearDirty()`。`emptyRow()` 以訊息含「失敗／無法取得」判斷錯誤樣式，錯誤訊息要沿用這兩個用詞。`tests/test_monitor_api.py` 會檢查首頁含「農場即時監控」。
- **介紹網站前端**（`app/site/static/`）：夜空銀河主題，背景是 `site.js` 的 canvas（視差、流星；尊重 prefers-reduced-motion）。手冊表格由 `/api/guide` 填入，`index.html` 裡的數值只是 API 失敗時的預設值（會顯示警告）；音樂說明的前綴簡寫、歌單上限等由 `/api/info` 的 `music` 填入。API 請求用 `cache: "no-store"`。CSP 只額外允許 Google Fonts。`tests/test_site.py` 檢查頁面含 `/farm`、`/play`、`id="guide"` 與「農場操作手冊」。

## 測試慣例

`tests/conftest.py` 每個測試前 TRUNCATE `GAME_TABLES`（含 `farm_stats`、`farm_daily_stats`）、刪除測試新增的作物／動物種類並重設初始值、把作物生長時間設為短時間（60/180/300/600 秒）、品質突變設為 100% 普通無突變。新增資料表時記得加入 `GAME_TABLES`。fixture：`service`（`RankingService`）、`clock`、`player_id`、`home_group`、`make_farmer()`、`grow()`；管理功能測試用 `AdminService(session_factory, clock)`。Bot 測試（`tests/test_discord_bot.py`）用 `FakeInteraction` 模擬互動並直接呼叫指令的 `callback`，沒有 pytest-asyncio，以 `asyncio.run` 執行。API 測試以 `create_app(session_factory, clock=..., allowed_hosts=["testserver"], admin_auth=AdminAuth(...))` 建立。音樂測試（`tests/test_music.py`）不連 Lavalink：`FakePlayer` 模擬 `wavelink.Player` 的 async 介面，`skip()`／`stop()`／`finish()` 會像 Lavalink 一樣送出 `on_wavelink_track_end`；解析以 monkeypatch `music.lavalink_search` 取代。
