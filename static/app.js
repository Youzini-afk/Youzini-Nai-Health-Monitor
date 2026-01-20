const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtTs(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toISOString().replace("T", " ").replace("Z", " UTC");
}

async function apiGet(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  const contentType = (resp.headers.get("content-type") || "").toLowerCase();
  if (contentType.includes("application/json")) {
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `request failed (${resp.status})`);
    return data;
  }
  const text = await resp.text().catch(() => "");
  if (!resp.ok) throw new Error(text || `request failed (${resp.status})`);
  return { text };
}

async function apiPostForm(url, form) {
  const resp = await fetch(url, { method: "POST", body: form });
  if (resp.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  const contentType = (resp.headers.get("content-type") || "").toLowerCase();
  if (contentType.includes("application/json")) {
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `request failed (${resp.status})`);
    return data;
  }
  const text = await resp.text().catch(() => "");
  if (!resp.ok) throw new Error(text || `request failed (${resp.status})`);
  return { text };
}

function toast(message, type = "info", timeoutMs = 2200) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.style.position = "fixed";
    el.style.right = "18px";
    el.style.bottom = "18px";
    el.style.zIndex = "50";
    el.style.maxWidth = "520px";
    el.style.padding = "12px 14px";
    el.style.borderRadius = "14px";
    el.style.border = "1px solid rgba(90,105,125,0.45)";
    el.style.background = "rgba(10, 14, 24, 0.88)";
    el.style.backdropFilter = "blur(10px)";
    el.style.fontWeight = "800";
    el.style.color = "rgba(230,237,246,0.92)";
    el.style.boxShadow = "0 24px 60px rgba(10,12,20,0.6)";
    el.style.display = "none";
    document.body.appendChild(el);
  }
  el.style.display = "block";
  el.style.borderColor =
    type === "ok"
      ? "rgba(34,197,94,0.55)"
      : type === "bad"
      ? "rgba(239,68,68,0.55)"
      : "rgba(124,58,237,0.55)";
  el.textContent = message;
  clearTimeout(el._t);
  el._t = setTimeout(() => {
    el.style.display = "none";
  }, timeoutMs);
}

function setActiveSection(section) {
  const navButtons = document.querySelectorAll(".nav-item");
  const sections = document.querySelectorAll(".section");
  navButtons.forEach((b) => b.classList.toggle("active", b.dataset.section === section));
  sections.forEach((s) => s.classList.toggle("active", s.id === `section-${section}`));

  const title = $("sectionTitle");
  const subtitle = $("sectionSubtitle");
  if (section === "dashboard") {
    title.textContent = "概览";
    subtitle.textContent = "Key 池统计 · 探活趋势";
  } else if (section === "keys") {
    title.textContent = "Key 池";
    subtitle.textContent = "上传 / 管理 / 健康检查";
  } else if (section === "settings") {
    title.textContent = "设置";
    subtitle.textContent = "Key 监控参数";
  }
  history.replaceState(null, "", `#${section}`);
}

function drawPie(canvas, segments) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const total = segments.reduce((a, s) => a + (s.value || 0), 0);
  const cx = w * 0.35;
  const cy = h * 0.5;
  const r = Math.min(w, h) * 0.32;

  const bg = "rgba(90,105,125,0.18)";
  ctx.fillStyle = bg;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fill();

  if (total <= 0) {
    ctx.fillStyle = "rgba(154,164,178,0.85)";
    ctx.font = "700 18px ui-sans-serif, system-ui";
    ctx.fillText("暂无数据", cx - 36, cy + 6);
    return;
  }

  let start = -Math.PI / 2;
  segments.forEach((s) => {
    const v = s.value || 0;
    if (v <= 0) return;
    const angle = (v / total) * Math.PI * 2;
    ctx.fillStyle = s.color;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + angle);
    ctx.closePath();
    ctx.fill();
    start += angle;
  });

  // Legend
  const lx = w * 0.62;
  let ly = h * 0.22;
  ctx.font = "800 13px ui-sans-serif, system-ui";
  segments.forEach((s) => {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, ly, 12, 12);
    ctx.fillStyle = "rgba(230,237,246,0.92)";
    ctx.fillText(`${s.label}: ${s.value ?? 0}`, lx + 18, ly + 11);
    ly += 22;
  });
}

