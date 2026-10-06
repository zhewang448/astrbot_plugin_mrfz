const bridge = window.AstrBotPluginPage;

// 语音按游戏内的使用场景分组，详情页按组排列；后端返回了不在表里的语音会归入“其他”。
const VOICE_GROUPS = [
  {
    name: "交谈",
    voices: [
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
    ],
  },
  {
    name: "养成与作战",
    voices: [
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
    ],
  },
  { name: "基建与互动", voices: ["进驻设施", "戳一下", "信赖触摸"] },
  { name: "标题与节日", voices: ["标题", "新年祝福", "问候", "生日", "周年庆典"] },
];

const VOICE_TYPES = VOICE_GROUPS.flatMap((group) => group.voices);

const LANGUAGES = [
  { code: "fy", name: "方言", rank: "1" },
  { code: "cn", name: "中文", rank: "2" },
  { code: "jp", name: "日语", rank: "3" },
  { code: "us", name: "英语", rank: "4" },
  { code: "kr", name: "韩语", rank: "5" },
  { code: "it", name: "意语", rank: "6" },
];

const STATUS_LABELS = {
  own: "本档案",
  fallback: "基础回退",
  missing: "缺失",
  damaged: "损坏",
};

const TASK_STATUS = {
  queued: "排队中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const AUDIT_LABELS = {
  rescan: "重建索引",
  replace_voice: "替换语音",
  import_archive: "导入 ZIP",
  trash_voice: "回收语音",
  trash_batch: "批量回收",
  restore_voice: "恢复语音",
  purge_voice: "永久删除",
  export_voice: "导出语音",
  export_archive: "导出语音包",
  save_binding: "保存快捷绑定",
  remove_binding: "删除快捷绑定",
  save_alias: "保存干员别称",
  remove_alias: "删除干员别称",
  task_completed: "后台任务完成",
  task_failed: "后台任务失败",
  task_cancelled: "后台任务取消",
};

// 视图数据超过这个时间才在切换页签时后台刷新；切换本身总是先用缓存立即渲染。
const STALE_MS = 15000;
const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 8000;

const state = {
  view: "archives",
  kind: "all",
  archives: [],
  groups: [],
  detail: null,
  current: null,
  selecting: false,
  selected: new Set(),
  bindings: [],
  aliases: [],
  operators: null,
  rosterSelected: new Set(),
  pendingReplace: null,
  audioUrl: null,
  pollTimer: null,
  hasActiveTasks: false,
  taskSignature: "",
  searchTimer: null,
  drawerReturnFocus: null,
  avatars: new Map(),
  avatarQueue: new Set(),
  avatarTimer: null,
  voiceTexts: new Map(),
  detailSequence: 0,
  audioSequence: 0,
};

