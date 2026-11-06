# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

農場經營遊戲：Discord Bot（主要介面）+ CMD（開發維護）+ PostgreSQL（唯一資料來源）+ FastAPI 即時監控／管理網站。程式註解、訊息與文件皆為繁體中文，新程式碼請沿用。

`discordbot.txt` 是移植到其他電腦的安裝指南（需要的軟體、套件版本、步驟與常見問題；改依賴或部署方式時要同步更新）。`discordbot_原始需求.txt` 是 Discord Bot 改造的完整需求；已實作（`app/discord_bot/`、Migration 0010；0011 為作物時間與每日活動，0012 為賭場，0013 為作物價格調整，0014 為山洞冒險，0015 改為一次一趟的探險，0016 為釣魚，0017 讓魚的售價不受稀有度限制，0018 為放生動物，0019 為世界 Boss，0020 為全服世界 Boss 與固定排程（深淵魔龍），0021 讓釣到的魚售價跟隨魚種、不再限 1–200，0022 為世界 Boss 參與門檻 min_points，0023 為農場改名（交易類型 rename_farm），0024 為每日簽到（`daily_checkins`、交易類型 checkin_reward），0025 為圖鑑（`farm_codex`，從歷史補建），0026 為成就與稱號（`farm_achievements`、`farm_titles`、`farms.title`、交易類型 achievement_reward，匯入既有世界 Boss 稱號），0027 為 `farm_stats` 的偷菜／防守／賭場次數（成就進度用），0028 為溫室等級 `farms.greenhouse_level` 與每週特賣限購 `weekly_shop_purchases`，0029 為寵物 `farm_pets`），README「Discord Bot」一節是操作與故障排查文件。
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
- 檔案一律 UTF-8 無 BOM、LF。Windows PowerShell 5.1 的 `Set-Content -Encoding utf8` 會加 BOM、`-Encoding ascii` 會把中文變成「?」，`Invoke-RestMethod` 讀回應會亂碼；改用 Python 或 `[System.IO.File]::WriteAllText` 寫檔。

## 在新電腦上設定（專案由 zip 或 Git 取得時）

需求：Python 3.12+、Docker Desktop（PostgreSQL 16）、Node 18+（只用於前端測試）。

1. `Copy-Item .env.example .env`，填入 `POSTGRES_PASSWORD` 並讓 `DATABASE_URL`／`TEST_DATABASE_URL` 中的密碼一致，`LAVALINK_PASSWORD` 設一組隨機字串；
   `ADMIN_PASSWORD_HASH` 由使用者執行 `python -m app.hash_password` 產生（隱藏輸入，**不要**代替使用者輸入或保存明文密碼；填明文會無法登入）。`.env` 不會隨專案移轉。
   若沿用既有的 `farmgm_farmgm_pgdata` Volume，資料庫密碼必須是當初建立 Volume 時的密碼（可從既有容器的設定取得，不要顯示出來）。
   `.env` 必須有 `COMPOSE_PROJECT_NAME=farmgm`（`.env.example` 沒列）：否則 compose 以資料夾名稱建立另一組專案與**空的** Volume，看不到原本的資料。
