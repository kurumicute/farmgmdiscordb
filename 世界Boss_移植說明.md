# 世界 Boss 功能：移植說明（給人與 Claude Code 看）

> 建立日期：2026-11-04　｜　Migration：0019、0020（head = **0020**）　｜　完整測試：516 passed
>
> 這份文件列出「世界 Boss（群組後勤活動）」功能新增與修改的**全部檔案**、資料庫變更、移植步驟、驗證方式與設計重點。
> 移植到其他電腦時照「四、移植步驟」做；交給 Claude Code 時請它先讀這份文件、`CLAUDE.md` 的「世界 Boss」一節，
> 再讀下面列出的檔案。

---

## 一、功能摘要

- 玩家是**農場後勤**，不直接攻擊 Boss：在 Discord `/farm` →「🐉 世界Boss」捐獻農作物、漁獲、農場素材、加工品、特殊材料。
- 四種補給影響前線自動作戰：🌾 糧食（出勤率）、🩹 醫療（恢復能力）、⚔️ 戰鬥（傷害）、🛡️ 裝備（傷害）。
  每小時傷害 = 基礎 × 攻擊倍率 × 出勤率；每次捐獻另有立即傷害。HP 依時間推進並寫入 PostgreSQL（Bot 重啟不影響）。
- 範圍：🌏 **全服**（`group_id` NULL，所有伺服器與私訊都看得到）或 🏠 **伺服器**活動（其他伺服器看不到）。
- 固定出現：排程「**深淵魔龍**」全服、**2 天一次、晚上 9 點**（台灣時間）、持續 3 小時、HP 15,000，
  以 10 位玩家同時支援可擊敗為目標（1 人、5 人打不倒，有測試）。上一場結束後自動建立下一場，出現前可在監控網站個別修改。
- 獎勵：擊敗／參與獎勵、積分達標獎勵、排名獎勵、稱號；每人每場只能領一次（資料庫唯一限制）。
- 防重複：捐獻帶 `request_id`（`(event, player, request_id)` 唯一）、領獎 `(event, player)` 唯一、先鎖活動列再鎖農場列。
- 介面不顯示 Boss 等級（只顯示名稱）；Discord 世界 Boss 各頁附固定圖（`dragon.png` 縮小版），**只上傳一次**，之後沿用附件。
- 管理監控網站新增「世界 Boss」分頁；介紹網站操作手冊新增「🐉 世界 Boss」一節。

---

## 二、檔案清單

> 「核對」欄是 SHA-256 前 16 碼（在這台電腦完成時的內容），複製到新電腦後可用
> `Get-FileHash <檔案> -Algorithm SHA256` 比對前 16 碼確認是否一致。

### 2-1 新增的檔案

