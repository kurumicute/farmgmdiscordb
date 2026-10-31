"use strict";

// ---------------------------------------------------------------------------
// WebSocket 連線管理（獨立、可注入，方便以 Node 測試斷線重連）
// ---------------------------------------------------------------------------
class MonitorConnection {
  constructor({ url, WebSocketImpl, setTimeoutImpl, clearTimeoutImpl, onStatus, onMessage }) {
    this.url = url;
    this.WebSocketImpl = WebSocketImpl;
    this.setTimeout = setTimeoutImpl;
    this.clearTimeout = clearTimeoutImpl;
    this.onStatus = onStatus;
    this.onMessage = onMessage;
    this.attempt = 0;
    this.timer = null;
    this.ws = null;
  }

  static backoff(attempt) {
    return Math.min(15000, 1000 * 2 ** Math.max(0, attempt - 1));
  }

  connect() {
    this.onStatus(this.attempt === 0 ? "connecting" : "reconnecting", null);
    let ws;
    try {
      ws = new this.WebSocketImpl(this.url);
    } catch (err) {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.onStatus("open", null);
    };
    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      this.onMessage(msg);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.scheduleReconnect();
    };
    ws.onerror = () => {
      // 錯誤之後瀏覽器一定會觸發 close，由 onclose 處理重連
    };
  }

  scheduleReconnect() {
    this.ws = null;
    this.attempt += 1;
    const delay = MonitorConnection.backoff(this.attempt);
    this.onStatus("closed", delay);
    if (this.timer) this.clearTimeout(this.timer);
    this.timer = this.setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  requestRefresh() {
    if (this.ws && this.ws.readyState === 1) this.ws.send("refresh");
  }
}

if (typeof module !== "undefined") {
  module.exports = { MonitorConnection };
}