2. `docker compose up -d postgres lavalink yt-cipher`：啟動 PostgreSQL、Lavalink 與 YouTube 簽章服務（`LAVALINK_PASSWORD`／`YT_CIPHER_TOKEN` 未設定時 compose 會直接報錯；網站與 Bot 要等 Migration 完成再啟動）。第一次啟動時 `docker/initdb` 會建立 `farmgm` 與 `farmgm_test`（只在 Volume 為空時執行；若測試資料庫不存在需手動 `CREATE DATABASE farmgm_test`）；Lavalink 每次建立容器都會從網路下載插件。
3. `python -m venv .venv` → `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`。
4. `.\.venv\Scripts\python.exe -m alembic upgrade head`（目前 head 為 0029）。
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
.\.venv\Scripts\python.exe scripts/world_boss_stresstest.py 200 global   # 世界 Boss 壓測（global／guild；清空 _test 資料庫，含一致性檢查）
```

- 設定在 `.env`（gitignored）：`DATABASE_URL`、`TEST_DATABASE_URL`（資料庫名稱必須以 `_test` 結尾，測試會清空遊戲資料表）、`ALLOWED_HOSTS`、`ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH`（scrypt 雜湊，不存明文）、`RANKING_SETTLE_INTERVAL_SECONDS`、`BACKGROUND_TASKS_RUNNER`（web／bot／none）、`APP_ENV`、`DB_POOL_SIZE`／`DB_MAX_OVERFLOW`／`DB_POOL_TIMEOUT`、`DISCORD_BOT_TOKEN`、`DISCORD_APPLICATION_ID`、`DISCORD_DEV_GUILD_ID`、`DISCORD_SYNC_COMMANDS`、`BOT_WORKER_THREADS`、`MUSIC_*`、`LAVALINK_URI`／`LAVALINK_PASSWORD`、`SITE_*`。環境變數優先於 `.env`（`app/config.py`，`extra="ignore"`，舊的 `FFMPEG_PATH` 等殘留設定會被忽略）。設定只在程序啟動時讀取，改 `.env` 後要重啟對應程序／容器。
- 程式啟動時只檢查 DB 連線與 Migration 是否最新，**不會**自動建表。改資料結構一律新增 Alembic Migration（`migrations/versions/000N_*.py`，Migration 內寫死常數，不引用會變動的 app 常數），並在測試資料庫做 upgrade → downgrade -1 → upgrade → `alembic check`。
- 修改開發資料庫前先 `pg_dump` 備份（容器 `farmgm-postgres-1`）；不要執行 `docker compose down -v`。

## 正式環境（VPS，2026-11-05 起搬遷）

- Contabo Cloud VPS 4（東京，Ubuntu 22.04，Docker／compose 已內建），專案在 `/opt/farmgm`，由 GitHub `feature/exploration-update` 分支 `git clone`；`.env` 由本機 `scp` 上傳，資料庫以 `pg_dump`／`pg_restore` 從本機搬過去。更新方式：push → VPS `git pull` → 先備份再 `docker compose run --rm web python -m alembic upgrade head` → `docker compose up -d --build --wait`。
- IP `13.140.248.193`，主機時區 Europe/Berlin。SSH 只允許金鑰（`/etc/ssh/sshd_config.d/00-farmgm-hardening.conf`，檔名 00- 才能蓋過 `50-cloud-init.conf` 的 `PasswordAuthentication yes`），另有 fail2ban；`authorized_keys` 有 `b1700-pc`（使用者）與 `farmgm-claude`（Claude 用，本機 `~/.ssh/farmgm_vps`：`ssh -i ~/.ssh/farmgm_vps -o BatchMode=yes root@13.140.248.193 '…'`）。正式環境寫入操作可能被 auto mode 權限檢查擋下，被擋時把指令交給使用者執行，不要繞過。救援用 Contabo VNC（root 密碼，不受 sshd 設定影響）。
- 對外連線走 Cloudflare Tunnel（遠端管理，cloudflared 以 systemd 服務跑在 VPS 主機上，不開任何防火牆 Port；ufw 只允許 OpenSSH）：`dc.kurumicute.com` → `localhost:24444`（介紹網站，公開），`addmin.kurumicute.com`（兩個 d，是使用者設定的實際名稱）→ `localhost:8000`（監控網站，前面有 Cloudflare Access，只允許使用者本人的 Email）。VPS 的 `ALLOWED_HOSTS` 必須包含 admin 網域，否則回「Invalid host header」；改網域時 Cloudflare 與 `ALLOWED_HOSTS` 要一起改，改完 `docker compose up -d --force-recreate web`。
- 資料庫每日備份：`/opt/backup/backup.sh`（`pg_dump -Fc`，保留 14 天），root crontab 每天主機時間 21:00（≈ 台灣 04:00）執行，紀錄在 `/opt/backup/backup.log`；備份只在同一台 VPS 上。
- 同一個 Bot Token 只能有一處執行：2026-11-05 已切換到 VPS（資料無遺漏），本機的 `bot`／`web` 容器已停止，不要在本機重新啟動它們，除非使用者要切回本機；本機 postgres 的資料停在切換當下，不再是最新資料。
- 音樂節點在使用者的 PC：VPS（VMware）會不定時停頓（ping 本機 Docker bridge 偶爾 40ms+，CPU 閒置），在 VPS 播放會卡，所以 Lavalink＋yt-cipher 改在 PC 以 `start_music_node.bat`（compose 專案 `farmgm-music`，同一份 `docker-compose.yml`／`.env`）執行，經 PC 的 Tunnel「Kurumicute」公開成 `https://lavalink.kurumicute.com`（只靠 Lavalink 密碼保護），VPS 的 `.env` 設 `LAVALINK_REMOTE_URI` 讓 compose 的 `LAVALINK_URI` 改連它。PC 關機時只有音樂不能用（watchdog 會自動重連）。PC 上 `farmgm` 與 `farmgm-music` 的 lavalink 都綁 127.0.0.1:2333，不能同時執行。
- 已知風險：Tunnel 到網站是 HTTP，登入 cookie 不帶 `Secure`（`app/api/auth.py` 依 `request.url.scheme`，uvicorn 沒信任 `X-Forwarded-Proto`）；機房 IP 較易被 YouTube 擋，`/play` 可能失敗。

