# 農場經營遊戲（Discord Bot + CMD + PostgreSQL + 即時網頁監控）

- **CMD 遊戲**：建立玩家與農場、買種子、種植、收成、販售、擴建土地。
- **PostgreSQL**：唯一的正式資料來源；作物生長依資料庫中的 `planted_at` / `ready_at` 計算，離線時也會持續生長。
- **監控與管理網站**：FastAPI + WebSocket，每 3 秒從資料庫推送最新快照；`/api/monitor` 唯讀，管理員登入後可在網站管理作物、動物、活動、排名獎勵、任務競賽與玩家資料（皆有稽核紀錄）。
- **Discord Bot**：主要遊戲介面（Slash Commands），與 CMD、網站共用同一套服務與資料庫；見下方「Discord Bot」。
- 改造需求見 `discordbot.txt`；給 Claude Code 的專案說明見 `CLAUDE.md`。

## 專案結構

```
app/
  config.py            環境設定（.env）
  clock.py             時間來源（測試可注入可控制時鐘）
  game/                純遊戲規則與數值（balance.py 集中調整數值）
  db/                  SQLAlchemy Model、連線、啟動檢查
  repositories/        資料存取
  services/            遊戲服務（GameService）與監控服務（MonitorService）
  cli/                 CMD 介面（python -m app.cli；開發與維護用）
  discord_bot/         Discord Bot 介面層（python -m app.discord_bot；農場面板＋音樂）
  music/               音樂播放（原 YTDiscordBot 移植）
  site/                介紹網站（python -m app.site，Port 24444）
  api/                 唯讀監控 API 與 WebSocket
  web/                 HTML / CSS / JavaScript 儀表板
  main.py              FastAPI 入口（python -m app.main）
migrations/            Alembic Migration（建立資料表與初始作物）
tests/                 pytest（遊戲、資料庫、API、CLI）＋ tests/web（Node 測試前端重連邏輯）
scripts/loadtest.py    500 玩家負載測試（只對 _test 資料庫）
scripts/farm_stats.py  排行統計表一致性檢查與重算
scripts/bot_stresstest.py  Discord 面板壓力測試（只對 _test 資料庫）
scripts/music_stresstest.py  音樂壓力測試（音訊編碼容量、佇列、選用實際解析）
scripts/purge_legacy_players.py  一次性清除舊 CMD 測試玩家（預設只檢視）
docker-compose.yml     PostgreSQL 16（持久化 Volume，只綁定 127.0.0.1）
```

## 安裝與啟動（Windows PowerShell）

需求：Python 3.12+、Docker Desktop（或自行安裝的 PostgreSQL 14+）。

```powershell
# 1. 環境設定：複製範例並把 change-me 改成自己的密碼（三處要一致）
Copy-Item .env.example .env
notepad .env

# 2. 啟動 PostgreSQL（第一次啟動會同時建立 farmgm 與測試用 farmgm_test 資料庫）
docker compose up -d

# 3. 安裝 Python 套件
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt     # 正式環境只需 requirements.txt

# 4. 建立／升級資料表（不會刪除既有資料）
alembic upgrade head

# 5. 啟動 CMD 遊戲
python -m app.cli

# 6. 另開一個視窗啟動監控網頁，瀏覽 http://127.0.0.1:8000
python -m app.main
```

CMD（cmd.exe）中啟用虛擬環境請改用 `.venv\Scripts\activate.bat`。

若使用自行安裝的 PostgreSQL，只要修改 `.env` 的 `DATABASE_URL` / `TEST_DATABASE_URL`，並手動建立兩個資料庫即可。

### 啟動檢查

CMD 遊戲與監控網頁啟動時都會：

1. 確認可以連線到 PostgreSQL（失敗時只顯示錯誤類型，不印出帳密）。
2. 確認 Migration 已套用到最新版本；否則提示執行 `alembic upgrade head`。

程式**不會**自動建立、刪除或覆蓋資料表。

## 遊戲指令

| 指令 | 說明 |
|---|---|
| `help` | 顯示說明 |
| `player create <名稱>` / `player list` / `player use <id>` | 建立、列出、選擇玩家 |
| `farm start [群組ID] [農場名稱]` | 建立農場（500 金幣、4 格土地、倉庫 50），並固定主要所屬群組；未指定群組 ID 時依序使用 `farm group set` 預先指定的群組、目前群組 |
| `farm group` | 查看農場主要所屬群組 |
| `farm group set <群組ID>` | 建立農場前指定所屬群組；或為尚未設定群組的舊農場做一次性設定（已綁定的農場一律拒絕） |
| `farm status` | 農場狀態 |
| `plots` | 土地狀態、剩餘數量與剩餘時間 |
| `shop` / `market` | 種子、道具價格與農產品售價 |
| `buy wheat 5` / `buy fertilizer 2` / `buy auto_waterer 1` | 購買種子或道具 |
| `plant 1 wheat` | 在第 1 格種植（種下後需澆水） |
| `water 1` / `water all` | 澆水，澆水後才開始生長 |
| `harvest 1` / `harvest all` | 收成（拿到土地上剩餘的數量） |
| `clear 1` | 清除乾枯的作物 |
| `use fertilizer 1` / `use earthworm` | 使用化肥／人工養殖蚯蚓 |
| `tool on\|off auto_waterer` | 啟用／停用自動澆水器 |
| `inventory` / `inventory items` / `inventory tools` | 完整背包／可堆疊物品／工具與限時效果 |
| `sell wheat 2` | 出售農產品 |
| `upgrade` | 擴建 1 格土地（農場等級 +1），價格依目前格數遞增：500、1,000、2,000、4,000、8,000、20,000、40,000、80,000、120,000、160,000、170,000、180,000（最多 16 格） |
| `log [筆數]` | 最近操作紀錄 |
| `exit` | 離開 |

作物可用代碼或中文名稱：`wheat`/小麥、`carrot`/胡蘿蔔、`tomato`/番茄、`strawberry`/草莓。
道具：`fertilizer`/化肥、`earthworm`/蚯蚓、`auto_waterer`/自動澆水器。

### 群組、偷菜與排行榜（CMD 模擬 Discord 伺服器）

| 指令 | 說明 |
|---|---|
| `group create <名稱>` / `group list` / `group use <ID>` | 建立／列出／選擇目前群組 |
| `group add <玩家ID>` / `group remove <玩家ID>` | 加入／移出目前群組（移出不影響農場與紀錄） |
| `group members` / `group status` | 目前群組成員與農場狀態／基本資訊 |
| `steal targets` | 列出目前群組其他成員可偷的作物 |
| `steal <玩家ID> <土地編號> <數量>` | 偷菜 |
| `leaderboard global [money\|sales]` | 總排行榜（不需選擇群組） |
| `leaderboard group [money\|sales]` | 目前群組排行榜（需選擇群組，且目前玩家是成員） |

- 未選擇玩家或群組時，相關指令會提示先選擇，不會默認使用其他玩家或群組。
- 每位玩家只有一座全域農場（`farms.player_id` 唯一），並有一個固定的主要所屬群組 `home_group_id`：
  建立農場時綁定（玩家會自動成為該群組成員）；之後只能透過群組轉移變更（每次轉移後 7 天冷卻，服務層與資料庫觸發器都會檢查，
  CMD 為 `farm group transfer <群組ID>`、Discord 為 `/farm` 面板的「👥 群組」頁）。
  Migration 前就存在的舊農場 `home_group_id` 為空，可用 `farm group set` 一次性設定（記錄為 `bind_group` 事件）。
  若未來需要管理員移轉農場，應另行設計有權限驗證與操作紀錄的功能，並調整觸發器。
- **群組排行榜**只計算「目前是該群組成員，且農場 `home_group_id` 等於該群組」的玩家：
  加入其他群組不會讓農場出現在其他群組排行；未綁定的舊農場只出現在總排行榜；
  離開主要群組後不列入該群組排行，重新加入後依原本歸屬恢復。總排行榜每位玩家計算一次。
- 每筆遊戲操作會記錄 `context_group_id`（操作時 CMD 選擇的群組，且玩家需為該群組成員），
  供未來群組任務／競賽使用；它不影響 `home_group_id`，也不參與排行。