| 檔案 | 用途 | 核對 |
|---|---|---|
| `app/game/world_boss.py` | 純規則：補給／類別定義、預設設定 `DEFAULT_SETTINGS`、預設獎勵 `DEFAULT_REWARDS`、深淵魔龍預設 `ABYSS_DRAGON`／`abyss_dragon()`、設定驗證 `validate_settings`／`validate_rewards`／`validate_schedule`、積分 `price_points`、戰鬥 `combat_stats`／`simulate`／`instant_damage`、獎勵組合 `compose_rewards`、排程時間 `slot_after`／`upcoming_slots` | `663010f41c551e97` |
| `app/services/world_boss_service.py` | `WorldBossService(RankingService)`：玩家 `boss_current`／`boss_donation_options`／`boss_donate`／`boss_leaderboard`／`boss_claim`；背景 `finalize_due_world_bosses`／`spawn_due_world_bosses`（覆寫 `run_background_settlement`）；管理 `create_world_boss`／`update_world_boss`／`update_world_boss_settings`／`update_world_boss_rewards`／`world_boss_action`（start／pause／resume／end）與排程 `create/update/set_enabled/delete_world_boss_schedule`；唯讀共用 `event_view`／`detail_view`／`schedule_view`／`defaults_view` | `aa00533fc8497d2c` |
| `app/discord_bot/world_boss_panel.py` | `WorldBossPages`（FarmPanel 的 mixin）：頁面 `boss`、`boss_donate`、`boss_board`、`boss_help`；按鈕 📦 捐獻物資／🏆 查看排行榜／🎁 領取獎勵／📖 活動說明／🔄 更新狀態；🔀 切換世界 Boss；配色取自 dragon.png | `ff272ea39f7ada7a` |
| `app/discord_bot/farm/scenes/world_boss.png` | Discord 世界 Boss 頁的固定圖（由 `dragon.png` 縮成 1024×576、256 色，約 262 KB） | `f6a1074298032789` |
| `migrations/versions/0019_world_boss.py` | 建立 `world_boss_events`／`world_boss_supplies`／`world_boss_contributions`／`world_boss_reward_claims`；`farm_fish.donated_at`；交易類型 `boss_donate`／`boss_reward` | `c3899a793adc9683` |
| `migrations/versions/0020_world_boss_global_schedule.py` | `world_boss_events.group_id` 可空（全服）、`schedule_id`／`slot_at`、唯一索引調整；建立 `world_boss_schedules` 並寫入「深淵魔龍」 | `5a92a7fc00345ec8` |
| `scripts/world_boss_stresstest.py` | 世界 Boss 壓力測試（`global`／`guild`）：模擬大量玩家同時捐獻、連點、重送與重複領獎，並檢查資料一致性（只能對 `_test` 資料庫，會清空） | — |
| `tests/test_world_boss.py` | 37 個測試：查看、作物／漁獲／素材捐獻、數量不足、重複提交（含並行）、群組隔離、全服、排行榜、HP 與補給、結束後不能捐、領獎一次（含並行）、管理 API、介紹網站、重啟後資料仍在、排程、深淵魔龍 10 人可擊敗、面板流程與圖片只上傳一次 | `3cc7683e93f3bdc3` |
| `dragon.png`（專案根目錄） | 原始示意圖（2.4 MB），只當設計參考與產生 `world_boss.png` 的來源；已在 `.dockerignore` 排除 | `b27cf716d1eea31e` |
| `世界Boss_移植說明.md` | 本文件 | — |

### 2-2 修改的檔案