## 架構

分層：`game/`（純規則，無 DB）→ `repositories/`（SQLAlchemy 查詢）→ `services/`（交易邊界）→ `cli/`、`api/` + `web/`（介面）。

- **音樂與介紹網站**：`app/music/player.py` 移植自 YTDiscordBot（播放邏輯沿用原版、模組層級狀態，`setup(bot, MusicConfig)` 註冊 Hybrid Command；ruff 對 `app/music/*` 放寬 E501／UP045／B904 以便對照原專案）。`FarmBot` 是 `commands.Bot`，農場用 `self.farm`（不可用 `self.commands`，那是 discord.py 的屬性）。音訊由 Lavalink 4.2.2（docker compose 的 `lavalink` 服務，設定 `docker/lavalink/application.yml`，插件 youtube-plugin（暫用 snapshot；B 站與 lavabili-plugin 已停止支援，`/play` 收到 B 站連結直接回覆不支援）；YouTube 簽章由 compose 的 `yt-cipher` 服務經 remoteCipher 解析）解析與播放，Bot 用 wavelink 3.5.2 控制，不需要 ffmpeg／yt-dlp；`connect_lavalink()` 在 `setup_hook` 呼叫，歌曲結束靠 `on_wavelink_track_end`（比對 `track.encoded`，忽略 `replaced`）。語音連線一律用 `vc_busy`／`vc_paused` 等輔助函式判斷，Player 的 play／pause／stop 是 async。設定 `MUSIC_BOT_TOKEN` 時音樂改由獨立程序 `python -m app.music` 執行（另一個 Discord 應用程式，兩程序不可共用 Token），農場 Bot 不載入音樂（`bot.music_embedded`）。一鍵操作（種滿、全部澆水／收成／清除）用 `GameService.plant_all` 等單一交易方法。介紹網站 `app/site/`（`python -m app.site`，Port 24444）只輸出彙總統計（`/api/stats`）與手冊用的遊戲設定（`/api/guide`：啟用中的作物／動物、balance 數值、品質突變、啟用中的活動與排名獎勵規則），不含任何玩家資料；頁面上的手冊數值是 API 失敗時的預設值。壓力測試：`scripts/bot_stresstest.py`、`scripts/music_stresstest.py`、`scripts/world_boss_stresstest.py`。
- **Docker 執行**：`Dockerfile`（python:3.13.18-slim-trixie，非 root 使用者 uid 10001，`.dockerignore` 排除 `.env`）建置共用映像 `farmgm-app`，compose 的 `web`／`site`／`bot`／`music`（profile）以 `x-farmgm-app` 共用設定：`env_file: .env`，但 `DATABASE_URL`（由 `POSTGRES_*` 組成，密碼不編碼）、`LAVALINK_URI`、`APP_HOST`／`SITE_HOST`（0.0.0.0）在 `environment` 覆蓋成容器位址；`init: true` 讓 SIGTERM 正常結束；音樂設定存在 Volume `farmgm_data`。主機上的 `.venv` 只用於測試與開發；容器執行中不要從主機再啟動同一個 Bot Token。
- **Discord 面板**（`app/discord_bot/panels.py`）：預設只註冊 `/farm`（私密互動面板）與 `/help`；`FarmPanel` 每頁有 `_load_<page>`（執行緒中呼叫服務）與 `_build_<page>`（建 Embed 與按鈕／選單），操作經 `act()` 執行後在頂端顯示 ✅／❌；換頁與操作在 `DIRECT_REPLY_SECONDS`（1.5 秒）內準備好就用 `response.edit_message` 直接回覆，超過才先 `defer`（`_in_time()`／`_show()`），新的按鈕處理不要再自己先 `defer` 再 `refresh`；`tests/test_discord_panel.py` 以模擬互動點擊。
  總覽／農田／動物／加工／建造／商店／賭場頁與山洞區域（區域頁、探險中、探索結果）附圖：`_load_*` 在執行緒中呼叫 `farm_image`（Pillow，素材與規則在 `app/discord_bot/farm/`，含 `manifest.json` 與 `拼接說明.md`），`_build_*` 用 `attach_image()`，`refresh()` 以 `attachments=self.files` 更新（失敗時退回純文字）。農田與建築圖上傳 64 色（`farm_image.PANEL_COLORS`；預先產生的素材由 `file_png` 轉換後快取，場景圖仍上傳原檔）。`attach_image()` 記錄圖片雜湊，`_attachments()` 在雜湊等於 `sent_digest` 時沿用訊息上既有的附件、不重新上傳（世界 Boss、賭場連續操作靠這個）。圖片是 4×4 農田，所以 `balance.MAX_PLOTS = 16`（擴建與管理員新增土地都檢查）。完整文字指令在 `DISCORD_ADVANCED_COMMANDS=true` 時才註冊。