- 偷菜範圍仍為「目前群組的成員」，與農場主要群組無關。
- 服務層不保存「目前群組」：每個方法都明確接收 `group_id`，未來 Discord 以
  `get_group_by_guild(guild_id)` 由 Guild ID 決定群組上下文。

範例：

```text
農場[玩家 #3 / 群組 #1]> steal targets
========== 可偷菜目標 ==========

玩家：Farmer02（ID 4）
農場：綠野農場
土地：第 1 格
作物：小麥
剩餘數量：2
本次最多可偷：2

================================
農場[玩家 #3 / 群組 #1]> steal 4 1 1
成功從 Farmer02 的第 1 格偷走 1 個小麥。
```

### 防盜與農場防守

| 指令 | 說明 |
|---|---|
| `defense` / `defense status` | 持有設施、守護範圍與各土地的阻止機率 |
| `defense shop` | 防守商店 |
| `defense buy <稻草人\|警戒犬\|圍欄>` / `defense buy 巡查券 [數量]` | 購買設施或巡查券 |
| `defense place <稻草人\|圍欄> <起始土地> [編號]` | 調整稻草人（連續 3 格）或防盜圍欄（連續 4 格）的守護範圍 |
| `defense log` | 自己農場近期被阻止的偷菜（不顯示偷菜者） |
| `patrol` | 消耗 1 張巡查券，查看自己農場近 7 天所有偷菜嘗試（含偷菜者）；只能巡查自己的農場 |

| 設施 | 價格 | 上限 | 阻止機率 | 守護範圍 |
|---|---:|---:|---:|---|
| 稻草人 | 400 | 2 | 25% | 連續 3 格（購買後預設第 1 格起，可調整） |
| 警戒犬 | 1,200 | 1 | 30% | 全部土地 |
| 防盜圍欄 | 800 | 1 | 20% | 連續 4 格（可調整） |
| 巡查券 | 60 | － | － | 消耗型道具 |

- 數值集中在 `app/game/balance.py` 的 `DEFENSES`、`DEFENSE_ORDER`、`DEFENSE_MAX_BLOCK_CHANCE`、`PATROL_DAYS`。
- 判定：所有驗證（成員、土地、成熟、剩餘數量、背包容量）通過後才判定；每次偷菜**只擲一次亂數**，
  依「稻草人 → 警戒犬 → 防盜圍欄」累加阻止機率（同種設施重疊只計算一次），整體上限 60%（目前全部生效約 58%）。
  亂數來源可注入（`SocialService(..., rng=...)`），方便單元測試。
- 阻止成功時雙方作物與物品都不變；**被阻止也算用掉該偷菜者對這株作物的唯一一次機會**，
  由資料庫唯一限制保證重複指令、並行請求或程式重啟都不會重新判定。背包不足等錯誤不會消耗機會。

### 動物養殖與食品加工

| 指令 | 說明 |
|---|---|
| `workshop` / `workshop build <設施>` | 查看／建造畜舍、磨坊、烘焙坊、果醬工坊 |
| `animal shop` / `animal list` / `animal status` | 動物商店／我的動物／養殖狀態 |
| `animal buy <動物> <數量>` | 購買動物（需有畜舍，容量不足或金幣不足時不扣款） |
| `animal feed <編號>` / `animal feed all` | 餵食（扣餵食費用）後開始生產週期 |
| `animal collect <編號>` | 收集產品（背包不足時保留在動物身上） |
| `recipe list` | 加工配方 |
| `craft <配方> <份數>` / `craft status` / `craft collect <訂單ID>` | 開始加工／查看訂單／領取成品 |
| `sell <作物\|商品> <數量>` | 販售作物、動物產品或加工品 |
| `leaderboard global\|group goods` | 商品總販售排行（作物＋動物產品＋加工品，依收入） |

| 動物 | 價格 | 餵食費用 | 週期 | 產量 | 產品售價 |
|---|---:|---:|---:|---|---:|
| 雞 | 500 | 20 | 24 小時 | 2 雞蛋 | 25 |
| 牛 | 2,000 | 50 | 24 小時 | 2 牛奶 | 60 |
| 羊 | 1,500 | 35 | 48 小時 | 1 羊毛 | 120 |

| 設施 | 價格 | 上限 | 配方 | 每份時間 | 成品售價 |
|---|---:|---:|---|---:|---:|
| 畜舍 | 1,000 | 3 | 每座 4 個動物欄位 | － | － |
| 磨坊 | 2,000 | 1 | 3 小麥 → 1 麵粉 | 30 分鐘 | 45 |
| 烘焙坊 | 3,500 | 1 | 2 麵粉 → 1 麵包 | 60 分鐘 | 130 |
| 果醬工坊 | 2,500 | 1 | 3 草莓 → 1 草莓果醬 | 60 分鐘 | 140 |

- 所有數值（動物、建築、配方、商品售價、單張訂單上限 10 份）集中在 `app/game/balance.py` 的
  `ANIMALS`、`BUILDINGS`、`BARN_SLOTS`、`RECIPES`、`GOODS`、`CRAFT_MAX_QUANTITY`。
- 動物狀態依時間戳記即時結算：待餵食 → 生產中 → 可收集 → 收集後回到待餵食；未餵食不生產、也不會死亡。
  `animal feed all` 依編號順序餵到金幣不足為止，只對實際餵到的動物扣款，全部在同一筆交易中完成。
- 加工：開單時在同一筆交易中扣除原料並建立訂單；每座設施同時只有一張加工中的訂單，
  完成但待領取的訂單不佔用設施；背包不足時成品保留為待領取。
- 所有操作先鎖定農場資料列，重複指令、並行請求或程式重啟都不會重複扣款、重複扣料或重複發放。
- 動物產品與加工品不能被偷；`sales` 排行只統計作物，`goods` 為商品總販售。

### 群組共同任務與群組競賽

| 指令 | 說明 |
|---|---|
| `quest list` | 我所屬各群組的進行中與已完成任務 |
| `quest group` | 目前群組的全部任務（需先 `group use`） |
| `quest status <任務ID>` / `quest claim <任務ID>` | 任務進度與個人貢獻／領取獎勵（目前群組，且須為成員） |
| `competition list` / `competition history` | 競賽列表／已結算競賽的最終排名 |
| `competition status <競賽ID>` / `competition leaderboard <競賽ID>` | 競賽資訊與我的貢獻／群組排名與個人貢獻 |

- **計分歸屬**：只有「操作發生時的群組（`context_group_id`）= 農場主要所屬群組（`home_group_id`）」且玩家是成員時才計分，
  因此每筆操作最多計入一個群組，加入多個群組不會重複計分。CMD 未選群組或在其他群組中的操作不計分；
  未來 Discord 版以事件發生時的 Guild 對應群組即可。
- 計分在遊戲操作的同一筆資料庫交易中完成：只有成功的操作才計分；每筆貢獻以 (任務或競賽, 交易紀錄) 唯一。
- 任務類型：共同收成（收成數量）、共同販售（數量或金額）、農場建設（擴建與建造次數）、動物生產（雞蛋／牛奶／羊毛，可指定）、
  防守任務（被偷者在該群組中防守成功次數）。達標即完成，之後不再累積；截止或取消後不再累積。
- 任務獎勵：金幣、化肥、蚯蚓或巡查券；「每位貢獻者固定獎勵」或「獎池依貢獻比例分配（無條件捨去）」；
  需有貢獻且仍為群組成員才能領取，每人一次；道具在背包不足時保留到可領取為止。
- 競賽類型：收成（每個農產品 1 分）、銷售（每 1 金幣 1 分）、農場發展（擴建 10 分、建造設施 5 分）、防守（每次阻止 1 分），
  數值在 `app/game/balance.py` 的 `COMPETITION_TYPES`、`DEVELOPMENT_POINTS`。
- 競賽結算：截止後 60 秒（緩衝，確保截止前的操作都已提交）由查詢或管理員觸發結算；同分依群組 ID 排序。
  保存群組排名快照，並把各名次獎金發給「結算當下仍是該群組成員、且主要群組就是該群組」的全體成員（每人每場最多一次）。