// 每个视图的数据缓存：{ at: 加载时间, signature: 上次渲染内容的签名, pending: 进行中的请求 }
const cache = {};

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

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function formatDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(bridge?.getLocale?.() || "zh-CN", {
    ...(withTime ? {} : { year: "numeric" }),
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function languageName(code) {
  return LANGUAGES.find((item) => item.code === code)?.name || code || "自动";
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast is-${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.textContent = message;
  $("#toasts").append(node);
  window.setTimeout(() => node.remove(), type === "error" ? 6000 : 3600);
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

// 事件回调里的失败已经通过 toast 提示过，这里只吞掉 rejection，避免控制台报未处理错误。
function quietly(handler) {
  return (...args) => {
    Promise.resolve(handler(...args)).catch(() => {});
  };
}

/**
 * 带缓存的加载：同一资源的并发请求合并为一个；内容没变时跳过重新渲染，
 * 避免切换页签或轮询时整块 DOM 重建。
 */
async function cachedLoad(key, fetcher, render, { force = false, silent = false } = {}) {
  const entry = (cache[key] ||= { at: 0, signature: "", pending: null, data: null });
  if (!force && entry.data && Date.now() - entry.at < STALE_MS) return entry.data;
  if (entry.pending) return entry.pending;
  entry.pending = run(fetcher, { silent })
    .then((data) => {
      entry.at = Date.now();
      entry.data = data;
      const signature = JSON.stringify(data);
      if (signature !== entry.signature) {
        entry.signature = signature;
        render(data);
      }
      return data;
    })
    .finally(() => {
      entry.pending = null;
    });
  return entry.pending;
}

function invalidate(...keys) {
  keys.forEach((key) => {
    if (cache[key]) cache[key].at = 0;
  });
}

function setConnection(stateName, label) {
  const node = $("#conn");
  node.dataset.state = stateName;
  node.textContent = label;
}

function setBadge(id, count) {
  const badge = $(id);
  const value = Number(count || 0);
  badge.hidden = value <= 0;
  badge.textContent = value > 99 ? "99+" : String(value);
}

/* ---------- 视图切换 ---------- */

const VIEW_LOADERS = {
  archives: (options) => loadArchives(options),
  tasks: (options) => Promise.all([loadTasks(options), loadOperators(options)]),
  integrity: (options) => loadIntegrity(options),
  bindings: (options) => loadBindings(options),
  aliases: (options) => loadAliases(options),
  recovery: (options) => loadRecovery(options),
};

function switchView(view, { focus = false } = {}) {
  state.view = view;
  $$(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  $$(".view").forEach((panel) => {
    panel.hidden = panel.id !== `view-${view}`;
  });
  VIEW_LOADERS[view]?.().catch(() => {});
}

/* ---------- 顶部汇总 ---------- */

function renderSummary(data) {
  const storage = data.storage || {};
  const values = {
    operators: formatNumber(data.operators),
    skins: formatNumber(data.skins),
    wav: formatNumber(storage.wavFiles),
    bytes: formatBytes(storage.bytes),
    languages: (data.languages || []).map(languageName).join(" ") || "—",
  };
  Object.entries(values).forEach(([key, value]) => {
    $(`[data-stat="${key}"]`).textContent = value;
  });
  setBadge("#badge-trash", storage.trashItems);
  setBadge("#badge-integrity", data.integrity?.issueCount);
}

async function loadOverview(options = {}) {
  try {
    const data = await cachedLoad("overview", () => bridge.apiGet("page/overview"), renderSummary, {
      ...options,
      silent: true,
    });
    setConnection("ok", "已连接");
    return data;
  } catch (error) {
    setConnection("error", "连接失败");
    if (!options.silent) toast(errorMessage(error), "error");
    throw error;
  }
}

async function rescan() {
  const button = $("#rescan");
  button.disabled = true;
  try {
    await run(() => bridge.apiPost("page/rescan", {}), { success: "索引已重建" });
    await refreshAfterChange();
  } finally {
    button.disabled = false;
  }
}

// 文件有变动后，统一让依赖本地档案的数据失效并刷新当前可见的部分。
async function refreshAfterChange() {
  invalidate("overview", "archives", "operators", "trash", "audit");
  const jobs = [loadOverview({ silent: true })];
  if (state.view === "archives") jobs.push(loadArchives());
  if (state.view === "tasks") jobs.push(loadOperators());
  if (state.view === "recovery") jobs.push(loadRecovery());
  await Promise.allSettled(jobs);
}

/* ---------- 头像 ---------- */

function avatarHtml(name, className = "avatar") {
  const url = state.avatars.get(name);
  const initial = escapeHtml([...String(name || "?")][0] || "?");
  return `<span class="${className}" data-avatar="${escapeHtml(name)}" aria-hidden="true">${
    url ? `<img src="${url}" alt="" />` : initial
  }</span>`;
}

function paintAvatars(name) {
  const url = state.avatars.get(name);
  if (!url) return;
  $$(`[data-avatar="${CSS.escape(name)}"]`).forEach((node) => {
    if (!node.querySelector("img")) node.innerHTML = `<img src="${url}" alt="" />`;
  });
}

function requestAvatar(name) {
  if (!name || state.avatars.has(name)) return;
  state.avatarQueue.add(name);
  window.clearTimeout(state.avatarTimer);
  state.avatarTimer = window.setTimeout(flushAvatars, 60);
}

async function flushAvatars() {
  const names = [...state.avatarQueue].slice(0, 60);
  names.forEach((name) => {
    state.avatarQueue.delete(name);
    state.avatars.set(name, null);
  });
  if (state.avatarQueue.size) state.avatarTimer = window.setTimeout(flushAvatars, 60);
  if (!names.length) return;
  try {
    const data = await bridge.apiGet("page/avatars", { names: names.join(",") });
    Object.entries(data.avatars || {}).forEach(([name, url]) => {
      state.avatars.set(name, url);
      paintAvatars(name);
    });
  } catch {
    // 头像只是辅助信息，失败时保留首字占位，下次打开页面再试。
    names.forEach((name) => state.avatars.delete(name));
  }
}

const avatarObserver =
  "IntersectionObserver" in window
    ? new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            avatarObserver.unobserve(entry.target);
            requestAvatar(entry.target.dataset.avatar);
          });
        },
        { rootMargin: "200px" },
      )
    : null;

function observeAvatars(root) {
  $$("[data-avatar]", root).forEach((node) => {
    if (state.avatars.has(node.dataset.avatar)) return;
    if (avatarObserver) avatarObserver.observe(node);
    else requestAvatar(node.dataset.avatar);
  });
}

/* ---------- 档案目录 ---------- */

function renderLanguageFilter() {
  $("#archive-language").innerHTML =
    '<option value="all">全部语言</option>' +
    LANGUAGES.map((item) => `<option value="${item.code}">${escapeHtml(item.name)}</option>`).join("");
}

// 把基础档案和它的皮肤档案合成一组，同一个干员只出现一张卡片。
function groupArchives(items) {
  const groups = new Map();
  items.forEach((item) => {
    const group = groups.get(item.base) || { base: item.base, operator: null, skins: [] };
    if (item.kind === "skin") group.skins.push(item);
    else group.operator = item;
    groups.set(item.base, group);
  });
  return [...groups.values()].map((group) => {
    const members = [group.operator, ...group.skins].filter(Boolean);
    const languages = LANGUAGES.filter((info) =>
      members.some((item) => (item.languages || []).includes(info.code)),
    ).map((info) => info.code);
    return {
      ...group,
      members,
      languages,
      voiceCount: members.reduce((sum, item) => sum + Number(item.ownVoiceCount || 0), 0),
      searchText: [group.base, ...group.skins.map((item) => item.skinName || "")].join(" ").toLowerCase(),
    };
  });
}

function filteredGroups() {
  const query = $("#archive-search").value.trim().toLowerCase();
  const language = $("#archive-language").value;
  return state.groups.filter(
    (group) =>
      (!query || group.searchText.includes(query)) &&
      (state.kind !== "skin" || group.skins.length > 0) &&
      (language === "all" || group.languages.includes(language)),
  );
}

function renderCatalog() {
  const groups = filteredGroups();
  const total = state.groups.length;
  $("#archive-count").textContent =
    groups.length === total ? `${formatNumber(total)} 名干员` : `${formatNumber(groups.length)} / ${formatNumber(total)} 名干员`;
  $("#archive-empty").hidden = groups.length > 0;
  $("#archive-empty-text").textContent = total
    ? "没有匹配的档案。换个关键词或筛选条件试试。"
    : "还没有任何语音档案。去下载页挑几个干员吧。";
  $("#catalog").innerHTML = groups
    .map((group) => {
      const primary = group.operator || group.skins[0];
      return `
        <li class="entry">
          <button class="entry-main" type="button" data-archive="${escapeHtml(primary.character)}">
            ${avatarHtml(group.base)}
            <span class="entry-text">
              <span class="entry-name">${escapeHtml(group.base)}</span>
              <span class="entry-sub">${
                group.operator ? `${formatNumber(group.operator.ownVoiceCount)} 条基础语音` : "仅皮肤语音"
              }</span>
            </span>
          </button>
          <span class="entry-langs">${
            group.languages.map((code) => `<span>${escapeHtml(languageName(code))}</span>`).join("") ||
            '<span class="is-none">暂无语言</span>'
          }</span>
          ${
            group.skins.length
              ? `<span class="entry-skins">${group.skins
                  .map(
                    (skin) =>
                      `<button class="chip" type="button" data-archive="${escapeHtml(skin.character)}" title="${escapeHtml(
                        `${skin.skinName}，${skin.ownVoiceCount} 条`,
                      )}">${escapeHtml(skin.skinName || "未命名皮肤")}</button>`,
                  )
                  .join("")}</span>`
              : '<span class="entry-skins is-empty">没有皮肤语音</span>'
          }
        </li>
      `;
    })
    .join("");
  observeAvatars($("#catalog"));
}

async function loadArchives(options = {}) {
  return cachedLoad(
    "archives",
    () => bridge.apiGet("page/archives", { q: "", kind: "all", language: "all" }),
    (data) => {
      state.archives = data.items || [];
      state.groups = groupArchives(state.archives);
      renderCatalog();
    },
    options,
  );
}

/* ---------- 档案详情 ---------- */

function groupedVoices(voices) {
  const byName = new Map(voices.map((item) => [item.voice, item]));
  const groups = VOICE_GROUPS.map((group) => ({
    name: group.name,
    items: group.voices.filter((voice) => byName.has(voice)).map((voice) => byName.get(voice)),
  }));
  const known = new Set(VOICE_TYPES);
  const rest = voices.filter((item) => !known.has(item.voice));
  if (rest.length) groups.push({ name: "其他", items: rest });
  return groups.filter((group) => group.items.length);
}

function renderPackages(detail) {
  const group = state.groups.find((item) => item.base === detail.base);
  const members = group?.members || [];
  const root = $("#drawer-packages");
  root.hidden = members.length < 2;
  root.innerHTML = members
    .map(
      (item) => `
        <button type="button" data-package="${escapeHtml(item.character)}" aria-pressed="${item.character === detail.character}">
          ${escapeHtml(item.kind === "skin" ? item.skinName || "未命名皮肤" : "基础")}
          <small>${formatNumber(item.ownVoiceCount)}</small>
        </button>
      `,
    )
    .join("");
}

function renderDrawerLanguages(detail) {
  const cached = new Set(detail.availableLanguages || []);
  $("#drawer-languages").innerHTML = LANGUAGES.map(
    (item) => `
      <button type="button" data-lang="${item.code}" aria-pressed="${item.code === detail.language}"
        class="${cached.has(item.code) ? "" : "is-uncached"}"
        title="${cached.has(item.code) ? item.name : `${item.name}（本地没有缓存）`}">${escapeHtml(item.name)}</button>
    `,
  ).join("");
}

function renderTally(voices) {
  const counts = { own: 0, fallback: 0, missing: 0, damaged: 0 };
  voices.forEach((item) => {
    if (item.status in counts) counts[item.status] += 1;
  });
  $("#drawer-tally").innerHTML = Object.entries(counts)
    .map(
      ([status, count]) => `
        <span class="tally-item" data-status="${status}">
          <i aria-hidden="true"></i>${STATUS_LABELS[status]}<b>${count}</b>
        </span>
      `,
    )
    .join("");
}

function tileHtml(item) {
  const isCurrent = state.current === item.voice;
  const isSelected = state.selected.has(item.voice);
  const disabled = state.selecting && !item.deletable;
  const meta =
    item.status === "missing"
      ? "缺失"
      : item.status === "damaged"
        ? "损坏"
        : `${item.status === "fallback" ? "回退 " : ""}${formatBytes(item.bytes)}`;
  return `
    <button class="tile" type="button" data-voice="${escapeHtml(item.voice)}" data-status="${escapeHtml(item.status)}"
      aria-pressed="${state.selecting ? isSelected : isCurrent}" ${disabled ? "disabled" : ""}
      aria-label="${escapeHtml(item.voice)}，${STATUS_LABELS[item.status] || item.source}">
      <span class="tile-name">${escapeHtml(item.voice)}</span>
      <span class="tile-meta">${escapeHtml(meta)}</span>
    </button>
  `;
}

function renderMatrix() {
  const detail = state.detail;
  if (!detail) return;
  $("#matrix").classList.toggle("is-selecting", state.selecting);
  $("#matrix").innerHTML = groupedVoices(detail.voices || [])
    .map(
      (group) => `
        <section class="matrix-group">
          <h3>${escapeHtml(group.name)}</h3>
          <div class="tiles">${group.items.map(tileHtml).join("")}</div>
        </section>
      `,
    )
    .join("");
}

function voiceLine(voice) {
  const detail = state.detail;
  const lines = state.voiceTexts.get(detail?.character)?.[voice];
  if (!lines) return "";
  return lines[detail.language] || lines.cn || "";
}

function renderDock() {
  const detail = state.detail;
  const voice = detail?.voices?.find((item) => item.voice === state.current);
  $("#dock-batch").hidden = !state.selecting;
  $("#dock-voice").hidden = state.selecting || !voice;
  $("#dock-idle").hidden = state.selecting || Boolean(voice);
  if (state.selecting) {
    const count = state.selected.size;
    $("#batch-count").textContent = count ? `已选 ${count} 条` : "点选要回收的语音";
    $("#batch-remove").disabled = count === 0;
    const deletable = (detail?.voices || []).filter((item) => item.deletable);
    $("#batch-all").textContent =
      deletable.length && count === deletable.length ? "取消全选" : "全选可回收";
    $("#batch-all").disabled = deletable.length === 0;
    return;
  }
  if (!voice) return;
  const previewable = ["own", "fallback"].includes(voice.status);
  $("#dock-name").textContent = voice.voice;
  $("#dock-meta").textContent = [
    STATUS_LABELS[voice.status] || voice.source,
    languageName(detail.language),
    voice.bytes ? formatBytes(voice.bytes) : null,
    voice.updatedAt ? `更新于 ${formatDate(voice.updatedAt)}` : null,
  ]
    .filter(Boolean)
    .join("，");
  $("#dock-voice").dataset.status = voice.status;
  $("#audio-player").hidden = !previewable;
  $('#dock-voice [data-voice-action="download"]').disabled = !previewable;
  $('#dock-voice [data-voice-action="remove"]').disabled = !voice.deletable;
  const line = voiceLine(voice.voice);
  $("#dock-line").hidden = !line;
  $("#dock-line").textContent = line;
}

function renderDetail(data) {
  state.detail = data;
  if (!(data.voices || []).some((item) => item.voice === state.current)) {
    state.current = null;
  }
  state.selected = new Set(
    [...state.selected].filter((voice) =>
      (data.voices || []).some((item) => item.voice === voice && item.deletable),
    ),
  );
  $("#drawer-avatar").outerHTML = avatarHtml(data.base, "avatar avatar-lg").replace(
    "<span ",
    '<span id="drawer-avatar" ',
  );
  requestAvatar(data.base);
  $("#drawer-title").textContent = data.kind === "skin" ? `${data.base} / ${data.skinName || "未命名皮肤"}` : data.base;
  $("#drawer-subtitle").textContent = `${data.kind === "skin" ? "皮肤语音包" : "基础档案"}，${formatNumber(
    data.ownVoiceCount,
  )} 条语音，${formatBytes(data.bytes)}`;
  $("#drawer-export").disabled = !data.language;
  $("#drawer-import").disabled = !data.importToken;
  $("#drawer-select").setAttribute("aria-pressed", String(state.selecting));
  renderPackages(data);
  renderDrawerLanguages(data);
  renderTally(data.voices || []);
  renderMatrix();
  renderDock();
}

async function fetchDetail(character, language = "") {
  const sequence = ++state.detailSequence;
  const data = await run(() => bridge.apiGet("page/archive", { character, language }));
  if (sequence !== state.detailSequence) return null;
  renderDetail(data);
  loadVoiceTexts(data.character).catch(() => {});
  return data;
}

async function loadVoiceTexts(character) {
  if (state.voiceTexts.has(character)) return;
  state.voiceTexts.set(character, null);
  try {
    const data = await bridge.apiGet("page/voice-text", { character });
    state.voiceTexts.set(character, data.texts || {});
    if (state.detail?.character === character) renderDock();
  } catch {
    // 台词来自 PRTS，离线时不显示即可。
    state.voiceTexts.delete(character);
  }
}

function stopAudio() {
  state.audioSequence += 1;
  const player = $("#audio-player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
  state.audioUrl = null;
}

async function openArchive(character) {
  state.drawerReturnFocus = document.activeElement;
  state.current = null;
  state.selecting = false;
  state.selected.clear();
  stopAudio();
  if (!(await fetchDetail(character))) return;
  const drawer = $("#drawer");
  drawer.hidden = false;
  document.body.classList.add("has-drawer");
  window.requestAnimationFrame(() => drawer.classList.add("is-open"));
  $("#drawer-close").focus({ preventScroll: true });
}

async function switchPackage(character) {
  if (!state.detail || character === state.detail.character) return;
  stopAudio();
  state.current = null;
  state.selected.clear();
  await fetchDetail(character, state.detail.language);
}

function closeArchive() {
  const drawer = $("#drawer");
  state.detailSequence += 1;
  state.current = null;
  stopAudio();
  if (drawer.hidden) return;
  drawer.classList.remove("is-open");
  document.body.classList.remove("has-drawer");
  window.setTimeout(() => {
    if (!drawer.classList.contains("is-open")) drawer.hidden = true;
  }, 220);
  state.drawerReturnFocus?.focus?.({ preventScroll: true });
}

async function reloadDetail() {
  if (!state.detail) return;
  await fetchDetail(state.detail.character, state.detail.language);
}

function base64Blob(encoded, mime) {
  const binary = window.atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mime || "audio/wav" });
}