- **Discord Bot**（`app/discord_bot/`）：`commands.py` 從互動取得 User／Guild ID（不接受玩家輸入的 ID），以 `SocialService.external_context` 對應群組與玩家，再用 `CommandHandler.execute(args)` 重用 CMD 的服務呼叫與文字輸出（參數以清單傳入，不經字串解析）；新增 Discord 指令時沿用此方式，並讓 `tests/test_discord_bot.py::test_every_command_maps_to_services` 通過。服務在 `runner.ServiceRunner` 的執行緒池執行（不可在事件迴圈直接呼叫），`RetryingService` 只重試 40P01／40001；2 秒內未完成先 `defer`。
- **服務繼承鏈**：`GameService` → `RanchService` → `ExplorationService`（山洞探索、機器、假幣）→ `FishingService`（釣魚）→ `SocialService` → `CommunityService` → `RankingService` → `WorldBossService`（世界 Boss）→ `ProgressService`（每日簽到、圖鑑、成就與稱號）→ `AdminService`。子類別的私有方法會覆蓋父類別同名方法，新增前先 `grep "def <名稱>(" app/services/*.py`。CLI 使用 `RankingService`；網站 `app.state.social_service` 是 `AdminService`；`MonitorService` 獨立且所有查詢在 `SET TRANSACTION READ ONLY` 中執行。服務方法只收基本型別，不依賴 CMD／FastAPI／Discord（玩家以 `external_platform`/`external_user_id` 對應，Discord ID 一律字串）。
- **交易與併發**：每個公開服務方法 = 一筆 DB 交易（`self._transaction()`）；改變資產前先以 `farms_repo.get_by_player(..., for_update=True)`（`_locked_farm`）鎖農場列。重複發獎／重複偷菜等由資料庫 UNIQUE／CHECK 約束做最後防線。面板的 `_load_*` 經 `run_batch` 在同一筆交易中執行（`_transaction()` 偵測到共用交易時沿用，其中任何例外都會回滾整批；`RetryingService` 在共用交易中不重試單一方法，改由外層重試整個 `run_batch`）。遊戲規則錯誤皆為 `app.game.errors.GameError` 子類別（訊息可直接給玩家；API 轉成 400／404）。
- **交易紀錄不變式**：所有資產變動都經 `GameService._record()` 寫入 `game_transactions`（含建立農場的初始金幣），因此全部 `gold_delta` 加總 = 全服金幣；`_record()` 同時更新排行用的 `farm_stats`／`farm_daily_stats`（`repositories/farm_stats.py`，不要繞過 `_record` 直接改金幣或販售）；管理員調整用 `admin_adjust`、排名獎勵用 `ranking_reward`。新增事件類型需同時改 `models.EVENT_TYPES`、`rules.EventType`／`EVENT_TYPE_LABELS`、Migration 的 CHECK 約束與 `web/app.js` 的 `EVENT_LABELS`。
- **所屬群組轉移**：`farms.home_group_id` 只能經 `GameService.transfer_home_group`（7 天冷卻、需為目標群組成員）或 `AdminService.admin_transfer_farm`（必填原因，可 `SET LOCAL farmgm.admin_group_transfer = 'on'` 略過冷卻並寫 `admin_actions`）變更；觸發器 `farms_home_group_immutable`（Migration 0010）在資料庫層同樣檢查冷卻，不要移除。
- **任務／競賽計分**：`_record()` 在同一交易中呼叫 `services/activity.on_event`；只有 `context_group_id`（操作所在群組，由 `in_group_context()` 設定）等於農場 `home_group_id`（轉移受冷卻保護，見上）時才計分。
- **時間**：一律注入 `Clock`（測試用 `ManualClock`，起點 2026-01-01 00:00 UTC = 台灣週四 08:00）；DB 存 UTC `timestamptz`，顯示與期間計算用台灣時間（`clock.TAIPEI_TZ`）。土地狀態依時間即時計算（`rules.plot_state`），不靠排程。
- **資料目錄**：作物（含珍稀變種 `variant_code`）與動物／動物產品在 DB（`crop_types`、`animal_types`，有 `active` 停用旗標，代碼不可改、被使用過只能停用）；道具、防守設施、建築、加工配方、加工品在 `game/balance.py`。顯示名稱與合法物品代碼用 `repositories/catalog.py`，不要再寫死作物／動物清單。
- **山洞冒險**：規則與數值只在 `game/exploration.py`（區域、掉落表、事件、機器配方、等級門檻）；服務在 `services/exploration_service.py`。探險一次一趟：`explore` 出發時決定獎勵寫入 `exploration_logs`（`claimed_at` NULL），`duration_seconds` 後 `claim_exploration` 才入背包並寫交易紀錄（部分唯一索引 `uq_exploration_logs_farm_unclaimed`）。機器存在 `farm_tools`（`settings` JSON 存自動加工機的項目），稀有作物種子由 `crop_types.shop_buyable = false` 擋住商店購買，培育器沿用 `craft_orders`（`building_code = rare_incubator`）。自動收割／加工機在面板 `render()` 時經 `run_machines` 執行（沒有機器時不鎖農場）。conftest 會保留並重設 `exploration.RARE_CROPS`。
- **釣魚**：規則與數值只在 `game/fishing.py`（稀有度、售價區間、預設設定與驗證、釣具效果、拉扯博弈 `step()`、模擬策略）；
  服務 `services/fishing_service.py`（`FishingService`，繼承鏈 `ExplorationService` → `FishingService` → `SocialService`）；
  面板頁面在 `discord_bot/fishing_panel.py`（`FishingPages` mixin，不使用圖片）。稀有度機率與基礎成功率是獨立設定
  （`fishing_settings` 版本，拋竿時快照進 `fishing_sessions`）；拉扯操作帶 `turn` 防重複點擊；`farm_fish.session_id` 唯一防重複發魚；
  逾時依時間判定（`_expire`），Bot `setup_hook` 呼叫 `cleanup_fishing_sessions`。釣魚面板會依 `fish_due` 自動更新（測試設
  `panel.fish_timers_enabled = False`）。初始魚種／釣具在 Migration 0016 與 `fishing.INITIAL_FISH`／`INITIAL_GEAR` 各一份（conftest 用後者重設）。