function drawLine(canvas, points) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const pad = 26;
  const plotW = w - pad * 2;
  const plotH = h - pad * 2;

  const maxY = Math.max(1, ...points.map((p) => p.total || 0));
  const n = points.length;
  if (n <= 1) {
    ctx.fillStyle = "rgba(154,164,178,0.85)";
    ctx.font = "700 18px ui-sans-serif, system-ui";
    ctx.fillText("等待探活数据…", pad, pad + 22);
    return;
  }

  // grid
  ctx.strokeStyle = "rgba(90,105,125,0.25)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + (plotH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(pad + plotW, y);
    ctx.stroke();
  }

  function xy(i, yVal) {
    const x = pad + (plotW * i) / (n - 1);
    const y = pad + plotH - (plotH * yVal) / maxY;
    return [x, y];
  }

  function strokeSeries(getY, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const [x, y] = xy(i, getY(points[i]));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  strokeSeries((p) => p.healthy || 0, "rgba(34,197,94,0.85)");
  strokeSeries((p) => p.unhealthy || 0, "rgba(249,115,22,0.85)");
  strokeSeries((p) => p.invalid || 0, "rgba(239,68,68,0.85)");
  strokeSeries((p) => p.pending || 0, "rgba(148,163,184,0.75)");

  // legend
  ctx.font = "800 13px ui-sans-serif, system-ui";
  ctx.fillStyle = "rgba(230,237,246,0.92)";
  ctx.fillText("健康", pad + 2, pad - 6);
  ctx.fillStyle = "rgba(34,197,94,0.85)";
  ctx.fillRect(pad + 44, pad - 16, 12, 12);

  ctx.fillStyle = "rgba(230,237,246,0.92)";
  ctx.fillText("不健康", pad + 70, pad - 6);
  ctx.fillStyle = "rgba(249,115,22,0.85)";
  ctx.fillRect(pad + 126, pad - 16, 12, 12);

  ctx.fillStyle = "rgba(230,237,246,0.92)";
  ctx.fillText("无效", pad + 154, pad - 6);
  ctx.fillStyle = "rgba(239,68,68,0.85)";
  ctx.fillRect(pad + 190, pad - 16, 12, 12);

  ctx.fillStyle = "rgba(230,237,246,0.92)";
  ctx.fillText("待检查", pad + 218, pad - 6);
  ctx.fillStyle = "rgba(148,163,184,0.75)";
  ctx.fillRect(pad + 266, pad - 16, 12, 12);
}