async function playVoice(voice) {
  const detail = state.detail;
  if (!detail) return;
  const audioSequence = ++state.audioSequence;
  const sequence = state.detailSequence;
  const data = await run(() =>
    bridge.apiGet("page/audio", { character: detail.character, language: detail.language, voice }),
  );
  // 请求返回前用户可能已经点了别的语音或关掉了详情。
  if (sequence !== state.detailSequence || audioSequence !== state.audioSequence || state.detail !== detail || state.current !== voice) return;
  stopAudio();
  state.audioUrl = URL.createObjectURL(base64Blob(data.base64, data.mime));
  const player = $("#audio-player");
  player.src = state.audioUrl;
  await player.play().catch(() => {});
}

function selectTile(voice) {
  const item = state.detail?.voices?.find((entry) => entry.voice === voice);
  if (!item) return;
  if (state.selecting) {
    if (!item.deletable) return;
    if (state.selected.has(voice)) state.selected.delete(voice);
    else state.selected.add(voice);
  } else {
    if (state.current !== voice) stopAudio();
    state.current = voice;
  }
  $$(".tile", $("#matrix")).forEach((tile) => {
    const name = tile.dataset.voice;
    tile.setAttribute(
      "aria-pressed",
      String(state.selecting ? state.selected.has(name) : state.current === name),
    );
  });
  renderDock();
  if (!state.selecting && ["own", "fallback"].includes(item.status)) {
    playVoice(voice).catch(() => {});
  }
}