- **世界 Boss**（群組後勤活動）：規則、預設值與戰鬥公式只在 `game/world_boss.py`；服務 `services/world_boss_service.py`
  （`WorldBossService`，繼承鏈 `RankingService` → `WorldBossService` → `ProgressService` → `AdminService`，Bot 與網站都用 `AdminService`）；
  面板 `discord_bot/world_boss_panel.py`（`WorldBossPages` mixin；介紹區 `boss`／`boss_board`／`boss_help` 附同一張固定圖 `farm/scenes/world_boss.png`，戰鬥區 `boss_battle`／`boss_donate` 不附圖，由根目錄 `dragon.png` 縮小，`dragon.png` 不進映像）。活動、補給、捐獻、領獎在 Migration 0019 的
  `world_boss_*` 四張表，設定與獎勵是建立時的 JSON 快照。HP 依時間推進（`_advance`，寫回 `hp`／`last_tick_at`）；監控網站
  用 `event_view` 唯讀推算。玩家操作一律以互動來源的 `group_id` 比對活動群組並檢查成員；先鎖活動列再鎖農場列。
  捐獻帶面板產生的 `request_id`（`(event, player, request_id)` 唯一），領獎 `(event, player)` 唯一；捐出的魚設 `farm_fish.donated_at`
  （魚簍查詢用 `fishing_repo.in_basket()`）。`boss_reward` 金幣列入 `ranking.NET_GOLD_EXCLUDED`。服務的私有方法名稱要避開
  父類別（`FishingService` 已有 `_finish`，世界 Boss 用 `_boss_finish`）。
  `group_id` NULL = 全服活動（任何伺服器或私訊都看得到，`_scope()`）；手動活動每個範圍最多一場未結束，排程活動每個排程最多一場。
  固定出現的 Boss 在 `world_boss_schedules`（Migration 0020 寫入「深淵魔龍」：全服、每 2 天 21:00 台灣時間、3 小時、HP 15,000，
  數值與 `world_boss.ABYSS_DRAGON` 相同）；`_spawn_due` 在上一場結束後立即建立下一場（`slot_at`，`(schedule_id, slot_at)` 唯一），
  由背景結算與 `boss_current` 呼叫，出現前管理員可個別修改該場的時間與獎勵。介面不顯示等級（欄位保留，預設 1）。
  查看類方法（`boss_current`、`boss_leaderboard`、`boss_donation_options`）只讀取不鎖（`_member_event(lock=False)`，HP 由
  `event_view` 推算）；只有捐獻、領獎、管理與背景結算鎖活動列並 `_advance` 寫回。`FarmPanel.refresh`／`show_error` 從重繪到送出
  畫面持有面板的鎖（`_lock()`，依事件迴圈建立），連點兩下不會交錯重繪。
