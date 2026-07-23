const bridge = window.AstrBotPluginPage;

const VOICE_TYPES = [
  "任命助理",
  "交谈1",
  "交谈2",
  "交谈3",
  "晋升后交谈1",
  "晋升后交谈2",
  "信赖提升后交谈1",
  "信赖提升后交谈2",
  "信赖提升后交谈3",
  "闲置",
  "干员报到",
  "观看作战记录",
  "精英化晋升1",
  "精英化晋升2",
  "编入队伍",
  "任命队长",
  "行动出发",
  "行动开始",
  "选中干员1",
  "选中干员2",
  "部署1",
  "部署2",
  "作战中1",
  "作战中2",
  "作战中3",
  "作战中4",
  "完成高难行动",
  "3星结束行动",
  "非3星结束行动",
  "行动失败",
  "进驻设施",
  "戳一下",
  "信赖触摸",
  "标题",
  "新年祝福",
  "问候",
  "生日",
  "周年庆典",
];

const LANGUAGES = [
  { code: "fy", name: "方言", rank: "1" },
  { code: "cn", name: "中文", rank: "2" },
  { code: "jp", name: "日语", rank: "3" },
  { code: "us", name: "英语", rank: "4" },
  { code: "kr", name: "韩语", rank: "5" },
  { code: "it", name: "意语", rank: "6" },
];