- 任務與競賽只能由網站管理員（需登入）建立；建立後條件不可修改，取消必須填寫原因，所有管理操作寫入 `admin_actions`，
  不刪除任何既有貢獻或操作紀錄。

### 作物品質與突變

| 指令 | 說明 |
|---|---|
| `crop info <作物>` | 作物資料、珍稀變種、各品質與突變的單價 |
| `crop quality` / `crop mutations` | 品質機率（含我的農場等級加成後機率）與售價倍率／突變機率與效果 |
| `market prices` | 所有作物與珍稀變種在各品質下的單價 |
| `inventory` | 背包會依作物、品質、突變分開列出數量與單價 |
| `sell <作物> <數量> [品質] [突變]` | 例如 `sell 小麥 2 稀有 黃金`；不指定時賣普通、無突變；珍稀變種用變種名稱（`sell 琥珀小麥 1`） |

| 品質 | 基礎機率 | 售價倍率 | | 突變（預設機率） | 效果 |
|---|---:|---:|---|---|---|
| 普通 | 70% | 1.0 | | 巨型作物 3% | 收成數量 ×2 |
| 優良 | 20% | 1.2 | | 黃金作物 1% | 基本售價 ×3 |
| 稀有 | 8% | 1.5 | | 快速生長 3% | 生長時間減少 2 小時（最短 60 秒） |
| 傳說 | 2% | 2.0 | | 珍稀變種 1% | 長成琥珀小麥／紫晶胡蘿蔔／黑鑽番茄／白雪草莓，基本售價 = 原作物 ×4 |

- **作物生長時間**：Migration 0011 設為小麥 1 小時、胡蘿蔔 2 小時、番茄 4 小時、草莓 8 小時（種子 10／20／35／60 金），
  存在 `crop_types.grow_seconds`，可在網站調整；
  已澆水的作物成熟時間不變，之後澆水的作物使用新時間。
- **判定時機**：播種時一次抽出整批的品質與一個主要突變，保存在 `farm_plots`；之後收成、偷菜、重啟或重新查詢都不重新抽取。
  品質與突變使用獨立的亂數來源（`genetics_rng`），可注入固定值測試。
- **農場等級加成**：每高 1 級，`0.5%` 機率從普通依其他品質的基礎比例移過去，上限 `5%`；重新分配後總和恆為 100%。
  化肥在播種後才使用，不影響品質；目前沒有每日事件系統，因此沒有事件加成。
- **售價公式**（`app/game/genetics.py` 統一計算）：最終單價 = 基本售價 × 品質倍率 × 突變售價倍率，整數運算後無條件捨去。
  巨型只影響數量、快速生長只影響時間、珍稀變種的基本售價 = 原作物售價 × 變種倍率。販售時把實際單價、品質、突變存入交易紀錄，
  日後調整價格不會改寫歷史。
- **規則**：快速生長仍需澆水後才開始計時；乾枯作物只能清除、不產生任何物品；偷到的作物保留原品質與突變；
  巨型作物的加倍數量就是土地上的剩餘數量，收成或被偷只會發放一次。
- **加工**只使用普通、無突變的原料，避免把高品質作物磨掉。販售排行仍依實際販售數量與收入計算，珍稀變種計入作物販售。
- **設定管理**：網站管理員可調整品質機率、售價倍率、突變機率與效果、等級加成與作物生長時間；
  機率總和必須為 100%、突變總和最多 50%、各參數有範圍限制，無效設定不會套用。每次調整建立新版本並寫入管理操作紀錄；
  只影響之後播種的作物與之後的販售單價。

### 遊戲規則重點

- 土地狀態依時間即時結算：空地 → 等待澆水 → 生長中 → 可收成 → 乾枯（成熟後 16 小時未收成）。
- 偷菜：只能偷目前群組其他成員已成熟、未乾枯、仍有剩餘的作物；每次最多 2 個；**同一株作物每人只能偷一次**；
  **每人每天（台灣時間）只能成功偷同一位玩家 1 格田**（`balance.STEAL_PLOTS_PER_VICTIM_PER_DAY`，被防守設施阻止的不算）
  （資料庫唯一限制保證，重複指令或並行請求都不會重複執行）。偷光後土地恢復為空地，主人收成時拿到剩下的數量。
- 化肥：用於生長中的土地，剩餘時間減少 25%，每次種植限用一次。蚯蚓：3 天內澆水的作物生長時間 ×0.75，
  有效期間再次使用再延長 3 天。自動澆水器（3,500 金）：永久工具，啟用時種下即自動澆水，不可重複購買。
- 自動播種機（60,000 金，奢侈品）：啟用時，收成、清除乾枯、擴建或購買種子後，自動從背包隨機挑選種子把所有空地種滿。
- 自動販售機（300,000 金，奢侈品）：啟用時，收成的作物依當下價目表（含活動售價加成）立即賣出，不佔倉庫。
- 倉庫擴大卷：每次立即 +50 格，價格依序 4,000／20,000／50,000／100,000／200,000 金，最大 300 格。
- 🎰 賭場（Migration 0012）：只能押稀有、傳說品質，或巨型作物、黃金作物、珍稀變種的作物。系統從 1–10 擲一個數字：
  比大（6–10）／比小（1–5）贏了價值 ×2，猜中數字 ×5，輸了沒收下注的作物。贏得的作物存成「賭場獎品」（`gamble_prizes`，
  佔倉庫空間），在販售頁賣出（單價 = 價目表單價 × 倍率，再套用活動售價加成），或留著展示；「全部賣出」不會賣掉獎品。
  每次下注寫入 `gamble` 交易紀錄（含擲出的數字）。
- ⛰️ 山洞冒險（Migration 0014／0015，主選單「⛰️ 山洞」）：不耗體力，**一次只能去一個區域**；出發後要等該區域的探險時間
  （🌿 青青草原 Lv.1／8 小時、🌲 古老森林 Lv.5／16 小時、⛏️ 神秘礦坑 Lv.10／24 小時）結束才會回來，回來後按「📦 領取探險獎勵」
  才把獎勵放進背包（素材與假幣放進獨立的素材背包，上限 `MATERIAL_BAG_CAPACITY` = 500，不佔倉庫；稀有種子放倉庫；
  放不下時不領取，清出空間後再領），領取後才能再出發。素材仍存在 `inventory_items`，倉庫用量以
  `inventory_repo.in_warehouse()` 排除素材代碼（監控網站的倉庫用量同樣不含素材）。獎勵在出發時依當下等級決定並記在
  `exploration_logs`（`claimed_at` 為 NULL = 進行中或待領取，部分唯一索引保證每座農場最多一筆），領取時才寫 `explore` 交易紀錄；
  `farm_explorations` 只記各區域探險次數與最近一次的出發／回來時間（以伺服器時間計算）。
  區域、掉落表、獎勵類型機率（普通 65/20/8/5/2%、高級 45/30/12/8/5%）、特殊事件、機器配方與等級門檻全部集中在
  `app/game/exploration.py`（探險時間為 `Area.duration_seconds`）。
  - 素材：木材、石塊、銅礦、鐵礦、樹脂、藥草、齒輪、水晶、古木核心、機械核心，以及假幣（1 個 = 100 金幣，背包頁兌換）。
  - 稀有種子：琥珀瓜（草原）、月光花（森林）是 `crop_types.shop_buyable = false` 的作物，商店不販售、一鍵種植不會自動購買。
  - 機器（沿用 `farm_tools`，可啟用／停用；「🏭 加工」或山洞頁的「🔧 機器」製作）：種子製造機（Lv.3，1 個普通作物 = 2 顆種子，
    稀有作物是否可做由 `SEED_MAKER_RARE_CROPS` 設定）、自動收割機（Lv.5，開啟面板時收成成熟作物，擁有期間成熟的作物即使已過
    乾枯時間也會收）、自動加工機（Lv.8，開啟面板時領取完成的訂單，並用背包原料開始設定的加工項目）、
    稀有作物培育器（Lv.12，以加工訂單把普通作物＋素材培育成稀有種子）。
  - 依需求的建議：草原 Lv.2 起少量銅礦、Lv.4 起少量鐵礦，森林 Lv.6 起少量鐵礦，讓種子製造機與自動收割機在解鎖等級附近可以製作。
  - 注意：農場等級只靠擴建提升，最高 Lv.13；Lv.14–16 的內容目前只有管理員調整等級時才會出現。