- **每日簽到**：規則與獎勵只在 `game/checkin.py`（台灣日期、7 天一輪、連續天數中斷歸 1）；服務 `services/progress_service.py`
  （`ProgressService.daily_checkin`，`(farm_id, day)` 唯一，先不鎖檢查、要發獎時才鎖農場並再確認；道具放不進倉庫改發等值金幣）。
  面板 `_render` 在 `_run_machines` 後呼叫 `_auto_checkin`（開啟或換頁就簽到，結果顯示在頂端；台灣隔天 00:00 前不再查詢），
  連續天數顯示在「🏅 成就圖鑑」（總覽不顯示）。`checkin_reward` 列入 `ranking.NET_GOLD_EXCLUDED`。測試預設關閉（conftest 的 `no_auto_checkin`），
  簽到測試以 `monkeypatch.setattr(FarmPanel, "auto_checkin", True)` 開啟。
- **圖鑑／成就／稱號**：圖鑑 `farm_codex` 由 `_record()` 在 `codex_repo.CODEX_EVENTS`（收成、偷菜、收集動物產品、領取加工品）時 upsert，
  山洞素材在 `claim_exploration` 登記，魚類用 `farm_fish`。成就定義只在 `game/achievements.py`（代碼上線後不可改），進度由
  `ProgressService._achievement_metrics` 從既有資料即時計算（含歷史；每個查詢都走農場／玩家索引，偷菜／防守／賭場次數讀 `farm_stats` 的
  `*_count` 欄位，由 `farm_stats.on_transaction` 遞增、`rebuild`／`check` 一併重算比對，不要改回掃描交易紀錄），只有已領取存 `farm_achievements`；簽到與成就獎勵共用
  `_grant_reward`（道具放不進倉庫改發等值金幣）。稱號存在 `farm_titles`（成就與世界 Boss 領獎時 `titles_repo.grant`），
  `farms.title` 是選用的稱號，顯示在總覽、排行榜與世界 Boss 排行（`player_titles`）。面板頁面在 `discord_bot/progress_panel.py`
  （`ProgressPages` mixin：progress／achievements／titles／codex），總覽「🏅 成就圖鑑」進入。