| 檔案 | 修改內容 | 核對 |
|---|---|---|
| `app/db/models.py` | `EVENT_TYPES` 加 `boss_donate`、`boss_reward`；`FarmFish` 加 `donated_at` 與 CHECK `sold_or_donated`；檔尾新增 `WorldBossSchedule`、`WorldBossEvent`、`WorldBossSupply`、`WorldBossContribution`、`WorldBossRewardClaim` | `9aca5710fed39dea` |
| `app/game/rules.py` | `EventType.BOSS_DONATE`／`BOSS_REWARD` 與 `EVENT_TYPE_LABELS`（世界 Boss 捐獻／獎勵） | `852966c3acc16210` |
| `app/game/ranking.py` | `NET_GOLD_EXCLUDED` 加 `boss_reward`（獎勵金幣不算淨賺） | `c883bad6f4726e8d` |
| `app/repositories/fishing.py` | 新增 `in_basket()`（`sold_at IS NULL AND donated_at IS NULL`），魚簍與統計查詢改用它 | `b3d6e6821c41f833` |
| `app/services/fishing_service.py` | `fish_view` 加 `donated_at`；分享魚時捐出的魚視為不存在 | `6c3f0763ff38ed95` |
| `app/services/admin_service.py` | `AdminService` 改繼承 `WorldBossService`（原本繼承 `RankingService`） | `9e145d720a8a2d9e` |
| `app/services/monitor_service.py` | 新增唯讀 `world_boss()`（活動、排程、表單預設）與 `world_boss_detail()`（HP 以公式推算，不寫入） | `4dd307ce304c6ef5` |
| `app/api/monitor.py` | `GET /api/monitor/world-boss`、`GET /api/monitor/world-boss/{event_id}` | `510b2632c04d8f7c` |
| `app/api/admin.py` | 世界 Boss 與排程的管理端點（見「六、API」），Pydantic 模型 `Boss*` | `941f9d6e23b00ac9` |
| `app/main.py` | 背景結算的日誌條件加 `world_boss` | `c487a7d42f3002a6` |
| `app/discord_bot/bot.py` | 同上（Bot 執行背景結算時） | `4eef3d76f808b951` |
| `app/discord_bot/panels.py` | `FarmPanel(FishingPages, WorldBossPages, View)`；總覽第 4 列加「🐉 世界Boss」按鈕；`help_embed()` 加世界 Boss 說明；**壓測後修正**：`_lock()`，`render`／`refresh`／`show_error` 從重繪到送出畫面依序執行（連點兩下不再出錯、不再重傳圖片），影響所有頁面 | `7509fcafa096ce18` |
| `app/discord_bot/farm_image.py` | `import functools`；`world_boss_png()`（`lru_cache`）；`reset_cache()` 一併清除 | `85cf9d5f56aaa24c` |
| `app/discord_bot/farm/manifest.json` | `scenes` 加 `"world_boss": "scenes/world_boss.png"` | `6371057f7449d0cd` |
| `app/web/index.html` | SVG 圖示 `i-flame`；側邊欄「世界 Boss」按鈕（`data-tab="worldboss"`）；`data-tab-panel="worldboss"` 面板：活動列表與建立表單、排程列表與表單、活動詳情（補給、狀態、獎勵、名稱／HP／時間、補給設定、獎勵設定、排行榜、捐獻紀錄、領獎紀錄） | `5c1c73bb163696e0` |
| `app/web/app.js` | `EVENT_LABELS`、`ACTION_LABELS`、`TAB_META.worldboss`、`TAB_LOADERS.worldboss`、`initWorldBoss()`、`loadWorldBoss()`、排程函式；登入／登出時重新載入 | `11e2e9c0b30a5c99` |
| `app/site/server.py` | `build_world_boss(session, names)` 加入 `/api/guide` 的 `world_boss`（規則、預設獎勵、啟用中的排程；不含玩家或伺服器名稱） | `debcfe8246a104ae` |
| `app/site/static/index.html` | 操作手冊新增 `<details id="world-boss">`：介紹、參與方式、積分、限制、週期、排程表（出現頻率「2 天一次」、出現時間「晚上 9 點」）、補給表、指定物品表、獎勵表 | `5b298e02a8a3e5bb` |
| `app/site/static/site.js` | `renderWorldBoss()`（時間轉成「晚上 9 點」等口語格式） | `3515962f48b246cd` |
| `tests/conftest.py` | `GAME_TABLES` 加 `world_boss_*` 五張表 | `c9cbf8b290ad26ad` |
| `tests/test_discord_bot.py` | 背景結算回傳值多 `world_boss`、`world_boss_spawned` | `6a6fc623fd32990c` |
| `.dockerignore` | 排除 `dragon.png` | `26b3e84bb0a1832b` |
| `CLAUDE.md` | Migration head 0020、架構「世界 Boss」一節 | `cdf2d98f66703bb8` |
| `README.md` | 資料表說明、「🐉 世界 Boss」使用說明 | `6dee17cb04d1d081` |
| `discordbot.txt` | Migration head 改為 0020 | `783b49d038cf82c7` |

### 2-3 只在這台電腦、不要直接複製的檔案

| 檔案 | 說明 |
|---|---|
| `.env` | 含密碼與管理員雜湊（gitignored）。新電腦依 `.env.example` 自行建立；**必須包含 `COMPOSE_PROJECT_NAME=farmgm`**（見「七、注意事項」） |
| `backups/farmgm_before_worldboss_20261104_112848.dump` | 升級前（Migration 0016）的資料庫備份，需要還原時才用 |

---

## 三、資料庫變更

### Migration 0019（世界 Boss）

| 資料表 | 重點 |
|---|---|
| `world_boss_events` | 活動：群組、名稱、`level`（介面不顯示，預設 1）、`max_hp`／`hp`、`status`（active／paused／ended）、`starts_at`／`ends_at`、`last_tick_at`（戰鬥推進時間）、`paused_at`、`defeated_at`、`ended_at`、`end_reason`（defeated／timeout／admin）、`settled_at`、`settings`／`rewards`（JSON 快照） |
| `world_boss_supplies` | 主鍵 `(event_id, supply_code)`；`target`、`progress` |
| `world_boss_contributions` | 捐獻紀錄（類別、補給、物品、品質、數量、積分、立即傷害）；唯一 `(event_id, player_id, request_id)` |
| `world_boss_reward_claims` | 領獎紀錄（名次、積分、結果、金幣、物品 JSON、稱號）；唯一 `(event_id, player_id)` |
| `farm_fish.donated_at` | 捐出的魚；CHECK `sold_at IS NULL OR donated_at IS NULL` |
| `game_transactions` CHECK | 加 `boss_donate`、`boss_reward` |