// ---------------------------------------------------------------------------
// 儀表板 UI
// ---------------------------------------------------------------------------
if (typeof document !== "undefined") {
  const $ = (id) => document.getElementById(id);
  const TZ = "Asia/Taipei";
  const dtFmt = new Intl.DateTimeFormat("zh-TW", {
    timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const hourFmt = new Intl.DateTimeFormat("zh-TW", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const numFmt = new Intl.NumberFormat("zh-TW");
  const EVENT_LABELS = {
    create_farm: "建立農場", buy_seed: "購買種子", plant: "種植", harvest: "收成", sell: "販售", upgrade: "擴建土地",
    buy_item: "購買道具", use_item: "使用道具", water: "澆水", clear: "清除乾枯", steal: "偷菜", stolen: "被偷",
    bind_group: "設定所屬群組", steal_blocked: "偷菜被阻止", defended: "防守成功", defense_place: "調整防守位置",
    build: "建造設施", buy_animal: "購買動物", feed_animal: "餵食動物", collect_animal: "收集動物產品",
    craft_start: "開始加工", craft_collect: "領取加工品", quest_reward: "任務獎勵", competition_reward: "競賽獎勵", admin_adjust: "管理員調整", ranking_reward: "排名獎勵",
    transfer_group: "轉移所屬群組", gamble: "賭場",
    explore: "山洞探索", build_machine: "製作機器", exchange: "假幣兌換", make_seed: "製造種子",
  };

  const state = {
    snapshot: null,
    serverOffsetMs: 0, // 伺服器時間 - 本機時間，用於倒數計算
    selectedFarmId: null,
    detail: null,
    search: "",
    searchResult: null,
    lastSync: null,
    tab: null,
    ready: false, // init 完成後才在切換分頁時載入資料
  };

  const fmtTime = (iso) => (iso ? dtFmt.format(new Date(iso)) : "—");
  const fmtNum = (n) => numFmt.format(n);
  const serverNow = () => Date.now() + state.serverOffsetMs;

  function el(tag, opts = {}, children = []) {
    const node = document.createElement(tag);
    if (opts.className) node.className = opts.className;
    if (opts.text !== undefined) node.textContent = String(opts.text); // 一律使用 textContent，避免 XSS
    if (opts.colSpan) node.colSpan = opts.colSpan;
    for (const child of children) node.appendChild(child);
    return node;
  }
  const td = (text, className) => el("td", { text, className });

  function fmtDuration(totalSeconds) {
    const s = Math.max(0, Math.ceil(totalSeconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h} 小時 ${m} 分 ${sec} 秒`;
    if (m) return `${m} 分 ${sec} 秒`;
    return `${sec} 秒`;
  }

  function goldCell(delta) {
    const cell = td(delta > 0 ? `+${fmtNum(delta)}` : delta < 0 ? `-${fmtNum(-delta)}` : "0", "num");
    if (delta > 0) cell.classList.add("pos");
    if (delta < 0) cell.classList.add("neg");
    return cell;
  }

  // 載入失敗與「沒有資料」要能區分：失敗訊息以錯誤樣式顯示，不會被誤認為空資料
  const isErrorText = (text) => /失敗|無法取得/.test(String(text));
  const emptyRow = (text, colSpan) => el("tr", {}, [
    el("td", { text, className: isErrorText(text) ? "empty is-error" : "empty", colSpan }),
  ]);

  // 長列表：tbody 設定 data-page-size 時自動分頁，設定 data-filter 時加上關鍵字篩選。
  // 每 3 秒的自動更新會保留目前的頁碼與篩選字。
  const pagers = {};

  function tableTools(tbody) {
    const wrap = tbody.closest(".table-wrap");
    let tools = wrap.previousElementSibling;
    if (tools && tools.classList.contains("table-tools")) return tools;
    tools = el("div", { className: "table-tools" });
    if (tbody.dataset.filter) {
      const input = el("input", { className: "table-filter" });
      input.type = "search";
      input.placeholder = tbody.dataset.filter;
      input.setAttribute("aria-label", tbody.dataset.filter);
      input.addEventListener("input", () => {
        const st = pagers[tbody.id];
        st.filter = input.value.trim().toLowerCase();
        st.page = 1;
        renderPage(tbody);
      });
      tools.appendChild(input);
    }
    const info = el("span", { className: "pager-info muted small" });
    const prev = el("button", { className: "pager-btn", text: "‹ 上一頁" });
    const next = el("button", { className: "pager-btn", text: "下一頁 ›" });
    prev.type = next.type = "button";
    prev.addEventListener("click", () => { pagers[tbody.id].page -= 1; renderPage(tbody); });
    next.addEventListener("click", () => { pagers[tbody.id].page += 1; renderPage(tbody); });
    tools.append(el("span", { className: "spacer" }), info, prev, next);
    wrap.before(tools);
    return tools;
  }

  function renderPage(tbody) {
    const st = pagers[tbody.id];
    const size = Number(tbody.dataset.pageSize);
    const matched = st.filter ? st.rows.filter((r) => r.textContent.toLowerCase().includes(st.filter)) : st.rows;
    const pages = Math.max(1, Math.ceil(matched.length / size));
    st.page = Math.min(Math.max(1, st.page), pages);
    const shown = matched.slice((st.page - 1) * size, st.page * size);
    const empty = st.filter && st.rows.length ? "沒有符合篩選條件的資料" : st.emptyText;
    tbody.replaceChildren(...(shown.length ? shown : [emptyRow(empty, st.colSpan)]));
    const tools = tableTools(tbody);
    const [info, prev, next] = [tools.querySelector(".pager-info"), ...tools.querySelectorAll(".pager-btn")];
    const filtered = st.filter ? `（篩選 ${matched.length} / ${st.rows.length}）` : "";
    info.textContent = matched.length ? `第 ${st.page} / ${pages} 頁・共 ${matched.length} 筆${filtered}` : "";
    prev.disabled = st.page <= 1;
    next.disabled = st.page >= pages;
    const showNav = pages > 1;
    prev.hidden = next.hidden = !showNav;
    tools.hidden = !showNav && !tbody.dataset.filter;
  }

  function replaceRows(tbody, rows, emptyText, colSpan) {
    if (!tbody.dataset.pageSize) {
      tbody.replaceChildren(...(rows.length ? rows : [emptyRow(emptyText, colSpan)]));
      return;
    }
    const st = (pagers[tbody.id] ||= { page: 1, filter: "" });
    Object.assign(st, { rows, emptyText, colSpan });
    renderPage(tbody);
  }

  // ---------- 狀態標籤 ----------
  const STATUS_TONES = {
    可收成: "good", 生長中: "info", 等待澆水: "warn", 已乾枯: "muted", 空地: "muted",
    偷菜成功: "bad", 進行中: "info", 已完成: "good", 已截止: "muted", 已取消: "bad", 已結算: "good",
    尚未開始: "muted", 待結算: "warn", 待餵食: "warn", 生產中: "info", 可收集: "good",
    加工中: "info", 待領取: "good", 已領取: "muted", 是: "good",
  };

  function badge(text, tone) {
    return el("span", { className: `badge ${tone || STATUS_TONES[text] || (text.startsWith("被") ? "good" : "neutral")}`, text });
  }

  const statusTd = (text, tone) => el("td", {}, [badge(text, tone)]);

  // ---------- 分頁導覽 ----------
  const TAB_META = {
    overview: ["儀表板", "即時營運狀態・資料來源：PostgreSQL・台灣時間（UTC+8）"],
    farms: ["農場監控", "所有農場的土地、作物與倉庫狀態，點選一列查看詳情"],
    groups: ["群組", "Discord 群組與成員；群組建立與成員管理由 Discord 端處理"],
    players: ["玩家調整", "修改玩家金幣、等級、背包、土地、建築與動物（需管理員）"],
    backpack: ["玩家背包", "查看玩家持有的種子、農產品、道具與限時效果"],
    crops: ["作物種類", "作物價格、生長時間、收成數量與停用狀態"],
    genetics: ["品質與突變", "作物品質機率、突變效果與實際統計"],
    animals: ["動物種類", "動物價格、生產週期、飼料與產品設定"],
    ranch: ["養殖與加工", "玩家飼養的動物、生產狀態與加工訂單"],
    events: ["限時活動", "加成活動的效果、排程與啟用狀態"],
    community: ["任務與競賽", "群組任務進度、群組競賽排名與結算"],
    leaderboard: ["排行榜", "總排行榜、群組內排行與群組總排行（名次以後端排序為準）"],
    rewards: ["排名獎勵", "每日／每週／每月排名獎勵規則與結算紀錄"],
    steals: ["偷菜與防守", "偷菜成功與被阻止紀錄、各農場防守設施"],
    logs: ["操作紀錄", "最新遊戲操作與管理員操作紀錄"],
  };
  const TABS = Object.keys(TAB_META);
  const TAB_ALIASES = { manage: "events" }; // 舊網址 #manage 仍可使用

  function showTab(name) {
    const tab = TABS.includes(name) ? name : TAB_ALIASES[name] || "overview";
    const [title, desc] = TAB_META[tab];
    $("page-title").textContent = title;
    $("page-desc").textContent = desc;
    document.title = `${title}｜農場即時監控`;
    closeDrawer();
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== tab; });
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("on", on);
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    if (location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
    const changed = state.tab !== tab;
    state.tab = tab;
    if (changed && state.ready) refreshExtras(true);
  }

  function initTabs() {
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => b.addEventListener("click", () => {
      showTab(b.dataset.tab);
      window.scrollTo({ top: 0 });
    }));
    window.addEventListener("hashchange", () => { showTab(location.hash.slice(1)); window.scrollTo({ top: 0 }); });
    initSidebar();
    showTab(location.hash.slice(1));
  }

  // ---------- 側邊欄：桌面可收合（記住偏好），窄螢幕改為抽屜 ----------
  const SIDEBAR_KEY = "farmgm.sidebarCollapsed";
  const drawerMode = () => window.matchMedia("(max-width: 960px)").matches;

  function closeDrawer() {
    const shell = $("app-shell");
    if (!shell.classList.contains("drawer-open")) return;
    shell.classList.remove("drawer-open");
    $("drawer-backdrop").hidden = true;
    $("drawer-open").setAttribute("aria-expanded", "false");
    $("drawer-open").focus();
  }

  function initSidebar() {
    const shell = $("app-shell");
    const collapseBtn = $("sidebar-collapse");
    const setCollapsed = (on) => {
      shell.classList.toggle("collapsed", on);
      collapseBtn.setAttribute("aria-expanded", String(!on));
      const label = on ? "展開側邊欄" : "收合側邊欄";
      collapseBtn.setAttribute("aria-label", label);
      collapseBtn.title = label;
    };
    try { setCollapsed(localStorage.getItem(SIDEBAR_KEY) === "1"); } catch (e) { setCollapsed(false); }
    collapseBtn.addEventListener("click", () => {
      const on = !shell.classList.contains("collapsed");
      setCollapsed(on);
      try { localStorage.setItem(SIDEBAR_KEY, on ? "1" : "0"); } catch (e) { /* 無法儲存時只影響本次 */ }
    });
    $("drawer-open").addEventListener("click", () => {
      shell.classList.add("drawer-open");
      $("drawer-backdrop").hidden = false;
      $("drawer-open").setAttribute("aria-expanded", "true");
      const current = document.querySelector("#tabs button.on") || document.querySelector("#tabs button");
      current.focus();
    });
    $("drawer-close").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && drawerMode()) closeDrawer();
      if (e.key === "Escape" && $("admin-menu").open) { $("admin-menu").open = false; }
    });
    // 點選管理員選單以外的地方時關閉選單
    document.addEventListener("click", (e) => {
      const menu = $("admin-menu");
      if (menu.open && !menu.contains(e.target)) menu.open = false;
    });
    // 頂部搜尋：前往農場監控並套用搜尋字
    $("top-search").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const q = e.target.value.trim().slice(0, 40);
      $("farm-search").value = q;
      state.search = q;
      showTab("farms");
      loadSearch();
    });
  }

  // ---------- 玩家搜尋（輸入名稱或 ID，從建議清單選擇） ----------
  // 建議清單的值為「#ID 名稱」；也接受直接輸入數字 ID 或完整名稱
  function pickedPlayerId(input, players) {
    const v = input.value.trim();
    if (!v) return null;
    const m = v.match(/^#?(\d+)/);
    if (m) return Number(m[1]);
    const exact = players.filter((p) => p.player_name === v);
    return exact.length === 1 ? exact[0].player_id : undefined; // undefined = 無法辨識
  }

  function fillDatalist(id, players) {
    $(id).replaceChildren(...players.map((p) => {
      const opt = document.createElement("option");
      opt.value = playerLabel(p); // value 屬性為純文字，不解析 HTML
      return opt;
    }));
  }

  // ---------- 連線狀態 ----------
  function setStatus(kind, text) {
    const pill = $("conn-status");
    pill.textContent = text;
    pill.className = `pill ${kind === "ok" ? "pill-ok" : kind === "bad" ? "pill-bad" : "pill-wait"}`;
  }

  function showError(message) {
    const banner = $("error-banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  // ---------- 提示訊息（Toast）：管理操作的成功／失敗結果；自動更新不會清掉 ----------
  const TOAST_ICONS = { good: "i-check", bad: "i-alert", warn: "i-alert", info: "i-info" };
  const TOAST_TITLES = { good: "已完成", bad: "操作失敗", warn: "請注意", info: "提示" };

  function toast(message, kind = "info") {
    const close = el("button", {});
    close.type = "button";
    close.setAttribute("aria-label", "關閉提示");
    close.appendChild(iconEl("i-x"));
    const node = el("div", { className: `toast ${kind}` }, [
      iconEl(TOAST_ICONS[kind] || "i-info"),
      el("div", {}, [el("strong", { text: TOAST_TITLES[kind] || "提示" }), el("span", { text: message })]),
      close,
    ]);
    node.setAttribute("role", kind === "bad" ? "alert" : "status");
    const remove = () => node.remove();
    close.addEventListener("click", remove);
    $("toasts").appendChild(node);
    while ($("toasts").children.length > 4) $("toasts").firstChild.remove();
    setTimeout(remove, kind === "bad" ? 9000 : 5000);
  }

  // 表單下方的結果訊息：同時以文字與樣式（✓／✕）區分成功與失敗
  function setNote(id, text, kind) {
    const note = $(id);
    note.textContent = text;
    note.classList.toggle("ok", kind === "good");
    note.classList.toggle("bad", kind === "bad");
  }

  // ---------- 確認對話框：取代 window.confirm／prompt，可顯示調整前後與要求填寫原因 ----------
  let lastClicked = null; // 最近點擊的按鈕，用於顯示送出中狀態

  function confirmDialog({ title, message, diff, confirmText = "確認", danger = false, reason = null }) {
    const dlg = $("confirm-dialog");
    const opener = lastClicked;
    $("cd-title").textContent = title;
    $("cd-message").textContent = message || "";
    $("cd-message").hidden = !message;
    dlg.classList.toggle("danger", danger);
    $("cd-ok").textContent = confirmText;
    $("cd-ok").className = danger ? "danger-solid" : "primary";
    const diffTable = $("cd-diff");
    diffTable.hidden = !diff;
    diffTable.tBodies[0].replaceChildren(...(diff || []).map(([k, a, b]) => el("tr", {}, [td(k), td(a), td(b)])));
    const reasonBox = $("cd-reason-box");
    const reasonInput = $("cd-reason");
    const reasonError = $("cd-reason-error");
    reasonBox.hidden = !reason;
    reasonInput.value = "";
    reasonError.hidden = true;
    reasonInput.classList.remove("invalid");
    if (reason) $("cd-reason-label").textContent = reason.label;
    return new Promise((resolve) => {
      const finish = (ok) => {
        $("cd-ok").removeEventListener("click", onOk);
        $("cd-cancel").removeEventListener("click", onCancel);
        dlg.removeEventListener("cancel", onCancel);
        dlg.close();
        lastClicked = ok ? opener : null; // 送出中狀態顯示在原本的按鈕上
        if (opener && opener.isConnected) opener.focus();
        resolve(ok ? { ok: true, reason: reasonInput.value.trim() } : { ok: false });
      };
      const onOk = () => {
        if (reason) {
          const len = reasonInput.value.trim().length;
          if (len < reason.min || len > reason.max) {
            reasonError.textContent = `請填寫原因（${reason.min}–${reason.max} 字），目前 ${len} 字。`;
            reasonError.hidden = false;
            reasonInput.classList.add("invalid");
            reasonInput.focus();
            return;
          }
        }
        finish(true);
      };
      const onCancel = (e) => { if (e) e.preventDefault(); finish(false); };
      $("cd-ok").addEventListener("click", onOk);
      $("cd-cancel").addEventListener("click", onCancel);
      dlg.addEventListener("cancel", onCancel); // Esc
      dlg.showModal();
      (reason ? reasonInput : $("cd-cancel")).focus(); // 預設焦點在安全的選項
    });
  }

  // ---------- 送出中狀態：防止重複點擊造成重複提交 ----------
  function trackClicks() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (btn && !btn.closest("#confirm-dialog")) lastClicked = btn;
    }, true);
  }

  function markBusy() {
    const btn = lastClicked;
    lastClicked = null;
    if (!btn || btn.disabled) return () => {};
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.setAttribute("aria-busy", "true");
    return () => {
      btn.disabled = false;
      btn.classList.remove("is-busy");
      btn.removeAttribute("aria-busy");
    };
  }

  // ---------- 表單即時驗證：指出哪個欄位有問題、如何修正（最終仍以後端驗證為準） ----------
  function fieldMessage(input) {
    const v = input.validity;
    if (v.valid) return "";
    if (v.valueMissing) return "此欄位為必填。";
    if (v.badInput) return "請輸入有效的數字。";
    if (v.rangeUnderflow) return `不可小於 ${input.min}。`;
    if (v.rangeOverflow) return `不可大於 ${input.max}。`;
    if (v.stepMismatch) return input.step === "1" || !input.step ? "必須是整數。" : `最小單位為 ${input.step}。`;
    if (v.patternMismatch) return input.dataset.patternHint || "格式不正確。";
    if (v.tooLong) return `最多 ${input.maxLength} 字。`;
    return "輸入內容不正確。";
  }

  function checkField(input) {
    if (!input.matches("input, select") || input.type === "checkbox" || input.type === "search") return true;
    const msg = input.disabled ? "" : fieldMessage(input);
    const label = input.closest("label");
    let err = label && label.querySelector(".field-error");
    if (msg && label && !err) {
      err = el("span", { className: "field-error" });
      err.id = `${input.id || `f${Math.random().toString(36).slice(2)}`}-err`;
      label.appendChild(err);
    }
    if (err) {
      err.textContent = msg;
      err.hidden = !msg;
    }
    input.classList.toggle("invalid", Boolean(msg));
    if (msg && err) input.setAttribute("aria-describedby", err.id); else input.removeAttribute("aria-describedby");
    input.setAttribute("aria-invalid", String(Boolean(msg)));
    return !msg;
  }

  // 送出前檢查整個表單；有錯時聚焦第一個錯誤欄位
  function validateForm(container) {
    let first = null;
    container.querySelectorAll("input, select").forEach((input) => {
      if (input.closest("[hidden]")) return;
      if (!checkField(input) && !first) first = input;
    });
    if (first) first.focus();
    return !first;
  }

  function initFormValidation() {
    const main = document.querySelector("main");
    // 輸入時只清除已顯示的錯誤；離開欄位時才顯示新錯誤，避免邊打字邊跳錯誤
    main.addEventListener("input", (e) => {
      if (e.target.classList.contains("invalid")) checkField(e.target);
      markDirty(e.target);
    });
    main.addEventListener("change", (e) => markDirty(e.target));
    main.addEventListener("focusout", (e) => {
      // 焦點移到按鈕時不顯示（錯誤訊息會推動版面，使按鈕點擊落空）；送出時會完整檢查
      if (e.relatedTarget && e.relatedTarget.matches("button")) return;
      if (e.target.closest(".form-grid") && e.target.value !== "") checkField(e.target);
    });
    window.addEventListener("beforeunload", (e) => {
      if (document.querySelector("details.form-box.dirty")) {
        e.preventDefault();
        e.returnValue = ""; // 瀏覽器顯示「尚有未儲存的變更」提示
      }
    });
  }

  // ---------- 未儲存變更 ----------
  function markDirty(target) {
    const box = target.closest && target.closest("details.form-box");
    if (box && target.closest(".form-grid, #rr-rewards")) box.classList.add("dirty");
  }

  function clearDirty(boxId) {
    const box = $(boxId);
    box.classList.remove("dirty");
    box.querySelectorAll(".invalid").forEach((n) => n.classList.remove("invalid"));
    box.querySelectorAll(".field-error").forEach((n) => { n.hidden = true; });
  }

  // 資料庫錯誤時清空數值，不顯示任何非即時或虛構的資料
  function clearData(reason) {
    state.snapshot = null;
    document.querySelectorAll("[data-key], [data-derived], [data-sub]").forEach((n) => { n.textContent = "—"; });
    $("plot-status-bar").replaceChildren();
    $("recent-activity").replaceChildren(el("li", { className: "empty-state is-error", text: reason }));
    replaceRows($("farm-rows"), [], reason, 13);
    for (const id of ["group-rows", "lb-rows", "steal-rows", "def-rows", "ranch-rows", "animal-rows", "order-rows",
      "quest-rows", "comp-rows", "admin-action-rows", "group-member-rows"]) {
      replaceRows($(id), [], reason, 13);
    }
    $("group-detail").hidden = true;
    $("bp-body").replaceChildren(el("p", { className: "muted", text: reason }));
    replaceRows($("tx-rows"), [], reason, 8);
    for (const id of ["chart-crops", "chart-activity", "chart-gold"]) {
      $(id).replaceChildren(el("div", { className: "chart-empty", text: reason }));
    }
    $("farm-detail").hidden = true;
  }

  // ---------- 渲染 ----------
  // 數值改變時加上短暫的強調動畫（減少動態效果偏好由 CSS 處理）
  function setNumber(node, v) {
    const text = typeof v === "number" ? fmtNum(v) : "—";
    if (node.textContent === text) return;
    const changed = node.textContent !== "—";
    node.textContent = text;
    if (changed) {
      node.classList.remove("bump");
      void node.offsetWidth; // 重新觸發動畫
      node.classList.add("bump");
    }
  }

  const PLOT_STATES = [
    ["empty", "st-empty", "空地"], ["waiting_water", "st-water", "等待澆水"], ["growing", "st-grow", "生長中"],
    ["ready", "st-ready", "可收成"], ["withered", "st-wither", "已乾枯"],
  ];

  function renderSummary(summary) {
    document.querySelectorAll("[data-key]").forEach((n) => setNumber(n, summary[n.dataset.key]));
    // 以下皆由同一份真實統計推導，不另外估算
    const s = summary;
    const empty = Math.max(0, s.plots - s.waiting_water - s.growing - s.ready - s.withered);
    const counts = { ...s, empty };
    document.querySelectorAll("[data-derived]").forEach((n) => setNumber(n, counts[n.dataset.derived]));
    const subs = {
      players: `分布於 ${fmtNum(s.groups)} 個群組`,
      farms: `共 ${fmtNum(s.plots)} 格土地`,
      total_gold: s.farms ? `平均每座農場 ${fmtNum(Math.round(s.total_gold / s.farms))}` : "尚無農場",
      transactions_today: "台灣時間今日 00:00 起累計",
    };
    document.querySelectorAll("[data-sub]").forEach((n) => { n.textContent = subs[n.dataset.sub] || "—"; });
    const bar = $("plot-status-bar");
    const total = PLOT_STATES.reduce((sum, [key]) => sum + counts[key], 0);
    bar.setAttribute("aria-label", total
      ? `土地狀態分布：${PLOT_STATES.map(([key, , label]) => `${label} ${counts[key]} 格`).join("、")}`
      : "目前沒有土地");
    bar.replaceChildren(...(total ? PLOT_STATES.filter(([key]) => counts[key] > 0).map(([key, cls, label]) => {
      const seg = el("span", { className: cls });
      seg.style.width = `${(counts[key] / total) * 100}%`; // CSSOM 設定樣式，不違反 CSP
      bindTooltip(seg, `${label}：${fmtNum(counts[key])} 格（${Math.round((counts[key] / total) * 100)}%）`);
      return seg;
    }) : []));
  }

  // ---------- 儀表板：最新遊戲活動 ----------
  const EVENT_TONES = {
    harvest: "good", sell: "good", collect_animal: "good", craft_collect: "good", quest_reward: "good",
    competition_reward: "good", ranking_reward: "good", create_farm: "info", steal: "bad", stolen: "bad",
    steal_blocked: "info", defended: "info", admin_adjust: "warn", gamble: "warn",
    explore: "good", exchange: "good",
  };
  const EVENT_ICONS = {
    harvest: "i-sprout", plant: "i-sprout", buy_seed: "i-sprout", water: "i-droplet", sell: "i-coins",
    create_farm: "i-barn", steal: "i-shield", stolen: "i-shield", steal_blocked: "i-shield", defended: "i-shield",
    defense_place: "i-shield", buy_animal: "i-paw", feed_animal: "i-paw", collect_animal: "i-paw",
    craft_start: "i-factory", craft_collect: "i-factory", build: "i-factory", quest_reward: "i-flag",
    competition_reward: "i-trophy", ranking_reward: "i-trophy", admin_adjust: "i-user-cog", gamble: "i-coins",
    explore: "i-flag", build_machine: "i-factory", exchange: "i-coins", make_seed: "i-sprout",
  };

  function iconEl(id) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", `#${id}`);
    svg.appendChild(use);
    return svg;
  }

  function renderRecentActivity(txs) {
    const list = $("recent-activity");
    if (!txs.length) {
      list.replaceChildren(el("li", { className: "empty-state", text: "目前還沒有遊戲操作紀錄" }));
      return;
    }
    list.replaceChildren(...txs.slice(0, 8).map((t) => {
      const tone = EVENT_TONES[t.event_type] || "";
      const label = EVENT_LABELS[t.event_type] || t.event_type;
      const side = el("div", { className: "act-side" });
      if (t.gold_delta) {
        side.appendChild(el("span", {
          className: t.gold_delta > 0 ? "pos" : "neg",
          text: `${t.gold_delta > 0 ? "+" : "-"}${fmtNum(Math.abs(t.gold_delta))} 金幣`,
        }));
      }
      side.appendChild(el("span", { text: fmtTime(t.created_at) }));
      return el("li", { className: "activity" }, [
        el("span", { className: `act-icon ${tone}` }, [iconEl(EVENT_ICONS[t.event_type] || "i-activity")]),
        el("div", { className: "act-main" }, [
          el("div", { className: "act-title" }, [badge(label, tone || "neutral"), el("span", { text: `${t.player_name}・${t.farm_name}` })]),
          el("div", { className: "act-desc", text: t.description || "—" }),
        ]),
        side,
      ]);
    }));
  }

  // ---------- 儀表板：營運活動（限時活動、任務、競賽；皆來自 API） ----------
  async function loadOverview() {
    const status = $("ops-status");
    try {
      const [catalog, quests, comps] = await Promise.all([
        fetchJson("/api/monitor/catalog"), fetchJson("/api/monitor/quests?limit=200"),
        fetchJson("/api/monitor/competitions?limit=200"),
      ]);
      const activeEvents = catalog.events.filter((e) => e.enabled && e.active_now);
      $("ops-events").textContent = fmtNum(activeEvents.length);
      $("ops-quests").textContent = fmtNum(quests.filter((q) => q.state === "active").length);
      $("ops-comps").textContent = fmtNum(comps.filter((c) => c.state === "active").length);
      $("ops-event-list").replaceChildren(...(activeEvents.length
        ? activeEvents.map((e) => el("li", { className: "chip", text: `${e.name}｜${e.effects_text}` }))
        : [el("li", { className: "muted small", text: "目前沒有生效中的限時活動" })]));
      status.textContent = `更新於 ${hourFmt.format(new Date())}`;
      status.classList.remove("is-error");
    } catch (err) {
      for (const id of ["ops-events", "ops-quests", "ops-comps"]) $(id).textContent = "—";
      $("ops-event-list").replaceChildren(el("li", { className: "small is-error", text: `營運活動載入失敗：${err.message}` }));
      status.textContent = "載入失敗";
      status.classList.add("is-error");
    }
  }

  function renderFarms() {
    const source = state.search ? state.searchResult : state.snapshot && state.snapshot.farms;
    if (!source) return;
    const note = $("farm-note");
    note.hidden = source.total <= source.items.length;
    note.textContent = `共 ${source.total} 座農場，列表只載入前 ${source.items.length} 座；請使用上方搜尋找到其他農場。`;
    const rows = source.items.map((f) => {
      const tr = el("tr", { className: "clickable" }, [
        td(f.player_name), td(f.farm_name), td(f.home_group_name ? `#${f.home_group_id} ${f.home_group_name}` : "未設定"),
        td(f.level, "num"), td(fmtNum(f.gold), "num"),
        td(f.plots, "num"), td(f.empty, "num"), td(f.waiting_water, "num"), td(f.growing, "num"),
        td(f.ready, "num"), td(f.withered, "num"),
        td(`${f.warehouse_used}/${f.warehouse_capacity}`, "num"), td(fmtTime(f.updated_at)),
      ]);
      if (f.farm_id === state.selectedFarmId) tr.classList.add("selected");
      tr.tabIndex = 0;
      tr.addEventListener("click", () => selectFarm(f.farm_id));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") selectFarm(f.farm_id); });
      return tr;
    });
    replaceRows($("farm-rows"), rows, state.search ? "找不到符合的農場" : "目前沒有農場", 13);
  }

  function renderTransactions(txs) {
    const rows = txs.map((t) => el("tr", {}, [
      td(fmtTime(t.created_at)), td(t.player_name), td(t.farm_name), td(EVENT_LABELS[t.event_type] || t.event_type),
      td(t.item_code || "—"), td(t.quantity, "num"), goldCell(t.gold_delta), td(t.description, "wrap"),
    ]));
    replaceRows($("tx-rows"), rows, "目前沒有操作紀錄", 8);
    renderRecentActivity(txs);
  }

  function renderDetail() {
    const d = state.detail;
    const panel = $("farm-detail");
    if (!d) { panel.hidden = true; return; }
    panel.hidden = false;
    $("detail-title").textContent = `${d.farm_name}（${d.player_name}）`;
    $("detail-meta").textContent =
      `等級 ${d.level}　金幣 ${fmtNum(d.gold)}　倉庫 ${d.warehouse_used}/${d.warehouse_capacity}　最近更新 ${fmtTime(d.updated_at)}`;
    $("detail-inventory").textContent = d.inventory.length
      ? d.inventory.map((i) => `${i.item_code} × ${i.quantity}`).join("　")
      : "（空的）";
    renderPlots();
  }

  // 剩餘時間依真實 ready_at 與伺服器時間計算，每秒更新一次
  function renderPlots() {
    const d = state.detail;
    if (!d) return;
    const now = serverNow();
    const rows = d.plots.map((p) => {
      // 與後端相同的規則：依真實時間即時結算狀態
      let remaining = "—";
      let status = "空地";
      if (p.crop_code && !p.watered_at) {
        status = "等待澆水";
      } else if (p.crop_code) {
        const left = (new Date(p.ready_at).getTime() - now) / 1000;
        if (left > 0) {
          status = "生長中";
          remaining = fmtDuration(left);
        } else {
          status = now >= new Date(p.wither_at).getTime() ? "已乾枯" : "可收成";
        }
      }
      const statusCell = statusTd(status);
      if (p.fertilized) statusCell.append(" ", badge("已施肥", "info"));
      const tr = el("tr", {}, [
        td(p.plot_number, "num"), td(p.crop_name || "—"), td(fmtTime(p.planted_at)), td(fmtTime(p.watered_at)),
        td(fmtTime(p.ready_at)), td(remaining), td(p.remaining_quantity ?? "—", "num"), statusCell,
      ]);
      if (status === "可收成") tr.classList.add("ready");
      if (status === "已乾枯") tr.classList.add("withered");
      return tr;
    });
    replaceRows($("plot-rows"), rows, "沒有土地資料", 8);
  }

  // ---------- 圖表（SVG，資料皆來自資料庫快照） ----------
  const SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  const tooltip = () => $("tooltip");
  function bindTooltip(node, text) {
    node.addEventListener("mousemove", (e) => {
      const tip = tooltip();
      tip.textContent = text;
      tip.hidden = false;
      tip.style.left = `${Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8)}px`;
      tip.style.top = `${e.clientY + 12}px`;
    });
    node.addEventListener("mouseleave", () => { tooltip().hidden = true; });
  }

  function niceMax(v) {
    if (v <= 4) return 4;
    const mag = 10 ** Math.floor(Math.log10(v));
    const step = [1, 2, 2.5, 5, 10].find((s) => s * mag * 4 >= v) || 10;
    return step * mag * 4;
  }

  const W = 320, H = 190, PAD = { l: 44, r: 8, t: 8, b: 26 };

  function axes(svg, max, fmt = fmtNum, min = 0) {
    for (let i = 0; i <= 4; i++) {
      const v = min + ((max - min) / 4) * i;
      const y = PAD.t + (H - PAD.t - PAD.b) * (1 - i / 4);
      svg.appendChild(svgEl("line", { class: "gridline", x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
      svg.appendChild(svgEl("text", { class: "axis", x: PAD.l - 6, y: y + 4, "text-anchor": "end" }, fmt(v)));
    }
  }

  // 圓角只在資料端（上方），底部貼齊基線
  function barPath(x, y, w, h) {
    if (h <= 0) return "";
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  function renderCropChart(crops) {
    const box = $("chart-crops");
    const total = crops.reduce((s, c) => s + c.growing + c.ready, 0);
    if (!total) {
      box.replaceChildren(el("div", { className: "chart-empty", text: "目前沒有種植中的作物" }));
      return;
    }
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各作物生長中與可收成數量" });
    const max = niceMax(Math.max(...crops.map((c) => Math.max(c.growing, c.ready))));
    axes(svg, max);
    const plotH = H - PAD.t - PAD.b;
    const groupW = (W - PAD.l - PAD.r) / crops.length;
    const barW = Math.min(22, groupW / 3);
    crops.forEach((c, i) => {
      const gx = PAD.l + groupW * i + groupW / 2;
      [[c.growing, "bar1", -barW - 1], [c.ready, "bar2", 1]].forEach(([v, cls, dx]) => {
        const h = (v / max) * plotH;
        svg.appendChild(svgEl("path", { class: cls, d: barPath(gx + dx, PAD.t + plotH - h, barW, h) }));
      });
      svg.appendChild(svgEl("text", { class: "axis", x: gx, y: H - 8, "text-anchor": "middle" }, c.name));
      const hit = svgEl("rect", { class: "hit", x: gx - groupW / 2, y: PAD.t, width: groupW, height: plotH });
      bindTooltip(hit, `${c.name}\n生長中：${c.growing}\n可收成：${c.ready}`);
      svg.appendChild(hit);
    });
    box.replaceChildren(svg);
  }

  function renderActivityChart(activity, hasHistory) {
    const box = $("chart-activity");
    if (!hasHistory || !activity.some((a) => a.transactions > 0)) {
      box.replaceChildren(el("div", { className: "chart-empty", text: "近 12 小時沒有操作紀錄" }));
      return;
    }
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "每小時操作數" });
    const max = niceMax(Math.max(...activity.map((a) => a.transactions)));
    axes(svg, max);
    const plotH = H - PAD.t - PAD.b;
    const slot = (W - PAD.l - PAD.r) / activity.length;
    const barW = Math.max(4, slot - 2); // 相鄰長條間留 2px 間隔
    activity.forEach((a, i) => {
      const x = PAD.l + slot * i + (slot - barW) / 2;
      const h = (a.transactions / max) * plotH;
      svg.appendChild(svgEl("path", { class: "bar1", d: barPath(x, PAD.t + plotH - h, barW, h) }));
      if (i % 3 === 0 || i === activity.length - 1) {
        svg.appendChild(svgEl("text", { class: "axis", x: x + barW / 2, y: H - 8, "text-anchor": "middle" }, hourFmt.format(new Date(a.hour))));
      }
      const hit = svgEl("rect", { class: "hit", x: PAD.l + slot * i, y: PAD.t, width: slot, height: plotH });
      bindTooltip(hit, `${hourFmt.format(new Date(a.hour))} 起一小時\n操作數：${a.transactions}`);
      svg.appendChild(hit);
    });
    box.replaceChildren(svg);
  }

  function renderGoldChart(activity, hasHistory) {
    const box = $("chart-gold");
    if (!hasHistory) {
      box.replaceChildren(el("div", { className: "chart-empty", text: "尚無交易紀錄，無法顯示趨勢" }));
      return;
    }
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "全服金幣總量趨勢" });
    // 一般情況金幣總量不會是負數；若資料被直接修改導致負值，也要畫在圖表範圍內
    const values = activity.map((a) => a.total_gold);
    const low = Math.min(0, ...values);
    const max = low + niceMax(Math.max(...values) - low || 1);
    const compact = (v) => {
      const a = Math.abs(v);
      if (a >= 1e8) return `${+(v / 1e8).toFixed(1)}億`;
      if (a >= 1e4) return `${+(v / 1e4).toFixed(1)}萬`;
      return fmtNum(Math.round(v));
    };
    axes(svg, max, compact, low);
    const plotH = H - PAD.t - PAD.b;
    const step = (W - PAD.l - PAD.r) / (activity.length - 1);
    const pts = activity.map((a, i) => [PAD.l + step * i, PAD.t + plotH - ((a.total_gold - low) / (max - low)) * plotH]);
    svg.appendChild(svgEl("path", { class: "line1", d: pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join("") }));
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl("circle", { class: "dot1", cx: last[0], cy: last[1], r: 4 }));
    activity.forEach((a, i) => {
      if (i % 3 === 0 || i === activity.length - 1) {
        svg.appendChild(svgEl("text", { class: "axis", x: pts[i][0], y: H - 8, "text-anchor": "middle" }, hourFmt.format(new Date(a.hour))));
      }
      const hit = svgEl("rect", { class: "hit", x: pts[i][0] - step / 2, y: PAD.t, width: step, height: plotH });
      bindTooltip(hit, `${hourFmt.format(new Date(a.hour))} 時段結束時\n總金幣：${fmtNum(a.total_gold)}`);
      svg.appendChild(hit);
    });
    box.replaceChildren(svg);
  }

  // ---------- 資料取得 ----------
  async function fetchJson(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      const err = new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  async function loadDetail() {
    if (state.selectedFarmId === null) return;
    const id = state.selectedFarmId;
    try {
      const detail = await fetchJson(`/api/monitor/farms/${encodeURIComponent(id)}`);
      if (id !== state.selectedFarmId) return;
      state.serverOffsetMs = new Date(detail.server_time).getTime() - Date.now();
      state.detail = detail;
      renderDetail();
    } catch (err) {
      if (err.status === 404) state.selectedFarmId = null;
      state.detail = null;
      renderDetail();
      showError(`農場詳情載入失敗：${err.message}`);
    }
  }

  function selectFarm(id) {
    state.selectedFarmId = id;
    renderFarms();
    loadDetail();
  }

  async function loadSearch() {
    if (!state.search) { state.searchResult = null; renderFarms(); return; }
    const q = state.search;
    try {
      const result = await fetchJson(`/api/monitor/farms?limit=200&q=${encodeURIComponent(q)}`); // 前端分頁
      if (q !== state.search) return;
      state.searchResult = result;
      renderFarms();
    } catch (err) {
      showError(`搜尋失敗：${err.message}`);
    }
  }

  function onSnapshot(data) {
    showError("");
    document.body.classList.remove("stale");
    state.snapshot = data;
    state.serverOffsetMs = new Date(data.server_time).getTime() - Date.now();
    state.lastSync = new Date();
    $("last-sync").textContent = dtFmt.format(state.lastSync);
    setStatus("ok", "已連線・即時更新中");
    renderSummary(data.summary);
    renderTransactions(data.transactions);
    renderCropChart(data.charts.crops);
    renderActivityChart(data.charts.activity, data.charts.has_history);
    renderGoldChart(data.charts.activity, data.charts.has_history);
    if (state.search) loadSearch(); else renderFarms();
    loadDetail();
    refreshExtras();
  }

  // ---------- 群組、排行榜、偷菜紀錄、背包 ----------
  const extra = {
    players: [],
    groups: [],
    selectedGroupId: null,
    lbScope: "global",
    lbKind: "money",
    lbPeriod: "all",
    lbGroupId: null,
    bpPlayerId: null,
    admin: { enabled: false, loggedIn: false, username: null },
  };
  const playerLabel = (p) => `#${p.player_id} ${p.player_name}`;

  // ---------- 作物品質與突變 ----------
  const genetics = { data: null, formBuilt: false };
  const pctText = (bp) => `${(bp / 100).toFixed(2)}%`;
  const mulText = (pct) => `×${(pct / 100).toFixed(2)}`;

  function geneticsLabel(labels, quality, mutation) {
    const parts = [labels.qualities[quality] || quality];
    if (mutation && mutation !== "none") parts.push(labels.mutations[mutation] || mutation);
    return parts.join("・");
  }

  function numberInput(id, label, value, step, min, max) {
    const input = el("input");
    input.type = "number";
    input.id = id;
    input.step = String(step);
    input.min = String(min);
    input.max = String(max);
    input.value = String(value);
    return el("label", { text: label }, [input]);
  }

  // 表單以「%」與「倍」輸入，送出前轉成後端使用的整數（萬分比、百分比）
  function buildGeneticsForm(d) {
    const s = d.settings;
    const q = d.labels.qualities;
    const m = d.labels.mutations;
    const fields = [];
    for (const code of Object.keys(q)) {
      fields.push(numberInput(`gf-q-${code}`, `${q[code]}機率（%）`, s.quality_bp[code] / 100, 0.01, 0, 100));
    }
    for (const code of Object.keys(q)) {
      fields.push(numberInput(`gf-qp-${code}`, `${q[code]}售價倍率（倍）`, s.quality_price_pct[code] / 100, 0.01, 0.5, 10));
    }
    for (const code of ["giant", "golden", "fast", "variant"]) {
      fields.push(numberInput(`gf-m-${code}`, `${m[code]}機率（%）`, s.mutation_bp[code] / 100, 0.01, 0, 20));
    }
    fields.push(numberInput("gf-giant", "巨型作物數量倍率（倍，整數）", s.giant_multiplier, 1, 1, 5));
    fields.push(numberInput("gf-golden", "黃金作物售價倍率（倍）", s.golden_price_pct / 100, 0.01, 1, 10));
    fields.push(numberInput("gf-fast", "快速生長減少秒數", s.fast_reduction_seconds, 1, 0, 604800));
    fields.push(numberInput("gf-variant", "珍稀變種售價倍率（倍）", s.variant_price_pct / 100, 0.01, 1, 20));
    fields.push(numberInput("gf-level", "每級品質加成（%）", s.level_bonus_bp / 100, 0.01, 0, 2));
    fields.push(numberInput("gf-level-max", "品質加成上限（%）", s.level_bonus_max_bp / 100, 0.01, 0, 20));
    $("genetics-form").replaceChildren(...fields);
    checkGeneticsForm();
  }

  const toInt = (id, scale) => Math.round(Number($(id).value) * scale);

  function readGeneticsForm() {
    const q = Object.keys(genetics.data.labels.qualities);
    return {
      quality_bp: Object.fromEntries(q.map((c) => [c, toInt(`gf-q-${c}`, 100)])),
      quality_price_pct: Object.fromEntries(q.map((c) => [c, toInt(`gf-qp-${c}`, 100)])),
      mutation_bp: Object.fromEntries(["giant", "golden", "fast", "variant"].map((c) => [c, toInt(`gf-m-${c}`, 100)])),
      giant_multiplier: toInt("gf-giant", 1),
      golden_price_pct: toInt("gf-golden", 100),
      fast_reduction_seconds: toInt("gf-fast", 1),
      variant_price_pct: toInt("gf-variant", 100),
      level_bonus_bp: toInt("gf-level", 100),
      level_bonus_max_bp: toInt("gf-level-max", 100),
    };
  }

  // 前端只做即時提示；是否有效以後端驗證為準
  function checkGeneticsForm() {
    const body = readGeneticsForm();
    const total = Object.values(body.quality_bp).reduce((a, b) => a + b, 0);
    const mutTotal = Object.values(body.mutation_bp).reduce((a, b) => a + b, 0);
    const ok = total === 10000;
    $("genetics-form-check").textContent =
      `品質機率總和 ${pctText(total)}${ok ? "" : "（必須為 100%）"}；突變機率總和 ${pctText(mutTotal)}（上限 50%）`;
    $("genetics-submit").disabled = !ok;
  }

  function statList(id, rows, labels, render) {
    $(id).replaceChildren(...(rows.length
      ? rows.map((r) => el("li", { text: `${geneticsLabel(labels, r.quality, r.mutation)}：${render(r)}` }))
      : [el("li", { className: "muted", text: "目前沒有紀錄" })]));
  }

  async function loadGenetics() {
    document.querySelectorAll("#genetics-form-box").forEach((n) => { n.hidden = !extra.admin.loggedIn; });
    try {
      const [d, stats] = await Promise.all([fetchJson("/api/monitor/genetics"), fetchJson("/api/monitor/genetics/stats")]);
      genetics.data = d;
      const s = d.settings;
      const last = d.history[0];
      $("genetics-meta").textContent =
        `目前設定版本 ${d.version}${last ? `（${last.created_by}，${fmtTime(last.created_at)}）` : "（預設值）"}；`
        + `最終單價 = 基本售價 × 品質倍率 × 突變售價倍率（無條件捨去）；農場每高 1 級品質加成 ${pctText(s.level_bonus_bp)}，`
        + `上限 ${pctText(s.level_bonus_max_bp)}。`;
      $("gen-quality-rows").replaceChildren(...Object.entries(d.labels.qualities).map(([code, name]) => el("tr", {}, [
        td(name), td(pctText(s.quality_bp[code]), "num"), td(mulText(s.quality_price_pct[code]), "num"),
      ])));
      const effects = {
        giant: `收成數量 ×${s.giant_multiplier}`,
        golden: `基本售價 ${mulText(s.golden_price_pct)}`,
        fast: `生長時間減少 ${fmtDuration(s.fast_reduction_seconds)}（最短 ${fmtDuration(d.min_grow_seconds)}，仍需澆水）`,
        variant: `獨立的珍稀作物，基本售價 = 原作物 ${mulText(s.variant_price_pct)}`,
      };
      $("gen-mutation-rows").replaceChildren(...["giant", "golden", "fast", "variant"].map((code) => el("tr", {}, [
        td(d.labels.mutations[code]), td(pctText(s.mutation_bp[code]), "num"), td(effects[code], "wrap"),
      ])));
      $("gen-crop-rows").replaceChildren(...d.crops.map((c) => el("tr", {}, [
        td(`${c.name}（${c.code}）`), td(`${c.grow_seconds}（${fmtDuration(c.grow_seconds)}）`, "num"),
        td(c.sell_price, "num"), td("在「遊戲管理 → 作物種類」修改", "muted"),
      ])));
      if (extra.admin.loggedIn && !genetics.formBuilt) {
        buildGeneticsForm(d);
        genetics.formBuilt = true;
      }
      const L = d.labels;
      statList("gen-stat-planted", stats.planted, L, (r) => `${r.plots} 格`);
      statList("gen-stat-harvested", stats.harvested, L, (r) => `${r.count} 次，共 ${fmtNum(r.quantity)} 個`);
      statList("gen-stat-stolen", stats.stolen, L, (r) => `${r.count} 次，共 ${fmtNum(r.quantity)} 個`);
      statList("gen-stat-sold", stats.sold, L, (r) => `${r.count} 筆，${fmtNum(r.quantity)} 個，收入 ${fmtNum(r.income)}`);
      $("gen-stat-holdings").replaceChildren(...(stats.holdings.length
        ? stats.holdings.map((h) => el("li", { text: `${h.item_code}（${geneticsLabel(L, h.quality, h.mutation)}）× ${fmtNum(h.quantity)}` }))
        : [el("li", { className: "muted", text: "目前沒有紀錄" })]));
    } catch (err) {
      $("genetics-meta").textContent = `品質與突變資料載入失敗：${err.message}`;
    }
  }

  function initGenetics() {
    $("genetics-form").addEventListener("input", checkGeneticsForm);  // 只註冊一次（表單內容可重建）
    $("genetics-submit").addEventListener("click", async () => {
      if (!validateForm($("genetics-form"))) return;
      const answer = await confirmDialog({
        title: "套用新的品質與突變設定",
        message: "會建立新的設定版本，只影響之後播種的作物與之後的販售單價；已種下的作物不變。",
        confirmText: "套用設定",
      });
      if (!answer.ok) return;
      try {
        const res = await adminRequest("PUT", "/api/admin/genetics", readGeneticsForm());
        genetics.formBuilt = false;
        clearDirty("genetics-form-box");
        await loadGenetics();
        $("genetics-form-check").textContent = `已套用設定版本 ${res.version}。`;
        toast(`已套用品質與突變設定版本 ${res.version}。`, "good");
      } catch (err) {
        $("genetics-form-check").textContent = `設定未套用：${err.message}`;
        toast(`設定未套用：${err.message}`, "bad");
      }
    });
  }

  // ---------- 群組任務與競賽 ----------
  const community = { questId: null, compId: null };
  const ACTION_LABELS = {
    create_quest: "建立任務", cancel_quest: "取消任務", create_competition: "建立競賽",
    cancel_competition: "取消競賽", settle_competition: "結算競賽",
    update_genetics: "調整品質設定", update_crop_grow_time: "調整生長時間",
    create_crop: "新增作物", update_crop: "修改作物", enable_crop: "啟用作物", disable_crop: "停用作物", delete_crop: "刪除作物",
    create_animal_type: "新增動物", update_animal_type: "修改動物", enable_animal_type: "啟用動物",
    disable_animal_type: "停用動物", delete_animal_type: "刪除動物",
    create_event: "新增活動", update_event: "修改活動", enable_event: "啟用活動", disable_event: "停用活動", delete_event: "刪除活動",
    set_inventory: "調整背包", add_plots: "新增土地", delete_plot: "刪除土地", plant_for_player: "代為種植",
    clear_plot: "清除作物", add_building: "新增設施", delete_building: "刪除設施", add_animals: "新增動物給玩家",
    delete_animal: "刪除玩家動物", set_farm_values: "調整金幣與等級",
    create_ranking_rule: "新增排名獎勵", update_ranking_rule: "修改排名獎勵", enable_ranking_rule: "啟用排名獎勵",
    disable_ranking_rule: "停用排名獎勵", delete_ranking_rule: "刪除排名獎勵", settle_ranking: "結算排名獎勵",
  };

  // datetime-local 預設值：現在與 7 天後（本機時間）
  function localInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function isoFromLocal(value) {
    return value ? new Date(value).toISOString() : null; // 轉成含時區的 ISO，交由後端驗證
  }

  async function cancelEvent(kind, id, name) {
    const what = kind === "quests" ? "任務" : "競賽";
    const answer = await confirmDialog({
      title: `取消${what}「${name}」`,
      message: `取消後${what}會立即結束且無法恢復，已發出的獎勵不會收回。`,
      confirmText: `確認取消${what}`,
      danger: true,
      reason: { label: "取消原因（必填，2–200 字，會寫入管理操作紀錄）", min: 2, max: 200 },
    });
    if (!answer.ok) return;
    try {
      await adminRequest("POST", `/api/admin/${kind}/${id}/cancel`, { reason: answer.reason });
      toast(`已取消${what}「${name}」。`, "good");
      await loadCommunity();
    } catch (err) {
      toast(`取消${what}失敗：${err.message}`, "bad");
    }
  }

  function cancelButton(kind, id, name) {
    const btn = el("button", { className: "danger", text: "取消" });
    btn.type = "button";
    btn.addEventListener("click", (e) => { e.stopPropagation(); cancelEvent(kind, id, name); });
    return el("td", {}, extra.admin.loggedIn ? [btn] : []);
  }

  async function loadQuestDetail() {
    const id = community.questId;
    if (id === null) { $("quest-detail").hidden = true; return; }
    try {
      const q = await fetchJson(`/api/monitor/quests/${encodeURIComponent(id)}`);
      if (id !== community.questId) return;
      $("quest-detail").hidden = false;
      $("quest-detail-title").textContent = `任務 #${q.quest_id}「${q.name}」（${q.state_label}）`;
      $("quest-detail-meta").textContent =
        `${q.group_name}｜${q.goal_text}｜進度 ${q.progress}/${q.target}｜${q.reward_text}｜`
        + `${fmtTime(q.starts_at)} ～ ${fmtTime(q.ends_at)}${q.cancel_reason ? `｜取消原因：${q.cancel_reason}` : ""}`;
      $("quest-contributors").replaceChildren(...(q.contributors.length
        ? q.contributors.map((c) => el("li", { text: `${c.player_name}：${c.amount}` }))
        : [el("li", { text: "尚無貢獻" })]));
      $("quest-claims").replaceChildren(...(q.claims.length
        ? q.claims.map((c) => el("li", { text: `${c.player_name}：${c.amount}（${fmtTime(c.claimed_at)}）` }))
        : [el("li", { text: "尚無領取" })]));
    } catch (err) {
      $("quest-detail").hidden = true;
    }
  }

  async function loadCompDetail() {
    const id = community.compId;
    if (id === null) { $("comp-detail").hidden = true; return; }
    try {
      const c = await fetchJson(`/api/monitor/competitions/${encodeURIComponent(id)}`);
      if (id !== community.compId) return;
      $("comp-detail").hidden = false;
      $("comp-detail-title").textContent =
        `競賽 #${c.competition_id}「${c.name}」${c.final ? "最終排名" : "即時排名"}（${c.state_label}）`;
      $("comp-detail-meta").textContent = `${c.type_name}｜${c.scoring_rule}${c.cancel_reason ? `｜取消原因：${c.cancel_reason}` : ""}`;
      $("comp-ranking").replaceChildren(...(c.ranking.length
        ? c.ranking.map((r) => el("li", { text: `${r.group_name}：${fmtNum(r.score)} 分（${r.contributors} 人貢獻）` }))
        : [el("li", { text: "尚無群組得分" })]));
      $("comp-players").replaceChildren(...(c.top_players.length
        ? c.top_players.map((p) => el("li", { text: `${p.player_name}（${p.group_name}）：${fmtNum(p.points)} 分` }))
        : [el("li", { text: "尚無貢獻" })]));
      $("comp-awards").replaceChildren(...(c.awards.length
        ? c.awards.map((a) => el("li", { text: `第 ${a.rank} 名 ${a.player_name}：${fmtNum(a.gold)} 金幣` }))
        : [el("li", { text: c.final ? "無人獲獎" : "結算後發放" })]));
    } catch (err) {
      $("comp-detail").hidden = true;
    }
  }

  function progressTd(progress, target, percent) {
    const bar = el("span", { className: "progress" }, [el("span", { className: "progress-fill" })]);
    bar.firstChild.style.width = `${Math.min(100, percent)}%`;
    return el("td", { className: "num" }, [el("span", { className: "small", text: `${progress}/${target}` }), bar]);
  }

  async function loadCommunity() {
    fillSelect($("qf-group"), extra.groups.map((g) => [g.group_id, `#${g.group_id} ${g.name}`]), "選擇群組…");
    try {
      const [quests, comps, actions] = await Promise.all([
        fetchJson("/api/monitor/quests?limit=200"), fetchJson("/api/monitor/competitions?limit=200"),
        fetchJson("/api/monitor/admin-actions?limit=100"),
      ]);
      const questRows = quests.map((q) => {
        const tr = el("tr", { className: "clickable" }, [
          td(q.quest_id, "num"), td(q.group_name), td(q.name), td(q.goal_text),
          progressTd(q.progress, q.target, q.percent), statusTd(q.state_label), td(q.reward_text, "wrap"),
          td(fmtTime(q.ends_at)),
          q.state === "active" || q.state === "scheduled" ? cancelButton("quests", q.quest_id, q.name) : td(""),
        ]);
        if (q.quest_id === community.questId) tr.classList.add("selected");
        tr.addEventListener("click", () => { community.questId = q.quest_id; loadCommunity(); });
        return tr;
      });
      replaceRows($("quest-rows"), questRows, "目前沒有任務", 9);
      const compRows = comps.map((c) => {
        const tr = el("tr", { className: "clickable" }, [
          td(c.competition_id, "num"), td(c.name), td(c.type_name), statusTd(c.state_label),
          td(`${fmtTime(c.starts_at)} ～ ${fmtTime(c.ends_at)}`), td(c.rewards.map((g) => fmtNum(g)).join(" / ")),
          c.state === "active" || c.state === "scheduled" ? cancelButton("competitions", c.competition_id, c.name) : td(""),
        ]);
        if (c.competition_id === community.compId) tr.classList.add("selected");
        tr.addEventListener("click", () => { community.compId = c.competition_id; loadCommunity(); });
        return tr;
      });
      replaceRows($("comp-rows"), compRows, "目前沒有競賽", 7);
      const actionRows = actions.map((a) => el("tr", {}, [
        td(fmtTime(a.created_at)), td(a.actor), td(ACTION_LABELS[a.action] || a.action),
        td(`${a.target_type} #${a.target_id ?? "-"}`),
        td(a.details.reason ? `原因：${a.details.reason}` : a.details.name || JSON.stringify(a.details).slice(0, 80), "wrap"),
      ]));
      replaceRows($("admin-action-rows"), actionRows, "目前沒有管理操作紀錄", 5);
    } catch (err) {
      for (const id of ["quest-rows", "comp-rows", "admin-action-rows"]) {
        replaceRows($(id), [], `任務與競賽資料載入失敗：${err.message}`, 9);
      }
    }
    await Promise.all([loadQuestDetail(), loadCompDetail()]);
  }

  function initCommunity() {
    const now = new Date();
    for (const [start, end] of [["qf-start", "qf-end"], ["cf-start", "cf-end"]]) {
      $(start).value = localInput(now);
      $(end).value = localInput(new Date(now.getTime() + 7 * 86400000));
    }
    $("qf-submit").addEventListener("click", async () => {
      const filter = $("qf-filter").value;
      const body = {
        group_id: Number($("qf-group").value), name: $("qf-name").value.trim(), description: $("qf-desc").value.trim(),
        quest_type: $("qf-type").value, metric: $("qf-metric").value, item_filter: filter || null,
        target: Number($("qf-target").value), reward_mode: $("qf-mode").value, reward_type: $("qf-reward").value,
        reward_amount: Number($("qf-amount").value),
        starts_at: isoFromLocal($("qf-start").value), ends_at: isoFromLocal($("qf-end").value),
      };
      if (!validateForm($("quest-form-box"))) return;
      if (body.ends_at && body.starts_at && body.ends_at <= body.starts_at) {
        toast("截止時間必須晚於開始時間。", "bad");
        $("qf-end").focus();
        return;
      }
      try {
        const q = await adminRequest("POST", "/api/admin/quests", body);
        community.questId = q.quest_id;
        $("qf-name").value = "";
        $("qf-desc").value = "";
        clearDirty("quest-form-box");
        toast(`已建立任務 #${q.quest_id}「${body.name}」。`, "good");
        await loadCommunity();
      } catch (err) {
        toast(`建立任務失敗：${err.message}`, "bad");
      }
    });
    $("cf-submit").addEventListener("click", async () => {
      const body = {
        name: $("cf-name").value.trim(), description: $("cf-desc").value.trim(), comp_type: $("cf-type").value,
        rewards: ["cf-r1", "cf-r2", "cf-r3"].map((id) => Number($(id).value || 0)),
        starts_at: isoFromLocal($("cf-start").value), ends_at: isoFromLocal($("cf-end").value),
      };
      if (!validateForm($("comp-form-box"))) return;
      if (body.ends_at && body.starts_at && body.ends_at <= body.starts_at) {
        toast("截止時間必須晚於開始時間。", "bad");
        $("cf-end").focus();
        return;
      }
      try {
        const c = await adminRequest("POST", "/api/admin/competitions", body);
        community.compId = c.competition_id;
        $("cf-name").value = "";
        $("cf-desc").value = "";
        clearDirty("comp-form-box");
        toast(`已建立競賽 #${c.competition_id}「${body.name}」。`, "good");
        await loadCommunity();
      } catch (err) {
        toast(`建立競賽失敗：${err.message}`, "bad");
      }
    });
    $("comp-settle").addEventListener("click", async () => {
      const answer = await confirmDialog({
        title: "結算到期競賽",
        message: "會結算所有已截止但尚未結算的競賽並立即發放獎勵；同一競賽只會發獎一次。",
        confirmText: "開始結算",
      });
      if (!answer.ok) return;
      try {
        const res = await adminRequest("POST", "/api/admin/competitions/settle-due");
        toast(res.settled.length ? `已結算 ${res.settled.length} 個競賽。` : "目前沒有需要結算的競賽。",
          res.settled.length ? "good" : "info");
        await loadCommunity();
      } catch (err) {
        toast(`結算失敗：${err.message}`, "bad");
      }
    });
  }

  // ---------- 管理員登入（session 存在 HttpOnly cookie，JS 無法讀取） ----------
  function renderAdmin() {
    const a = extra.admin;
    $("admin-login").hidden = !a.enabled || a.loggedIn;
    $("admin-bar").hidden = !a.loggedIn;
    document.querySelectorAll(".admin-only").forEach((n) => { n.hidden = !a.loggedIn; });
    document.querySelectorAll(".guest-only").forEach((n) => { n.hidden = a.loggedIn; });
    $("admin-menu").classList.toggle("logged-in", a.loggedIn);
    $("admin-trigger-text").textContent = a.loggedIn ? a.username || "管理員" : a.enabled ? "管理員登入" : "檢視模式";
    $("admin-name").textContent = a.username || "";
    $("admin-note").textContent = !a.enabled
      ? "未設定管理員帳號，管理功能停用（僅能檢視）。"
      : a.loggedIn ? "" : "登入後可管理任務、競賽、作物、動物、活動與玩家資料。";
  }

  async function loadAuth() {
    try {
      const me = await fetchJson("/api/auth/me");
      extra.admin = { enabled: me.enabled, loggedIn: me.logged_in, username: me.username };
    } catch (err) {
      extra.admin = { enabled: false, loggedIn: false, username: null };
    }
    renderAdmin();
  }

  async function adminLogin() {
    const username = $("admin-user").value.trim();
    const password = $("admin-pass").value;
    $("admin-pass").value = ""; // 不論成功與否都清除密碼欄位
    // 登入結果顯示在登入區塊下方（全域錯誤橫幅會被每次快照清除）
    if (!username || !password) { $("admin-note").textContent = "請輸入管理員帳號與密碼。"; return; }
    try {
      await adminRequest("POST", "/api/auth/login", { username, password });
      await loadAuth();
      $("admin-menu").open = false;
      toast(`已登入管理員 ${extra.admin.username || ""}`, "good");
      await Promise.all([loadCommunity(), loadGenetics(), loadCatalog(), loadPlayerAdmin(), loadRankingRewards()]);
    } catch (err) {
      $("admin-note").textContent = `登入失敗：${err.message}`;
    }
  }

  async function adminLogout() {
    try {
      await adminRequest("POST", "/api/auth/logout");
    } catch (err) { /* 即使失敗也回到未登入狀態 */ }
    await loadAuth();
    $("admin-menu").open = false;
    toast("已登出管理員", "info");
    genetics.formBuilt = false;
    await Promise.all([loadCommunity(), loadGenetics(), loadCatalog(), loadRankingRewards()]);
  }

  // 下拉選單：保留目前選擇；選項文字由 Option 建構子設定（純文字，不解析 HTML）
  function fillSelect(select, items, placeholder) {
    const current = select.value;
    const opts = [new Option(placeholder, "")];
    for (const [value, label] of items) opts.push(new Option(label, String(value)));
    select.replaceChildren(...opts);
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  }

  async function adminRequest(method, url, body) {
    const release = markBusy(); // 送出期間停用觸發的按鈕，避免重複提交
    try {
      return await sendAdminRequest(method, url, body);
    } finally {
      release();
    }
  }

  async function sendAdminRequest(method, url, body) {
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: { "X-Farmgm-Admin": "1", "Content-Type": "application/json", Accept: "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      throw new Error("無法連線到伺服器，請確認網路後再試（操作可能未完成，請重新整理確認結果）");
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        detail = typeof data.detail === "string" ? data.detail : "輸入資料不正確";
      } catch (e) { /* ignore */ }
      // session 過期或被登出：回到未登入畫面（登入請求本身的 401 是帳密錯誤，不需重新整理）
      if (res.status === 401 && !url.startsWith("/api/auth/")) {
        extra.admin.loggedIn = false;
        renderAdmin();
        toast("管理員登入已逾時，請從右上角重新登入後再操作。", "warn");
        detail = "登入已逾時，請重新登入";
      } else if (res.status === 403 && detail === "HTTP 403") {
        detail = "沒有執行此操作的權限";
      }
      throw new Error(detail);
    }
    return res.json();
  }

  async function loadPlayersAndGroups() {
    [extra.players, extra.groups] = await Promise.all([
      fetchJson("/api/monitor/players"), fetchJson("/api/monitor/groups"),
    ]);
    const groupItems = extra.groups.map((g) => [g.group_id, `#${g.group_id} ${g.name}`]);
    fillDatalist("player-options", extra.players);
    fillDatalist("farm-player-options", extra.players.filter((p) => p.farm_id));
    fillSelect($("steal-group"), groupItems, "全部群組");
    fillSelect($("lb-group"), groupItems, "選擇群組…");
    renderGroups();
  }

  function renderGroups() {
    const rows = extra.groups.map((g) => {
      const tr = el("tr", { className: "clickable" }, [
        td(g.group_id, "num"), td(g.name), td(g.member_count, "num"), td(g.discord_guild_id || "—"), td(fmtTime(g.created_at)),
      ]);
      if (g.group_id === extra.selectedGroupId) tr.classList.add("selected");
      tr.tabIndex = 0;
      const pick = () => { extra.selectedGroupId = g.group_id; renderGroups(); loadGroupDetail(); };
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
      return tr;
    });
    replaceRows($("group-rows"), rows, "目前沒有群組", 5);
  }

  async function loadGroupDetail() {
    const id = extra.selectedGroupId;
    if (id === null) { $("group-detail").hidden = true; return; }
    try {
      const g = await fetchJson(`/api/monitor/groups/${encodeURIComponent(id)}`);
      if (id !== extra.selectedGroupId) return;
      $("group-detail").hidden = false;
      $("group-detail-title").textContent = `#${g.group_id}「${g.name}」成員（${g.members.length} 人）`;
      const rows = g.members.map((m) => {
        const f = m.farm;
        const cells = f
          ? [td(f.farm_name), td(f.level, "num"), td(fmtNum(f.gold), "num"), td(f.plots, "num"), td(f.waiting_water, "num"),
             td(f.growing, "num"), td(f.ready, "num"), td(f.withered, "num"), td(`${f.warehouse_used}/${f.warehouse_capacity}`, "num"),
             f.ranked_in_group ? statusTd("是")
               : statusTd(f.home_group_id ? `否（主要群組 #${f.home_group_id}）` : "否（農場未設定群組）", "muted")]
          : [el("td", { text: "（尚未建立農場）", colSpan: 10 })];
        return el("tr", {}, [td(playerLabel(m)), ...cells, td(fmtTime(m.joined_at))]);
      });
      replaceRows($("group-member-rows"), rows, "這個群組目前沒有成員", 12);
    } catch (err) {
      if (err.status === 404) extra.selectedGroupId = null;
      $("group-detail").hidden = true;
      showError(`群組資料載入失敗：${err.message}`);
    }
  }

  const LB_PERIODS = { all: "全部期間", day: "本日", week: "本週", month: "本月" };

  function renderLeaderboardHead() {
    const period = extra.lbPeriod !== "all";
    const groups = extra.lbScope === "groups";
    const who = groups ? [["群組", ""], ["成員", "num"]] : [["玩家", ""]];
    let cols;
    if (extra.lbKind === "money") {
      cols = period
        ? [["排名", "num"], ...who, [`${LB_PERIODS[extra.lbPeriod]}淨賺金幣`, "num"], [groups ? "目前金幣加總" : "目前金幣", "num"]]
        : [["排名", "num"], ...who, ...(groups ? [] : [["農場等級", "num"]]), [groups ? "目前金幣加總" : "目前金幣", "num"]];
    } else {
      const span = period ? LB_PERIODS[extra.lbPeriod] : "累積";
      cols = [["排名", "num"], ...who, [`${span}${extra.lbKind === "goods" ? "商品販售數量" : "販售數量"}`, "num"],
        [`${span}販售收入`, "num"], [groups ? "目前金幣加總" : "目前金幣", "num"]];
    }
    $("lb-head").replaceChildren(el("tr", {}, cols.map(([t, c]) => el("th", { text: t, className: c }))));
    return cols.length;
  }

  function leaderboardRow(r) {
    const period = extra.lbPeriod !== "all";
    const who = extra.lbScope === "groups"
      ? [td(`#${r.group_id} ${r.group_name}`), td(r.member_count, "num")] : [td(r.player_name)];
    if (extra.lbKind === "money") {
      if (period) return [td(r.rank, "num"), ...who, goldCell(r.net_gold), td(fmtNum(r.gold), "num")];
      return [td(r.rank, "num"), ...who, ...(extra.lbScope === "groups" ? [] : [td(r.level, "num")]), td(fmtNum(r.gold), "num")];
    }
    return [td(r.rank, "num"), ...who, td(fmtNum(r.sold_quantity), "num"), td(fmtNum(r.sales_income), "num"),
      td(fmtNum(r.gold), "num")];
  }

  async function loadLeaderboard() {
    const span = renderLeaderboardHead();
    $("lb-group").hidden = extra.lbScope !== "group";
    $("lb-note").textContent = extra.lbScope === "groups"
      ? "群組總排行：主要所屬群組為該群組的成員加總後排名（每座農場只計入一個群組）。期間以台灣時間計算；金幣在期間內為淨賺金幣（不含建立農場、管理員調整與獎勵）。顯示前 100 名。"
      : "顯示前 100 名。期間以台灣時間計算；金幣在期間內為淨賺金幣（不含建立農場、管理員調整與獎勵）。";
    if (extra.lbScope === "group" && !extra.lbGroupId) {
      replaceRows($("lb-rows"), [], "請先選擇群組（群組內排行只包含該群組成員）", span);
      return;
    }
    const params = new URLSearchParams({ scope: extra.lbScope, kind: extra.lbKind, period: extra.lbPeriod, limit: "100" });
    if (extra.lbScope === "group") params.set("group_id", extra.lbGroupId);
    try {
      const data = await fetchJson(`/api/monitor/leaderboard?${params}`);
      // 名次與順序完全使用後端結果；前三名只加上視覺標示
      const rows = data.items.map((r) => {
        const cells = leaderboardRow(r);
        if (r.rank <= 3) {
          cells[0].replaceChildren(el("span", { className: `medal m${r.rank}`, text: r.rank }));
          cells[0].setAttribute("aria-label", `第 ${r.rank} 名`);
        }
        return el("tr", { className: r.rank <= 3 ? `rank-${r.rank}` : "" }, cells);
      });
      replaceRows($("lb-rows"), rows, extra.lbScope === "groups" ? "目前沒有群組資料" : "目前沒有玩家資料", span);
    } catch (err) {
      replaceRows($("lb-rows"), [], `排行榜載入失敗：${err.message}`, span);
    }
  }

  async function loadSteals() {
    const params = new URLSearchParams({ limit: "200" });
    const filters = [["group_id", "steal-group"], ["outcome", "steal-outcome"], ["date_from", "steal-from"], ["date_to", "steal-to"]];
    for (const [key, id] of filters) {
      if ($(id).value) params.set(key, $(id).value);
    }
    const pid = pickedPlayerId($("steal-player"), extra.players);
    if (pid === undefined) {
      replaceRows($("steal-rows"), [], "找不到這位玩家，請從建議清單選擇", 8);
      return;
    }
    if (pid) params.set("player_id", pid);
    try {
      const rows = (await fetchJson(`/api/monitor/steals?${params}`)).map((r) => el("tr", {}, [
        td(fmtTime(r.created_at)), td(r.group_name), td(r.thief_name), td(r.victim_name), td(r.crop_name),
        td(r.quantity, "num"), td(r.plot_number, "num"),
        statusTd(r.outcome === "success" ? "偷菜成功" : `被${r.blocked_by_name}阻止`),
      ]));
      replaceRows($("steal-rows"), rows, "沒有符合條件的偷菜紀錄", 8);
    } catch (err) {
      replaceRows($("steal-rows"), [], `偷菜紀錄載入失敗：${err.message}`, 8);
    }
  }

  // 剩餘時間依伺服器回傳的完成時間計算
  const leftText = (readyAt, fallback) => {
    if (!readyAt) return fallback;
    const left = (new Date(readyAt).getTime() - serverNow()) / 1000;
    return left > 0 ? fmtDuration(left) : fallback;
  };

  async function loadRanch() {
    try {
      const [farms, orders] = await Promise.all([
        fetchJson("/api/monitor/ranch"), fetchJson("/api/monitor/craft-orders?limit=200"),
      ]);
      const farmRows = farms.map((f) => el("tr", {}, [
        td(`#${f.player_id} ${f.player_name}`), td(f.farm_name),
        td(Object.entries(f.buildings).map(([n, c]) => `${n}×${c}`).join("、") || "—"),
        td(`${f.animal_count}/${f.capacity}`, "num"), td(f.state_counts.hungry, "num"),
        td(f.state_counts.producing, "num"), td(f.state_counts.ready, "num"),
        td(`${fmtNum(f.feed_cost_hungry)} 金幣`, "num"), td(fmtTime(f.next_ready_at)),
      ]));
      replaceRows($("ranch-rows"), farmRows, "目前沒有農場建造設施或飼養動物", 9);
      const animalRows = farms.flatMap((f) => f.animals.map((a) => el("tr", {}, [
        td(f.player_name), td(a.animal_no, "num"), td(a.name), statusTd(a.state_label),
        td(fmtTime(a.fed_at)), td(fmtTime(a.ready_at)),
        td(a.state === "producing" ? leftText(a.ready_at, "可收集") : "—"),
        td(`${a.product_quantity} 個${a.product_name}`), td(a.cycles_completed, "num"),
      ])));
      replaceRows($("animal-rows"), animalRows, "目前沒有動物", 9);
      const orderRows = orders.map((o) => el("tr", {}, [
        td(`#${o.order_id}`, "num"), td(o.player_name), td(o.building_name), td(`${o.recipe_name} ×${o.quantity}`),
        td(Object.entries(o.inputs).map(([n, q]) => `${n}×${q}`).join("、")),
        td(`${o.output_quantity} ${o.output_name}`, "num"), td(fmtTime(o.started_at)), td(fmtTime(o.ready_at)),
        td(o.state === "in_progress" ? leftText(o.ready_at, "待領取") : "—"), statusTd(o.state_label),
      ]));
      replaceRows($("order-rows"), orderRows, "目前沒有加工訂單", 10);
    } catch (err) {
      for (const id of ["ranch-rows", "animal-rows", "order-rows"]) {
        replaceRows($(id), [], `養殖與加工資料載入失敗：${err.message}`, 10);
      }
    }
  }

  async function loadDefenses() {
    try {
      const farms = await fetchJson("/api/monitor/defenses");
      const rows = farms.flatMap((f) => f.devices.map((d) => el("tr", {}, [
        td(`#${f.player_id} ${f.player_name}`), td(f.farm_name), td(`${d.name} #${d.slot}`),
        td(`${Math.round(d.chance * 100)}%`, "num"),
        td(d.end_plot === null ? "全部土地" : `第 ${d.start_plot}–${d.end_plot} 格`), td(fmtTime(d.acquired_at)),
      ])));
      replaceRows($("def-rows"), rows, "目前沒有農場持有防守設施", 6);
    } catch (err) {
      replaceRows($("def-rows"), [], `防守資料載入失敗：${err.message}`, 6);
    }
  }

  function bpSection(title, lines) {
    return el("div", {}, [
      el("h3", { text: title }),
      lines.length ? el("ul", {}, lines.map((t) => el("li", { text: t }))) : el("p", { className: "muted", text: "無" }),
    ]);
  }

  const EFFECT_LABELS = { inactive: "未啟用", active: "有效中", expired: "已過期" };

  function effectLine(e) {
    if (e.status === "inactive") return `${e.name}：未啟用`;
    const left = e.status === "active" ? fmtDuration(e.remaining_seconds) : "0 秒";
    return `${e.name}：${EFFECT_LABELS[e.status]}｜開始 ${fmtTime(e.started_at)}｜到期 ${fmtTime(e.expires_at)}`
      + `｜剩餘 ${left}｜生長時間 ×${e.multiplier}`;
  }

  async function loadBackpack() {
    const id = extra.bpPlayerId;
    const body = $("bp-body");
    if (!id) { body.replaceChildren(el("p", { className: "muted", text: "請選擇玩家查看背包。" })); return; }
    try {
      const bp = await fetchJson(`/api/monitor/players/${encodeURIComponent(id)}/backpack`);
      if (id !== extra.bpPlayerId) return;
      const header = el("p", { className: "muted", text: `${bp.farm_name}｜倉庫 ${bp.used}/${bp.capacity}` });
      if (bp.is_empty) {
        body.replaceChildren(header, el("p", { text: "目前背包是空的" }));
        return;
      }
      const toolLine = (t) => `${t.name}：${t.owned ? `已永久持有，${t.enabled ? "已啟用" : "已停用"}` : "尚未購買"}`;
      body.replaceChildren(
        header,
        bpSection("種子", bp.seeds.map((x) => `${x.name} × ${x.quantity}（商店售價 ${x.price}）`)),
        bpSection("農產品", bp.produce.map((x) => {
          const tags = [x.quality && x.quality !== "normal" ? { good: "優良", rare: "稀有", legendary: "傳說" }[x.quality] : "",
            x.mutation && !["none", "variant"].includes(x.mutation)
              ? { giant: "巨型作物", golden: "黃金作物", fast: "快速生長" }[x.mutation] : ""].filter(Boolean);
          return `${x.name}${tags.length ? `【${tags.join("・")}】` : ""} × ${x.quantity}（單價 ${x.price}）`;
        })),
        bpSection("動物產品與加工品", (bp.goods || []).map((x) => `${x.name} × ${x.quantity}（售價 ${x.price}）`)),
        bpSection("消耗型道具", bp.consumables.map((x) => `${x.name} × ${x.quantity}：${x.description}`)),
        bpSection("永久工具", bp.tools.map(toolLine)),
        bpSection("限時效果", bp.effects.map(effectLine)),
      );
    } catch (err) {
      body.replaceChildren(el("p", { className: "muted", text: `背包載入失敗：${err.message}` }));
    }
  }

  // ---------- 遊戲管理：限時活動、作物與動物種類、調整玩家 ----------
  // 表格每次快照都會重新整理（只含按鈕）；表單與「調整玩家」區塊不會被自動更新覆蓋。
  const manage = { catalog: null, editEventId: null, editCrop: null, editAnimal: null, playerId: null };
  const WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"];
  const QUALITY_NAMES = { normal: "普通", good: "優良", rare: "稀有", legendary: "傳說" };
  const MUTATION_NAMES = { none: "無", giant: "巨型作物", golden: "黃金作物", fast: "快速生長", variant: "珍稀變種" };
  const ITEM_KIND_NAMES = {
    seed: "種子", produce: "農產品", variant: "珍稀變種", animal_product: "動物產品", goods: "加工品", item: "道具",
  };

  function smallButton(text, onClick, className) {
    const btn = el("button", { text, className });
    btn.type = "button";
    btn.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return btn;
  }

  const actionsTd = (buttons) => el("td", {}, [el("div", { className: "controls" }, buttons)]);
  const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

  // 送出管理操作：成功後重新載入目錄；note 為顯示結果的元素 ID
  async function manageAction(noteId, method, url, body, okText) {
    try {
      const res = await adminRequest(method, url, body);
      setNote(noteId, okText, "good");
      toast(okText, "good");
      await loadCatalog();
      return res;
    } catch (err) {
      setNote(noteId, `失敗：${err.message}`, "bad");
      toast(err.message, "bad");
      return null;
    }
  }

  // 停用／啟用、刪除等重要操作的確認
  async function confirmToggle(kindLabel, name, enabled, extraNote) {
    const answer = await confirmDialog({
      title: `${enabled ? "停用" : "啟用"}${kindLabel}「${name}」`,
      message: enabled ? `停用後${extraNote}` : `啟用後玩家即可再次使用這個${kindLabel}。`,
      confirmText: enabled ? "確認停用" : "確認啟用",
      danger: enabled,
    });
    return answer.ok;
  }

  async function confirmDelete(title, message) {
    const answer = await confirmDialog({ title, message: `${message}\n此操作無法復原。`, confirmText: "確認刪除", danger: true });
    return answer.ok;
  }

  async function loadCatalog() {
    try {
      const c = await fetchJson("/api/monitor/catalog");
      manage.catalog = c;
      renderEvents(c);
      renderCropAdmin(c);
      renderAnimalAdmin(c);
      fillSelect($("ef-crop"), c.crops.map((x) => [x.code, `${x.name}（${x.code}）${x.active ? "" : "【停用】"}`]), "全部作物");
      fillSelect($("qf-filter"), c.animals.map((a) => [a.product_code, a.product_name]), "全部");
    } catch (err) {
      for (const id of ["event-rows", "crop-admin-rows", "animal-admin-rows"]) {
        replaceRows($(id), [], `管理資料載入失敗：${err.message}`, 10);
      }
    }
  }

  // ----- 限時活動 -----
  function renderEvents(c) {
    const admin = extra.admin.loggedIn;
    const rows = c.events.map((e) => {
      // 狀態以後端的 active_now 為準；不在時段內時再依實際起訖時間區分已排程／已結束
      const now = serverNow();
      const notStarted = e.starts_at && new Date(e.starts_at).getTime() > now;
      const ended = e.ends_at && new Date(e.ends_at).getTime() <= now;
      const status = !e.enabled ? badge("已停用", "muted")
        : e.active_now ? badge("生效中", "good")
          : ended ? badge("已結束", "muted")
            : notStarted ? badge("已排程", "info") : badge("未在時段內", "neutral");
      const cells = [
        td(e.id, "num"), td(e.name), td(e.crop_code ? cropName(c, e.crop_code) : "全部作物"),
        td(e.effects_text, "wrap"), td(e.schedule_text, "wrap"), el("td", {}, [status]),
      ];
      if (admin) {
        cells.push(actionsTd([
          smallButton("編輯", () => fillEventForm(e)),
          smallButton(e.enabled ? "停用" : "啟用", async () => {
            if (!await confirmToggle("活動", e.name, e.enabled, "活動立即失效，之後的播種、澆水與販售不再套用加成。")) return;
            manageAction("ef-note", "PUT", `/api/admin/events/${e.id}/enabled`,
              { enabled: !e.enabled }, `活動 #${e.id} 已${e.enabled ? "停用" : "啟用"}。`);
          }),
          smallButton("刪除", async () => {
            if (!await confirmDelete(`刪除活動「${e.name}」`, "歷史交易紀錄會保留活動名稱。")) return;
            if (manage.editEventId === e.id) resetEventForm();
            manageAction("ef-note", "DELETE", `/api/admin/events/${e.id}`, null, `活動 #${e.id} 已刪除。`);
          }, "danger"),
        ]));
      }
      return el("tr", {}, cells);
    });
    replaceRows($("event-rows"), rows, "目前沒有限時活動", 7);
    const cap = c.event_caps;
    $("ev-help").textContent = "活動在操作當下判斷是否生效：播種時決定品質、突變與收成量；澆水時決定生長時間；販售作物時決定單價。"
      + `多個活動同時生效時加成相加，上限：售價 +${cap.sell_price_pct}%、生長時間 -${cap.grow_reduction_pct}%、`
      + `品質 +${cap.quality_bonus_bp / 100}%、突變 +${cap.mutation_bonus_bp / 100}%、收成 +${cap.harvest_bonus_pct}%。每日／每週時段以台灣時間計算。`;
  }

  const cropName = (c, code) => (c.crops.find((x) => x.code === code) || { name: code }).name;

  function updateEventScheduleFields() {
    const kind = $("ef-schedule").value;
    document.querySelectorAll("[data-ef-window]").forEach((n) => { n.hidden = kind === "once"; });
    $("ef-weekdays").hidden = kind !== "weekly";
  }

  function resetEventForm() {
    manage.editEventId = null;
    $("ef-title").textContent = "新增活動";
    $("ef-submit").textContent = "新增活動";
    for (const id of ["ef-name", "ef-desc", "ef-start", "ef-end", "ef-time-start", "ef-time-end"]) $(id).value = "";
    $("ef-crop").value = "";
    $("ef-sell").value = "1";
    for (const id of ["ef-grow", "ef-quality", "ef-mutation", "ef-harvest", "ef-wind"]) $(id).value = "0";
    $("ef-free-water").checked = false;
    $("ef-schedule").value = "once";
    $("ef-enabled").checked = true;
    document.querySelectorAll("#ef-weekdays input").forEach((n) => { n.checked = false; });
    updateEventScheduleFields();
  }

  function fillEventForm(e) {
    manage.editEventId = e.id;
    $("event-form-box").open = true;
    $("ef-title").textContent = `編輯活動 #${e.id}`;
    $("ef-submit").textContent = "儲存變更";
    $("ef-name").value = e.name;
    $("ef-desc").value = e.description;
    $("ef-crop").value = e.crop_code || "";
    $("ef-sell").value = String(e.sell_price_pct / 100);
    $("ef-grow").value = String(e.grow_reduction_pct);
    $("ef-quality").value = String(e.quality_bonus_bp / 100);
    $("ef-mutation").value = String(e.mutation_bonus_bp / 100);
    $("ef-harvest").value = String(e.harvest_bonus_pct);
    $("ef-wind").value = String(e.harvest_loss);
    $("ef-free-water").checked = Boolean(e.free_water);
    $("ef-schedule").value = e.schedule_type;
    $("ef-start").value = e.starts_at ? localInput(new Date(e.starts_at)) : "";
    $("ef-end").value = e.ends_at ? localInput(new Date(e.ends_at)) : "";
    $("ef-time-start").value = e.start_minute === null ? "" : hhmm(e.start_minute);
    $("ef-time-end").value = e.end_minute === null ? "" : hhmm(e.end_minute);
    document.querySelectorAll("#ef-weekdays input").forEach((n) => {
      n.checked = Boolean(e.weekdays && e.weekdays & (1 << Number(n.value)));
    });
    $("ef-enabled").checked = e.enabled;
    $("ef-note").textContent = "";
    updateEventScheduleFields();
    $("event-form-box").scrollIntoView({ block: "nearest" });
  }

  function readEventForm() {
    const kind = $("ef-schedule").value;
    const windowed = kind !== "once";
    return {
      name: $("ef-name").value.trim(),
      description: $("ef-desc").value.trim(),
      crop_code: $("ef-crop").value || null,
      sell_price_pct: Math.round(Number($("ef-sell").value) * 100),
      grow_reduction_pct: Math.round(Number($("ef-grow").value)),
      quality_bonus_bp: Math.round(Number($("ef-quality").value) * 100),
      mutation_bonus_bp: Math.round(Number($("ef-mutation").value) * 100),
      harvest_bonus_pct: Math.round(Number($("ef-harvest").value)),
      harvest_loss: Math.round(Number($("ef-wind").value)),
      free_water: $("ef-free-water").checked ? 1 : 0,
      schedule_type: kind,
      starts_at: isoFromLocal($("ef-start").value),
      ends_at: isoFromLocal($("ef-end").value),
      start_time: windowed ? $("ef-time-start").value.trim() || null : null,
      end_time: windowed ? $("ef-time-end").value.trim() || null : null,
      weekdays: kind === "weekly"
        ? [...document.querySelectorAll("#ef-weekdays input:checked")].map((n) => Number(n.value)) : [],
      enabled: $("ef-enabled").checked,
    };
  }

  async function submitEvent() {
    const body = readEventForm();
    if (!validateForm($("event-form-box"))) return;
    if (body.schedule_type === "once" && (!body.starts_at || !body.ends_at)) {
      setNote("ef-note", "單次活動必須填寫開始與結束時間。", "bad");
      (body.starts_at ? $("ef-end") : $("ef-start")).focus();
      return;
    }
    if (body.starts_at && body.ends_at && body.ends_at <= body.starts_at) {
      setNote("ef-note", "結束時間必須晚於開始時間。", "bad");
      $("ef-end").focus();
      return;
    }
    if (body.schedule_type === "weekly" && !body.weekdays.length) {
      setNote("ef-note", "每週活動請至少勾選一個星期。", "bad");
      return;
    }
    const id = manage.editEventId;
    const res = id === null
      ? await manageAction("ef-note", "POST", "/api/admin/events", body, "活動已新增。")
      : await manageAction("ef-note", "PUT", `/api/admin/events/${id}`, body, `活動 #${id} 已更新。`);
    if (res) {
      const note = $("ef-note").textContent;
      resetEventForm();
      clearDirty("event-form-box");
      setNote("ef-note", note, "good");
    }
  }

  // ----- 作物種類 -----
  function renderCropAdmin(c) {
    const admin = extra.admin.loggedIn;
    const rows = c.crops.map((x) => {
      const cells = [
        td(x.code), td(x.name), td(fmtNum(x.seed_price), "num"), td(fmtDuration(x.grow_seconds), "num"),
        td(x.harvest_quantity, "num"), td(fmtNum(x.sell_price), "num"), td(`${x.variant_name}（${x.variant_code}）`),
        el("td", {}, [x.active ? badge("啟用", "good") : badge("已停用", "muted")]),
      ];
      if (admin) {
        cells.push(actionsTd([
          smallButton("編輯", () => fillCropForm(x)),
          smallButton(x.active ? "停用" : "啟用", async () => {
            if (!await confirmToggle("作物", x.name, x.active, "玩家不能購買種子或種植；已種下、已持有的仍可收成與販售。")) return;
            manageAction("crf-note", "PUT", `/api/admin/crops/${encodeURIComponent(x.code)}/active`, { active: !x.active },
              `${x.name}已${x.active ? "停用" : "啟用"}。`);
          }),
          smallButton("刪除", async () => {
            if (!await confirmDelete(`刪除作物「${x.name}」`, "只有從未被使用的作物可以刪除；已被使用的請改用停用。")) return;
            manageAction("crf-note", "DELETE", `/api/admin/crops/${encodeURIComponent(x.code)}`, null, `${x.name}已刪除。`);
          }, "danger"),
        ]));
      }
      return el("tr", {}, cells);
    });
    replaceRows($("crop-admin-rows"), rows, "目前沒有作物", 9);
  }

  const CROP_FIELDS = [
    ["crf-name", "name"], ["crf-seed", "seed_price"], ["crf-grow", "grow_seconds"], ["crf-qty", "harvest_quantity"],
    ["crf-sell", "sell_price"], ["crf-vname", "variant_name"],
  ];

  function resetCropForm() {
    manage.editCrop = null;
    $("crf-title").textContent = "新增作物";
    $("crf-submit").textContent = "新增作物";
    for (const id of ["crf-code", "crf-vcode", "crf-name", "crf-vname"]) $(id).value = "";
    $("crf-code").disabled = $("crf-vcode").disabled = false;
  }

  function fillCropForm(x) {
    manage.editCrop = x.code;
    $("crop-form-box").open = true;
    $("crf-title").textContent = `編輯作物 ${x.name}（${x.code}）`;
    $("crf-submit").textContent = "儲存變更";
    $("crf-code").value = x.code;
    $("crf-vcode").value = x.variant_code;
    $("crf-code").disabled = $("crf-vcode").disabled = true; // 代碼不可修改
    for (const [id, key] of CROP_FIELDS) $(id).value = String(x[key]);
    $("crf-note").textContent = "";
    $("crop-form-box").scrollIntoView({ block: "nearest" });
  }

  // 修改既有資料時列出變更前後，確認後才送出
  function changedFields(fields, labels, before, body) {
    return fields.filter(([, key]) => String(before[key]) !== String(body[key]))
      .map(([, key]) => [labels[key] || key, String(before[key]), String(body[key])]);
  }

  const CROP_LABELS = {
    name: "名稱", seed_price: "種子價格", grow_seconds: "生長秒數", harvest_quantity: "收成數量",
    sell_price: "售價", variant_name: "珍稀變種名稱",
  };

  async function submitCrop() {
    if (!validateForm($("crop-form-box"))) return;
    const body = {};
    for (const [id, key] of CROP_FIELDS) body[key] = key.endsWith("name") ? $(id).value.trim() : Number($(id).value);
    const code = manage.editCrop;
    let res;
    if (code === null) {
      Object.assign(body, { code: $("crf-code").value.trim(), variant_code: $("crf-vcode").value.trim() });
      res = await manageAction("crf-note", "POST", "/api/admin/crops", body, `已新增作物 ${body.name}。`);
    } else {
      const before = manage.catalog.crops.find((x) => x.code === code);
      const diff = before ? changedFields(CROP_FIELDS, CROP_LABELS, before, body) : [];
      if (before && !diff.length) { setNote("crf-note", "沒有任何變更。", "bad"); return; }
      const answer = await confirmDialog({
        title: `儲存作物「${before ? before.name : code}」的變更`,
        message: "價格變更立即生效；生長時間只影響之後澆水的作物。",
        diff: diff.length ? diff : null,
        confirmText: "儲存變更",
      });
      if (!answer.ok) return;
      res = await manageAction("crf-note", "PUT", `/api/admin/crops/${encodeURIComponent(code)}`, body, `${body.name}已更新。`);
    }
    if (res) {
      const note = $("crf-note").textContent;
      resetCropForm();
      clearDirty("crop-form-box");
      setNote("crf-note", note, "good");
      loadGenetics();
    }
  }

  // ----- 動物種類 -----
  function renderAnimalAdmin(c) {
    const admin = extra.admin.loggedIn;
    const rows = c.animals.map((a) => {
      const cells = [
        td(a.code), td(a.name), td(fmtNum(a.price), "num"), td(fmtNum(a.feed_cost), "num"),
        td(fmtDuration(a.cycle_seconds), "num"), td(`${a.product_name}（${a.product_code}）`),
        td(a.product_quantity, "num"), td(fmtNum(a.product_price), "num"),
        el("td", {}, [a.active ? badge("啟用", "good") : badge("已停用", "muted")]),
      ];
      if (admin) {
        cells.push(actionsTd([
          smallButton("編輯", () => fillAnimalForm(a)),
          smallButton(a.active ? "停用" : "啟用", async () => {
            if (!await confirmToggle("動物", a.name, a.active, "玩家不能再購買；已飼養的仍可餵食、收集與販售產品。")) return;
            manageAction("atf-note", "PUT", `/api/admin/animal-types/${encodeURIComponent(a.code)}/active`, { active: !a.active },
              `${a.name}已${a.active ? "停用" : "啟用"}。`);
          }),
          smallButton("刪除", async () => {
            if (!await confirmDelete(`刪除動物「${a.name}」`, "只有從未被使用的動物可以刪除；已被使用的請改用停用。")) return;
            manageAction("atf-note", "DELETE", `/api/admin/animal-types/${encodeURIComponent(a.code)}`, null, `${a.name}已刪除。`);
          }, "danger"),
        ]));
      }
      return el("tr", {}, cells);
    });
    replaceRows($("animal-admin-rows"), rows, "目前沒有動物種類", 10);
  }

  const ANIMAL_FIELDS = [
    ["atf-name", "name"], ["atf-price", "price"], ["atf-feed", "feed_cost"], ["atf-cycle", "cycle_seconds"],
    ["atf-pname", "product_name"], ["atf-pqty", "product_quantity"], ["atf-pprice", "product_price"],
  ];

  function resetAnimalForm() {
    manage.editAnimal = null;
    $("atf-title").textContent = "新增動物";
    $("atf-submit").textContent = "新增動物";
    for (const id of ["atf-code", "atf-pcode", "atf-name", "atf-pname"]) $(id).value = "";
    $("atf-code").disabled = $("atf-pcode").disabled = false;
  }

  function fillAnimalForm(a) {
    manage.editAnimal = a.code;
    $("animal-form-box").open = true;
    $("atf-title").textContent = `編輯動物 ${a.name}（${a.code}）`;
    $("atf-submit").textContent = "儲存變更";
    $("atf-code").value = a.code;
    $("atf-pcode").value = a.product_code;
    $("atf-code").disabled = $("atf-pcode").disabled = true;
    for (const [id, key] of ANIMAL_FIELDS) $(id).value = String(a[key]);
    $("atf-note").textContent = "";
    $("animal-form-box").scrollIntoView({ block: "nearest" });
  }

  const ANIMAL_LABELS = {
    name: "名稱", price: "購買價格", feed_cost: "餵食費用", cycle_seconds: "生產週期秒數",
    product_name: "產品名稱", product_quantity: "每次產量", product_price: "產品售價",
  };

  async function submitAnimal() {
    if (!validateForm($("animal-form-box"))) return;
    const body = {};
    for (const [id, key] of ANIMAL_FIELDS) body[key] = key.endsWith("name") ? $(id).value.trim() : Number($(id).value);
    const code = manage.editAnimal;
    let res;
    if (code === null) {
      Object.assign(body, { code: $("atf-code").value.trim(), product_code: $("atf-pcode").value.trim() });
      res = await manageAction("atf-note", "POST", "/api/admin/animal-types", body, `已新增動物 ${body.name}。`);
    } else {
      const before = manage.catalog.animals.find((x) => x.code === code);
      const diff = before ? changedFields(ANIMAL_FIELDS, ANIMAL_LABELS, before, body) : [];
      if (before && !diff.length) { setNote("atf-note", "沒有任何變更。", "bad"); return; }
      const answer = await confirmDialog({
        title: `儲存動物「${before ? before.name : code}」的變更`,
        message: "價格變更立即生效；生產週期從下次餵食起生效。",
        diff: diff.length ? diff : null,
        confirmText: "儲存變更",
      });
      if (!answer.ok) return;
      res = await manageAction("atf-note", "PUT", `/api/admin/animal-types/${encodeURIComponent(code)}`, body,
        `${body.name}已更新。`);
    }
    if (res) {
      const note = $("atf-note").textContent;
      resetAnimalForm();
      clearDirty("animal-form-box");
      setNote("atf-note", note, "good");
    }
  }

  // ----- 調整玩家 -----
  function selectEl(id, items, value) {
    const select = el("select");
    if (id) select.id = id;
    select.replaceChildren(...items.map(([v, label]) => new Option(label, v)));
    if (value !== undefined) select.value = value;
    return select;
  }

  function numInput(id, value, min, max) {
    const input = el("input", { className: "qty" });
    input.type = "number";
    if (id) input.id = id;
    input.min = String(min);
    if (max !== undefined) input.max = String(max);
    input.step = "1";
    input.value = String(value);
    return input;
  }

  function checkLabel(id, text) {
    const input = el("input");
    input.type = "checkbox";
    input.id = id;
    const label = el("label", { className: "check" }, [input]);
    label.append(` ${text}`);
    return label;
  }

  async function playerAction(method, path, body, okText) {
    const pid = manage.playerId;
    if (!pid) return;
    try {
      await adminRequest(method, `/api/admin/players/${encodeURIComponent(pid)}${path}`, body);
      setNote("pa-note", okText, "good");
      toast(okText, "good");
    } catch (err) {
      setNote("pa-note", `失敗：${err.message}`, "bad");
      toast(err.message, "bad");
    }
    await loadPlayerAdmin();
  }

  // 玩家資料調整：一律先顯示調整前後並確認
  async function confirmPlayerChange(title, diff, message, danger = false) {
    const answer = await confirmDialog({
      title, diff, danger,
      message: message || "調整會寫入玩家交易紀錄（管理員調整）與管理操作紀錄。",
      confirmText: danger ? "確認執行" : "確認調整",
    });
    return answer.ok;
  }

  const genLabel = (q, m) => [q !== "normal" ? QUALITY_NAMES[q] : "", m !== "none" ? MUTATION_NAMES[m] : ""]
    .filter(Boolean).join("・") || "—";

  function inventoryBlock(f, c) {
    const rows = f.inventory.map((i) => {
      const qty = numInput(null, i.quantity, 0, 100000);
      const set = (n) => playerAction("PUT", "/inventory",
        { item_code: i.item_code, quality: i.quality, mutation: i.mutation, quantity: n },
        `${i.name}（${genLabel(i.quality, i.mutation)}）已設為 ${n}。`);
      const label = `${i.name}（${genLabel(i.quality, i.mutation)}）`;
      return el("tr", {}, [
        td(`${i.name}（${i.item_code}）`), td(genLabel(i.quality, i.mutation)),
        el("td", { className: "num" }, [qty]),
        actionsTd([
          smallButton("儲存", async () => {
            const n = Number(qty.value);
            if (!checkField(qty)) { qty.focus(); return; }
            if (n === i.quantity) { setNote("pa-note", `${label}的數量沒有變更。`, "bad"); return; }
            if (await confirmPlayerChange(`調整 ${f.player_name} 的背包`, [[label, fmtNum(i.quantity), fmtNum(n)]])) set(n);
          }),
          smallButton("刪除", async () => {
            if (await confirmPlayerChange(`刪除 ${f.player_name} 的物品`, [[label, fmtNum(i.quantity), "0（刪除）"]],
              "物品會從玩家背包移除，並寫入管理員調整紀錄。", true)) set(0);
          }, "danger"),
        ]),
      ]);
    });
    const table = el("table", {}, [
      el("thead", {}, [el("tr", {}, ["物品", "品質／突變", "數量", ""].map((t) => el("th", { text: t })))]),
      el("tbody", {}, rows.length ? rows : [emptyRow("背包是空的", 4)]),
    ]);
    const items = c.items.map((i) => [i.code, `${ITEM_KIND_NAMES[i.kind] || i.kind}：${i.name}（${i.code}）`]);
    const item = selectEl("pa-item", items);
    const quality = selectEl("pa-quality", Object.entries(QUALITY_NAMES));
    const mutation = selectEl("pa-mutation", Object.entries(MUTATION_NAMES));
    const qty = numInput("pa-qty", 1, 0, 100000);
    const add = smallButton("設定數量", async () => {
      if (!checkField(qty)) { qty.focus(); return; }
      const n = Number(qty.value);
      const current = f.inventory.find((x) => x.item_code === item.value && x.quality === quality.value
        && x.mutation === mutation.value);
      const label = `${item.options[item.selectedIndex].text}（${genLabel(quality.value, mutation.value)}）`;
      if (!await confirmPlayerChange(`設定 ${f.player_name} 的物品數量`,
        [[label, fmtNum(current ? current.quantity : 0), n === 0 ? "0（刪除）" : fmtNum(n)]])) return;
      playerAction("PUT", "/inventory",
        { item_code: item.value, quality: quality.value, mutation: mutation.value, quantity: n }, "背包已更新。");
    }, "primary");
    return el("div", {}, [
      el("h3", { text: `背包（${f.used}/${f.capacity}）` }),
      el("div", { className: "table-wrap" }, [table]),
      el("p", { className: "muted small", text: "新增或設定某物品（同一品質／突變）的數量；0 = 刪除。只有農產品可設定品質與突變，珍稀變種的突變固定為「珍稀變種」。" }),
      el("div", { className: "controls" }, [item, quality, mutation, qty, add]),
    ]);
  }

  function plotsBlock(f, c) {
    const rows = f.plots.map((p) => {
      const buttons = p.crop_code
        ? [smallButton("清除作物", async () => {
          if (await confirmPlayerChange(`清除第 ${p.plot_number} 格作物`,
            [[`第 ${p.plot_number} 格`, `${p.crop_name}（${p.state_label}）`, "空地"]], "作物會直接移除，不會進入背包。", true)) {
            playerAction("DELETE", `/plots/${p.plot_number}/crop`, null, `已清除第 ${p.plot_number} 格。`);
          }
        }, "danger")]
        : [smallButton("刪除土地", async () => {
          if (await confirmPlayerChange(`刪除第 ${p.plot_number} 格土地`,
            [["土地數量", fmtNum(f.plots.length), fmtNum(f.plots.length - 1)]], "刪除後偷菜歷史紀錄會保留。", true)) {
            playerAction("DELETE", `/plots/${p.plot_number}`, null, `已刪除第 ${p.plot_number} 格土地。`);
          }
        }, "danger")];
      return el("tr", {}, [
        td(p.plot_number, "num"), statusTd(p.state_label), td(p.crop_name || "—"),
        td(p.crop_code ? genLabel(p.quality, p.mutation) : "—"), td(p.remaining_quantity ?? "—", "num"), actionsTd(buttons),
      ]);
    });
    const table = el("table", {}, [
      el("thead", {}, [el("tr", {}, ["土地", "狀態", "作物", "品質／突變", "數量", ""].map((t) => el("th", { text: t })))]),
      el("tbody", {}, rows),
    ]);
    const empty = f.plots.filter((p) => !p.crop_code).map((p) => [p.plot_number, `第 ${p.plot_number} 格`]);
    const plot = selectEl("pa-plot", empty.length ? empty : [["", "（沒有空地）"]]);
    const crop = selectEl("pa-crop", c.crops.filter((x) => x.active).map((x) => [x.code, x.name]));
    const quality = selectEl("pa-plant-quality", Object.entries(QUALITY_NAMES));
    const mutation = selectEl("pa-plant-mutation", Object.entries(MUTATION_NAMES));
    const watered = checkLabel("pa-watered", "已澆水");
    const ready = checkLabel("pa-ready", "立即成熟");
    const plant = smallButton("種下", async () => {
      if (!plot.value) { setNote("pa-note", "沒有空地可種植。", "bad"); return; }
      const cropText = crop.options[crop.selectedIndex] ? crop.options[crop.selectedIndex].text : crop.value;
      if (!await confirmPlayerChange(`代 ${f.player_name} 種植`,
        [[`第 ${plot.value} 格`, "空地", `${cropText}（${genLabel(quality.value, mutation.value)}）`]])) return;
      playerAction("PUT", `/plots/${plot.value}/crop`, {
        crop_code: crop.value, quality: quality.value, mutation: mutation.value,
        watered: $("pa-watered").checked, ready_now: $("pa-ready").checked,
      }, `已在第 ${plot.value} 格種下作物。`);
    }, "primary");
    const count = numInput("pa-plot-count", 1, 1, 100);
    const addPlots = smallButton("新增土地", async () => {
      if (!checkField(count)) { count.focus(); return; }
      const n = Number(count.value);
      if (!await confirmPlayerChange(`為 ${f.player_name} 新增土地`,
        [["土地數量", fmtNum(f.plots.length), fmtNum(f.plots.length + n)]])) return;
      playerAction("POST", "/plots", { count: n }, "已新增土地。");
    });
    return el("div", {}, [
      el("h3", { text: `土地與作物（${f.plots.length} 格）` }),
      el("div", { className: "table-wrap" }, [table]),
      el("div", { className: "controls" }, [plot, crop, quality, mutation, watered, ready, plant]),
      el("div", { className: "controls" }, [count, addPlots]),
      el("p", { className: "muted small", text: "只能刪除空地；有作物時請先清除。刪除土地後偷菜歷史紀錄會保留。" }),
    ]);
  }

  function ranchBlock(f, c) {
    const busy = new Set(f.busy_buildings);
    const buildingRows = f.buildings.map((b) => el("tr", {}, [
      td(`${b.name} #${b.slot}`), td(busy.has(b.code) ? "有未領取的加工訂單" : "—"),
      actionsTd([smallButton("刪除", async () => {
        if (await confirmPlayerChange(`刪除${b.name} #${b.slot}`, [["設施", `${b.name} #${b.slot}`, "（刪除）"]],
          "有未領取加工訂單的設施、或動物超過畜舍容量時，伺服器會拒絕刪除。", true)) {
          playerAction("DELETE", `/buildings/${encodeURIComponent(b.code)}/${b.slot}`, null, `已刪除${b.name} #${b.slot}。`);
        }
      }, "danger")]),
    ]));
    const buildingSel = selectEl("pa-building", c.buildings.map((b) => [b.code, `${b.name}（上限 ${b.max}）`]));
    const addBuilding = smallButton("新增設施", async () => {
      const name = buildingSel.options[buildingSel.selectedIndex].text;
      if (!await confirmPlayerChange(`為 ${f.player_name} 新增設施`, [["建築數量", fmtNum(f.buildings.length), `${fmtNum(f.buildings.length + 1)}（+ ${name}）`]])) return;
      playerAction("POST", "/buildings", { building_code: buildingSel.value }, "已新增設施。");
    }, "primary");
    const animalRows = f.animals.map((a) => el("tr", {}, [
      td(`#${a.animal_no}`, "num"), td(a.name), statusTd(a.state_label),
      actionsTd([smallButton("刪除", async () => {
        if (await confirmPlayerChange(`刪除 ${a.name}#${a.animal_no}`, [["動物數量", fmtNum(f.animals.length), fmtNum(f.animals.length - 1)]],
          "未收集的產品會一併消失。", true)) {
          playerAction("DELETE", `/animals/${a.animal_no}`, null, `已刪除 ${a.name}#${a.animal_no}。`);
        }
      }, "danger")]),
    ]));
    const animalSel = selectEl("pa-animal", c.animals.filter((a) => a.active).map((a) => [a.code, a.name]));
    const animalQty = numInput("pa-animal-qty", 1, 1, 12);
    const addAnimals = smallButton("新增動物", async () => {
      if (!checkField(animalQty)) { animalQty.focus(); return; }
      const n = Number(animalQty.value);
      const name = animalSel.options[animalSel.selectedIndex] ? animalSel.options[animalSel.selectedIndex].text : animalSel.value;
      if (!await confirmPlayerChange(`為 ${f.player_name} 新增動物`,
        [["動物數量", `${fmtNum(f.animals.length)} / ${f.animal_capacity}`, `${fmtNum(f.animals.length + n)}（+ ${name} × ${n}）`]])) return;
      playerAction("POST", "/animals", { animal_code: animalSel.value, quantity: n }, "已新增動物。");
    }, "primary");
    const table = (heads, rows, emptyText) => el("div", { className: "table-wrap" }, [el("table", {}, [
      el("thead", {}, [el("tr", {}, heads.map((t) => el("th", { text: t })))]),
      el("tbody", {}, rows.length ? rows : [emptyRow(emptyText, heads.length)]),
    ])]);
    return el("div", {}, [
      el("h3", { text: "建築" }),
      table(["設施", "狀態", ""], buildingRows, "沒有建築"),
      el("div", { className: "controls" }, [buildingSel, addBuilding]),
      el("h3", { text: `動物（${f.animals.length}/${f.animal_capacity}）` }),
      table(["編號", "動物", "狀態", ""], animalRows, "沒有動物"),
      el("div", { className: "controls" }, [animalSel, animalQty, addAnimals]),
      el("p", { className: "muted small", text: "動物需要畜舍容量（每座畜舍 4 隻）；動物超過容量時不能刪除畜舍，有未領取訂單的加工設施也不能刪除。" }),
    ]);
  }

  function farmValuesBlock(f) {
    const gold = numInput("pa-gold", f.gold, 0, 1000000000000);
    gold.classList.add("wide-num");
    const level = numInput("pa-level", f.level, 1, 100);
    const save = smallButton("儲存金幣與等級", async () => {
      const body = { gold: Number(gold.value), level: Number(level.value) };
      if (!Number.isInteger(body.gold) || !Number.isInteger(body.level) || body.gold < 0 || body.level < 1 || body.level > 100) {
        setNote("pa-note", "金幣必須是 0 以上的整數，等級必須是 1–100 的整數。", "bad");
        checkField(gold);
        checkField(level);
        return;
      }
      if (body.gold === f.gold && body.level === f.level) { setNote("pa-note", "金幣與等級沒有變更。", "bad"); return; }
      const diff = [];
      if (body.gold !== f.gold) {
        const delta = body.gold - f.gold;
        diff.push(["金幣", fmtNum(f.gold), `${fmtNum(body.gold)}（${delta > 0 ? "+" : "-"}${fmtNum(Math.abs(delta))}）`]);
      }
      if (body.level !== f.level) diff.push(["農場等級", String(f.level), String(body.level)]);
      if (!await confirmPlayerChange(`調整 ${f.player_name} 的金幣與等級`, diff,
        "金幣差額會記入玩家交易紀錄（管理員調整）；等級只影響品質加成，不會自動增減土地。")) return;
      playerAction("PUT", "/farm", body, "金幣與農場等級已更新。");
    }, "primary");
    return el("div", { className: "subpanel" }, [
      el("h3", { text: "金幣與農場等級" }),
      el("div", { className: "controls" }, [
        el("label", { className: "inline", text: "金幣 " }, [gold]),
        el("label", { className: "inline", text: "農場等級 " }, [level]),
        save,
      ]),
      el("p", { className: "muted small", text: "直接設定數值；金幣差額會記入玩家交易紀錄。等級只影響品質加成，不會自動增減土地（土地請在下方調整）。" }),
    ]);
  }

  function transferBlock(f) {
    const others = extra.groups.filter((g) => g.group_id !== f.home_group_id)
      .map((g) => [g.group_id, `#${g.group_id} ${g.name}`]);
    const target = selectEl("pa-transfer-group", [["", "選擇目標群組…"], ...others], "");
    const reason = el("input");
    reason.type = "text";
    reason.maxLength = 200;
    reason.placeholder = "轉移原因（必填，會寫入管理操作紀錄）";
    reason.className = "reason-input";
    const skip = checkLabel("pa-transfer-skip", "略過 7 天冷卻（仍會留下紀錄）");
    const home = f.home_group_id ? `#${f.home_group_id} ${f.home_group_name}` : "未設定";
    const save = smallButton("轉移所屬群組", async () => {
      const groupId = Number(target.value);
      const text = reason.value.trim();
      if (!groupId) { setNote("pa-note", "請選擇目標群組。", "bad"); target.focus(); return; }
      if (!text) { setNote("pa-note", "請填寫轉移原因（會寫入管理操作紀錄）。", "bad"); reason.focus(); return; }
      const skipCooldown = $("pa-transfer-skip").checked;
      const label = target.options[target.selectedIndex].text;
      if (!await confirmPlayerChange(`轉移 ${f.player_name} 的所屬群組`, [["所屬群組", home, label]],
        `原因：${text}${skipCooldown ? "\n將略過 7 天冷卻（仍會留下紀錄）。" : ""}`, true)) return;
      playerAction("POST", "/transfer-group", { group_id: groupId, reason: text, skip_cooldown: skipCooldown },
        "農場所屬群組已轉移。");
    }, "danger");
    const cooldown = f.next_group_transfer_at ? `冷卻中，玩家下次可轉移：${fmtTime(f.next_group_transfer_at)}` : "玩家目前可自行轉移";
    return el("div", { className: "subpanel" }, [
      el("h3", { text: "所屬群組" }),
      el("p", { className: "muted small", text: `目前：${home}｜上次轉移：${fmtTime(f.last_group_transfer_at)}｜${cooldown}` }),
      el("div", { className: "controls" }, [target, reason, skip, save]),
      el("p", { className: "muted small", text: "只變更農場所屬群組，不影響金幣、物品與土地；歷史交易、任務、競賽與排名獎勵紀錄不改寫。" }),
    ]);
  }

  async function loadPlayerAdmin() {
    const body = $("pa-body");
    const pid = manage.playerId;
    if (!pid) { body.replaceChildren(el("p", { className: "muted", text: "請輸入玩家名稱或 ID，從建議清單選擇。" })); return; }
    try {
      if (!manage.catalog) await loadCatalog();
      const f = await fetchJson(`/api/monitor/players/${encodeURIComponent(pid)}/admin-farm`);
      if (pid !== manage.playerId) return;
      const chip = (label, value) => el("span", {}, [document.createTextNode(`${label} `), el("strong", { text: value })]);
      body.replaceChildren(
        el("div", { className: "pa-summary" }, [
          chip("玩家", `#${f.player_id} ${f.player_name}`), chip("農場", f.farm_name), chip("等級", String(f.level)),
          chip("金幣", fmtNum(f.gold)), chip("所屬群組", f.home_group_id ? `#${f.home_group_id} ${f.home_group_name}` : "未設定"),
          ...(f.external_user_id ? [chip("Discord", f.external_user_id)] : []),
        ]),
        farmValuesBlock(f),
        transferBlock(f),
        el("div", { className: "pa-grid" }, [inventoryBlock(f, manage.catalog), plotsBlock(f, manage.catalog), ranchBlock(f, manage.catalog)]),
      );
    } catch (err) {
      body.replaceChildren(el("p", { className: "is-error", text: `玩家資料載入失敗：${err.message}` }));
    }
  }

  function initManage() {
    $("ef-weekdays").append(...WEEKDAYS.map((name, i) => {
      const input = el("input");
      input.type = "checkbox";
      input.value = String(i);
      const label = el("label", {}, [input]);
      label.append(` ${name}`);
      return label;
    }));
    $("ef-schedule").addEventListener("change", updateEventScheduleFields);
    $("ef-submit").addEventListener("click", submitEvent);
    $("ef-reset").addEventListener("click", () => { resetEventForm(); clearDirty("event-form-box"); setNote("ef-note", ""); });
    $("crf-submit").addEventListener("click", submitCrop);
    $("crf-reset").addEventListener("click", () => { resetCropForm(); clearDirty("crop-form-box"); setNote("crf-note", ""); });
    $("atf-submit").addEventListener("click", submitAnimal);
    $("atf-reset").addEventListener("click", () => { resetAnimalForm(); clearDirty("animal-form-box"); setNote("atf-note", ""); });
    $("catalog-refresh").addEventListener("click", loadCatalog);
    $("pa-refresh").addEventListener("click", loadPlayerAdmin);
    $("pa-player").addEventListener("change", (e) => {
      const pid = pickedPlayerId(e.target, extra.players);
      manage.playerId = pid || null;
      setNote("pa-note", pid === undefined ? "找不到這位玩家，請從建議清單選擇。" : "", pid === undefined ? "bad" : undefined);
      loadPlayerAdmin();
    });
    resetEventForm();
  }

  // ---------- 排名獎勵 ----------
  const rewardsState = { editId: null, settlementId: null };
  const REWARD_TYPES = { gold: "金幣", fertilizer: "化肥", earthworm: "人工養殖蚯蚓", patrol_ticket: "巡查券" };

  function rewardRow(rank, reward) {
    const type = selectEl(null, Object.entries(REWARD_TYPES), reward.reward_type);
    type.className = "rr-type";
    const amount = numInput(null, reward.amount, 1, 10000000);
    amount.classList.add("rr-amount", "wide-num");
    return el("div", { className: "controls rr-rank" }, [el("span", { text: `第 ${rank} 名` }), type, amount]);
  }

  function setRewardRows(rewards) {
    $("rr-rewards").replaceChildren(...rewards.map((r, i) => rewardRow(i + 1, r)));
  }

  function readRewardRows() {
    return [...document.querySelectorAll("#rr-rewards .rr-rank")].map((row) => ({
      reward_type: row.querySelector(".rr-type").value,
      amount: Number(row.querySelector(".rr-amount").value),
    }));
  }

  function resetRuleForm() {
    rewardsState.editId = null;
    $("rr-form-title").textContent = "新增排名獎勵規則";
    $("rr-submit").textContent = "新增規則";
    $("rr-name").value = "";
    $("rr-scope").value = "group";
    $("rr-metric").value = "money";
    $("rr-period").value = "weekly";
    $("rr-enabled").checked = true;
    setRewardRows([{ reward_type: "gold", amount: 2000 }]);
  }

  function fillRuleForm(r) {
    rewardsState.editId = r.id;
    $("rr-form-box").open = true;
    $("rr-form-title").textContent = `編輯規則 #${r.id}`;
    $("rr-submit").textContent = "儲存變更";
    $("rr-name").value = r.name;
    $("rr-scope").value = r.scope;
    $("rr-metric").value = r.metric;
    $("rr-period").value = r.period;
    $("rr-enabled").checked = r.enabled;
    setRewardRows(r.rewards);
    $("rr-note").textContent = "";
    $("rr-form-box").scrollIntoView({ block: "nearest" });
  }

  async function rewardsAction(method, url, body, okText) {
    try {
      const res = await adminRequest(method, url, body);
      setNote("rr-note", okText, "good");
      if (okText) toast(okText, "good");
      await loadRankingRewards();
      return res;
    } catch (err) {
      setNote("rr-note", `失敗：${err.message}`, "bad");
      toast(err.message, "bad");
      return null;
    }
  }

  async function submitRule() {
    const body = {
      name: $("rr-name").value.trim(), scope: $("rr-scope").value, metric: $("rr-metric").value,
      period: $("rr-period").value, rewards: readRewardRows(), enabled: $("rr-enabled").checked,
    };
    if (!validateForm($("rr-form-box"))) return;
    if (body.rewards.some((r) => !Number.isInteger(r.amount) || r.amount < 1)) {
      setNote("rr-note", "每個名次的獎勵數量必須是 1 以上的整數。", "bad");
      document.querySelectorAll("#rr-rewards .rr-amount").forEach((n) => checkField(n));
      return;
    }
    const id = rewardsState.editId;
    if (id !== null) {
      const answer = await confirmDialog({
        title: `儲存規則 #${id} 的變更`,
        message: "修改獎勵從下一次結算起生效；停用或改週期後從現在起算，不回溯補發。",
        confirmText: "儲存變更",
      });
      if (!answer.ok) return;
    }
    const res = id === null
      ? await rewardsAction("POST", "/api/admin/ranking-rules", body, "規則已新增。")
      : await rewardsAction("PUT", `/api/admin/ranking-rules/${id}`, body, `規則 #${id} 已更新。`);
    if (res) {
      const note = $("rr-note").textContent;
      resetRuleForm();
      clearDirty("rr-form-box");
      setNote("rr-note", note, "good");
    }
  }

  async function loadSettlementDetail() {
    const id = rewardsState.settlementId;
    const box = $("rs-detail");
    if (!id) { box.hidden = true; return; }
    try {
      const d = await fetchJson(`/api/monitor/ranking-settlements/${encodeURIComponent(id)}`);
      if (id !== rewardsState.settlementId) return;
      const group = d.scope === "group";
      const ranking = d.ranking.slice(0, 10).map((r) => `${r.rank}. ${group ? `#${r.group_id} ${r.group_name}` : r.player_name}：${fmtNum(r.score)}`);
      const awards = d.awards.map((a) => `第 ${a.rank} 名｜${a.player_name}：${a.reward_text}`);
      box.hidden = false;
      box.replaceChildren(
        el("h3", { text: `結算 #${d.id}「${d.rule_name}」｜${d.summary}` }),
        el("div", { className: "bp-grid" }, [
          bpSection("當期排名（前 10）", ranking),
          bpSection(`得獎名單（${awards.length} 人次）`, awards),
        ]),
      );
    } catch (err) {
      box.hidden = false;
      box.replaceChildren(el("p", { className: "muted", text: `結算紀錄載入失敗：${err.message}` }));
    }
  }

  async function loadRankingRewards() {
    try {
      const data = await fetchJson("/api/monitor/ranking-rewards?limit=100");
      const admin = extra.admin.loggedIn;
      const rows = data.rules.map((r) => {
        const cells = [
          td(r.id, "num"), td(r.name), td(r.scope_label), td(r.metric_label), td(r.period_label), td(r.rewards_text, "wrap"),
          td(`${fmtTime(r.current_period_start)} ～ ${fmtTime(r.current_period_end)}`), td(fmtTime(r.next_settle_at)),
          el("td", {}, [r.enabled ? badge("啟用", "good") : badge("已停用", "muted")]),
        ];
        if (admin) {
          cells.push(actionsTd([
            smallButton("編輯", () => fillRuleForm(r)),
            smallButton(r.enabled ? "停用" : "啟用", async () => {
              if (!await confirmToggle("排名獎勵規則", r.name, r.enabled, "之後的期間不再結算與發獎；已發出的獎勵會保留。")) return;
              rewardsAction("PUT", `/api/admin/ranking-rules/${r.id}/enabled`,
                { enabled: !r.enabled }, `規則 #${r.id} 已${r.enabled ? "停用" : "啟用"}。`);
            }),
            smallButton("刪除", async () => {
              if (!await confirmDelete(`刪除規則「${r.name}」`, "已結算的紀錄與發出的獎勵會保留。")) return;
              if (rewardsState.editId === r.id) resetRuleForm();
              rewardsAction("DELETE", `/api/admin/ranking-rules/${r.id}`, null, `規則 #${r.id} 已刪除。`);
            }, "danger"),
          ]));
        }
        return el("tr", {}, cells);
      });
      replaceRows($("rr-rows"), rows, "目前沒有排名獎勵規則", 10);
      const settleRows = data.settlements.map((s) => {
        const top = s.ranking[0];
        const winner = top && top.score > 0 ? (s.scope === "group" ? top.group_name : top.player_name) : "（無人得獎）";
        const tr = el("tr", { className: "clickable" }, [
          td(s.id, "num"), td(s.rule_name), td(s.summary), td(winner), td(s.award_count, "num"),
          td(fmtTime(s.settled_at)), td(s.settled_by),
        ]);
        if (s.id === rewardsState.settlementId) tr.classList.add("selected");
        tr.tabIndex = 0;
        const pick = () => { rewardsState.settlementId = s.id; loadRankingRewards(); };
        tr.addEventListener("click", pick);
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
        return tr;
      });
      replaceRows($("rs-rows"), settleRows, "尚未有結算紀錄", 7);
    } catch (err) {
      for (const id of ["rr-rows", "rs-rows"]) replaceRows($(id), [], `排名獎勵資料載入失敗：${err.message}`, 10);
    }
    await loadSettlementDetail();
  }

  function initRankingRewards() {
    $("rr-add-rank").addEventListener("click", () => {
      const rows = readRewardRows();
      if (rows.length >= 10) { $("rr-note").textContent = "最多設定 10 個名次。"; return; }
      setRewardRows([...rows, { reward_type: "gold", amount: 1000 }]);
    });
    $("rr-remove-rank").addEventListener("click", () => {
      const rows = readRewardRows();
      if (rows.length > 1) setRewardRows(rows.slice(0, -1));
    });
    $("rr-submit").addEventListener("click", submitRule);
    $("rr-reset").addEventListener("click", () => { resetRuleForm(); clearDirty("rr-form-box"); setNote("rr-note", ""); });
    $("rr-settle").addEventListener("click", async () => {
      const answer = await confirmDialog({
        title: "立即結算到期期間",
        message: "會結算所有已結束但尚未結算的排名期間並立即發獎；同一期間只會發獎一次。",
        confirmText: "開始結算",
      });
      if (!answer.ok) return;
      const res = await rewardsAction("POST", "/api/admin/ranking-rules/settle-due", null, "");
      if (res) {
        const text = res.settled.length ? `已結算 ${res.settled.length} 個期間。` : "目前沒有到期需要結算的期間。";
        setNote("rr-note", text, res.settled.length ? "good" : undefined);
        toast(text, res.settled.length ? "good" : "info");
      }
    });
    resetRuleForm();
  }

  // ---------- 自動更新：只更新目前分頁；使用者操作中暫停，避免下拉選單、輸入與按鈕被重建 ----------
  const EDIT_PAUSE_MS = 20000;
  let lastInteraction = 0;
  const TAB_LOADERS = {
    overview: [loadOverview],
    groups: [loadGroupDetail],
    leaderboard: [loadLeaderboard],
    steals: [loadSteals, loadDefenses],
    ranch: [loadRanch],
    community: [loadCommunity, loadCatalog],  // 目錄提供任務表單的動物產品選項
    genetics: [loadGenetics],
    backpack: [loadBackpack],
    events: [loadCatalog],
    crops: [loadCatalog],
    animals: [loadCatalog],
    players: [loadCatalog],
    rewards: [loadRankingRewards],
    logs: [loadCommunity],
  };

  function userBusy() {
    const active = document.activeElement;
    const typing = active && active.matches("input, select, textarea") && active.closest("main, .tabs");
    return Boolean(typing) || Date.now() - lastInteraction < EDIT_PAUSE_MS;
  }

  function initEditGuard() {
    const mark = () => { lastInteraction = Date.now(); };
    const main = document.querySelector("main");
    for (const type of ["input", "change", "keydown", "pointerdown", "focusin"]) main.addEventListener(type, mark);
    // 滑鼠停在按鈕、選單或表單上（準備點擊）時也暫停，避免按鈕在點下前被重建
    main.addEventListener("pointerover", (e) => {
      if (e.target.closest("button, select, input, textarea, summary, label, details")) mark();
    });
  }

  async function refreshExtras(force = false) {
    const busy = !force && userBusy();
    $("edit-pause").hidden = !busy;
    if (busy) return;
    try {
      await loadPlayersAndGroups();
    } catch (err) {
      showError(`群組資料載入失敗：${err.message}`);
      return;
    }
    await Promise.all((TAB_LOADERS[state.tab] || []).map((fn) => fn()));
  }

  function initExtras() {
    initEditGuard();
    trackClicks();
    initFormValidation();
    initCommunity();
    initGenetics();
    initManage();
    initRankingRewards();
    loadAuth();
    $("admin-login-btn").addEventListener("click", adminLogin);
    $("admin-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") adminLogin(); });
    $("admin-logout").addEventListener("click", adminLogout);
    document.querySelectorAll("[data-lb-scope]").forEach((b) => b.addEventListener("click", () => {
      extra.lbScope = b.dataset.lbScope;
      document.querySelectorAll("[data-lb-scope]").forEach((x) => x.classList.toggle("on", x === b));
      loadLeaderboard();
    }));
    document.querySelectorAll("[data-lb-kind]").forEach((b) => b.addEventListener("click", () => {
      extra.lbKind = b.dataset.lbKind;
      document.querySelectorAll("[data-lb-kind]").forEach((x) => x.classList.toggle("on", x === b));
      loadLeaderboard();
    }));
    $("lb-group").addEventListener("change", (e) => { extra.lbGroupId = e.target.value || null; loadLeaderboard(); });
    document.querySelectorAll("[data-lb-period]").forEach((b) => b.addEventListener("click", () => {
      extra.lbPeriod = b.dataset.lbPeriod;
      document.querySelectorAll("[data-lb-period]").forEach((x) => x.classList.toggle("on", x === b));
      loadLeaderboard();
    }));
    for (const id of ["steal-group", "steal-player", "steal-outcome", "steal-from", "steal-to"]) {
      $(id).addEventListener("change", loadSteals);
    }
    // 清除搜尋欄（按 ×）時也要重新查詢
    $("steal-player").addEventListener("search", loadSteals);
    $("bp-player").addEventListener("change", (e) => {
      const pid = pickedPlayerId(e.target, extra.players);
      extra.bpPlayerId = pid || null;
      if (pid === undefined) {
        $("bp-body").replaceChildren(el("p", { className: "muted", text: "找不到這位玩家，請從建議清單選擇。" }));
        return;
      }
      loadBackpack();
    });
  }

  function init() {
    initTabs();
    initExtras();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const conn = new MonitorConnection({
      url: `${proto}://${location.host}/ws/monitor`,
      WebSocketImpl: window.WebSocket,
      setTimeoutImpl: window.setTimeout.bind(window),
      clearTimeoutImpl: window.clearTimeout.bind(window),
      onStatus: (status, delay) => {
        if (status === "connecting") setStatus("wait", "連線中…");
        else if (status === "reconnecting") setStatus("wait", "重新連線中…");
        else if (status === "open") setStatus("ok", "已連線・同步資料中");
        else if (status === "closed") {
          setStatus("bad", `連線中斷・${Math.round(delay / 1000)} 秒後重連`);
          document.body.classList.add("stale"); // 保留最後一次真實資料，但標示為過期
          showError("與監控伺服器的連線中斷，畫面資料可能已過期，正在自動重新連線。");
        }
      },
      onMessage: (msg) => {
        if (msg.type === "snapshot") onSnapshot(msg.data);
        else if (msg.type === "error") {
          setStatus("bad", "資料載入失敗");
          showError(msg.message || "資料載入失敗");
          clearData("資料暫時無法取得");
        }
      },
    });
    state.ready = true;
    refreshExtras(true);
    conn.connect();

    let debounce = null;
    $("farm-search").addEventListener("input", (e) => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        state.search = e.target.value.trim().slice(0, 40);
        loadSearch();
      }, 300);
    });
    $("detail-close").addEventListener("click", () => {
      state.selectedFarmId = null;
      state.detail = null;
      renderDetail();
      renderFarms();
    });
    setInterval(renderPlots, 1000);
  }

  document.addEventListener("DOMContentLoaded", init);
}