- 每日活動（Migration 0011 建立，可在網站修改或停用）：週一☀️晴朗好天氣（免澆水）、週二🌬️大風來襲（收成每格被吹走 1 個，
  至少保留 1 個）、週三🌧️及時雨（生長 -20%）、週四📈市集熱絡（售價 ×1.2）、週五🐝蜜蜂授粉（收成 +20%）、
  週六🌈彩虹日（品質 +5%、突變 +2%）、週日🎉豐收慶典（免澆水、售價 ×1.1）。活動的「免澆水」與「大風颳走」是 0011 新增的效果欄位。
  工具與效果都存在資料庫，程式重啟後不會重設。
- 販售排行榜依 `game_transactions` 的歷史 `sell` 紀錄加總；偷菜、收成、購買都不計入，偷來的作物賣出後才計入。
  同數值時依「販售收入 → 玩家 ID」排序，資料不變時排名穩定。
- 倉庫容量計算**種子、農產品與消耗型道具的總數**。倉庫放不下時拒絕購買、收成或偷菜，作物會留在土地上，不會遺失。
- 所有改變資產的操作都在單一資料庫交易中完成，並先以 `SELECT ... FOR UPDATE` 鎖定農場資料列，
  避免重複收成、重複扣款或同一格土地被同時種植；資料庫另有 CHECK／UNIQUE 限制作為最後防線。
- 每筆成功操作都寫入 `game_transactions`；建立農場的初始金幣也記為一筆 `+500`，
  因此任一時間點所有 `gold_delta` 加總等於全服金幣（監控的金幣趨勢圖即由此計算）。

### 調整遊戲數值

- 農場相關（初始金幣、土地數、倉庫容量、擴建費用）：`app/game/balance.py`。
- 作物（價格、生長時間、收成量、售價、珍稀變種）與動物（價格、餵食費用、週期、產品）：存在資料庫
  `crop_types`／`animal_types`，可在監控網站「遊戲管理」分頁新增、修改、停用或刪除；遊戲執行時一律以資料庫為準。

### 群組總排行與排名獎勵

- **群組總排行**：把「主要所屬群組為該群組且仍是成員」的玩家加總後排名（每座農場只計入一個群組），
  項目有金幣、農產品販售（數量）、商品總販售（收入），可看全部期間或本日／本週／本月（台灣時間）。
  指定期間時「金幣」改為**期間淨賺金幣**（期間內金幣變化加總，不含建立農場、管理員調整與各種獎勵）。
  網站「排行榜 → 群組總排行」；CMD：`leaderboard groups [money|sales|goods] [all|day|week|month]`。
- **排名獎勵**（網站「排名獎勵」分頁，需登入管理員）：每條規則設定排名對象（群組／個人）、項目（金幣／農產品販售／
  商品總販售）、結算週期（每日 00:00、每週一 00:00、每月 1 日 00:00 起算）與第 1～10 名的獎勵（金幣或化肥、蚯蚓、巡查券），
  可新增、修改、啟用／停用、刪除。例如「每週群組金幣：第 1 名全群組 +2000 金幣」「每週個人金幣：第 1 名 +10000 金幣」。
  - 期間結束 1 分鐘後由網頁服務在背景自動結算（間隔 `RANKING_SETTLE_INTERVAL_SECONDS`，預設 60 秒）；
    CMD 查詢 `leaderboard groups`／`leaderboard rewards` 時也會補結算，管理員也可按「立即結算到期期間」。
  - 群組獎勵發給結算當下該群組的全體成員；個人獎勵只發給該玩家。只有分數大於 0 才得獎；同分時依次要條件與 ID 排序（名次不重複）。
  - 每個期間只結算一次（資料庫唯一限制），重複呼叫或並行結算不會重複發獎；服務停止期間錯過的期間會在下次啟動後補結算。
  - 修改獎勵從下一次結算起生效；停用後重新啟用或變更週期，從當下起算，不回溯補發。刪除規則後結算紀錄保留。
  - 道具獎勵直接放入背包，不受倉庫容量限制（避免自動結算時遺失獎勵）。每筆獎勵寫入玩家交易紀錄（排名獎勵）。
  - CMD：`leaderboard rewards` 查看啟用中的規則與自己最近獲得的排名獎勵。

### 遊戲管理（監控網站，需登入管理員）

- **作物／動物種類**：代碼建立後不可修改，名稱與代碼不可與其他作物、變種、動物、產品、商品或道具重複。
  已被使用過的種類只能**停用**（不能購買或種植；已種下、已飼養、已持有的仍可收成、餵食、收集與販售），
  從未被使用的才能刪除。修改生長時間／週期只影響之後澆水／餵食的作物與動物。
- **限時活動**（`game_events`）：效果有售價倍率、生長時間減少、品質加成、突變機率加成、收成數量加成，可限定單一作物；
  排程為單次（開始～結束）、每日固定時段、每週指定星期與時段（台灣時間，不跨午夜，可設定到 24:00），可啟用／停用。
  - 播種時決定品質、突變與收成量；澆水時決定生長時間（蚯蚓 → 活動 → 快速生長 → 最短 60 秒）；販售作物時決定單價。
  - 多個活動同時生效時加成相加，上限：售價 +200%、生長時間 -75%、品質 +30%、突變 +30%、收成 +200%。
  - 套用的活動寫入交易紀錄 `applied_events` 與描述；之後修改或刪除活動不會重算已發生的結果。CMD 可用 `event list` 查看進行中的活動。
- **調整玩家**：設定背包某物品（含品質／突變）的數量（0 = 刪除，增加時不可超過倉庫容量）、新增／刪除土地（只能刪除空地，
  偷菜歷史保留）、在空地直接種下作物（可指定品質、突變、已澆水或立即成熟）或清除作物、新增／刪除建築（動物超過容量時
  不能刪畜舍；有未領取訂單的加工設施不能刪除）、新增／刪除動物（受畜舍容量限制）、直接設定金幣（0 以上）與農場等級
  （1–100，只影響品質加成，不會自動增減土地；金幣差額記入交易紀錄，`gold_delta` 加總仍等於全服金幣）。
  每次調整都寫入玩家交易紀錄（`admin_adjust`，不計入任務或競賽）與管理操作紀錄。
- **群組**：建立群組與成員管理改由 Discord 處理，網站只提供檢視。
- **自動更新**：監控網頁只更新目前分頁；在頁面上輸入、選擇或滑鼠停在按鈕／表單上時暫停分頁資料更新，約 20 秒無操作後恢復（頁首會顯示提示），切換分頁時立即載入。

## 資料庫結構