### Migration 0020（全服與排程）

| 變更 | 重點 |
|---|---|
| `world_boss_events.group_id` | 改為可空：NULL = 全服 |
| `world_boss_events.schedule_id`／`slot_at` | 排程產生的場次；FK `ON DELETE SET NULL`；唯一 `(schedule_id, slot_at)` |
| 唯一索引 | `uq_world_boss_events_scope_open`：手動活動每個範圍（`COALESCE(group_id, 0)`）最多一場未結束；`uq_world_boss_events_schedule_open`：每個排程最多一場未結束 |
| `world_boss_schedules` | 名稱、範圍、`interval_days`、`spawn_hour`／`spawn_minute`（台灣時間）、`anchor_date`、`duration_minutes`、`max_hp`、`settings`／`rewards` 範本、`enabled` |
| 預設資料 | 寫入「深淵魔龍」：全服、每 2 天 21:00、起始 2026-11-04、180 分鐘、HP 15,000、獎勵如下 |

降版（`downgrade`）會刪除世界 Boss 資料與 `boss_*` 交易紀錄，**不可逆**；執行前要備份並取得確認。

---

## 四、移植步驟（Windows PowerShell，專案資料夾）

> 前提：新電腦已依 `discordbot.txt`／`CLAUDE.md`「在新電腦上設定」完成基本安裝（Docker Desktop、Python 3.12+、`.env`）。
> **如果新電腦的專案和這台不是同一版本**（有其他自己的修改），不要直接覆蓋「修改的檔案」，
> 請依「2-2 修改內容」逐項合併（可請 Claude Code 依本文件合併）。

1. **複製檔案**：把「2-1 新增」與「2-2 修改」的檔案複製到新電腦同樣的路徑，再用 SHA-256 前 16 碼核對。
2. **`.env`**：確認有 `COMPOSE_PROJECT_NAME=farmgm`；`ADMIN_PASSWORD_HASH` 等含 `$` 的值要用單引號包住。
3. **備份資料庫**（會修改資料表，先備份；容器名稱以 `docker compose ps` 為準）：
   ```powershell
   docker compose up -d --wait postgres
   docker exec farmgm-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -f /tmp/farmgm_before_worldboss.dump farmgm'
   New-Item -ItemType Directory -Force backups | Out-Null
   docker cp farmgm-postgres-1:/tmp/farmgm_before_worldboss.dump backups\farmgm_before_worldboss.dump
   ```
4. **建置映像並升級 Migration**（在容器內執行，會從目前版本一路升到 0020）：
   ```powershell
   docker compose build
   docker compose run --rm --no-deps web python -m alembic upgrade head
   docker exec farmgm-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d farmgm -tAc "SELECT version_num FROM alembic_version; SELECT name, interval_days, spawn_hour FROM world_boss_schedules;"'
   ```
   預期輸出：`0020` 與 `深淵魔龍|2|21`。
5. **啟動服務**：
   ```powershell
   docker compose up -d --wait            # web 8000、site 24444、bot、postgres、lavalink、yt-cipher
   ```
   `bot` 需要 `.env` 的 `DISCORD_BOT_TOKEN`、`DISCORD_APPLICATION_ID`；沒有 Token 會一直重啟，可 `docker compose stop bot`。
   排程的下一場由背景結算建立（`BACKGROUND_TASKS_RUNNER=web` 或 `bot`），玩家開啟世界 Boss 頁時也會補建。