function renderKeysTable(items) {
  const table = $("keysTable");
  if (!Array.isArray(items) || items.length === 0) {
    table.innerHTML = `<div class="cell">暂无 Key（或未开启 KEYPOOL）</div>`;
    return;
  }
  const cols = "60px 2.2fr 0.9fr 0.6fr 0.7fr 0.7fr 0.9fr 1.2fr 1.2fr 1.2fr";
  const head = `
    <div class="table-head" style="--cols:${cols}">
      <div class="cell">ID</div>
      <div class="cell">Hash</div>
      <div class="cell">状态</div>
      <div class="cell">Tier</div>
      <div class="cell">启用</div>
      <div class="cell">失败</div>
      <div class="cell">冷却</div>
      <div class="cell">上次检查</div>
      <div class="cell">上次拉取</div>
      <div class="cell">操作</div>
    </div>
  `;

  function statusBadge(status) {
    if (status === "healthy") return `<span class="badge ok">healthy</span>`;
    if (status === "unhealthy") return `<span class="badge warn">unhealthy</span>`;
    if (status === "invalid") return `<span class="badge bad">invalid</span>`;
    return `<span class="badge muted">${escapeHtml(status || "pending")}</span>`;
  }

  const rows = items
    .map((k) => {
      const enabled = !!k.is_enabled;
      const tier = k.tier == null ? "-" : String(k.tier);
      const err = k.last_error ? String(k.last_error) : "";
      const cooldown = k.cooldown_seconds == null ? "-" : `${k.cooldown_seconds}s`;
      return `
        <div class="table-row" style="--cols:${cols}">
          <div class="cell mono">${k.id}</div>
          <div class="cell mono" title="${escapeHtml(err)}">${escapeHtml(k.key_hash)}</div>
          <div class="cell">${statusBadge(k.status)}</div>
          <div class="cell mono">${escapeHtml(tier)}</div>
          <div class="cell mono">${enabled ? "是" : "否"}</div>
          <div class="cell mono">${k.fail_streak || 0}</div>
          <div class="cell mono">${escapeHtml(cooldown)}</div>
          <div class="cell mono">${fmtTs(k.last_checked_at)}</div>
          <div class="cell mono">${fmtTs(k.last_checked_out_at)}</div>
          <div class="cell">
            <div class="actions">
              <button class="btn ghost" data-action="check" data-id="${k.id}" type="button">检查</button>
              <button class="btn ghost" data-action="toggle" data-id="${k.id}" data-enabled="${enabled ? "0" : "1"}" type="button">
                ${enabled ? "禁用" : "启用"}
              </button>
              <button class="btn ghost" data-action="delete" data-id="${k.id}" type="button">删除</button>
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  table.innerHTML = `<div class="table">${head}${rows}</div>`;
}

async function loadDashboard() {
  const overallBadge = $("overallBadge");
  const keySummaryTag = $("keySummaryTag");

  const status = await apiGet("/statusz");
  $("statTime").textContent = (status.time || "-").replace("T", " ").replace("Z", " UTC");

  overallBadge.textContent = status.keypool_enabled ? "keypool" : "-";
  overallBadge.className = "badge " + (status.keypool_enabled ? "ok" : "muted");

  // Key pool summary (optional)
  let summary = null;
  try {
    summary = status.keypool || (await apiGet("/api/keys/summary"));
  } catch (e) {
    summary = null;
  }

  const statuses = (summary && summary.statuses) || {};
  const healthy = statuses.healthy || 0;
  const unhealthy = statuses.unhealthy || 0;
  const invalid = statuses.invalid || 0;
  const pending = statuses.pending || 0;
  const enabled = (summary && summary.enabled) || 0;

  $("statKeyEnabled").textContent = String(enabled);
  $("statKeyHealthy").textContent = String(healthy);
  $("statKeyUnhealthy").textContent = String(unhealthy);
  $("statKeyInvalid").textContent = String(invalid);
  $("statKeyPending").textContent = String(pending);
  keySummaryTag.textContent = summary ? `${healthy} healthy` : "未开启";

  $("statTargetTotal").textContent = String(enabled);
  $("statTargetOk").textContent = String(healthy);
  $("statTargetBad").textContent = String(unhealthy + invalid);
  const monitorSummaryTag = $("monitorSummaryTag");
  monitorSummaryTag.textContent = enabled ? `${healthy}/${enabled}` : "-";

  drawPie($("pieChart"), [
    { label: "healthy", value: healthy, color: "rgba(34,197,94,0.85)" },
    { label: "unhealthy", value: unhealthy, color: "rgba(249,115,22,0.85)" },
    { label: "invalid", value: invalid, color: "rgba(239,68,68,0.85)" },
    { label: "pending", value: pending, color: "rgba(148,163,184,0.75)" },
  ]);

  let timeline = { items: [] };
  try {
    timeline = await apiGet("/api/keypool/timeline?limit=240");
  } catch (e) {
    timeline = { items: [] };
  }
  drawLine(
    $("lineChart"),
    (timeline.items || []).map((p) => ({
      total: p.enabled || 0,
      healthy: p.healthy || 0,
      unhealthy: p.unhealthy || 0,
      invalid: p.invalid || 0,
      pending: p.pending || 0,
    }))
  );
}

async function loadKeys() {
  try {
    const data = await apiGet("/api/keys");
    renderKeysTable(data.items || []);
  } catch (e) {
    $("keysTable").innerHTML = `<div class="cell">加载失败：${escapeHtml(e.message || e)}</div>`;
  }
}

function wireEvents() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", async () => {
      setActiveSection(btn.dataset.section);
      if (btn.dataset.section === "keys") await loadKeys();
      if (btn.dataset.section === "settings") await loadConfig();
    });
  });

  $("refreshBtn").addEventListener("click", async () => {
    await loadDashboard();
    if (location.hash === "#keys") await loadKeys();
  });

  $("checkoutBtn").addEventListener("click", async () => {
    $("checkoutBtn").disabled = true;
    try {
      toast("正在拉取…");
      const data = await apiPostForm("/api/keys/checkout", new FormData());
      $("checkoutKey").textContent = data.key || "-";
      toast("拉取成功", "ok");
    } catch (e) {
      toast(`拉取失败：${String(e.message || e)}`, "bad", 4200);
    } finally {
      $("checkoutBtn").disabled = false;
    }
  });

  $("copyCheckoutBtn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("checkoutKey").textContent || "");
    } catch (e) {
      alert("复制失败（浏览器权限限制）");
    }
  });
  $("hideCheckoutBtn").addEventListener("click", () => {
    $("checkoutKey").textContent = "-";
  });

  $("checkAllBtn").addEventListener("click", async () => {
    $("checkAllBtn").disabled = true;
    try {
      toast("正在全量检查…");
      await apiPostForm("/api/keys/check-all", new FormData());
      await loadKeys();
      await loadDashboard();
      toast("全量检查完成", "ok");
    } catch (e) {
      toast(`检查失败：${String(e.message || e)}`, "bad", 4200);
    } finally {
      $("checkAllBtn").disabled = false;
    }
  });

  $("importBtn").addEventListener("click", async () => {
    const keys = $("keysInput").value || "";
    $("importResult").textContent = "导入中…";
    try {
      toast("正在导入…");
      const form = new FormData();
      form.set("keys", keys);
      const data = await apiPostForm("/api/keys/import", form);
      $("importResult").textContent = `收到 ${data.received} 条，新增 ${data.created} 条，跳过重复 ${data.skipped_existing} 条`;
      $("keysInput").value = "";
      await loadKeys();
      await loadDashboard();
      toast("导入成功", "ok");
    } catch (e) {
      $("importResult").textContent = `导入失败：${String(e.message || e)}`;
      toast(`导入失败：${String(e.message || e)}`, "bad", 4200);
    }
  });

  $("refreshKeysBtn").addEventListener("click", loadKeys);

  $("keysTable").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-action");
    const id = btn.getAttribute("data-id");
    try {
      if (action === "check") {
        btn.disabled = true;
        toast(`正在检查 #${id}…`);
        await apiPostForm(`/api/keys/${id}/check`, new FormData());
        toast(`检查完成 #${id}`, "ok");
      } else if (action === "toggle") {
        btn.disabled = true;
        const form = new FormData();
        form.set("enabled", btn.getAttribute("data-enabled") === "1" ? "true" : "false");
        toast(`正在切换 #${id}…`);
        await apiPostForm(`/api/keys/${id}/toggle`, form);
        toast(`切换完成 #${id}`, "ok");
      } else if (action === "delete") {
        if (!confirm("确定删除这个 Key？")) return;
        btn.disabled = true;
        toast(`正在删除 #${id}…`);
        await apiPostForm(`/api/keys/${id}/delete`, new FormData());
        toast(`已删除 #${id}`, "ok");
      }
      await loadKeys();
      await loadDashboard();
    } catch (e) {
      toast(`操作失败：${String(e.message || e)}`, "bad", 4200);
    } finally {
      btn.disabled = false;
    }
  });
}