| 資料表 | 重點 |
|---|---|
| `players` | `display_name`；`external_platform` + `external_user_id`（Discord 使用者 ID 以字串保存，兩者須同時存在，且組合唯一） |
| `farms` | 每位玩家一座（`player_id` 唯一）；`home_group_id` 主要所屬群組（觸發器只允許帶 7 天冷卻的轉移，舊資料可為空）；`last_group_transfer_at` 最近一次轉移時間；`gold >= 0`、`level >= 1` |
| `farm_plots` | `(farm_id, plot_number)` 唯一；`watered_at`／`ready_at` 同時為空（待澆水）或同時有值；`remaining_quantity`、`fertilized`、`planting_no`（每次種植 +1，識別同一株作物）；`quality`、`mutation`（播種時決定，空地時為空） |
| `groups` | 群組；Discord 伺服器以 `discord_guild_id`（字串，唯一）對應；舊 CMD 模擬群組為空 |
| `farm_stats` | 每座農場累積販售統計與非經營金幣（全部期間排行用），隨販售在同一筆交易中更新，可由交易紀錄重算 |
| `farm_daily_stats` | 每座農場每個台灣日期的販售與淨賺金幣（本日／本週／本月排行與排名獎勵結算用） |
| `group_members` | 主鍵 `(group_id, player_id)`；移除成員只刪這一列 |
| `farm_tools` | 永久工具，`(farm_id, tool_code)` 唯一 |
| `farm_effects` | 限時效果，`(farm_id, effect_code)` 唯一，記錄開始／到期時間與倍率 |
| `steal_records` | 偷菜嘗試紀錄（土地被刪除時 `plot_id` 設為 NULL，紀錄保留）；`outcome` 為 success／blocked（被阻止時記錄 `blocked_by`、當次 `block_chance` 與 `roll`）；`(plot_id, planting_no, thief_player_id)` 唯一、數量 1–2、偷菜者 ≠ 被偷者 |
| `farm_defenses` | 防守設施；`(farm_id, device_code, slot)` 唯一，`start_plot` 為守護範圍起點（警戒犬為空，守護全部土地） |
| `farm_buildings` | 畜舍與加工設施；`(farm_id, building_code, slot)` 唯一 |
| `farm_animals` | 動物；`(farm_id, animal_no)` 唯一；`fed_at`／`ready_at` 同時為空表示待餵食，否則 `ready_at > fed_at` |
| `craft_orders` | 加工訂單；`inputs`（已扣除的原料）、`output_quantity`、`started_at`、`ready_at`、`status`（in_progress／collected）、`collected_at` |
| `quests` | 群組任務；目標、期間、獎勵條件建立後由觸發器禁止修改；`status` active／completed／cancelled（取消須有原因） |
| `quest_contributions` | 任務個人貢獻；`(quest_id, transaction_id)` 唯一，同一筆操作只計一次 |
| `quest_claims` | 任務獎勵領取紀錄；`(quest_id, player_id)` 唯一 |
| `competitions` | 群組競賽；類型、期間、各名次獎勵建立後不可修改 |
| `competition_contributions` | 競賽貢獻；`(competition_id, transaction_id)` 唯一，記錄計入的群組 |
| `competition_results` | 結算時的群組排名快照（含群組名稱，之後不受成員或名稱變動影響） |
| `competition_awards` | 結算獎勵發放紀錄；`(competition_id, player_id)` 為主鍵，不會重複發獎 |
| `admin_actions` | 管理員操作紀錄（建立、取消原因、結算） |
| `crop_types` | `code`、`variant_code` 唯一；價格、時間、收成量皆有 CHECK；`active` 停用旗標 |
| `animal_types` | 動物與產品；`code`、`product_code` 唯一；`farm_animals.animal_code` 外鍵參照；`active` 停用旗標 |
| `ranking_reward_rules` | 排名獎勵規則；`active_since` 為啟用（或變更週期）時間，只結算之後結束的期間 |
| `ranking_settlements` | 每條規則每個期間的結算快照（規則與排名）；`(rule_id, period_start)` 唯一；規則刪除後 `rule_id` 設為 NULL |
| `ranking_awards` | 排名獎勵發放紀錄；`(settlement_id, player_id)` 唯一 |
| `game_events` | 限時活動；效果範圍與排程（once／daily／weekly）由 CHECK 限制；`crop_type_id` 可空（全部作物） |
| `inventory_items` | `(farm_id, item_code, quality, mutation)` 唯一，不同品質／突變分開存放；`quantity >= 0`；種子為 `seed_<code>`、農產品為 `<code>`、珍稀變種為獨立代碼 |
| `crop_genetics_settings` | 品質與突變設定的版本紀錄（只新增），最新版本生效 |
| `game_transactions` | `context_group_id` 操作所在群組（可空，只做紀錄）；`applied_events` 該次套用的限時活動；`event_type` 由 CHECK 限定（`app/db/models.EVENT_TYPES`，含 transfer_group 轉移所屬群組）；依 `created_at`、`(farm_id, created_at)` 建索引 |

所有時間欄位為 `timestamptz`，以 UTC 儲存；CMD 與網頁顯示時轉換為台灣時間（UTC+8）。

## 監控 API（唯讀）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/monitor/summary` | 統計卡片 |
| GET | `/api/monitor/farms?q=&limit=&offset=` | 農場列表（`q` ≤ 40 字、`limit` 1–200） |
| GET | `/api/monitor/farms/{farm_id}` | 指定農場、土地、倉庫及最近紀錄 |
| GET | `/api/monitor/transactions?limit=` | 最近操作紀錄（`limit` 1–100） |
| GET | `/api/monitor/charts` | 圖表資料 |
| GET | `/api/monitor/players` | 玩家清單 |
| GET | `/api/monitor/groups` / `/api/monitor/groups/{group_id}` | 群組清單／成員與農場狀態 |
| GET | `/api/monitor/steals?group_id=&player_id=&outcome=success\|blocked&date_from=&date_to=&limit=` | 偷菜嘗試紀錄（成功與被阻止；日期為台灣時間 YYYY-MM-DD，含結束日） |
| GET | `/api/monitor/defenses` | 各農場持有的防守設施與守護範圍 |
| GET | `/api/monitor/ranch` | 各農場的建築、養殖容量、動物狀態、待餵食費用與下次可收集時間 |
| GET | `/api/monitor/craft-orders?include_collected=&limit=` | 加工訂單（原料、成品、開始／完成時間、剩餘時間、狀態） |
| GET | `/api/monitor/quests?group_id=` / `/api/monitor/quests/{id}` | 群組任務、即時進度、個人貢獻與領獎紀錄 |
| GET | `/api/monitor/competitions` / `/api/monitor/competitions/{id}` | 競賽、即時或最終群組排名、個人貢獻與結算獎勵 |
| GET | `/api/monitor/admin-actions` | 管理員操作紀錄 |
| GET | `/api/monitor/genetics` / `/api/monitor/genetics/stats` | 品質與突變設定、作物生長時間／依品質與突變的種植、收成、偷取、販售與持有統計 |
| PUT | `/api/admin/genetics` | 套用新的品質與突變設定（建立新版本；無效設定一律拒絕） |
| PUT | `/api/admin/crops/{code}/grow-seconds` | 調整作物生長時間（60 秒～7 天） |
| POST | `/api/admin/quests`、`/api/admin/quests/{id}/cancel` | 建立任務／取消任務（須附原因） |
| POST | `/api/admin/competitions`、`/api/admin/competitions/{id}/cancel`、`/api/admin/competitions/settle-due` | 建立／取消（須附原因）／結算到期競賽 |
| GET | `/api/monitor/leaderboard?scope=global\|group\|groups&kind=money\|sales\|goods&period=all\|day\|week\|month&group_id=` | 排行榜（`scope=group` 必須帶 `group_id`；`groups` 為群組總排行） |
| GET | `/api/monitor/ranking-rewards` / `/api/monitor/ranking-settlements/{id}` | 排名獎勵規則與結算紀錄／單次結算的排名與得獎名單 |
| POST / PUT / DELETE | `/api/admin/ranking-rules`、`/api/admin/ranking-rules/{id}`、`PUT .../{id}/enabled`、`POST /api/admin/ranking-rules/settle-due` | 新增／修改／刪除排名獎勵規則；啟用／停用；立即結算到期期間 |
| GET | `/api/monitor/players/{player_id}/backpack` | 玩家背包（唯讀） |
| GET | `/api/monitor/catalog` | 作物、動物、限時活動、可放入背包的物品與設施清單 |
| GET | `/api/monitor/players/{player_id}/admin-farm` | 單一玩家的土地、建築、動物與背包庫存（含品質／突變） |
| POST / PUT / DELETE | `/api/admin/crops`、`/api/admin/crops/{code}`、`PUT /api/admin/crops/{code}/active` | 新增／修改／刪除（未使用）作物；啟用／停用 |
| POST / PUT / DELETE | `/api/admin/animal-types`、`/api/admin/animal-types/{code}`、`PUT .../{code}/active` | 新增／修改／刪除（未使用）動物；啟用／停用 |
| POST / PUT / DELETE | `/api/admin/events`、`/api/admin/events/{id}`、`PUT /api/admin/events/{id}/enabled` | 新增／修改／刪除限時活動；啟用／停用 |
| PUT | `/api/admin/players/{id}/farm` | 設定金幣與農場等級（`{"gold", "level"}`，可只帶其中一項） |
| PUT | `/api/admin/players/{id}/inventory` | 設定背包某物品（品質／突變）的數量，0 = 刪除 |
| POST / DELETE | `/api/admin/players/{id}/plots`、`/api/admin/players/{id}/plots/{no}` | 新增土地／刪除空地 |
| PUT / DELETE | `/api/admin/players/{id}/plots/{no}/crop` | 在空地種下指定作物／清除作物 |
| POST / DELETE | `/api/admin/players/{id}/buildings`、`/api/admin/players/{id}/buildings/{code}/{slot}` | 新增／刪除建築 |
| POST / DELETE | `/api/admin/players/{id}/animals`、`/api/admin/players/{id}/animals/{no}` | 新增／刪除動物 |