const state = {
  context: null,
  view: "overview",
  overview: null,
  archives: [],
  archiveDetail: null,
  pendingReplace: null,
  audioUrl: null,
  taskTimer: null,
  taskSignature: "",
  archiveTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(bridge.getLocale?.() || "zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function initials(name) {
  const clean = String(name || "?").replace(/皮肤.*$/, "").trim();
  return [...clean].slice(-2).join("") || "?";
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "is-error" : ""} ${
    type === "success" ? "is-success" : ""
  }`;
  node.textContent = message;
  $("#toast-stack").append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error || "操作失败");
}

async function run(action, { success = null, silent = false } = {}) {
  try {
    const result = await action();
    if (success) toast(success, "success");
    return result;
  } catch (error) {
    if (!silent) toast(errorMessage(error), "error");
    throw error;
  }
}

function setConnection(connected, label) {
  $("#connection-label").textContent = label;
  const dot = $(".topbar-meta .live-dot");
  dot?.classList.toggle("is-offline", !connected);
}

function switchView(view) {
  state.view = view;
  $$(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
  });
  $$("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === view);
  });
  $("#sidebar").classList.remove("is-open");
  window.scrollTo({ top: 0, behavior: "smooth" });

  if (view === "overview") loadOverview();
  if (view === "archives") loadArchives();
  if (view === "tasks") loadTasks();
  if (view === "integrity") loadIntegrity();
  if (view === "bindings") loadBindings();
  if (view === "recovery") loadRecovery();
}

function renderLanguages() {
  const archiveSelect = $("#archive-language");
  archiveSelect.innerHTML =
    '<option value="all">全部语言</option>' +
    LANGUAGES.map(
      (item) => `<option value="${item.code}">${escapeHtml(item.name)}</option>`,
    ).join("");

  $("#fetch-languages").innerHTML = LANGUAGES.map(
    (item) => `
      <label class="check-item">
        <input type="checkbox" name="fetch-language" value="${item.rank}" ${
          ["1", "2", "3"].includes(item.rank) ? "checked" : ""
        } />
        <span>${escapeHtml(item.name)} <small>R-${item.rank}</small></span>
      </label>
    `,
  ).join("");
}

function statCard(code, label, value, note, icon) {
  return `
    <article class="stat-card">
      <div class="stat-top"><span>${code} / ${escapeHtml(label)}</span><span class="stat-icon">${icon}</span></div>
      <strong>${escapeHtml(value)}</strong>
      <p>${escapeHtml(note)}</p>
    </article>
  `;
}

function auditActionLabel(action) {
  const labels = {
    rescan: "重建索引",
    replace_voice: "替换语音",
    import_archive: "批量导入",
    trash_voice: "移入回收站",
    trash_batch: "批量移入回收站",
    restore_voice: "恢复语音",
    purge_voice: "永久删除",
    export_voice: "导出语音",
    export_archive: "导出语音包",
    save_binding: "保存快捷绑定",
    remove_binding: "删除快捷绑定",
    task_completed: "后台任务完成",
    task_failed: "后台任务失败",
    task_cancelled: "后台任务取消",
  };
  return labels[action] || action || "系统操作";
}

function renderAuditItems(items, target, emptyText = "暂无操作记录") {
  const root = $(target);
  if (!items?.length) {
    root.innerHTML = `<div class="empty-state compact"><p>${escapeHtml(emptyText)}</p></div>`;
    return;
  }
  root.innerHTML = items
    .map(
      (item) => `
        <div class="${target === "#overview-audit" ? "timeline-item" : "audit-item"}">
          <span class="timeline-icon">${item.action?.includes("failed") ? "!" : "↳"}</span>
          <div>
            <b>${escapeHtml(auditActionLabel(item.action))}</b>
            <small>${escapeHtml(item.target || "—")} · ${escapeHtml(item.username || "dashboard")}</small>
          </div>
          <time>${escapeHtml(formatDate(item.time))}</time>
        </div>
      `,
    )
    .join("");
}

function renderOverview(data) {
  state.overview = data;
  const storage = data.storage || {};
  $("#overview-stats").innerHTML = [
    statCard("OPERATOR", "基础干员", data.operators, "已建立本地基础档案", "◫"),
    statCard("OUTFIT", "皮肤档案", data.skins, "每套皮肤独立管理", "◇"),
    statCard("AUDIO", "WAV 文件", storage.wavFiles, `${data.voiceTypes} 类语音定义`, "≋"),
    statCard("TASK", "运行任务", data.runningTasks, `${data.bindings} 条快捷绑定`, "↻"),
  ].join("");
  $("#storage-size").textContent = formatBytes(storage.bytes);
  $("#storage-legend").innerHTML = `
    <div><i></i><span>本地 WAV</span><b>${escapeHtml(storage.wavFiles || 0)}</b></div>
    <div><i style="background:var(--yellow)"></i><span>回收站</span><b>${escapeHtml(storage.trashItems || 0)}</b></div>
    <div><i style="background:var(--green)"></i><span>语言包</span><b>${escapeHtml(data.languageCount || 0)}</b></div>
  `;
  $("#sidebar-index-status").textContent = "索引已同步";
  $("#sidebar-storage").textContent = `${storage.wavFiles || 0} files / ${formatBytes(storage.bytes)}`;
  $("#sidebar-meter").style.width = `${Math.min(100, 12 + Math.log10((storage.wavFiles || 0) + 1) * 24)}%`;
  renderAuditItems(data.recentAudit, "#overview-audit");
  renderIntegrity(data.integrity || {});
}

async function loadOverview() {
  try {
    const data = await run(() => bridge.apiGet("page/overview"), { silent: true });
    renderOverview(data);
    setConnection(true, "AstrBot 已连接");
  } catch (error) {
    setConnection(false, "连接失败");
    toast(errorMessage(error), "error");
  }
}

function renderArchiveCards(items) {
  const grid = $("#archive-grid");
  $("#archive-count").textContent = `${items.length} ARCHIVES`;
  $("#archive-empty").classList.toggle("is-hidden", items.length > 0);
  grid.innerHTML = items
    .map(
      (item) => `
        <button class="archive-card ${item.kind === "skin" ? "is-skin" : ""}"
          data-archive="${escapeHtml(item.character)}">
          <div class="archive-card-top">
            <div class="archive-avatar">${escapeHtml(initials(item.base))}</div>
            <div style="min-width:0">
              <h3>${escapeHtml(item.base)}</h3>
              ${
                item.kind === "skin"
                  ? `<p class="skin-name">${escapeHtml(item.skinName || "未命名皮肤")}</p>`
                  : '<p class="skin-name" style="color:var(--muted)">基础语音档案</p>'
              }
              <div class="archive-code">${item.kind === "skin" ? "OUTFIT" : "OPERATOR"} / ${
                item.resourceId ? escapeHtml(String(item.resourceId).slice(-12)) : "BASE"
              }</div>
            </div>
          </div>
          <div class="archive-card-bottom">
            <div class="language-tags">
              ${(item.languageNames || [])
                .map((name) => `<span class="language-tag">${escapeHtml(name)}</span>`)
                .join("")}
            </div>
            <span class="archive-counts">${escapeHtml(item.ownVoiceCount)} WAV</span>
          </div>
        </button>
      `,
    )
    .join("");
}

async function loadArchives() {
  const params = {
    q: $("#archive-search").value.trim(),
    kind: $("#archive-kind").value,
    language: $("#archive-language").value,
  };
  const data = await run(() => bridge.apiGet("page/archives", params));
  state.archives = data.items || [];
  renderArchiveCards(state.archives);
}

function voiceActionButtons(item) {
  const previewable = ["own", "fallback"].includes(item.status);
  return `
    <div class="voice-actions">
      <button class="button button-small button-secondary" data-voice-action="play"
        ${previewable ? "" : "disabled"}>试听</button>
      <button class="button button-small button-secondary" data-voice-action="download"
        ${previewable ? "" : "disabled"}>下载</button>
      <button class="button button-small button-secondary" data-voice-action="replace">替换</button>
      <button class="button button-small button-warning" data-voice-action="remove"
        ${item.deletable ? "" : "disabled"}>回收</button>
    </div>
  `;
}

function renderArchiveDetail(data) {
  state.archiveDetail = data;
  $("#drawer-kind").textContent = data.kind === "skin" ? "OUTFIT VOICE ARCHIVE" : "OPERATOR VOICE ARCHIVE";
  $("#drawer-title").textContent = data.base;
  $("#drawer-subtitle").textContent =
    data.kind === "skin"
      ? `${data.skinName || "未命名皮肤"} · ${data.ownVoiceCount} 个本包文件`
      : `基础档案 · ${data.ownVoiceCount} 个文件`;
  $("#drawer-language").innerHTML = LANGUAGES
    .map((info) => {
      const cached = (data.availableLanguages || []).includes(info.code);
      return `<option value="${info.code}" ${info.code === data.language ? "selected" : ""}>${
        escapeHtml(info.name)
      }${cached ? "" : "（未缓存）"}</option>`;
    })
    .join("");
  $("#drawer-export").disabled = !data.language;
  $("#drawer-import").disabled = !data.importToken;
  $("#drawer-selected-count").textContent = "0";
  $("#drawer-batch-remove").disabled = true;
  $("#drawer-select-all").textContent = "全选本包";
  $("#drawer-voice-list").innerHTML = (data.voices || [])
    .map(
      (item) => `
        <article class="voice-row" data-voice="${escapeHtml(item.voice)}"
          data-token="${escapeHtml(item.replaceToken)}">
          <label class="voice-select" title="${item.deletable ? "选择此语音" : "此条目不能从当前档案回收"}">
            <input type="checkbox" data-voice-select
              ${item.deletable ? "" : "disabled"} aria-label="选择${escapeHtml(item.voice)}" />
          </label>
          <i class="voice-status-dot ${escapeHtml(item.status)}"></i>
          <h4>${escapeHtml(item.voice)}</h4>
          <span class="voice-source">${escapeHtml(item.source)}</span>
          <span class="voice-size">${formatBytes(item.bytes)}</span>
          ${voiceActionButtons(item)}
        </article>
      `,
    )
    .join("");
}

function selectedArchiveVoices() {
  return $$("[data-voice-select]:checked", $("#drawer-voice-list")).map(
    (input) => input.closest(".voice-row").dataset.voice,
  );
}

function updateArchiveSelection() {
  const selectable = $$("[data-voice-select]:not(:disabled)", $("#drawer-voice-list"));
  const selected = selectedArchiveVoices();
  $("#drawer-selected-count").textContent = String(selected.length);
  $("#drawer-batch-remove").disabled = selected.length === 0;
  $("#drawer-select-all").textContent =
    selectable.length > 0 && selected.length === selectable.length
      ? "取消全选"
      : "全选本包";
}

async function openArchive(character, language = "") {
  const data = await run(() =>
    bridge.apiGet("page/archive", { character, language }),
  );
  renderArchiveDetail(data);
  $("#archive-drawer").classList.add("is-open");
  $("#archive-drawer").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeArchive() {
  $("#archive-drawer").classList.remove("is-open");
  $("#archive-drawer").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

function base64Blob(encoded, mime) {
  const binary = window.atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mime || "audio/wav" });
}

async function previewVoice(voice) {
  const detail = state.archiveDetail;
  const data = await run(() =>
    bridge.apiGet("page/audio", {
      character: detail.character,
      language: detail.language,
      voice,
    }),
  );
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
  state.audioUrl = URL.createObjectURL(base64Blob(data.base64, data.mime));
  const player = $("#audio-player");
  player.src = state.audioUrl;
  $("#audio-title").textContent = `${detail.character} / ${voice} / ${detail.language}`;
  $("#audio-dock").classList.remove("is-hidden");
  await player.play().catch(() => {});
}

async function downloadVoice(voice) {
  const detail = state.archiveDetail;
  await run(
    () =>
      bridge.download(
        "page/export",
        {
          character: detail.character,
          language: detail.language,
          voice,
        },
        `${detail.base}-${detail.language}-${voice}.wav`,
      ),
    { success: "已开始下载语音文件" },
  );
}

async function exportCurrentArchive() {
  const detail = state.archiveDetail;
  if (!detail?.language) return;
  await run(
    () =>
      bridge.download(
        "page/export",
        {
          character: detail.character,
          language: detail.language,
        },
        `${detail.base}-${detail.language}.zip`,
      ),
    { success: "已生成并下载语音包" },
  );
}

function previewMetrics(items) {
  return `
    <div class="preview-metrics">
      ${items
        .map(
          (item) => `
            <div class="${item.tone ? `is-${escapeHtml(item.tone)}` : ""}">
              <strong>${escapeHtml(item.value)}</strong>
              <span>${escapeHtml(item.label)}</span>
              ${item.note ? `<small>${escapeHtml(item.note)}</small>` : ""}
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function previewWarnings(items = []) {
  if (!items.length) return "";
  return `
    <div class="preview-warnings">
      ${items.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}
    </div>
  `;
}

function previewSample(items = []) {
  if (!items.length) return "";
  return `
    <details class="preview-sample">
      <summary>查看部分条目</summary>
      <div>${items.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
    </details>
  `;
}

async function discardOperationPreview(previewToken) {
  if (!previewToken) return;
  await bridge
    .apiPost("page/preview/discard", { previewToken })
    .catch(() => {});
}

function modalConfirm({
  eyebrow = "CONFIRM ACTION",
  title,
  message,
  danger = false,
  fields = "",
  confirmLabel = "确认",
}) {
  const modal = $("#modal");
  $("#modal-eyebrow").textContent = eyebrow;
  $("#modal-title").textContent = title;
  $("#modal-message").textContent = message;
  $("#modal-fields").innerHTML = fields;
  $("#modal-confirm").className = `button ${danger ? "button-warning" : "button-primary"}`;
  $("#modal-confirm").textContent = confirmLabel;
  modal.returnValue = "";
  modal.showModal();
  return new Promise((resolve) => {
    const onClose = () => {
      modal.removeEventListener("close", onClose);
      $("#modal-confirm").textContent = "确认";
      resolve(modal.returnValue === "confirm");
    };
    modal.addEventListener("close", onClose);
  });
}

async function removeVoice(voice) {
  const detail = state.archiveDetail;
  const confirmed = await modalConfirm({
    eyebrow: "MOVE TO RECYCLE BIN",
    title: `回收“${voice}”`,
    message:
      "文件会移动到插件回收站并立即从当前索引移除。你可以在“恢复与审计”中恢复它。",
    danger: true,
  });
  if (!confirmed) return;
  await run(
    () =>
      bridge.apiPost("page/remove", {
        character: detail.character,
        language: detail.language,
        voice,
      }),
    { success: "语音已移入回收站" },
  );
  await openArchive(detail.character, detail.language);
  await loadArchives();
}

async function batchRemoveVoices() {
  const detail = state.archiveDetail;
  const voices = selectedArchiveVoices();
  if (!detail || !voices.length) return;
  const preview = await run(() =>
    bridge.apiPost("page/remove/batch/preview", {
      character: detail.character,
      language: detail.language,
      voices,
    }),
  );
  const confirmed = await modalConfirm({
    eyebrow: "BATCH RECYCLE PREVIEW",
    title: preview.title || "批量回收预览",
    message: "以下结果来自当前文件状态。确认后才会执行，预览后发生变化会被安全拦截。",
    danger: true,
    confirmLabel: `回收 ${preview.affected} 个文件`,
    fields: [
      previewMetrics([
        { label: "已选择", value: preview.selected },
        { label: "将回收", value: preview.affected, tone: "danger" },
        { label: "状态变化", value: preview.unavailable || 0 },
        { label: "文件体积", value: formatBytes(preview.bytes) },
      ]),
      previewWarnings(preview.warnings),
      previewSample(preview.sample),
    ].join(""),
  });
  if (!confirmed) {
    await discardOperationPreview(preview.previewToken);
    return;
  }
  const result = await run(
    () =>
      bridge.apiPost("page/remove/batch", {
        previewToken: preview.previewToken,
      }),
    { success: `${preview.affected} 个语音已移入回收站` },
  );
  if (!result?.removed) return;
  await openArchive(detail.character, detail.language);
  await loadArchives();
  await loadOverview();
}

async function reloadArchiveDetail() {
  const detail = state.archiveDetail;
  if (!detail) return;
  await openArchive(detail.character, detail.language);
}

async function loadTasks({ silent = false } = {}) {
  try {
    const data = await run(() => bridge.apiGet("page/tasks"), { silent });
    renderTasks(data.items || []);
    return data.items || [];
  } catch (error) {
    if (!silent) throw error;
    return [];
  }
}

function renderTasks(items) {
  const root = $("#task-list");
  if (!items.length) {
    root.innerHTML =
      '<div class="empty-state compact"><div class="empty-glyph">↻</div><h3>任务队列为空</h3><p>创建 PRTS 下载或完整性检查任务。</p></div>';
    return;
  }
  root.innerHTML = items
    .map(
      (item) => `
        <article class="task-item">
          <span class="task-symbol">${item.kind === "fetch" ? "DL" : "CK"}</span>
          <div>
            <b>${escapeHtml(item.target)}</b>
            <p>${escapeHtml(item.message || "等待执行")} · ${escapeHtml(
              formatDate(item.createdAt),
            )}</p>
          </div>
          <div>
            <span class="task-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
            ${
              ["queued", "running"].includes(item.status)
                ? `<button class="text-button" data-cancel-task="${escapeHtml(item.id)}">取消</button>`
                : ""
            }
          </div>
        </article>
      `,
    )
    .join("");
}

async function submitFetch(event) {
  event.preventDefault();
  const character = $("#fetch-character").value.trim();
  const languages = $$('input[name="fetch-language"]:checked')
    .map((input) => input.value)
    .join("");
  if (!character) {
    toast("请输入角色名称", "error");
    return;
  }
  if (!languages) {
    toast("请至少选择一种语言", "error");
    return;
  }
  const requestPayload = {
    character,
    languages,
    includeSkin: $("#fetch-skin").checked,
  };
  const preview = await run(() =>
    bridge.apiPost("page/fetch/preview", requestPayload),
  );
  const confirmed = await modalConfirm({
    eyebrow: "PRTS TASK PREVIEW",
    title: preview.title || `获取 ${character} 的语音资源`,
    message: `语言：${(preview.languageNames || []).join("、")}。确认后才会创建后台任务。`,
    confirmLabel: "创建后台任务",
    fields: [
      previewMetrics([
        { label: "已存在/跳过", value: preview.existing },
        { label: "待补全", value: preview.missing },
        { label: "损坏重取", value: preview.damaged, tone: preview.damaged ? "danger" : "" },
        { label: "编号修复覆盖", value: preview.overwritten, tone: preview.overwritten ? "warning" : "" },
      ]),
      previewWarnings(preview.warnings),
    ].join(""),
  });
  if (!confirmed) {
    await discardOperationPreview(preview.previewToken);
    return;
  }
  await run(
    () =>
      bridge.apiPost("page/fetch", {
        previewToken: preview.previewToken,
      }),
    { success: "后台下载任务已创建" },
  );
  $("#fetch-character").value = "";
  await loadTasks();
}

function renderIntegrity(report) {
  const hasReport = Boolean(report?.checkedAt);
  const checked = Number(report?.checked || 0);
  const valid = Number(report?.valid || 0);
  const percent = checked ? Math.round((valid / checked) * 100) : 0;
  const gauge = $("#integrity-gauge");
  gauge.style.setProperty("--integrity-progress", `${percent}%`);
  gauge.querySelector("span").textContent = hasReport ? "LAST RESULT" : "NO DATA";
  gauge.querySelector("strong").textContent = hasReport ? `${percent}%` : "—";
  $("#integrity-time").textContent = hasReport
    ? `检查于 ${formatDate(report.checkedAt)}`
    : "尚未检查";
  $("#integrity-summary").innerHTML = [
    ["检查文件", checked],
    ["有效文件", valid],
    ["发现问题", Number(report?.issueCount || 0)],
    ["已隔离", Number(report?.isolated || 0)],
  ]
    .map(
      ([label, value]) => `<div><b>${escapeHtml(value)}</b><small>${escapeHtml(label)}</small></div>`,
    )
    .join("");
  $("#integrity-issues").innerHTML = (report?.issues || [])
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.path)}</td>
          <td>${escapeHtml(item.issue)}</td>
          <td>${item.isolated ? '<span class="availability">已隔离</span>' : '<span class="muted">未处理</span>'}</td>
        </tr>
      `,
    )
    .join("");
  if (!(report?.issues || []).length) {
    $("#integrity-issues").innerHTML =
      '<tr><td colspan="3" class="muted">没有异常记录。</td></tr>';
  }
}

async function loadIntegrity() {
  const report = await run(() => bridge.apiGet("page/integrity"));
  renderIntegrity(report || {});
}

async function startIntegrity(quarantine) {
  const title = quarantine ? "检查并隔离异常文件" : "开始只读检查";
  const message = quarantine
    ? "检测到的异常文件会移动到隔离目录，并自动重建语音索引。是否继续？"
    : "检查会在后台遍历本地 WAV，不会修改任何文件。";
  const confirmed = await modalConfirm({
    eyebrow: "INTEGRITY TASK",
    title,
    message,
    danger: quarantine,
  });
  if (!confirmed) return;
  await run(
    () => bridge.apiPost("page/integrity", { quarantine }),
    { success: "完整性检查任务已创建" },
  );
  toast("任务完成后报告会自动刷新");
}

function languageName(code) {
  return LANGUAGES.find((item) => item.code === code)?.name || code || "自动";
}

async function loadBindings() {
  const data = await run(() => bridge.apiGet("page/bindings"));
  const items = data.items || [];
  $("#binding-empty").classList.toggle("is-hidden", items.length > 0);
  $("#binding-list").innerHTML = items
    .map(
      (item) => `
        <tr>
          <td><strong>${escapeHtml(item.trigger)}</strong></td>
          <td>${escapeHtml(item.character)}</td>
          <td>${escapeHtml(item.voice)}</td>
          <td>${escapeHtml(item.languageName || languageName(item.language))}</td>
          <td><span class="availability ${item.available ? "" : "is-missing"}">${
            item.available ? "可播放" : "缺失"
          }</span></td>
          <td>
            <div class="table-actions">
              <button class="button button-small button-secondary" data-edit-binding="${escapeHtml(
                item.trigger,
              )}">编辑</button>
              <button class="button button-small button-warning" data-remove-binding="${escapeHtml(
                item.trigger,
              )}">删除</button>
            </div>
          </td>
        </tr>
      `,
    )
    .join("");
  $("#binding-list").dataset.items = JSON.stringify(items);
}

async function ensureArchives() {
  if (state.archives.length) return state.archives;
  const data = await bridge.apiGet("page/archives", {
    q: "",
    kind: "all",
    language: "all",
  });
  state.archives = data.items || [];
  return state.archives;
}

async function bindingModal(existing = null) {
  const archives = await ensureArchives();
  if (!archives.length) {
    toast("请先下载至少一个语音档案", "error");
    return;
  }
  const modal = $("#modal");
  $("#modal-eyebrow").textContent = existing ? "EDIT QUICK BINDING" : "NEW QUICK BINDING";
  $("#modal-title").textContent = existing ? "编辑快捷绑定" : "新建快捷绑定";
  $("#modal-message").textContent = "绑定保存前会校验档案、语音类型和语言是否可播放。";
  $("#modal-fields").innerHTML = `
    <label><span>触发词</span><input id="modal-trigger" maxlength="64" value="${escapeHtml(
      existing?.trigger || "",
    )}" ${existing ? "readonly" : ""} /></label>
    <label><span>目标档案</span><select id="modal-character">
      ${archives
        .map(
          (item) =>
            `<option value="${escapeHtml(item.character)}" ${
              item.character === existing?.character ? "selected" : ""
            }>${escapeHtml(item.kind === "skin" ? `${item.base} / ${item.skinName}` : item.base)}</option>`,
        )
        .join("")}
    </select></label>
    <label><span>语音类型</span><select id="modal-voice">
      ${VOICE_TYPES.map(
        (voice) =>
          `<option value="${escapeHtml(voice)}" ${voice === existing?.voice ? "selected" : ""}>${escapeHtml(
            voice,
          )}</option>`,
      ).join("")}
    </select></label>
    <label><span>语言</span><select id="modal-language">
      <option value="auto">自动选择</option>
      ${LANGUAGES.map(
        (item) =>
          `<option value="${item.code}" ${item.code === existing?.language ? "selected" : ""}>${escapeHtml(
            item.name,
          )}</option>`,
      ).join("")}
    </select></label>
  `;
  $("#modal-confirm").className = "button button-primary";
  $("#modal-confirm").textContent = "保存绑定";
  modal.returnValue = "";
  modal.showModal();
  const confirmed = await new Promise((resolve) => {
    const onClose = () => {
      modal.removeEventListener("close", onClose);
      resolve(modal.returnValue === "confirm");
    };
    modal.addEventListener("close", onClose);
  });
  $("#modal-confirm").textContent = "确认";
  if (!confirmed) return;
  await run(
    () =>
      bridge.apiPost("page/bindings/save", {
        trigger: $("#modal-trigger").value.trim(),
        character: $("#modal-character").value,
        voice: $("#modal-voice").value,
        language: $("#modal-language").value,
      }),
    { success: "快捷绑定已保存" },
  );
  await loadBindings();
}

async function removeBinding(trigger) {
  const confirmed = await modalConfirm({
    eyebrow: "REMOVE QUICK BINDING",
    title: `删除“${trigger}”`,
    message: "删除后，该触发词将不再响应。语音文件本身不会受到影响。",
    danger: true,
  });
  if (!confirmed) return;
  await run(
    () => bridge.apiPost("page/bindings/remove", { trigger }),
    { success: "快捷绑定已删除" },
  );
  await loadBindings();
}

function renderTrash(items) {
  $("#trash-count").textContent = items.length;
  $("#trash-empty").classList.toggle("is-hidden", items.length > 0);
  $("#trash-list").innerHTML = items
    .map(
      (item) => `
        <article class="trash-item">
          <span class="task-symbol">RB</span>
          <div>
            <b>${escapeHtml(item.character)} / ${escapeHtml(item.voice)}</b>
            <p>${escapeHtml(languageName(item.language))} · ${formatBytes(item.bytes)} · ${escapeHtml(
              formatDate(item.deletedAt),
            )}</p>
          </div>
          <div class="trash-actions">
            <button class="button button-small button-secondary" data-restore="${escapeHtml(item.id)}">恢复</button>
            <button class="button button-small button-warning" data-purge="${escapeHtml(item.id)}">永久删除</button>
          </div>
        </article>
      `,
    )
    .join("");
}

async function loadRecovery() {
  const [trash, audit] = await Promise.all([
    run(() => bridge.apiGet("page/trash")),
    run(() => bridge.apiGet("page/audit", { limit: 160 })),
  ]);
  renderTrash(trash.items || []);
  renderAuditItems(audit.items || [], "#audit-list");
}

async function restoreTrash(id) {
  await run(
    () => bridge.apiPost("page/restore", { id }),
    { success: "语音文件已恢复" },
  );
  await loadRecovery();
  await loadOverview();
}

async function purgeTrash(id) {
  const confirmed = await modalConfirm({
    eyebrow: "PERMANENT DELETE",
    title: "永久删除回收站文件",
    message: "此操作不可撤销。确认永久删除该 WAV 及其回收记录？",
    danger: true,
  });
  if (!confirmed) return;
  await run(
    () => bridge.apiPost("page/purge", { id }),
    { success: "回收站文件已永久删除" },
  );
  await loadRecovery();
}

async function rescan() {
  await run(
    () => bridge.apiPost("page/rescan", {}),
    { success: "语音索引已重建" },
  );
  state.archives = [];
  await loadOverview();
  if (state.view === "archives") await loadArchives();
}

function bindEvents() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  $$("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.jump));
  });
  $("#mobile-menu").addEventListener("click", () => {
    $("#sidebar").classList.toggle("is-open");
  });
  $("#overview-rescan").addEventListener("click", rescan);
  $("#archives-refresh").addEventListener("click", loadArchives);
  $("#archive-kind").addEventListener("change", loadArchives);
  $("#archive-language").addEventListener("change", loadArchives);
  $("#archive-search").addEventListener("input", () => {
    window.clearTimeout(state.archiveTimer);
    state.archiveTimer = window.setTimeout(loadArchives, 220);
  });
  $("#archive-grid").addEventListener("click", (event) => {
    const card = event.target.closest("[data-archive]");
    if (card) openArchive(card.dataset.archive);
  });
  $$("[data-close-drawer]").forEach((node) => {
    node.addEventListener("click", closeArchive);
  });
  $("#drawer-language").addEventListener("change", (event) => {
    openArchive(state.archiveDetail.character, event.target.value);
  });
  $("#drawer-export").addEventListener("click", exportCurrentArchive);
  $("#drawer-select-all").addEventListener("click", () => {
    const selectable = $$(
      "[data-voice-select]:not(:disabled)",
      $("#drawer-voice-list"),
    );
    const shouldSelect = !selectable.every((input) => input.checked);
    selectable.forEach((input) => {
      input.checked = shouldSelect;
    });
    updateArchiveSelection();
  });
  $("#drawer-batch-remove").addEventListener("click", batchRemoveVoices);
  $("#drawer-import").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    const detail = state.archiveDetail;
    event.target.value = "";
    if (!file || !detail?.importToken) return;
    const preview = await run(() =>
      bridge.upload(`page/import/preview/${detail.importToken}`, file),
    );
    const confirmed = await modalConfirm({
      eyebrow: "BATCH IMPORT PREVIEW",
      title: preview.title || `导入 ${detail.base} / ${languageName(detail.language)}`,
      message: "ZIP 已完成安全校验。确认后才会写入当前档案。",
      confirmLabel: `导入 ${preview.added + preview.overwritten} 个文件`,
      fields: [
        previewMetrics([
          { label: "新增", value: preview.added },
          { label: "覆盖", value: preview.overwritten, tone: preview.overwritten ? "warning" : "" },
          { label: "相同/跳过", value: preview.skipped },
          { label: "导入体积", value: formatBytes(preview.incomingBytes) },
        ]),
        preview.backupBytes
          ? `<p class="preview-note">覆盖前预计备份 ${escapeHtml(formatBytes(preview.backupBytes))}</p>`
          : "",
        previewWarnings(preview.warnings),
        previewSample(
          (preview.sample || []).map(
            (item) =>
              `${item.voice} · ${
                item.action === "add"
                  ? "新增"
                  : item.action === "overwrite"
                    ? "覆盖"
                    : "跳过"
              }`,
          ),
        ),
      ].join(""),
    });
    if (!confirmed) {
      await discardOperationPreview(preview.previewToken);
      return;
    }
    await run(
      () =>
        bridge.apiPost("page/import/commit", {
          previewToken: preview.previewToken,
        }),
      { success: "ZIP 语音包导入完成" },
    );
    await reloadArchiveDetail();
    await loadArchives();
  });
  $("#drawer-voice-list").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-voice-action]");
    const row = event.target.closest(".voice-row");
    if (!button || !row) return;
    const voice = row.dataset.voice;
    const action = button.dataset.voiceAction;
    if (action === "play") await previewVoice(voice);
    if (action === "download") await downloadVoice(voice);
    if (action === "remove") await removeVoice(voice);
    if (action === "replace") {
      state.pendingReplace = { voice, token: row.dataset.token };
      $("#replace-file").click();
    }
  });
  $("#drawer-voice-list").addEventListener("change", (event) => {
    if (event.target.matches("[data-voice-select]")) {
      updateArchiveSelection();
    }
  });
  $("#replace-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    const pending = state.pendingReplace;
    event.target.value = "";
    state.pendingReplace = null;
    if (!file || !pending) return;
    await run(
      () => bridge.upload(`page/replace/${pending.token}`, file),
      { success: `“${pending.voice}”已替换，旧文件已备份` },
    );
    await reloadArchiveDetail();
    await loadArchives();
  });
  $("#audio-close").addEventListener("click", () => {
    $("#audio-player").pause();
    $("#audio-dock").classList.add("is-hidden");
    if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
    state.audioUrl = null;
  });
  $("#fetch-form").addEventListener("submit", submitFetch);
  $("#tasks-refresh").addEventListener("click", () => loadTasks());
  $("#task-list").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel-task]");
    if (!button) return;
    await run(
      () => bridge.apiPost("page/task/cancel", { id: button.dataset.cancelTask }),
      { success: "已发送取消请求" },
    );
    await loadTasks();
  });
  $("#integrity-scan").addEventListener("click", () => startIntegrity(false));
  $("#integrity-quarantine").addEventListener("click", () => startIntegrity(true));
  $("#binding-new").addEventListener("click", () => bindingModal());
  $("#binding-list").addEventListener("click", async (event) => {
    const edit = event.target.closest("[data-edit-binding]");
    const remove = event.target.closest("[data-remove-binding]");
    if (edit) {
      const items = JSON.parse($("#binding-list").dataset.items || "[]");
      await bindingModal(items.find((item) => item.trigger === edit.dataset.editBinding));
    }
    if (remove) await removeBinding(remove.dataset.removeBinding);
  });
  $("#trash-list").addEventListener("click", async (event) => {
    const restore = event.target.closest("[data-restore]");
    const purge = event.target.closest("[data-purge]");
    if (restore) await restoreTrash(restore.dataset.restore);
    if (purge) await purgeTrash(purge.dataset.purge);
  });
  $("#audit-refresh").addEventListener("click", loadRecovery);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#archive-drawer").classList.contains("is-open")) {
      closeArchive();
    }
  });
}