function setSelecting(next) {
  state.selecting = next;
  state.selected.clear();
  if (next) {
    stopAudio();
    state.current = null;
  }
  $("#drawer-select").setAttribute("aria-pressed", String(next));
  renderMatrix();
  renderDock();
}

async function downloadVoice(voice) {
  const detail = state.detail;
  await run(
    () =>
      bridge.download(
        "page/export",
        { character: detail.character, language: detail.language, voice },
        `${detail.base}-${detail.language}-${voice}.wav`,
      ),
    { success: "已开始下载" },
  );
}

async function exportArchive() {
  const detail = state.detail;
  if (!detail?.language) return;
  await run(
    () =>
      bridge.download(
        "page/export",
        { character: detail.character, language: detail.language },
        `${detail.base}-${detail.language}.zip`,
      ),
    { success: "已开始下载 ZIP" },
  );
}

async function removeVoice(voice) {
  const detail = state.detail;
  const confirmed = await modalConfirm({
    title: `回收“${voice}”`,
    message: "文件会移到回收站，可以在“回收站与记录”里恢复。",
    danger: true,
    confirmLabel: "回收",
  });
  if (!confirmed) return;
  await run(
    () => bridge.apiPost("page/remove", { character: detail.character, language: detail.language, voice }),
    { success: "已移到回收站" },
  );
  stopAudio();
  await reloadDetail();
  refreshAfterChange().catch(() => {});
}

async function batchRemove() {
  const detail = state.detail;
  const voices = [...state.selected];
  if (!detail || !voices.length) return;
  const preview = await run(() =>
    bridge.apiPost("page/remove/batch/preview", {
      character: detail.character,
      language: detail.language,
      voices,
    }),
  );
  const confirmed = await modalConfirm({
    title: preview.title || "批量回收",
    message: "按当前文件状态生成的预览。如果文件在确认前发生变化，操作会被拦下。",
    danger: true,
    confirmLabel: `回收 ${preview.affected} 条`,
    fields: [
      previewFacts([
        ["已选", preview.selected],
        ["将回收", preview.affected, "danger"],
        ["状态已变化", preview.unavailable || 0],
        ["体积", formatBytes(preview.bytes)],
      ]),
      previewWarnings(preview.warnings),
      previewSample(preview.sample),
    ].join(""),
  });
  if (!confirmed) {
    await discardPreview(preview.previewToken);
    return;
  }
  const result = await run(() => bridge.apiPost("page/remove/batch", { previewToken: preview.previewToken }), {
    success: `已回收 ${preview.affected} 条语音`,
  });
  if (!result?.removed) return;
  setSelecting(false);
  await reloadDetail();
  refreshAfterChange().catch(() => {});
}

async function importZip(file) {
  const detail = state.detail;
  if (!file || !detail?.importToken) return;
  const preview = await run(() => bridge.upload(`page/import/preview/${detail.importToken}`, file));
  const actionLabel = { add: "新增", overwrite: "覆盖", skip: "跳过" };
  const confirmed = await modalConfirm({
    title: preview.title || `导入到 ${detail.base}（${languageName(detail.language)}）`,
    message: "ZIP 已通过安全校验，确认后才会写入。",
    confirmLabel: `导入 ${preview.added + preview.overwritten} 个文件`,
    fields: [
      previewFacts([
        ["新增", preview.added],
        ["覆盖", preview.overwritten, preview.overwritten ? "warn" : ""],
        ["相同，跳过", preview.skipped],
        ["体积", formatBytes(preview.incomingBytes)],
      ]),
      preview.backupBytes
        ? `<p class="preview-note">覆盖前会备份 ${escapeHtml(formatBytes(preview.backupBytes))} 的旧文件。</p>`
        : "",
      previewWarnings(preview.warnings),
      previewSample(
        (preview.sample || []).map((item) => `${item.voice}：${actionLabel[item.action] || item.action}`),
      ),
    ].join(""),
  });
  if (!confirmed) {
    await discardPreview(preview.previewToken);
    return;
  }
  await run(() => bridge.apiPost("page/import/commit", { previewToken: preview.previewToken }), {
    success: "ZIP 已导入",
  });
  await reloadDetail();
  refreshAfterChange().catch(() => {});
}

async function replaceVoice(file) {
  const pending = state.pendingReplace;
  state.pendingReplace = null;
  if (!file || !pending) return;
  await run(() => bridge.upload(`page/replace/${pending.token}`, file), {
    success: `“${pending.voice}”已替换，旧文件已备份`,
  });
  stopAudio();
  await reloadDetail();
  refreshAfterChange().catch(() => {});
}

/* ---------- 弹窗 ---------- */

function previewFacts(items) {
  return `
    <dl class="facts">
      ${items
        .map(
          ([label, value, tone]) => `
            <div class="${tone ? `is-${tone}` : ""}"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>
          `,
        )
        .join("")}
    </dl>
  `;
}