管理 API 不再提供群組建立與成員管理（改由 Discord 處理）；
請求必須帶 `X-Farmgm-Admin: 1` 標頭，有 Origin 時必須與 Host 相同，且必須先登入管理員帳號。
所有寫入都會記錄在 `admin_actions`；調整玩家資料另外寫入該玩家的交易紀錄。

### 管理員登入

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/auth/login` | 登入（`{"username", "password"}`），成功後設定 session cookie |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 目前登入狀態 |

- 帳號與密碼雜湊設定在 `.env`：`ADMIN_USERNAME`、`ADMIN_PASSWORD_HASH`、`ADMIN_SESSION_HOURS`（預設 8 小時）。
  **原始碼與設定檔只存 scrypt 雜湊，不存明文密碼。** 未設定時管理功能停用（fail closed），唯讀監控不受影響。
- 更換密碼：執行 `python -m app.hash_password`（隱藏輸入），把輸出的 `ADMIN_PASSWORD_HASH=...` 貼到 `.env` 並重新啟動網頁服務。
- Session token 存在 HttpOnly + SameSite=Strict cookie（Path=/api），前端 JS 無法讀取；session 存在記憶體，服務重啟後需重新登入。
- 同一來源連續 5 次登入失敗會鎖定 5 分鐘（HTTP 429）；錯誤訊息不區分帳號或密碼錯誤。
| WS | `/ws/monitor` | 每 `MONITOR_REFRESH_SECONDS` 秒推送快照；資料庫錯誤時送出 `{"type": "error"}` |

安全設計：

- 預設只監聽 `127.0.0.1`；監控查詢在 PostgreSQL `READ ONLY` 交易中執行。
- WebSocket 檢查 `Origin` 必須與 `Host` 相同，防止其他網站讀取本機監控資料。
- 回應附帶 CSP 等安全標頭；前端只以 `textContent` 顯示玩家輸入的名稱，避免 XSS。
- 資料庫錯誤回傳 503 與通用訊息，不暴露堆疊或連線資訊；網頁會清空數值並顯示錯誤，不顯示虛構資料。
- 只接受 `ALLOWED_HOSTS`（預設 `127.0.0.1,localhost`）的 Host 標頭，防止 DNS rebinding 讓外部網站讀取或操作本機服務。
- **若要開放區域網路或網際網路存取，必須先加入登入驗證、權限控制與 HTTPS**，不可直接把 `APP_HOST` 改成 `0.0.0.0`；
  目前的登入是單一管理員帳號、走 HTTP，只適合本機使用；對外開放前需改用 HTTPS（cookie 會自動加上 Secure）。

## 測試

```powershell
# Python：需要 TEST_DATABASE_URL（資料庫名稱必須以 _test 結尾，測試會清空其中的遊戲資料）
python -m pytest
ruff check app tests migrations scripts

# 前端 WebSocket 重連邏輯（Node 18+）
node --test tests/web/connection.test.js
```

## Discord Bot

Bot 只是介面層（`app/discord_bot/`）：接收 Slash Command、從互動來源取得 Discord User ID／Guild ID、
呼叫與 CMD 相同的服務方法，再把結果回覆到 Discord。遊戲規則與文字輸出都重用既有程式（`CommandHandler.execute`），沒有第二套邏輯。

### 建立 Discord 應用程式與邀請 Bot

1. 到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Application，在 **Bot** 頁產生 Token。
2. 把 Token 與 Application ID 填入 `.env`（`DISCORD_BOT_TOKEN`、`DISCORD_APPLICATION_ID`）。**不要**把 Token 寫進程式、貼到對話或日誌；外洩時立即在 Portal 重設。
3. 不需要開啟任何特權 Intent（Message Content、Server Members、Presence 都不用）；Bot 只使用 `guilds` Intent。
4. 邀請連結（OAuth2 → URL Generator）：scope 勾 `bot` 與 `applications.commands`；Bot 權限只需要 `Send Messages`、`Attach Files`（長內容以附件回覆）。
5. 開發時在 `.env` 設定 `DISCORD_DEV_GUILD_ID`（自己的測試伺服器 ID），指令會只同步到該伺服器並立即生效；
   正式環境設定 `APP_ENV=production` 並清空 `DISCORD_DEV_GUILD_ID`，啟動時全域同步（Discord 端可能需要數分鐘才出現）。
   指令沒有變更時可設 `DISCORD_SYNC_COMMANDS=false` 略過同步。

### 啟動

正式執行用 Docker（Linux 容器，`start.bat` 或下列指令；細節見 `discordbot.txt` 第四節步驟 8）：

```powershell
docker compose run --rm web python -m alembic upgrade head   # 部署更新時先備份再升級
docker compose up -d --build --wait                          # web（8000）、site（24444）、bot 與資料庫、Lavalink
docker compose logs -f bot
```

在主機上開發時（先 `docker compose stop web site bot`，避免搶 Port 與同一 Token 兩個 Bot）：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head   # 部署更新時先升級（不會刪除玩家或設定）
.\.venv\Scripts\python.exe -m app.discord_bot        # Discord Bot
.\.venv\Scripts\python.exe -m app.main               # 監控／管理網站（可與 Bot 分開啟動、重啟）
```

Bot、網站、CMD 共用同一個 PostgreSQL；網站修改的資料 Bot 下一個指令就會讀到（沒有快取）。

### 玩家、農場與伺服器

- 一個 Discord 帳號 = 一位全域玩家 = 最多一座農場（`players (external_platform, external_user_id)` 與 `farms.player_id` 唯一；並行建立只會成功一筆）。
- 一個 Discord 伺服器 = 一個群組（`groups.discord_guild_id` 唯一）。Bot 加入伺服器或第一次在伺服器使用指令時自動建立；玩家在伺服器使用指令時自動成為該群組成員。
- 第一次在伺服器輸入 `/farm` 並按「建立農場」時，所屬群組就是該伺服器。只有在農場所屬伺服器中進行的操作才會計入該群組的任務與競賽。
- 所有身分都取自 Discord 互動，不接受玩家輸入的 ID；偷菜對象用成員選擇器指定，服務層再驗證是同一群組成員。
- 私訊中可以查看與操作自己的農場；需要群組的指令（建立農場、偷菜、群組排行、任務等）會說明需在伺服器中使用。
- Discord ID 在資料庫、API 與網站一律以字串保存與輸出（避免 JavaScript 數字精度問題）。

### 指令與農場面板

玩家只需要兩個指令：

- **`/farm`**：打開只有自己看得到的農場面板，所有操作都用按鈕、下拉選單與數量輸入框完成。
- **`/help`**：玩法說明。

面板頁面：

