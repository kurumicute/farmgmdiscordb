// 介紹網站：填入邀請連結與即時統計。一律用 textContent，不使用 innerHTML。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const num = new Intl.NumberFormat("zh-TW");
  const time = new Intl.DateTimeFormat("zh-TW", { dateStyle: "short", timeStyle: "short", timeZone: "Asia/Taipei" });

  function setText(id, value) {
    const node = $(id);
    if (node) node.textContent = value;
  }

  async function loadInfo() {
    try {
      const res = await fetch("/api/info", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!res.ok) return;
      const info = await res.json();
      setText("site-name", info.name);
      setText("footer-name", info.name);
      if (info.invite_url) {
        for (const id of ["invite-btn", "invite-link", "invite-top"]) {
          const a = $(id);
          a.href = info.invite_url;
          a.target = "_blank";
          a.rel = "noopener";
        }
      }
      if (info.music_invite_url) {
        const a = $("music-invite-btn");
        a.href = info.music_invite_url;
        a.target = "_blank";
        a.rel = "noopener";
        a.hidden = false;
      }
      if (info.music) renderMusicInfo(info.music);
      if (info.support_url) {
        for (const id of ["support-btn", "support-link"]) {
          const a = $(id);
          a.href = info.support_url;
          a.target = "_blank";
          a.rel = "noopener";
          a.hidden = false;
        }
      } else {
        $("support-link").parentElement.hidden = true;
      }
    } catch (err) {
      // 網路錯誤時保留預設連結
    }
  }

  // ---------- 音樂設定（前綴指令是否開啟、歌單上限、閒置時間、預設音量） ----------
  function renderMusicInfo(m) {
    setText("m-playlist", num.format(m.playlist_limit));
    setText("m-idle", String(m.idle_minutes));
    setText("m-volume", String(m.default_volume_pct));
    const note = $("m-prefix-note");
    if (m.prefix_commands) {
      note.replaceChildren(
        document.createTextNode("所有音樂指令都可以用斜線 "), code("/"),
        document.createTextNode(" 或前綴 "), code(m.prefix),
        document.createTextNode(" 開頭（例如 "), code(`${m.prefix}p 歌名`),
        document.createTextNode("）；各伺服器可以用 /prefix 改前綴。"),
      );
      document.querySelectorAll("td.m-alias[data-alias]").forEach((td) => {
        td.replaceChildren(code(`${m.prefix}${td.dataset.alias}`));
      });
    } else {
      // 前綴指令未開啟時隱藏簡寫欄，避免玩家輸入沒有作用的指令
      document.querySelectorAll(".m-alias").forEach((n) => { n.hidden = true; });
    }
  }

  function code(text) {
    const c = document.createElement("code");
    c.textContent = text;
    return c;
  }

  // ---------- 操作手冊：數值即時讀自遊戲設定；讀取失敗時保留頁面上的預設值並標示 ----------
  function duration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h && m) return `${h} 小時 ${m} 分鐘`;
    if (h) return `${h} 小時`;
    if (m) return `${m} 分鐘`;
    return `${seconds} 秒`;
  }

  const mul = (v) => `×${Number(v.toFixed(2))}`;
  const pct = (v) => `${Number(v.toFixed(2))}%`;

  function fillRows(id, rows) {
    const body = $(id);
    if (!body) return;
    body.replaceChildren(...rows.map((cells) => {
      const tr = document.createElement("tr");
      for (const cell of cells) {
        const td = document.createElement("td");
        if (cell instanceof Node) td.appendChild(cell); else td.textContent = String(cell);
        tr.appendChild(td);
      }
      return tr;
    }));
  }

  function statusTag(active) {
    const span = document.createElement("span");
    span.className = active ? "tag tag-on" : "tag";
    span.textContent = active ? "進行中" : "未在時段內";
    return span;
  }

  function renderGuide(g) {
    const s = g.start;
    setText("g-start", `${num.format(s.gold)} 金幣、${s.plots} 格農田、倉庫 ${s.warehouse} 格`);
    setText("g-wither", duration(s.wither_minutes * 60));
    setText("g-upgrade", `${num.format(s.upgrade_cost)} 金幣，農田 +${s.upgrade_plots} 格（最多 ${s.max_plots} 格）`);
    setText("g-steal-max", String(s.steal_max));
    setText("g-transfer", `${s.transfer_cooldown_days} 天`);
    setText("g-craft-max", String(s.craft_max));
    const barn = g.buildings.find((b) => b.name === "畜舍");
    if (barn) setText("g-barn", `${num.format(barn.price)} 金幣，每座 ${s.barn_slots} 個欄位，最多 ${barn.max} 座`);
    setText("g-wh-price", `${num.format(s.warehouse_prices[0])} 起`);
    setText("g-wh-desc", `每次 +${s.warehouse_step} 格：${s.warehouse_prices.map((p) => num.format(p)).join(" → ")}，最大 ${s.warehouse_max} 格`);

    setText("crop-times", g.crops.length
      ? g.crops.map((c) => `${c.name} ${duration(c.grow_seconds)}`).join("、")
      : "目前沒有開放種植的作物");
    fillRows("g-crops", g.crops.length
      ? g.crops.map((c) => [c.name, num.format(c.seed_price), duration(c.grow_seconds), `×${c.harvest_quantity}`,
        num.format(c.sell_price), c.variant_name])
      : [["目前沒有開放種植的作物", "", "", "", "", ""]]);
    fillRows("g-qualities", g.qualities.map((q) => [q.name, pct(q.chance_pct), mul(q.price_mul)]));
    setText("g-level-note", `農場每升 1 級，有 ${pct(g.level_bonus_pct)} 機率從普通升到較高品質（最多 ${pct(g.level_bonus_max_pct)}）。`);
    fillRows("g-mutations", g.mutations.map((m) => [m.name, pct(m.chance_pct), m.effect]));
    fillRows("g-items", g.items.map((i) => [i.name, num.format(i.price), i.description]));
    fillRows("g-animals", g.animals.length
      ? g.animals.map((a) => [a.name, num.format(a.price), num.format(a.feed_cost), duration(a.cycle_seconds),
        `${a.product_name} ×${a.product_quantity}（${num.format(a.product_price)} / 個）`])
      : [["目前沒有開放購買的動物", "", "", "", ""]]);
    fillRows("g-recipes", g.recipes.map((r) => [r.name, `${r.building}（${num.format(r.building_price)}）`,
      r.inputs.map((i) => `${i.name} ×${i.quantity}`).join("、"), duration(r.seconds), num.format(r.sell_price)]));
    fillRows("g-defenses", g.defenses.map((d) => [d.name, num.format(d.price), String(d.max), `${d.chance_pct}%`,
      d.range === null ? "全部農田" : `連續 ${d.range} 格（可調整）`]));
    setText("g-defense-note", `判定順序：${g.defenses.map((d) => d.name).join(" → ")}，整體阻止機率最高 ${s.max_block_pct}%。`
      + `「📍 調整守護範圍」輸入起始農田編號；「🔦 巡查」消耗 1 張巡查券，可以看到近 ${s.patrol_days} 天是誰來偷。`);

    const gm = g.gamble;
    const half = gm.big_from - gm.min;
    const span = gm.max - gm.min + 1;
    $("g-gamble").replaceChildren(...[
      `只能押 ${gm.qualities.join("、")} 品質，或 ${gm.mutations.join("、")} 的作物。`,
      `系統從 ${gm.min}–${gm.max} 擲一個數字：比大（${gm.big_from}–${gm.max}）／比小（${gm.min}–${gm.big_from - 1}）`
        + `贏了價值 ×${gm.hilo_multiplier}（機率 ${Math.round(((span - half) / span) * 100)}%）；`
        + `猜數字猜中價值 ×${gm.exact_multiplier}（機率 ${Math.round(100 / span)}%）。`,
      "輸了下注的作物會被沒收；贏了存成「🏆 賭場獎品」，可以在販售頁賣掉，也可以留著展示。",
    ].map((t) => { const li = document.createElement("li"); li.textContent = t; return li; }));

    fillRows("g-events", g.events.length
      ? g.events.map((e) => [e.name, e.effects_text, e.schedule_text, statusTag(e.active_now)])
      : [["目前沒有排定的活動", "", "", ""]]);
  }

  async function loadGuide() {
    const note = $("guide-source");
    try {
      const res = await fetch("/api/guide", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      renderGuide(await res.json());
      note.textContent = `✓ 以下數值即時讀自遊戲設定（最後更新 ${time.format(new Date())}），管理員調整後會自動同步。`;
      note.className = "data-source ok";
    } catch (err) {
      note.textContent = "⚠ 暫時無法讀取最新遊戲設定，以下顯示預設值，實際數值請以遊戲內顯示為準。";
      note.className = "data-source warn";
      fillRows("g-events", [["暫時無法讀取活動，請在 /farm 總覽查看目前的活動", "", "", ""]]);
    }
  }

  const STAT_IDS = ["stat-players", "s-players", "stat-servers", "s-servers", "stat-growing", "s-growing", "s-farms",
    "s-today", "s-last"];
  let hasStats = false;

  // 統計狀態：讀取中／即時／無法取得。失敗時不顯示假數字：保留上次真實數值但淡化，並清楚標示
  function setLive(kind, text) {
    for (const id of ["hero-live", "status-live"]) {
      const node = $(id);
      node.textContent = text;
      node.className = `live ${kind}`;
    }
  }

  async function loadStats() {
    try {
      const res = await fetch("/api/stats", { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      const s = await res.json();
      for (const [id, key] of [["stat-players", "players"], ["s-players", "players"], ["stat-servers", "servers"],
        ["s-servers", "servers"], ["stat-growing", "growing"], ["s-growing", "growing"], ["s-farms", "farms"],
        ["s-today", "actions_today"]]) {
        setText(id, num.format(s[key]));
      }
      setText("s-last", s.last_activity ? time.format(new Date(s.last_activity)) : "尚無紀錄");
      for (const id of STAT_IDS) $(id).classList.remove("loading");
      hasStats = true;
      document.querySelectorAll(".quick-stats, #status-grid").forEach((n) => n.classList.remove("stale"));
      setLive("ok", "即時");
      $("status-note").classList.remove("error");
      setText("status-note", `統計每 ${s.cached_seconds} 秒更新一次。最後更新：${time.format(new Date())}`);
    } catch (err) {
      for (const id of STAT_IDS) {
        $(id).classList.remove("loading");
        if (!hasStats) setText(id, "—");
      }
      if (hasStats) document.querySelectorAll(".quick-stats, #status-grid").forEach((n) => n.classList.add("stale"));
      setLive("bad", "暫時無法取得");
      $("status-note").classList.add("error");
      setText("status-note", hasStats
        ? "統計資料暫時無法取得，畫面上是上一次成功取得的數字，可能不是最新，稍後會自動重試。"
        : "統計資料暫時無法取得，稍後會自動重試。");
    }
  }

  // 手機選單
  function initMenu() {
    const btn = $("menu-btn");
    const nav = $("nav");
    const setOpen = (open) => {
      nav.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "關閉選單" : "開啟選單");
    };
    btn.addEventListener("click", () => setOpen(!nav.classList.contains("open")));
    nav.addEventListener("click", (e) => { if (e.target.closest("a")) setOpen(false); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && nav.classList.contains("open")) { setOpen(false); btn.focus(); }
    });
    document.addEventListener("click", (e) => {
      if (nav.classList.contains("open") && !e.target.closest("#topbar")) setOpen(false);
    });
  }

  // 導覽列：捲動後加背景，並標示目前所在的區塊
  function initScrollSpy() {
    const bar = $("topbar");
    const onScroll = () => bar.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    if (!("IntersectionObserver" in window)) return;
    const links = [...document.querySelectorAll("#nav a[href^='#']:not(.nav-cta)")];
    const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
    const visible = new Map();
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) visible.set(entry.target.id, entry.isIntersecting ? entry.intersectionRatio : 0);
      let best = null;
      for (const [id, ratio] of visible) if (ratio > 0 && (!best || ratio > visible.get(best))) best = id;
      if (!best) return;
      for (const a of links) {
        const on = a === byId.get(best);
        a.classList.toggle("active", on);
        if (on) a.setAttribute("aria-current", "location"); else a.removeAttribute("aria-current");
      }
    }, { rootMargin: "-30% 0px -55% 0px", threshold: [0, 0.25, 0.5, 1] });
    for (const id of byId.keys()) {
      const section = $(id);
      if (section) observer.observe(section);
    }
  }

  // 操作手冊：全部展開／收合
  function initGuide() {
    const all = () => document.querySelectorAll("#guide details.guide");
    $("guide-expand").addEventListener("click", () => all().forEach((d) => { d.open = true; }));
    $("guide-collapse").addEventListener("click", () => all().forEach((d) => { d.open = false; }));
  }

  // ---------- 銀河星空背景 ----------
  // 三層星星＋銀河帶＋星雲，捲動時遠景移動慢、近景移動快（視差）；星星閃爍、偶爾出現流星。
  // 減少動態效果偏好：只畫靜態星空，不閃爍、不視差；分頁隱藏時暫停動畫。
  function galaxy() {
    const canvas = $("galaxy");
    const ctx = canvas && canvas.getContext && canvas.getContext("2d");
    if (!ctx) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const LAYERS = [
      { speed: 0.04, density: 1 / 2600, size: [0.4, 0.9], alpha: [0.25, 0.6] },  // 遠景
      { speed: 0.10, density: 1 / 6500, size: [0.8, 1.4], alpha: [0.45, 0.85] }, // 中景
      { speed: 0.20, density: 1 / 26000, size: [1.3, 2.2], alpha: [0.7, 1] },     // 近景（較亮、有光暈）
    ];
    const TINTS = ["255,255,255", "255,244,224", "214,226,255", "255,226,240", "200,215,255"];
    const rand = (a, b) => a + Math.random() * (b - a);
    let w = 0;
    let h = 0;
    let dpr = 1;
    let layers = [];
    let band = null;
    let glow = null;
    let shooting = null;
    let nextShoot = 0;
    let mouseX = 0;
    let mouseY = 0;
    let running = false;

    // 近景亮星的光暈貼圖（預先畫好，每幀只貼圖）
    function makeGlow() {
      const c = document.createElement("canvas");
      c.width = c.height = 32;
      const g = c.getContext("2d");
      const grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
      grad.addColorStop(0, "rgba(255,255,255,0.9)");
      grad.addColorStop(0.25, "rgba(220,230,255,0.35)");
      grad.addColorStop(1, "rgba(200,210,255,0)");
      g.fillStyle = grad;
      g.fillRect(0, 0, 32, 32);
      return c;
    }

    // 銀河帶：斜向的發光帶，由色霧、細塵與暗帶組成，畫在比畫面高的離屏 canvas 上供捲動
    function makeBand() {
      const c = document.createElement("canvas");
      const bw = Math.ceil(w * dpr);
      const bh = Math.ceil(h * 1.8 * dpr);
      c.width = bw;
      c.height = bh;
      const g = c.getContext("2d");
      g.scale(dpr, dpr);
      const H = h * 1.8;
      g.translate(w / 2, H / 2);
      g.rotate(-0.42);
      const len = Math.hypot(w, H);
      const thick = Math.max(160, Math.min(w, h) * 0.32);
      g.globalCompositeOperation = "lighter";
      // 色霧：沿著帶狀分布的大小光團
      const hues = ["120,90,255", "90,140,255", "255,120,190", "255,200,150"];
      for (let i = 0; i < 70; i += 1) {
        const x = rand(-len / 2, len / 2);
        const y = rand(-thick / 2, thick / 2) * (0.4 + Math.random() * 0.6);
        const r = rand(thick * 0.25, thick * 0.9);
        const core = Math.abs(x) < len * 0.18 && Math.random() < 0.5;
        const color = core ? hues[3] : hues[Math.floor(Math.random() * 3)];
        const grad = g.createRadialGradient(x, y, 0, x, y, r);
        grad.addColorStop(0, `rgba(${color},${core ? 0.07 : 0.05})`);
        grad.addColorStop(1, `rgba(${color},0)`);
        g.fillStyle = grad;
        g.beginPath();
        g.arc(x, y, r, 0, Math.PI * 2);
        g.fill();
      }
      // 細塵：大量極小的星點，越靠近中線越密
      const dust = Math.floor((len * thick) / 260);
      for (let i = 0; i < dust; i += 1) {
        const x = rand(-len / 2, len / 2);
        const y = (Math.random() + Math.random() + Math.random() - 1.5) * thick * 0.55;
        g.fillStyle = `rgba(255,255,255,${rand(0.08, 0.5)})`;
        const s = rand(0.3, 1.1);
        g.fillRect(x, y, s, s);
      }
      // 暗帶：銀河中央的塵埃遮蔽
      g.globalCompositeOperation = "source-over";
      for (let i = 0; i < 26; i += 1) {
        const x = rand(-len / 2, len / 2);
        const y = rand(-thick * 0.08, thick * 0.08);
        const r = rand(thick * 0.08, thick * 0.22);
        const grad = g.createRadialGradient(x, y, 0, x, y, r);
        grad.addColorStop(0, "rgba(5,8,22,0.35)");
        grad.addColorStop(1, "rgba(5,8,22,0)");
        g.fillStyle = grad;
        g.beginPath();
        g.arc(x, y, r, 0, Math.PI * 2);
        g.fill();
      }
      return c;
    }

    function makeStars() {
      layers = LAYERS.map((L) => {
        const count = Math.round(w * h * 1.6 * L.density);
        const stars = [];
        for (let i = 0; i < count; i += 1) {
          stars.push({
            x: Math.random() * w,
            y: Math.random() * h * 1.6, // 比畫面高，捲動時循環
            r: rand(L.size[0], L.size[1]),
            a: rand(L.alpha[0], L.alpha[1]),
            tw: rand(0.6, 2.2),        // 閃爍速度
            ph: Math.random() * Math.PI * 2,
            tint: TINTS[Math.floor(Math.random() * TINTS.length)],
          });
        }
        return { ...L, stars };
      });
    }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.ceil(w * dpr);
      canvas.height = Math.ceil(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      makeStars();
      band = makeBand();
      draw(performance.now());
    }

    function draw(t) {
      const scroll = reduce ? 0 : window.scrollY;
      ctx.clearRect(0, 0, w, h);
      // 銀河帶：捲動速度介於遠景與中景之間
      const bandH = h * 1.8;
      const by = -((scroll * 0.07) % bandH);
      const mx = reduce ? 0 : mouseX * 6;
      ctx.drawImage(band, 0, 0, band.width, band.height, mx, by, w, bandH);
      ctx.drawImage(band, 0, 0, band.width, band.height, mx, by + bandH, w, bandH);
      const time = t / 1000;
      for (const L of layers) {
        const span = h * 1.6;
        const offY = scroll * L.speed;
        const offX = reduce ? 0 : mouseX * L.speed * 60;
        const offMY = reduce ? 0 : mouseY * L.speed * 40;
        for (const s of L.stars) {
          let y = (s.y - offY - offMY) % span;
          if (y < 0) y += span;
          if (y > h + 4) continue;
          const x = (s.x + offX + w) % w;
          const a = reduce ? s.a : s.a * (0.65 + 0.35 * Math.sin(time * s.tw + s.ph));
          if (s.r > 1.6) {
            ctx.globalAlpha = a * 0.6;
            const gs = s.r * 7;
            ctx.drawImage(glow, x - gs / 2, y - gs / 2, gs, gs);
          }
          ctx.globalAlpha = a;
          ctx.fillStyle = `rgb(${s.tint})`;
          if (s.r < 1) {
            ctx.fillRect(x, y, s.r * 1.4, s.r * 1.4);
          } else {
            ctx.beginPath();
            ctx.arc(x, y, s.r * 0.75, 0, Math.PI * 2);
            ctx.fill();
          }
        }
      }
      ctx.globalAlpha = 1;
      if (!reduce) drawShooting(t);
    }

    // 流星：每 5–12 秒一顆，斜向劃過畫面上半部
    function drawShooting(t) {
      if (!shooting && t > nextShoot) {
        const fromLeft = Math.random() < 0.5;
        shooting = {
          x: fromLeft ? rand(-50, w * 0.4) : rand(w * 0.6, w + 50),
          y: rand(-20, h * 0.35),
          vx: (fromLeft ? 1 : -1) * rand(9, 14),
          vy: rand(3.5, 6),
          life: 0,
          max: rand(55, 80),
        };
      }
      if (!shooting) return;
      const s = shooting;
      s.life += 1;
      s.x += s.vx;
      s.y += s.vy;
      const fade = Math.sin((s.life / s.max) * Math.PI);
      const tail = 14;
      const grad = ctx.createLinearGradient(s.x, s.y, s.x - s.vx * tail, s.y - s.vy * tail);
      grad.addColorStop(0, `rgba(255,255,255,${0.95 * fade})`);
      grad.addColorStop(0.3, `rgba(200,215,255,${0.45 * fade})`);
      grad.addColorStop(1, "rgba(200,215,255,0)");
      ctx.strokeStyle = grad;
      ctx.lineWidth = 1.6;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(s.x - s.vx * tail, s.y - s.vy * tail);
      ctx.stroke();
      if (s.life >= s.max) {
        shooting = null;
        nextShoot = t + rand(5000, 12000);
      }
    }

    // 星雲與月亮：以 CSS transform 做較慢的視差（transform 不會觸發版面重排）
    const nebulaA = document.querySelector(".nebula-a");
    const nebulaB = document.querySelector(".nebula-b");
    const moon = $("moon");
    function parallaxDom() {
      if (reduce) return;
      const y = window.scrollY;
      nebulaA.style.transform = `translate3d(${mouseX * 14}px, ${-y * 0.05 + mouseY * 10}px, 0)`;
      nebulaB.style.transform = `translate3d(${mouseX * -18}px, ${-y * 0.09 + mouseY * -12}px, 0)`;
      moon.style.transform = `translate3d(${mouseX * -8}px, ${-y * 0.35}px, 0)`;
    }

    function frame(t) {
      if (!running) return;
      draw(t);
      parallaxDom();
      requestAnimationFrame(frame);
    }

    function start() {
      if (running || reduce) return;
      running = true;
      requestAnimationFrame(frame);
    }

    glow = makeGlow();
    nextShoot = performance.now() + 2500;
    resize();
    canvas.parentElement.classList.add("ready");
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(resize, 150);
    });
    if (reduce) {
      // 靜態星空：只在尺寸改變時重畫
      return;
    }
    window.addEventListener("pointermove", (e) => {
      mouseX = e.clientX / w - 0.5;
      mouseY = e.clientY / h - 0.5;
    }, { passive: true });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) running = false; else start();
    });
    start();
  }

  galaxy();
  initMenu();
  initScrollSpy();
  initGuide();
  loadInfo();
  loadGuide();
  loadStats();
  setInterval(loadStats, 30000);
  setInterval(loadGuide, 300000);
})();