- **特賣與升級**（商店第三個分頁 `shop_special`，`discord_bot/special_shop_panel.py` 的 `SpecialShopPages`）：規則只在
  `game/special_shop.py`。溫室 `farms.greenhouse_level` 由 `GameService.upgrade_greenhouse` 升級，`_water` 把
  `greenhouse_pct` 與限時活動的 `grow_reduction_pct` 相加；每週特賣依台灣週一決定商品（`weekly_offers`，不存資料庫），
  限購量存 `weekly_shop_purchases`，購買記 `buy_seed`；稱號商店 `buy_title` 記 `buy_item`、`titles_repo.grant(source="shop")`，
  商店稱號不可和成就稱號重複（`progress_service` 匯入時檢查）。化肥 `FERTILIZER_TIME_FACTOR` 0.85、蚯蚓 ×0.85（2026-11-06 調降），
  說明文字由常數產生。世界 Boss 介紹區「上一場排行榜」用 `boss_last_leaderboard`（同排程或同範圍的最近一場已結束活動），
  戰鬥區「目前排行」（`boss_live_board`）用 `boss_leaderboard`。
- **寵物（貓、狗）**：規則只在 `game/pets.py`（領養 Lv0、升到 Lv4，每級 5%）；`farm_pets` 存等級，服務在
  `RanchService.pet_status`／`buy_pet`／`upgrade_pet`（記 `buy_item`，item_code `pet_cat`／`pet_dog`）。貓在
  `_harvest_locked` 加收成數量（`_pet_rng` 決定小數部分）；狗在 `steal_crop` 的設施判定沒擋住時再擲一次 `_rng`，
  擋下時 `blocked_by = 'pet_dog'`，顯示名稱一律用 `pets.blocker_name()`（不要直接查 `balance.DEFENSES`）。
  `FarmStatus.pets` 讓 `farm_image.farm_png` 在農田圖上方加草地帶（`manifest.json` 的 `pets`，素材 `farm/pets/` 由根目錄
  `dogcat.png` 切出，`dogcat.png` 不進映像）。面板寵物頁 `pets` 從動物頁進入。
- **品質／突變與限時活動**：公式只在 `game/genetics.py`（設定版本存在 `crop_genetics_settings`）與 `game/events.py`（`game_events`，加成在播種／澆水／販售當下決定並記在 `applied_events`）。
- **排名獎勵**：`game/ranking.py`（期間、驗證）+ `services/ranking_service.py`；背景結算 `run_background_settlement`（advisory lock，排名＋競賽；`WorldBossService` 覆寫後另外結束到期的世界 Boss 並建立排程的下一場，回傳多 `world_boss`／`world_boss_spawned`）由 `BACKGROUND_TASKS_RUNNER` 指定的網站（`main._auto_settle`，注入 `session_factory` 的測試中不啟動）或 Bot 執行，CLI／Bot 的 `leaderboard groups|rewards` 查詢時補結算，`(rule_id, period_start)` 唯一保證只發一次。
  項目 `money` 在有期間時（本日／本週／本月與所有排名獎勵）= 期間內**作物**販售收入（使用者要求：買東西不能影響排名；動物產品、加工品、魚與獎勵不算），只有「全部期間」才是目前金幣；`net_gold`／`NET_GOLD_EXCLUDED` 仍會計算與回傳，但不再是排名分數。