| 頁面 | 內容與操作 |
|---|---|
| 🏠 總覽 | 玩家頭像、等級、金幣；每格農田的作物、品質與成熟倒數；倉庫、所屬群組、農田統計、動物、加工、活動與待辦；全部澆水／全部收成；分享到頻道 |
| 🌱 農田 | 每格農田的作物、品質與成熟倒數（Discord 動態時間戳，自動倒數）；選作物種滿空地（種子不夠自動購買）、全部澆水／收成／清除乾枯、擴建（二次確認）、施化肥、使用蚯蚓 |
| 🎒 背包 | 種子、農產品（含品質／突變）、商品、道具、工具開關、生效中的效果 |
| 📈 作物價格 | 總覽的「作物價格」按鈕：價目表（各品質、黃金作物、珍稀變種單價）與品質、突變機率 |
| 🎰 賭場 | 選作物與下注數量 → 比大／比小（×2）或猜數字（×5）；顯示可下注作物與我的獎品 |
| 🏪 商店 | 每項種子／道具一張卡片（價格、生長時間、收成、售價、持有數量）；選單選取後會保持選取，按數量按鈕（1／5／10 或自訂）購買，結果顯示購買後的持有數量與剩餘金幣，可直接加購；進行中的活動、價格表、品質與突變 |
| 💰 販售 | 每項的單價與小計；選單選商品 → 賣 1／一半／全部或自訂；全部賣出（二次確認） |
| 🐔 動物 | 動物狀態與倒數、全部餵食、收集全部、建造畜舍、購買動物 |
| 🏭 加工 | 設施狀態與完成倒數、配方、開始加工、建造設施、領取完成品 |
| 🛡️ 防守 | 設施與守護範圍、各土地阻止機率、購買、調整範圍（輸入起始土地）、巡查與防守紀錄 |
| 🥷 偷菜 | 本伺服器可偷的成熟作物（選單一鍵偷取，超過 25 筆分頁） |
| 🏆 排行榜 | 切換範圍（個人／本伺服器／群組總排行）、項目、期間；排名獎勵 |
| 📜 任務競賽 | 任務進度條與貢獻、領取獎勵、競賽排名、已結算競賽 |
| 👥 群組 | 成員、所屬群組與轉移冷卻；把農場轉移到目前伺服器（二次確認） |
| 🧾 紀錄 | 最近 15 筆操作 |

- 面板只有開啟者能操作，15 分鐘後逾時（再輸入 `/farm` 即可）。每次切換頁面都重新讀取資料庫，網站或其他介面的修改會立即顯示。
- 一鍵操作（全部澆水、全部收成、全部賣出、種滿空地）依序呼叫多個服務方法，每一步各自是一筆完整交易；結果訊息會列出成功與失敗的項目。
- 沒有使用 emoji「反應」：面板是私密訊息，Discord 不允許對私密訊息加反應，而且反應需要額外的 Gateway Intent；按鈕與選單可以即時回饋並由伺服器驗證。
- 進階玩家或管理者可設 `DISCORD_ADVANCED_COMMANDS=true`，另外註冊與 CMD 一一對應的文字指令（`/farm-cmd`、`/buy`、`/sell`、`/steal`…，用 `/farm-help` 列出）。

### 農場轉移所屬群組（7 天冷卻）

在要轉入的伺服器打開 `/farm` →「👥 群組」→「把農場轉移到這個伺服器」，畫面會說明影響與冷卻，再按一次「確認轉移」才執行（進階文字指令 `/farm-cmd transfer-group` 同樣有確認按鈕）。

- 只更新同一座農場的 `home_group_id`，金幣、物品、土地、動物都不變；之後的群組排行、任務與競賽計分改以新群組計算；歷史交易、已結算的任務／競賽／排名獎勵不改寫。
- 建立農場後第一次轉移不需等待；之後每次成功轉移需等待完整 7 天（`farms.last_group_transfer_at`）。
- 冷卻同時在服務層（鎖定農場列的交易內）與資料庫觸發器檢查；Bot 重啟、離開或重新加入伺服器都不會重設；兩個伺服器同時提交只會有一筆成功。
- 網站管理員可在「玩家管理 → 所屬群組」轉移，原因必填並寫入 `admin_actions`；可勾選略過冷卻（同樣留下紀錄）。

### 效能、連線池與背景工作

- 遊戲服務是同步 SQLAlchemy，Bot 以專用執行緒池（`BOT_WORKER_THREADS`，預設 16）執行，不在事件迴圈中等待資料庫。
  Bot 的連線池大小至少為執行緒數 + 2（背景結算與 advisory lock 各一條）；網站與 Bot 各自一組連線池（`DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT`）。
  預設 Bot 約 18+5、網站 10+5、介紹網站 2+2，遠低於 PostgreSQL 預設 `max_connections=100`。
- 交易完成後才回覆 Discord，不會在持有資料庫鎖時等待網路。死結（40P01）與序列化失敗（40001）代表整筆交易已回滾，Bot 會重試同一個服務方法最多 3 次；
  連線中斷或連線池逾時不重試，回覆玩家稍後再試並可用 `/farm log` 確認。
- 排行榜「全部期間」讀 `farm_stats`（每座農場一列），本日／本週／本月與排名獎勵結算讀 `farm_daily_stats`（台灣日期），
  兩者都在販售等操作的同一筆交易中更新；`scripts/farm_stats.py check|rebuild` 可比對或由交易紀錄重算。
- 背景結算（排名獎勵與競賽）由 `BACKGROUND_TASKS_RUNNER` 指定的程序執行（`web` 預設、`bot` 或 `none`），另一方只在查詢排行時補結算。
  同時執行也安全：PostgreSQL advisory lock 讓同一時間只有一個程序結算，`(rule_id, period_start)` 等唯一限制保證不會重複發獎；重啟或 Gateway 重連不會重新發放。
- 500 名玩家不需要分片（Sharding）。

### 故障排查

| 症狀 | 處理 |
|---|---|
| `[啟動失敗] 未設定 DISCORD_BOT_TOKEN` | 在 `.env` 填入 Token（或設定系統環境變數）。 |
| `[啟動失敗] DISCORD_BOT_TOKEN 無效或已被重設` | 到 Developer Portal 重新產生 Token 並更新 `.env`。 |
| `[啟動失敗] 無法連線到 PostgreSQL` | `docker compose up -d`，確認 `DATABASE_URL`。 |
| `[啟動失敗] 資料庫 Migration 尚未套用到最新版本` | `alembic upgrade head`。 |
| 日誌「開發環境未設定 DISCORD_DEV_GUILD_ID，未同步指令」 | 設定開發伺服器 ID，或正式環境設 `APP_ENV=production`。 |
| 日誌「指令同步失敗：Bot 沒有該伺服器的 applications.commands 權限」 | 用含 `applications.commands` scope 的邀請連結重新邀請。 |
| 伺服器看不到指令 | 全域同步需數分鐘；開發時用 `DISCORD_DEV_GUILD_ID` 立即同步。 |
| 玩家看到「資料庫暫時無法連線」 | 檢查 PostgreSQL；恢復後不需重啟 Bot（連線池會自動換掉失效連線）。 |
| 玩家看到「伺服器忙碌中」 | 連線池等待逾時或重試後仍死結；檢查 `BOT_WORKER_THREADS` 與 `DB_POOL_*`，以及 `scripts/loadtest.py` 的結果。 |
| 排行數字與交易紀錄不符 | `python scripts/farm_stats.py check`；必要時 `rebuild`。 |

### 🎵 音樂播放（原 YTDiscordBot 合併）

音樂功能移植自 `YTDiscordBot`（`app/music/`，播放邏輯不變）。預設與農場共用同一個 Bot、同一個 Token、同一個程序。

- 指令：`/play`（關鍵字或 YouTube／Spotify 連結）、`/nowplaying`、`/queue`、`/skip`、`/pause`、`/resume`、
  `/stop`、`/loop`、`/shuffle`、`/remove`、`/clear`、`/volume`、`/join`、`/leave`、`/prefix`、`/music`（音樂說明）。
- 預設只用斜線指令，不需要特權 Intent；要用 `!play` 等前綴指令請設 `MUSIC_PREFIX_COMMANDS=true` 並在 Developer Portal 開啟
  **Message Content Intent**。Bot 需要「連接」「說話」權限（介紹網站的邀請連結已包含）。
- 各伺服器的音量與前綴存在 `data/music_guild_settings.json`。原本的 `config.txt` 不再使用；舊 Bot 的 Token 若不再使用，建議到 Developer Portal 重設。