function previewWarnings(items = []) {
  if (!items.length) return "";
  return `<ul class="warnings">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function previewSample(items = []) {
  if (!items.length) return "";
  return `
    <details class="sample">
      <summary>查看部分条目</summary>
      <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </details>
  `;
}

async function discardPreview(previewToken) {
  if (!previewToken) return;
  await bridge.apiPost("page/preview/discard", { previewToken }).catch(() => {});
}

function modalConfirm({ title, message = "", danger = false, fields = "", confirmLabel = "确认" }) {
  const modal = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-message").textContent = message;
  $("#modal-message").hidden = !message;
  $("#modal-fields").innerHTML = fields;
  const confirm = $("#modal-confirm");
  confirm.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  confirm.textContent = confirmLabel;
  modal.returnValue = "";
  modal.showModal();
  const firstField = $("input:not([readonly]), select", $("#modal-fields"));
  (firstField || (danger ? $("#modal-cancel") : confirm)).focus();
  return new Promise((resolve) => {
    modal.addEventListener("close", () => resolve(modal.returnValue === "confirm"), { once: true });
  });
}

/* ---------- 下载任务 ---------- */

function renderFetchLanguages() {
  $("#fetch-languages").innerHTML = LANGUAGES.map(
    (item) => `
      <label class="check">
        <input type="checkbox" name="fetch-language" value="${item.rank}" ${
          ["1", "2", "3"].includes(item.rank) ? "checked" : ""
        } />
        <span>${escapeHtml(item.name)}</span>
      </label>
    `,
  ).join("");
}

function fetchOptions() {
  return {
    languages: $$('input[name="fetch-language"]:checked')
      .map((input) => input.value)
      .join(""),
    includeSkin: $("#fetch-skin").checked,
  };
}

function taskProgress(item) {
  const progress = item.progress;
  if (item.status !== "running") return "";
  if (progress?.total) {
    const percent = Math.min(100, (progress.done / progress.total) * 100);
    return `
      <span class="progress is-determinate" role="progressbar" aria-valuemin="0" aria-valuemax="${progress.total}"
        aria-valuenow="${progress.done}" aria-label="下载进度">
        <i style="width:${percent.toFixed(1)}%"></i>
      </span>
      <span class="progress-text">${formatNumber(progress.done)} / ${formatNumber(progress.total)}</span>
    `;
  }
  return '<span class="progress" aria-hidden="true"><i></i></span>';
}

function renderTasks(items) {
  const root = $("#task-list");
  setBadge("#badge-tasks", items.filter((item) => ["queued", "running"].includes(item.status)).length);
  if (!items.length) {
    root.innerHTML = '<li class="rows-empty">还没有任务。输入干员名称，或从“还没下载的干员”里勾选。</li>';
    return;
  }
  root.innerHTML = items
    .map((item) => {
      const active = ["queued", "running"].includes(item.status);
      return `
        <li class="row task" data-status="${escapeHtml(item.status)}">
          <div class="row-main">
            <p class="row-title">
              <span class="kind">${item.kind === "fetch" ? "下载" : "检查"}</span>${escapeHtml(item.target)}
            </p>
            <p class="row-sub">${escapeHtml(item.message || "等待执行")}</p>
            ${taskProgress(item)}
          </div>
          <div class="row-side">
            <span class="pill" data-status="${escapeHtml(item.status)}">${escapeHtml(TASK_STATUS[item.status] || item.status)}</span>
            <time>${escapeHtml(formatDate(item.finishedAt || item.createdAt))}</time>
            ${active ? `<button class="btn btn-quiet btn-small" type="button" data-cancel-task="${escapeHtml(item.id)}">取消</button>` : ""}
          </div>
        </li>
      `;
    })
    .join("");
}

async function loadTasks(options = {}) {
  const data = await cachedLoad("tasks", () => bridge.apiGet("page/tasks"), (value) => renderTasks(value.items || []), {
    ...options,
    force: true,
  });
  return data.items || [];
}

async function submitFetch(event) {
  event.preventDefault();
  const character = $("#fetch-character").value.trim();
  const { languages, includeSkin } = fetchOptions();
  if (!character) {
    toast("请输入干员名称", "error");
    $("#fetch-character").focus();
    return;
  }
  if (!languages) {
    toast("至少选择一种语言", "error");
    return;
  }
  const preview = await run(() => bridge.apiPost("page/fetch/preview", { character, languages, includeSkin }));
  const confirmed = await modalConfirm({
    title: preview.title || `下载 ${character} 的语音`,
    message: `语言：${(preview.languageNames || []).join("、")}。`,
    confirmLabel: "开始下载",
    fields: [
      previewFacts([
        ["已有，跳过", preview.existing],
        ["待下载", preview.missing],
        ["已损坏", preview.damaged, preview.damaged ? "danger" : ""],
        ["编号修正覆盖", preview.overwritten, preview.overwritten ? "warn" : ""],
      ]),
      previewWarnings(preview.warnings),
    ].join(""),
  });
  if (!confirmed) {
    await discardPreview(preview.previewToken);
    return;
  }
  await run(() => bridge.apiPost("page/fetch", { previewToken: preview.previewToken }), {
    success: "下载任务已开始",
  });
  $("#fetch-character").value = "";
  renderFetchHint();
  await loadTasks();
  schedulePoll(POLL_ACTIVE_MS);
}

async function cancelTask(id) {
  await run(() => bridge.apiPost("page/task/cancel", { id }), { success: "已请求取消" });
  await loadTasks();
}

/* ---------- PRTS 干员列表 ---------- */

function operatorNames() {
  return (state.operators?.items || []).map((item) => item.name);
}

// 简单的编辑距离，用来给拼错的名字找候选；干员名都很短，直接 O(n·m) 即可。
function editDistance(a, b) {
  const left = [...a];
  const right = [...b];
  let previous = Array.from({ length: right.length + 1 }, (_, index) => index);
  left.forEach((char, i) => {
    const current = [i + 1];
    right.forEach((other, j) => {
      current.push(Math.min(previous[j + 1] + 1, current[j] + 1, previous[j] + (char === other ? 0 : 1)));
    });
    previous = current;
  });
  return previous[right.length];
}

function suggestNames(name) {
  const names = operatorNames();
  const lower = name.toLowerCase();
  const contains = names.filter((item) => item.toLowerCase().includes(lower) && item !== name);
  const close = names
    .map((item) => ({ item, distance: editDistance(lower, item.toLowerCase()) }))
    .filter(({ item, distance }) => distance <= Math.max(1, Math.floor([...name].length / 2)) && !contains.includes(item))
    .sort((a, b) => a.distance - b.distance)
    .map(({ item }) => item);
  return [...contains, ...close].slice(0, 5);
}

function renderFetchHint() {
  const root = $("#fetch-hint");
  const name = $("#fetch-character").value.trim();
  const names = operatorNames();
  if (!name || !names.length) {
    root.innerHTML = "";
    return;
  }
  const operator = state.operators.items.find((item) => item.name === name);
  if (operator) {
    root.innerHTML = operator.local
      ? '<span class="is-ok">已下载过，会跳过已有文件，只补缺失的部分。</span>'
      : '<span class="is-ok">PRTS 上有这个干员的语音。</span>';
    return;
  }
  const suggestions = suggestNames(name);
  root.innerHTML = `
    <span>PRTS 干员列表里没有“${escapeHtml(name)}”。如果是别称，下载时会按 PRTS 的重定向找到对应干员。</span>
    ${
      suggestions.length
        ? `<span class="suggest">是不是：${suggestions
            .map((item) => `<button class="chip" type="button" data-suggest="${escapeHtml(item)}">${escapeHtml(item)}</button>`)
            .join("")}</span>`
        : ""
    }
  `;
}

function renderRoster() {
  const data = state.operators;
  const items = data?.items || [];
  const missing = items.filter((item) => !item.local);
  const query = $("#roster-search").value.trim().toLowerCase();
  const visible = missing.filter((item) => !query || item.name.toLowerCase().includes(query));
  state.rosterSelected = new Set([...state.rosterSelected].filter((name) => missing.some((item) => item.name === name)));

  $("#roster-meta").textContent = data?.error && !items.length
    ? `读取失败：${data.error}`
    : `PRTS 收录 ${formatNumber(items.length)} 名干员，本地已有 ${formatNumber(items.length - missing.length)} 名${
        data?.fetchedAt ? `，列表更新于 ${formatDate(data.fetchedAt)}` : ""
      }${data?.stale ? "（刷新失败，显示的是旧列表）" : ""}。按收录时间从新到旧排列。`;

  $("#roster").innerHTML = visible.length
    ? visible
        .map(
          (item) => `
            <li>
              <label class="roster-item">
                <input type="checkbox" data-roster="${escapeHtml(item.name)}" ${state.rosterSelected.has(item.name) ? "checked" : ""} />
                <span class="roster-name">${escapeHtml(item.name)}</span>
                <time>${escapeHtml(item.addedAt ? formatDate(item.addedAt, false) : "")}</time>
              </label>
            </li>
          `,
        )
        .join("")
    : `<li class="rows-empty">${items.length ? (query ? "没有匹配的干员。" : "PRTS 上的干员都已经下载过了。") : "暂无数据。"}</li>`;
  updateRosterFoot();
}

function updateRosterFoot() {
  const count = state.rosterSelected.size;
  $("#roster-count").textContent = count ? `已选 ${count} 名` : "勾选后可以一次下载多名干员";
  $("#roster-download").disabled = count === 0;
  $("#roster-download").textContent = count ? `下载所选 ${count} 名` : "下载所选";
}

async function loadOperators(options = {}) {
  return cachedLoad(
    "operators",
    () => bridge.apiGet("page/operators", options.refresh ? { refresh: "1" } : {}),
    (data) => {
      state.operators = data;
      $("#operator-names").innerHTML = (data.items || [])
        .map((item) => `<option value="${escapeHtml(item.name)}"></option>`)
        .join("");
      renderRoster();
      renderFetchHint();
    },
    { ...options, force: options.force || options.refresh },
  );
}

async function downloadRoster() {
  const names = [...state.rosterSelected];
  const { languages, includeSkin } = fetchOptions();
  if (!names.length) return;
  if (!languages) {
    toast("至少在上方选择一种语言", "error");
    return;
  }
  const languageNames = LANGUAGES.filter((item) => languages.includes(item.rank)).map((item) => item.name);
  const confirmed = await modalConfirm({
    title: `下载 ${names.length} 名干员的语音`,
    message: `语言：${languageNames.join("、")}${includeSkin ? "，包含皮肤语音" : ""}。每名干员是一个后台任务，同时最多下载两名。`,
    confirmLabel: `创建 ${names.length} 个任务`,
    fields: previewSample(names),
  });
  if (!confirmed) return;
  let created = 0;
  for (const character of names) {
    try {
      await bridge.apiPost("page/fetch", { character, languages, includeSkin });
      created += 1;
      state.rosterSelected.delete(character);
    } catch (error) {
      toast(`${character}：${errorMessage(error)}`, "error");
    }
  }
  if (created) toast(`已创建 ${created} 个下载任务`, "success");
  renderRoster();
  await loadTasks();
  schedulePoll(POLL_ACTIVE_MS);
}

/* ---------- 完整性 ---------- */

function renderIntegrity(report) {
  const hasReport = Boolean(report?.checkedAt);
  const checked = Number(report?.checked || 0);
  const valid = Number(report?.valid || 0);
  const issues = Number(report?.issueCount || 0);
  const isolated = Number(report?.isolated || 0);
  setBadge("#badge-integrity", issues);

  if (!hasReport) {
    $("#integrity-health").innerHTML = '<p class="health-text">还没有检查过。先做一次只读检查看看情况。</p>';
  } else {
    const validPct = checked ? (valid / checked) * 100 : 0;
    $("#integrity-health").innerHTML = `
      <p class="health-text">
        ${escapeHtml(formatDate(report.checkedAt))} 检查了 <b>${formatNumber(checked)}</b> 个文件，
        <b>${formatNumber(valid)}</b> 个正常，<b class="${issues ? "is-danger" : ""}">${formatNumber(issues)}</b> 个有问题${
          isolated ? `，已隔离 <b>${formatNumber(isolated)}</b> 个` : ""
        }。
      </p>
      <div class="health-bar" role="img" aria-label="正常文件占 ${validPct.toFixed(1)}%">
        <i style="width:${validPct.toFixed(2)}%"></i>
      </div>
    `;
  }

  const rows = report?.issues || [];
  $("#integrity-issues").innerHTML = rows.length
    ? rows
        .map(
          (item) => `
            <tr>
              <td class="path">${escapeHtml(item.path)}</td>
              <td>${escapeHtml(item.issue)}</td>
              <td>${item.isolated ? '<span class="pill" data-status="completed">已隔离</span>' : '<span class="muted">未处理</span>'}</td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td colspan="3" class="muted">${hasReport ? "没有发现问题。" : "暂无报告。"}</td></tr>`;
}

async function loadIntegrity(options = {}) {
  return cachedLoad("integrity", () => bridge.apiGet("page/integrity"), (report) => renderIntegrity(report || {}), options);
}

async function startIntegrity(quarantine) {
  const confirmed = await modalConfirm({
    title: quarantine ? "检查并隔离异常文件" : "只读检查",
    message: quarantine
      ? "有问题的文件会移到隔离目录，然后重建语音索引。"
      : "在后台遍历本地 WAV，不会改动任何文件。",
    danger: quarantine,
    confirmLabel: quarantine ? "检查并隔离" : "开始检查",
  });
  if (!confirmed) return;
  await run(() => bridge.apiPost("page/integrity", { quarantine }), {
    success: "检查已开始，完成后报告会自动刷新",
  });
  await loadTasks();
  schedulePoll(POLL_ACTIVE_MS);
}

/* ---------- 快捷绑定 ---------- */

function renderBindings(data) {
  const items = data.items || [];
  state.bindings = items;
  $("#binding-empty").hidden = items.length > 0;
  $("#binding-list").closest(".table-wrap").hidden = items.length === 0;
  $("#binding-list").innerHTML = items
    .map(
      (item) => `
        <tr>
          <td><b>${escapeHtml(item.trigger)}</b></td>
          <td>${escapeHtml(item.character)}</td>
          <td>${escapeHtml(item.voice)}</td>
          <td>${escapeHtml(item.languageName || languageName(item.language))}</td>
          <td><span class="pill" data-status="${item.available ? "completed" : "failed"}">${item.available ? "可播放" : "缺失"}</span></td>
          <td class="actions">
            <button class="btn btn-quiet btn-small" type="button" data-edit-binding="${escapeHtml(item.trigger)}">编辑</button>
            <button class="btn btn-quiet btn-small is-danger" type="button" data-remove-binding="${escapeHtml(item.trigger)}">删除</button>
          </td>
        </tr>
      `,
    )
    .join("");
}

async function loadBindings(options = {}) {
  return cachedLoad("bindings", () => bridge.apiGet("page/bindings"), renderBindings, options);
}

async function bindingModal(existing = null) {
  await loadArchives();
  const archives = state.archives;
  if (!archives.length) {
    toast("还没有语音档案，先去下载一个干员", "error");
    return;
  }
  const confirmed = await modalConfirm({
    title: existing ? `编辑“${existing.trigger}”` : "新建快捷绑定",
    message: "保存前会检查档案、语音和语言是否能播放。",
    confirmLabel: "保存",
    fields: `
      <label class="stack"><span>触发词</span>
        <input class="field" id="modal-trigger" maxlength="64" required value="${escapeHtml(existing?.trigger || "")}" ${existing ? "readonly" : ""} />
      </label>
      <label class="stack"><span>档案</span>
        <select class="field" id="modal-character">
          ${archives
            .map(
              (item) =>
                `<option value="${escapeHtml(item.character)}" ${item.character === existing?.character ? "selected" : ""}>${escapeHtml(
                  item.kind === "skin" ? `${item.base} / ${item.skinName}` : item.base,
                )}</option>`,
            )
            .join("")}
        </select>
      </label>
      <div class="field-pair">
        <label class="stack"><span>语音</span>
          <select class="field" id="modal-voice">
            ${VOICE_GROUPS.map(
              (group) => `
                <optgroup label="${escapeHtml(group.name)}">
                  ${group.voices
                    .map(
                      (voice) =>
                        `<option value="${escapeHtml(voice)}" ${voice === existing?.voice ? "selected" : ""}>${escapeHtml(voice)}</option>`,
                    )
                    .join("")}
                </optgroup>
              `,
            ).join("")}
          </select>
        </label>
        <label class="stack"><span>语言</span>
          <select class="field" id="modal-language">
            <option value="auto">自动</option>
            ${LANGUAGES.map(
              (item) =>
                `<option value="${item.code}" ${item.code === existing?.language ? "selected" : ""}>${escapeHtml(item.name)}</option>`,
            ).join("")}
          </select>
        </label>
      </div>
    `,
  });
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
  await loadBindings({ force: true });
}

async function removeBinding(trigger) {
  const confirmed = await modalConfirm({
    title: `删除“${trigger}”`,
    message: "删除后发送这个触发词不会再有回应，语音文件不受影响。",
    danger: true,
    confirmLabel: "删除",
  });
  if (!confirmed) return;
  await run(() => bridge.apiPost("page/bindings/remove", { trigger }), { success: "快捷绑定已删除" });
  await loadBindings({ force: true });
}

/* ---------- 干员别称 ---------- */

function renderAliases(data) {
  const items = data.items || [];
  state.aliases = items;
  $("#alias-empty").hidden = items.length > 0;
  $("#alias-list").closest(".table-wrap").hidden = items.length === 0;
  $("#alias-list").innerHTML = items
    .map(
      (item) => `
        <tr>
          <td><b>${escapeHtml(item.alias)}</b></td>
          <td>${escapeHtml(item.character)}</td>
          <td><span class="source ${item.builtin ? "" : "is-custom"}">${item.builtin ? "内置" : "自定义"}</span></td>
          <td class="actions">
            <button class="btn btn-quiet btn-small" type="button" data-edit-alias="${escapeHtml(item.alias)}">编辑</button>
            <button class="btn btn-quiet btn-small is-danger" type="button" data-remove-alias="${escapeHtml(item.alias)}">${
              item.builtin ? "恢复默认" : "删除"
            }</button>
          </td>
        </tr>
      `,
    )
    .join("");
}

async function loadAliases(options = {}) {
  return cachedLoad("aliases", () => bridge.apiGet("page/aliases"), renderAliases, options);
}

async function aliasModal(existing = null) {
  const confirmed = await modalConfirm({
    title: existing ? `编辑“${existing.alias}”` : "新增别称",
    message: "保存后立即用于播放、下载和快捷绑定。",
    confirmLabel: "保存",
    fields: `
      <div class="field-pair">
        <label class="stack"><span>别称</span>
          <input class="field" id="modal-alias" maxlength="80" required value="${escapeHtml(existing?.alias || "")}" ${existing ? "readonly" : ""} />
        </label>
        <label class="stack"><span>干员名</span>
          <input class="field" id="modal-alias-character" maxlength="80" required list="operator-names" value="${escapeHtml(existing?.character || "")}" />
        </label>
      </div>
    `,
  });
  if (!confirmed) return;
  await run(
    () =>
      bridge.apiPost("page/aliases/save", {
        alias: $("#modal-alias").value.trim(),
        character: $("#modal-alias-character").value.trim(),
      }),
    { success: "别称已保存" },
  );
  await loadAliases({ force: true });
}

async function removeAlias(item) {
  if (!item) return;
  const confirmed = await modalConfirm({
    title: item.builtin ? `恢复“${item.alias}”的默认映射` : `删除“${item.alias}”`,
    message: item.builtin ? `恢复为 ${item.alias} → ${item.character}。` : "删除后这个别称不再被识别。",
    danger: true,
    confirmLabel: item.builtin ? "恢复默认" : "删除",
  });
  if (!confirmed) return;
  await run(() => bridge.apiPost("page/aliases/remove", { alias: item.alias }), {
    success: item.builtin ? "已恢复默认别称" : "别称已删除",
  });
  await loadAliases({ force: true });
}

/* ---------- 回收站与记录 ---------- */

function renderTrash(data) {
  const items = data.items || [];
  setBadge("#badge-trash", items.length);
  $("#trash-list").innerHTML = items.length
    ? items
        .map(
          (item) => `
            <li class="row">
              <div class="row-main">
                <p class="row-title">${escapeHtml(item.character)} / ${escapeHtml(item.voice)}</p>
                <p class="row-sub">${escapeHtml(languageName(item.language))}，${formatBytes(item.bytes)}，${escapeHtml(
                  formatDate(item.deletedAt),
                )} 回收</p>
              </div>
              <div class="row-side">
                <button class="btn btn-small" type="button" data-restore="${escapeHtml(item.id)}">恢复</button>
                <button class="btn btn-quiet btn-small is-danger" type="button" data-purge="${escapeHtml(item.id)}">永久删除</button>
              </div>
            </li>
          `,
        )
        .join("")
    : '<li class="rows-empty">回收站是空的。</li>';
}

function renderAudit(data) {
  const items = data.items || [];
  $("#audit-list").innerHTML = items.length
    ? items
        .map(
          (item) => `
            <li class="row ${String(item.action || "").includes("failed") ? "is-failed" : ""}">
              <div class="row-main">
                <p class="row-title">${escapeHtml(AUDIT_LABELS[item.action] || item.action || "系统操作")}</p>
                <p class="row-sub">${escapeHtml(item.target || "—")}</p>
              </div>
              <div class="row-side">
                <time>${escapeHtml(formatDate(item.time))}</time>
                <span class="who">${escapeHtml(item.username || "dashboard")}</span>
              </div>
            </li>
          `,
        )
        .join("")
    : '<li class="rows-empty">还没有操作记录。</li>';
}

async function loadRecovery(options = {}) {
  return Promise.all([
    cachedLoad("trash", () => bridge.apiGet("page/trash"), renderTrash, options),
    cachedLoad("audit", () => bridge.apiGet("page/audit", { limit: 160 }), renderAudit, options),
  ]);
}

async function restoreTrash(id) {
  await run(() => bridge.apiPost("page/restore", { id }), { success: "已恢复" });
  await refreshAfterChange();
}

async function purgeTrash(id) {
  const confirmed = await modalConfirm({
    title: "永久删除",
    message: "这个 WAV 和它的回收记录会被删除，无法撤销。",
    danger: true,
    confirmLabel: "永久删除",
  });
  if (!confirmed) return;
  await run(() => bridge.apiPost("page/purge", { id }), { success: "已永久删除" });
  invalidate("trash", "audit", "overview");
  await Promise.allSettled([loadRecovery(), loadOverview({ silent: true })]);
}

/* ---------- 事件 ---------- */

function bindEvents() {
  const tabs = $$(".tab");
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
    tab.addEventListener("keydown", (event) => {
      const offset = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        switchView(tabs[event.key === "Home" ? 0 : tabs.length - 1].dataset.view, { focus: true });
      } else if (offset) {
        event.preventDefault();
        switchView(tabs[(index + offset + tabs.length) % tabs.length].dataset.view, { focus: true });
      }
    });
  });
  $$("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.jump));
  });

  $("#rescan").addEventListener("click", quietly(rescan));

  // 档案筛选全部在本地完成，不再请求后端。
  $("#archive-search").addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(renderCatalog, 120);
  });
  $("#archive-kind").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button || button.dataset.kind === state.kind) return;
    state.kind = button.dataset.kind;
    $$("[data-kind]", $("#archive-kind")).forEach((node) => {
      node.setAttribute("aria-pressed", String(node === button));
    });
    renderCatalog();
  });
  $("#archive-language").addEventListener("change", renderCatalog);
  $("#catalog").addEventListener(
    "click",
    quietly((event) => {
      const target = event.target.closest("[data-archive]");
      if (target) return openArchive(target.dataset.archive);
    }),
  );

  $$("[data-close-drawer]").forEach((node) => node.addEventListener("click", closeArchive));
  $("#drawer-packages").addEventListener(
    "click",
    quietly((event) => {
      const button = event.target.closest("[data-package]");
      if (button) return switchPackage(button.dataset.package);
    }),
  );
  $("#drawer-languages").addEventListener(
    "click",
    quietly((event) => {
      const button = event.target.closest("[data-lang]");
      if (!button || !state.detail || button.dataset.lang === state.detail.language) return;
      stopAudio();
      return fetchDetail(state.detail.character, button.dataset.lang);
    }),
  );
  $("#drawer-export").addEventListener("click", quietly(exportArchive));
  $("#drawer-import").addEventListener(
    "change",
    quietly((event) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      return importZip(file);
    }),
  );
  $("#drawer-select").addEventListener("click", () => setSelecting(!state.selecting));
  $("#matrix").addEventListener("click", (event) => {
    const tile = event.target.closest(".tile");
    if (tile) selectTile(tile.dataset.voice);
  });

  $("#dock-voice").addEventListener(
    "click",
    quietly((event) => {
      const button = event.target.closest("[data-voice-action]");
      const voice = state.current;
      if (!button || !voice) return;
      const action = button.dataset.voiceAction;
      if (action === "download") return downloadVoice(voice);
      if (action === "remove") return removeVoice(voice);
      if (action === "replace") {
        const item = state.detail.voices.find((entry) => entry.voice === voice);
        state.pendingReplace = { voice, token: item?.replaceToken };
        $("#replace-file").click();
      }
    }),
  );
  $("#replace-file").addEventListener(
    "change",
    quietly((event) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      return replaceVoice(file);
    }),
  );
  $("#batch-all").addEventListener("click", () => {
    const deletable = (state.detail?.voices || []).filter((item) => item.deletable).map((item) => item.voice);
    const allSelected = deletable.length && deletable.every((voice) => state.selected.has(voice));
    state.selected = new Set(allSelected ? [] : deletable);
    renderMatrix();
    renderDock();
  });
  $("#batch-remove").addEventListener("click", quietly(batchRemove));
  $("#batch-done").addEventListener("click", () => setSelecting(false));

  $("#fetch-form").addEventListener("submit", quietly(submitFetch));
  $("#fetch-character").addEventListener("input", renderFetchHint);
  $("#fetch-hint").addEventListener("click", (event) => {
    const button = event.target.closest("[data-suggest]");
    if (!button) return;
    $("#fetch-character").value = button.dataset.suggest;
    renderFetchHint();
    $("#fetch-character").focus();
  });
  $("#tasks-refresh").addEventListener("click", quietly(() => loadTasks()));
  $("#task-list").addEventListener(
    "click",
    quietly((event) => {
      const button = event.target.closest("[data-cancel-task]");
      if (button) return cancelTask(button.dataset.cancelTask);
    }),
  );
  $("#roster-search").addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(renderRoster, 120);
  });
  $("#roster").addEventListener("change", (event) => {
    const input = event.target.closest("[data-roster]");
    if (!input) return;
    if (input.checked) state.rosterSelected.add(input.dataset.roster);
    else state.rosterSelected.delete(input.dataset.roster);
    updateRosterFoot();
  });
  $("#roster-select-all").addEventListener("click", () => {
    const inputs = $$("[data-roster]", $("#roster"));
    const allChecked = inputs.length && inputs.every((input) => input.checked);
    inputs.forEach((input) => {
      input.checked = !allChecked;
      if (input.checked) state.rosterSelected.add(input.dataset.roster);
      else state.rosterSelected.delete(input.dataset.roster);
    });
    updateRosterFoot();
  });
  $("#roster-download").addEventListener("click", quietly(downloadRoster));
  $("#roster-refresh").addEventListener(
    "click",
    quietly(async () => {
      const button = $("#roster-refresh");
      button.disabled = true;
      try {
        await loadOperators({ refresh: true });
      } finally {
        button.disabled = false;
      }
    }),
  );

  $("#integrity-scan").addEventListener("click", quietly(() => startIntegrity(false)));
  $("#integrity-quarantine").addEventListener("click", quietly(() => startIntegrity(true)));

  $("#binding-new").addEventListener("click", quietly(() => bindingModal()));
  $("#binding-list").addEventListener(
    "click",
    quietly((event) => {
      const edit = event.target.closest("[data-edit-binding]");
      const remove = event.target.closest("[data-remove-binding]");
      if (edit) return bindingModal(state.bindings.find((item) => item.trigger === edit.dataset.editBinding));
      if (remove) return removeBinding(remove.dataset.removeBinding);
    }),
  );

  $("#alias-new").addEventListener("click", quietly(() => aliasModal()));
  $("#alias-list").addEventListener(
    "click",
    quietly((event) => {
      const edit = event.target.closest("[data-edit-alias]");
      const remove = event.target.closest("[data-remove-alias]");
      if (edit) return aliasModal(state.aliases.find((item) => item.alias === edit.dataset.editAlias));
      if (remove) return removeAlias(state.aliases.find((item) => item.alias === remove.dataset.removeAlias));
    }),
  );

  $("#trash-list").addEventListener(
    "click",
    quietly((event) => {
      const restore = event.target.closest("[data-restore]");
      const purge = event.target.closest("[data-purge]");
      if (restore) return restoreTrash(restore.dataset.restore);
      if (purge) return purgeTrash(purge.dataset.purge);
    }),
  );
  $("#audit-refresh").addEventListener("click", quietly(() => loadRecovery({ force: true })));

  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || $("#modal").open || $("#drawer").hidden) return;
    if (state.selecting) setSelecting(false);
    else closeArchive();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedulePoll(0);
  });
}

/* ---------- 轮询 ---------- */

// 有任务在跑时 2 秒一次，空闲时 8 秒一次；页面不可见时暂停。
function schedulePoll(delay) {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(pollBackgroundState, delay);
}

async function pollBackgroundState() {
  if (document.hidden) return;
  const items = await loadTasks({ silent: true }).catch(() => null);
  if (items) {
    state.hasActiveTasks = items.some((item) => ["queued", "running"].includes(item.status));
    // 只比较状态，进度变化不算：下载过程中不必反复刷新档案。
    const signature = items.map((item) => `${item.id}:${item.status}`).join("|");
    const changed = signature !== state.taskSignature;
    const firstPoll = state.taskSignature === "";
    state.taskSignature = signature;
    if (changed && !firstPoll) {
      invalidate("integrity");
      refreshAfterChange().catch(() => {});
      if (state.view === "integrity") loadIntegrity().catch(() => {});
    }
  }
  schedulePoll(state.hasActiveTasks ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

async function initialize() {
  if (!bridge) {
    setConnection("error", "无法连接");
    toast("请从 AstrBot 插件详情页打开这个页面。", "error");
    return;
  }
  await bridge.ready();
  document.title = bridge.t?.("pages.voice-manager.title", "语音档案控制台") || "语音档案控制台";
  renderLanguageFilter();
  renderFetchLanguages();
  bindEvents();
  await Promise.allSettled([loadOverview(), loadArchives()]);
  // 干员列表在后台预取，切到下载页时就不用等。
  loadOperators({ silent: true }).catch(() => {});
  await pollBackgroundState();
}

window.addEventListener("beforeunload", () => {
  window.clearTimeout(state.pollTimer);
  if (state.audioUrl) URL.revokeObjectURL(state.audioUrl);
});

initialize().catch((error) => {
  setConnection("error", "初始化失败");
  toast(errorMessage(error), "error");
});