6. **開發機測試**（選用，需要 `.venv` 與 `TEST_DATABASE_URL`，資料庫名稱必須以 `_test` 結尾）：
   ```powershell
   .\.venv\Scripts\python.exe -m alembic upgrade head        # DATABASE_URL 指向測試庫時才對測試庫執行
   .\.venv\Scripts\python.exe -m pytest tests/test_world_boss.py
   .\.venv\Scripts\python.exe -m pytest
   .\.venv\Scripts\python.exe -m ruff check app tests migrations scripts
   node --check app/web/app.js; node --check app/site/static/site.js
   ```

---

## 五、驗證清單

| 項目 | 方法 | 預期 |
|---|---|---|
| 監控網站 | http://127.0.0.1:8000/#worldboss | 側邊欄有「世界 Boss」；排程列表有「深淵魔龍」與下一場時間 |
| 監控 API | `GET /api/monitor/world-boss` | `schedules[0].name == "深淵魔龍"`，`events` 有排程產生的場次 |
| 介紹網站 | http://127.0.0.1:24444/ →「🐉 世界 Boss」 | 排程表：深淵魔龍｜全服｜2 天一次｜晚上 9 點（持續 3 小時）｜15,000 |
| Discord | 伺服器頻道 `/farm` →「🐉 世界Boss」 | 顯示圖片、Boss 名稱（無等級）、HP 條、四種補給、個人貢獻與 5 個按鈕 |
| 圖片不重傳 | 在世界 Boss 各頁切換、按「🔄 更新狀態」 | 圖片不閃爍；`test_panel_boss_image_uploaded_once` 通過 |
| 測試 | `pytest` | 516 passed（本機結果） |

---

## 六、API

**唯讀（不需登入）**

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/monitor/world-boss?limit=50` | `{events, schedules, defaults, server_time}` |
| GET | `/api/monitor/world-boss/{event_id}` | 單場詳情：補給、戰況、`ranking`、`contributions`（最近 200 筆）、`claim_rows` |

**管理（需管理員登入 + `X-Farmgm-Admin: 1` + 同源）**

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/admin/world-boss` | 建立活動（`group_id` 為 null 表示全服；`name`、`max_hp`、`starts_at`、`ends_at`、選填 `settings`／`rewards`） |
| PUT | `/api/admin/world-boss/{id}` | 修改名稱、HP、開始／結束時間（已開始不能改開始時間） |
| PUT | `/api/admin/world-boss/{id}/settings` | 補給目標、效果、限制、指定物品 |
| PUT | `/api/admin/world-boss/{id}/rewards` | 獎勵與排名獎勵（已有人領獎後不能改） |
| POST | `/api/admin/world-boss/{id}/{start\|pause\|resume\|end}` | 立即開始／暫停／繼續／結束 |
| POST | `/api/admin/world-boss-schedules` | 新增排程（建立後立即產生下一場） |
| PUT | `/api/admin/world-boss-schedules/{id}` | 修改排程（只影響之後的場次） |
| PUT | `/api/admin/world-boss-schedules/{id}/enabled` | `{"enabled": true/false}` |
| DELETE | `/api/admin/world-boss-schedules/{id}` | 刪除排程（尚未結束的那一場一併結束，紀錄保留） |

---

## 七、注意事項（給 Claude Code）

1. **數值只改 `app/game/world_boss.py`**；Migration 內寫死常數（0020 的深淵魔龍數值與 `ABYSS_DRAGON` 相同，兩邊要一起改）。
2. **服務的私有方法名稱要避開父類別**：`FishingService` 已有 `_finish`，世界 Boss 用 `_boss_finish`（曾經覆蓋造成釣魚壞掉）。
   新增方法前先 `grep "def <名稱>(" app/services/*.py`。
3. **監控網站唯讀**：`MonitorService` 在 `SET TRANSACTION READ ONLY` 中，世界 Boss 的 HP 用 `event_view` 的 `_projection` 推算，
   不可在監控路徑呼叫 `_advance`（會寫入）。寫入只在玩家操作、管理 API 與背景結算。