**Lavalink（音訊伺服器，取代 ffmpeg／yt-dlp）**：Bot 透過 [wavelink](https://github.com/PythonistaGuild/Wavelink) 3.5.2
連線到 docker compose 的 `lavalink` 服務（Lavalink 4.2.2），解析與播放都在 Lavalink 進行，Bot 端不需要 ffmpeg。

```powershell
docker compose up -d lavalink    # 第一次啟動會下載插件（約 10 秒）；.env 需要 LAVALINK_URI 與 LAVALINK_PASSWORD
docker compose logs lavalink     # 看到 Loaded 'youtube-plugin…' 與 Lavalink is ready 即可
```

- 設定在 `docker/lavalink/application.yml`（密碼由 `.env` 的 `LAVALINK_PASSWORD` 經環境變數傳入，不寫在檔案）。
  插件：YouTube 用官方 [youtube-source](https://github.com/lavalink-devs/youtube-source)（暫用 snapshot，內建 YouTube 來源已停用）。
  HTTP／本機檔案來源已關閉，避免被用來存取內部網路。
- 使用預設（Ubuntu／glibc）映像：Discord 自 2026-03 起要求語音加密 DAVE，Lavalink 4.2.0 起支援，需要 glibc 2.35 以上。
- Spotify 連結仍由 Bot 讀取歌名，播放前才到 YouTube 搜尋同一首歌（不需要 Spotify 金鑰）。
- **B 站已停止支援**：B 站風控（HTTP 412）與 lavabili-plugin 的錯誤處理問題（取不到播放網址時直接失敗、主要 CDN 失敗不改用備用節點），即使登入帳號仍有多數影片無法播放，因此移除 `/bilibili` 與 lavabili-plugin；貼 B 站連結時 `/play` 會直接回覆不支援。
- Lavalink 斷線時 wavelink 會自動重連；期間點歌會顯示「音樂伺服器（Lavalink）目前無法連線」。
- Lavalink 容器待機約 310 MiB，同時 30 個解析請求時約 380 MiB（`LAVALINK_MAX_HEAP` 預設 768M）。

**獨立程序**（很多伺服器同時聽歌時建議使用）：在 Developer Portal 另外建立一個應用程式當音樂 Bot，
於 `.env` 設定 `MUSIC_BOT_TOKEN`（與 `MUSIC_APPLICATION_ID`），然後分別啟動：

```powershell
.\.venv\Scripts\python.exe -m app.discord_bot   # 農場（設定 MUSIC_BOT_TOKEN 後不再載入音樂）
.\.venv\Scripts\python.exe -m app.music         # 音樂（不需要資料庫）
```

兩個程序不能共用同一個 Token：同步指令時會互相覆蓋對方的斜線指令，且同一個互動會兩邊搶著回應。
介紹網站在這個模式下會多顯示「邀請音樂機器人」。

### 🌐 介紹網站（Port 24444）

```powershell
.\.venv\Scripts\python.exe -m app.site   # http://127.0.0.1:24444；對外公開設 SITE_HOST=0.0.0.0
```

功能介紹、**農場操作手冊**（每個功能的操作步驟與數值表）、邀請連結（依 `DISCORD_APPLICATION_ID` 產生）、
支援伺服器（`SITE_SUPPORT_URL`）與即時統計。只提供彙總數字，不輸出任何玩家資料；統計在唯讀交易中查詢並快取 30 秒。
頁面回應 `Cache-Control: no-cache`，網站更新後訪客會立即看到新版。修改遊戲數值時記得同步更新 `app/site/static/index.html` 的操作手冊。

### 壓力測試結果（本機 12 核心，PostgreSQL 16 in Docker）

**音樂（Lavalink 版）**（`scripts/music_stresstest.py [--guilds 100,500,1000] [--resolve 5,15,30] [--no-network]`）：

Bot 端（假的 Lavalink 播放器，但走正式的 play_next → TrackEnd → on_song_end 流程與「正在播放」訊息；每首 0.2 秒就結束，切歌密度遠高於真實）：

| 同時播放的伺服器 | 切歌次數 | 切歌處理 P95 | 事件迴圈延遲 P95 | Bot 程序 CPU | Bot 記憶體 |
|---|---|---|---|---|---|
| 100 | 500 | 49 ms | 20 ms | 20% | 54 MB |
| 500 | 2,500 | 74 ms | 45 ms | 55% | 61 MB |
| 1,000 | 5,000 | 194 ms | 173 ms | 88% | 71 MB |
| 2,000 | 10,000 | 592 ms | 547 ms | 100% | 85 MB |

真實歌曲約 3–4 分鐘一首，2,000 個伺服器每秒約 10 次切歌，是上表的數百分之一；不再有每條串流 26–32 MB 的 ffmpeg 子程序與 200 MB 的 deno。

Lavalink 實際解析（真的查詢 YouTube，總數刻意壓在 50 個以內）：同時 5／15／30 個請求的延遲 P95 約 0.7–0.8 秒、
YouTube 搜尋／網址／播放清單全部成功（當時另有 B 站搜尋，因風控常失敗，現已停止支援）。

**未包含**：實際串流到 Discord 語音的音訊負載（Lavalink 沒有語音連線時不會持續解碼），需要真實 Discord 才能量測。

**農場面板**（`scripts/bot_stresstest.py <玩家數> [同時播放音樂的伺服器數] [工作執行緒]`，經過與正式環境相同的執行緒池與連線池）：

| 情境（平均完成時間） | 一般操作 | 購買 | 全部澆水 | 種滿農田 | 每秒互動 |
|---|---|---|---|---|---|
| 100 人同時（改善前：8 執行緒、逐格交易） | 約 0.9 秒 | 2.7 秒 | 3.2 秒 | 4.9 秒 | 57 |
| 100 人同時（改善後：16 執行緒、單一交易） | 約 0.8 秒 | 2.4 秒 | 2.4 秒 | 3.3 秒 | 71 |
| 500 人同一秒同時操作（改善前） | 約 4.4 秒 | 12.6 秒 | 16.2 秒 | 24.6 秒 | 59 |
| 500 人同一秒同時操作（改善後） | 約 4.2 秒 | 11.9 秒 | 12.1 秒 | 16.1 秒 | 72 |
| 500 人＋50 個伺服器播音樂（改善後，舊 ffmpeg 版） | 約 5.4 秒 | 15.4 秒 | 15.7 秒 | 20.6 秒 | 55 |
| 200 人（Lavalink 版，不播音樂） | 約 4.0 秒 | 11.7 秒 | 11.9 秒 | 15.3 秒 | 30 |
| 200 人＋300 個伺服器播音樂（Lavalink 版） | 約 3.7 秒 | 10.1 秒 | 11.1 秒 | 15.7 秒 | 31 |

Lavalink 版兩次是同一台電腦另一天的量測（數字和上面幾列不能直接比較）；同一次量測中播不播音樂幾乎沒有差別，
音樂已不再拖慢農場。所有情境都沒有錯誤、金幣與交易紀錄一致、統計表無差異；首次回應一律立即 defer，符合 Discord 3 秒規則。
「種滿農田」「全部澆水」「全部收成」「清除乾枯」已改為單一交易（`GameService.plant_all` 等）。
處理量的主要限制是 Python 的 CPU 時間（每次換頁都重新查詢並繪製整頁），再增加執行緒效果有限；
500 人「同一秒」同時操作是極端情況，實際玩家操作分散時等待會短很多。

### 一次性清除舊 CMD 測試玩家

`scripts/purge_legacy_players.py` 預設只列出目標資料庫與預計清除／保留的資料表筆數；必須先完成 `pg_dump -Fc` 備份，
再以 `--execute --confirm-db <資料庫名稱> --backup-file <備份檔>` 執行（單一交易）。只執行一次，不在啟動流程或 Migration 中。

### 尚未在真實 Discord 驗證的部分

自動化測試以模擬的互動物件（`tests/test_discord_bot.py`）驗證 Bot 介面層；登入、指令同步、按鈕與實際訊息呈現需要真實的 Bot Token 與測試伺服器才能驗證。

## 資料保存

PostgreSQL 資料存放在 Docker Volume `farmgm_farmgm_pgdata`。`docker compose down` 或重建容器不會遺失資料；
**請勿**執行 `docker compose down -v`（會刪除 Volume 與所有遊戲資料）。