- **排行榜**：`repositories/leaderboard.py` 全部期間讀 `farm_stats`、台灣日期對齊的期間讀 `farm_daily_stats`，只有非標準期間才回退掃描交易紀錄；`tests/test_farm_stats.py` 驗證與直接加總交易紀錄的結果完全相同。
- **網站安全**：`/api/monitor/*` 唯讀；寫入只在 `/api/admin/*`，需管理員登入（HttpOnly cookie，`app/auth.py`，記憶體 session）、`X-Farmgm-Admin: 1` 標頭與同源 Origin；`TrustedHostMiddleware` 只接受本機 Host。前端 `app.js` 一律用 `el()`／`textContent` 建 DOM（不使用 innerHTML）。WebSocket 每數秒推快照（所有連線共用 `SnapshotCache`，前端分頁在背景 30 秒後 `pause()`）；分頁資料只更新目前分頁，且使用者操作中暫停（`userBusy()`），避免表單被重建。
- **管理網站前端**（`app/web/`，原生 JS，無建置步驟）：CSP 為 `default-src 'self'`，不能用 inline script／style 或外部資源（圖示是 `index.html` 內的 SVG sprite `<use href="#i-…">`；JS 設 `element.style` 可以）。側邊欄分頁由 `TAB_META`（標題／說明）、`data-tab`／`data-tab-panel` 與 `TAB_LOADERS`（每頁要載入的函式）三處對應，新增分頁三處都要改；舊網址 `#manage` 由 `TAB_ALIASES` 導向。管理操作一律經 `adminRequest()`（自動把最近點擊的按鈕設為送出中，防重複提交；401 會切回未登入並提示）；重要／危險操作用 `confirmDialog()`（可帶調整前後 `diff` 與必填原因），不要用 `window.confirm／prompt`；結果用 `toast()`／`setNote()`，不要用會被下一次快照清掉的 `showError()`。表單 `label.req` 標示必填，送出前呼叫 `validateForm()`；`details.form-box` 有輸入就標為 dirty（關閉頁面會提醒），成功或重設後 `clearDirty()`。`emptyRow()` 以訊息含「失敗／無法取得」判斷錯誤樣式，錯誤訊息要沿用這兩個用詞。`tests/test_monitor_api.py` 會檢查首頁含「農場即時監控」。
- **介紹網站前端**（`app/site/static/`）：夜空銀河主題，背景是 `site.js` 的 canvas（視差、流星；尊重 prefers-reduced-motion）。手冊表格由 `/api/guide` 填入，`index.html` 裡的數值只是 API 失敗時的預設值（會顯示警告）；音樂說明的前綴簡寫、歌單上限等由 `/api/info` 的 `music` 填入。API 請求用 `cache: "no-store"`。CSP 只額外允許 Google Fonts。`tests/test_site.py` 檢查頁面含 `/farm`、`/play`、`id="guide"` 與「農場操作手冊」。

## 測試慣例

`tests/conftest.py` 每個測試前 TRUNCATE `GAME_TABLES`（含 `farm_stats`、`farm_daily_stats`）、刪除測試新增的作物／動物種類並重設初始值、把作物生長時間設為短時間（60/180/300/600 秒）、品質突變設為 100% 普通無突變。新增資料表時記得加入 `GAME_TABLES`。fixture：`service`（`RankingService`）、`clock`、`player_id`、`home_group`、`make_farmer()`、`grow()`；管理功能測試用 `AdminService(session_factory, clock)`。Bot 測試（`tests/test_discord_bot.py`）用 `FakeInteraction` 模擬互動並直接呼叫指令的 `callback`，沒有 pytest-asyncio，以 `asyncio.run` 執行。API 測試以 `create_app(session_factory, clock=..., allowed_hosts=["testserver"], admin_auth=AdminAuth(...))` 建立。音樂測試（`tests/test_music.py`）不連 Lavalink：`FakePlayer` 模擬 `wavelink.Player` 的 async 介面，`skip()`／`stop()`／`finish()` 會像 Lavalink 一樣送出 `on_wavelink_track_end`；解析以 monkeypatch `music.lavalink_search` 取代。