async function pollBackgroundState() {
  const items = await loadTasks({ silent: true });
  const hasRunning = items.some((item) => ["queued", "running"].includes(item.status));
  const signature = items
    .map((item) => `${item.id}:${item.status}:${item.finishedAt || ""}`)
    .join("|");
  const changed = signature !== state.taskSignature;
  state.taskSignature = signature;
  if (state.view === "integrity" && changed) {
    await loadIntegrity().catch(() => {});
  }
  if (state.view === "overview" && changed && !hasRunning) {
    await loadOverview().catch(() => {});
  }
}

async function initialize() {
  if (!bridge) {
    setConnection(false, "Bridge 不可用");
    toast("此页面必须从 AstrBot 插件详情页打开。", "error");
    return;
  }
  state.context = await bridge.ready();
  document.title = bridge.t?.("pages.voice-manager.title", "语音档案控制台") || "语音档案控制台";
  bridge.onContext?.((context) => {
    state.context = context;
  });
  renderLanguages();
  bindEvents();
  setConnection(true, "AstrBot 已连接");
  await loadOverview();
  state.taskTimer = window.setInterval(pollBackgroundState, 4500);
}

window.addEventListener("beforeunload", () => {
  window.clearInterval(state.taskTimer);
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
});

initialize().catch((error) => {
  setConnection(false, "初始化失败");
  toast(errorMessage(error), "error");
});