async function loadConfig() {
  const cfgResult = $("cfgResult");
  cfgResult.textContent = "加载中…";
  try {
    const data = await apiGet("/api/config");
    const kp = data.keypool || {};
    $("cfgInterval").value = String(kp.health_check_interval_seconds ?? 300);
    $("cfgFailThreshold").value = String(kp.health_check_fail_threshold ?? 3);
    $("cfgRequireOpus").checked = !!kp.require_opus_tier;
    $("cfgAutoEnabled").checked = kp.health_check_enabled !== false;
    cfgResult.textContent = "已加载";
  } catch (e) {
    cfgResult.textContent = `加载失败：${String(e.message || e)}`;
  }
}

async function saveConfig() {
  const cfgResult = $("cfgResult");
  cfgResult.textContent = "保存中…";
  try {
    const form = new FormData();
    form.set("health_check_interval_seconds", $("cfgInterval").value || "300");
    form.set("health_check_fail_threshold", $("cfgFailThreshold").value || "3");
    form.set("require_opus_tier", $("cfgRequireOpus").checked ? "true" : "false");
    form.set("health_check_enabled", $("cfgAutoEnabled").checked ? "true" : "false");
    await apiPostForm("/api/config", form);
    cfgResult.textContent = "已保存（下一轮检查生效）";
    toast("配置已保存", "ok");
  } catch (e) {
    cfgResult.textContent = `保存失败：${String(e.message || e)}`;
    toast(`保存失败：${String(e.message || e)}`, "bad", 4200);
  }
}

async function main() {
  const section = (location.hash || "#dashboard").slice(1);
  setActiveSection(["dashboard", "keys", "settings"].includes(section) ? section : "dashboard");
  wireEvents();
  await loadDashboard();
  if (location.hash === "#keys") await loadKeys();
  if (location.hash === "#settings") await loadConfig();

  const saveBtn = $("saveConfigBtn");
  if (saveBtn) saveBtn.addEventListener("click", saveConfig);
}

main().catch((e) => console.error(e));