4. **鎖順序**：先鎖 `world_boss_events` 列，再鎖農場列（`_locked_farm`），避免死結。
5. **群組隔離**：玩家操作的 `group_id` 一律來自 Discord 互動；`_member_event` 檢查群組與成員，全服活動（`group_id` NULL）任何玩家都可參加。
6. **捐獻防重複**：面板確認時先清除 `boss_pending`，服務層再以 `request_id` 唯一限制擋住；取消不扣物品。
7. **捐出的魚**：查魚簍一律用 `fishing_repo.in_basket()`，不要只判斷 `sold_at`。
8. **圖片**：`attach_image()` 以內容雜湊判斷，同一張圖會沿用訊息上的附件；`world_boss_png()` 有 `lru_cache`，測試換素材時用 `farm_image.reset_cache()`。
9. **`.env`**：`COMPOSE_PROJECT_NAME=farmgm` 必須保留，否則 Compose 會建立另一組專案與**空的資料庫 Volume**。
   PowerShell 5.1 的 `Set-Content -Encoding utf8` 會加 BOM、`-Encoding ascii` 會把中文變成「?」，寫檔請用 Python 或 `[System.IO.File]::WriteAllText` 搭配無 BOM 的 UTF8。
10. **新增交易事件類型**時要同時改 `models.EVENT_TYPES`、`rules.EventType`／`EVENT_TYPE_LABELS`、Migration 的 CHECK 與 `web/app.js` 的 `EVENT_LABELS`。
11. **管理網站前端**：CSP 禁止 inline script／style，DOM 一律用 `el()`／`textContent`；新分頁要同時改 `TAB_META`、`data-tab`／`data-tab-panel`、`TAB_LOADERS`。

---

## 八、深淵魔龍預設數值與獎勵

| 項目 | 值 |
|---|---|
| 範圍 | 全服 |
| 出現頻率／時間 | 2 天一次，晚上 9 點（台灣時間），持續 3 小時 |
| HP | 15,000 |
| 補給目標 | 糧食 1,000、醫療 600、戰鬥 700、裝備 800 |
| 平衡依據 | 每人約 290 積分；10 人同時捐獻在時限內擊敗，1 人與 5 人打不倒（`test_abyss_dragon_*`） |

| 條件 | 獎勵 |
|---|---|
| 擊敗（所有捐獻者） | 1,200 金幣、水晶 ×2 |
| 未擊敗（所有捐獻者） | 300 金幣 |
| 個人積分 ≥ 200 | 500 金幣、琥珀瓜種子 ×2 |
| 第 1 名 | 3,000 金幣、月光花種子 ×2、古木核心 ×1 |
| 第 2 名 | 2,000 金幣、月光花種子 ×1、機械核心 ×1 |
| 第 3 名 | 1,500 金幣、古木核心 ×1 |
| 第 4–5 名 | 800 金幣、齒輪 ×2 |
| 第 6–10 名 | 400 金幣、水晶 ×1 |
| 擊敗時前 3 名 | 稱號「降龍勇士」 |

---

## 九、壓力測試結果（2026-11-04，本機 12 核心）

`scripts/world_boss_stresstest.py`，16 執行緒，所有玩家同一秒開始：

| 情境 | 換頁／查看 | 確認捐獻 | 領獎 | 一致性 |
|---|---|---|---|---|
| 200 人，全服同一場 | 約 2.0 秒 | 6.4 秒 | 1.9 秒 | 全部通過、0 錯誤 |
| 200 人，20 個伺服器各一場 | 約 2.2 秒 | 4.5 秒 | 1.1 秒 | 全部通過、0 錯誤 |
| 500 人，全服同一場 | 約 6.0 秒 | 16.0 秒 | 4.8 秒 | 全部通過、0 錯誤 |

## 十、已知限制

- 尚未在真實 Discord 伺服器驗證（需要 Bot Token）。
- 稱號只記錄在領獎紀錄並顯示在世界 Boss 頁，沒有獨立的稱號系統；獎勵物品只能從現有物品中選。
- 所有世界 Boss 都顯示同一張 `world_boss.png`（看板文字「深淵魔龍」）；手動建立其他名稱的 Boss 時圖上名稱不會跟著變。
- 排程原名「中國龍」，2026-11-04 改名為「深淵魔龍」（Migration 0020 的預設資料已改）。已經以舊名稱套用 0020 的資料庫，請在監控網站「世界 Boss」修改排程與尚未開始那一場的名稱（Migration 版本相同，不會重新執行）。
