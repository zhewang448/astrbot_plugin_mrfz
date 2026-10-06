/* Helpers shared by both Page styles. Loaded as a classic script before each app.js. */
(() => {
  const LANGUAGES = [
    { code: "fy", name: "方言" },
    { code: "cn", name: "中文" },
    { code: "jp", name: "日语" },
    { code: "us", name: "英语" },
    { code: "kr", name: "韩语" },
    { code: "it", name: "意语" },
    { code: "ru", name: "俄语" },
    { code: "de", name: "德语" },
    { code: "es", name: "西班牙语" },
    { code: "fr", name: "法语" },
  ];
  const DEFAULT_FETCH_LANGUAGES = ["fy", "cn", "jp"];

  // 有任务在跑时 2 秒轮询一次，空闲时 8 秒一次；页面不可见时暂停。
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 8000;

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

  // Intl 格式化器创建开销不小，列表里每行都要用，按参数缓存。
  const formatters = new Map();

  function formatter(key, create) {
    if (!formatters.has(key)) formatters.set(key, create());
    return formatters.get(key);
  }

  function formatNumber(value) {
    return formatter("number", () => new Intl.NumberFormat("zh-CN")).format(Number(value || 0));
  }

  function formatDate(value, withTime = true) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const locale = window.AstrBotPluginPage?.getLocale?.() || "zh-CN";
    return formatter(
      `date:${locale}:${withTime}`,
      () =>
        new Intl.DateTimeFormat(locale, {
          ...(withTime ? {} : { year: "numeric" }),
          month: "2-digit",
          day: "2-digit",
          ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
        }),
    ).format(date);
  }

  function languageName(code) {
    return LANGUAGES.find((item) => item.code === code)?.name || code || "自动";
  }

  function errorMessage(error) {
    return error instanceof Error ? error.message : String(error || "操作失败");
  }

  // 各样式的 toast 不同，run 由各自的 toast 生成。
  function createRun(toast) {
    return async function run(action, { success = null, silent = false } = {}) {
      try {
        const result = await action();
        if (success) toast(success, "success");
        return result;
      } catch (error) {
        if (!silent) toast(errorMessage(error), "error");
        throw error;
      }
    };
  }

  // 事件回调里的失败已经通过 toast 提示过，这里只吞掉 rejection，避免控制台报未处理错误。
  function quietly(handler) {
    return (...args) => {
      Promise.resolve(handler(...args)).catch(() => {});
    };
  }

  async function base64Blob(encoded, mime) {
    const type = mime || "audio/wav";
    try {
      // 交给浏览器解码，避免在 JS 里逐字节循环几 MB 的数据。
      return await (await fetch(`data:${type};base64,${encoded}`)).blob();
    } catch {
      const binary = window.atob(encoded);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      return new Blob([bytes], { type });
    }
  }

  // 同一条语音在文件没变时重复播放，直接复用上次的音频，不再下载 base64。
  const audioCache = { key: "", url: null };

  function audioKey(detail, item) {
    return [detail.character, detail.language, item?.voice, item?.bytes, item?.updatedAt].join("|");
  }

  function cachedAudio(key) {
    return audioCache.key === key ? audioCache.url : null;
  }

  function cacheAudio(key, blob) {
    if (audioCache.url) URL.revokeObjectURL(audioCache.url);
    audioCache.key = key;
    audioCache.url = URL.createObjectURL(blob);
    return audioCache.url;
  }

  function exportRequest(bridge, detail, voice = null) {
    const query = { character: detail.character, language: detail.language };
    if (voice) query.voice = voice;
    return bridge.download(
      "page/export",
      query,
      voice ? `${detail.base}-${detail.language}-${voice}.wav` : `${detail.base}-${detail.language}.zip`,
    );
  }

  async function discardPreview(bridge, previewToken) {
    if (!previewToken) return;
    await bridge.apiPost("page/preview/discard", { previewToken }).catch(() => {});
  }

  // 预览 → 确认 → 提交：用户取消时通知后端丢弃预览，返回是否确认。
  async function confirmPreview(bridge, preview, modalConfirm, options) {
    if (await modalConfirm(options)) return true;
    await discardPreview(bridge, preview.previewToken);
    return false;
  }

  function languageOptions(selected = "") {
    return LANGUAGES.map(
      (item) =>
        `<option value="${item.code}" ${item.code === selected ? "selected" : ""}>${escapeHtml(item.name)}</option>`,
    ).join("");
  }

  function fetchLanguageChecks(className) {
    return LANGUAGES.map(
      (item) => `
        <label class="${className}">
          <input type="checkbox" name="fetch-language" value="${item.code}" ${
            DEFAULT_FETCH_LANGUAGES.includes(item.code) ? "checked" : ""
          } />
          <span>${escapeHtml(item.name)}</span>
        </label>
      `,
    ).join("");
  }

  // 插件配置载入或保存后，下载表单的默认语言和皮肤选项跟着变。
  function applyFetchDefaults(config) {
    document.querySelectorAll('input[name="fetch-language"]').forEach((input) => {
      input.checked = config.auto_download_language.includes(languageName(input.value));
    });
    document.querySelector("#fetch-skin").checked = config.auto_download_skin;
  }

  // 编辑快捷绑定时原档案可能已被删除；补一个选中项，避免下拉框默认选第一项、悄悄改掉目标。
  function archiveOptions(archives, existing) {
    const options = archives.map(
      (item) =>
        `<option value="${escapeHtml(item.character)}" ${item.character === existing?.character ? "selected" : ""}>${escapeHtml(
          item.kind === "skin" ? `${item.base} / ${item.skinName}` : item.base,
        )}</option>`,
    );
    if (existing && !archives.some((item) => item.character === existing.character)) {
      options.unshift(
        `<option value="${escapeHtml(existing.character)}" selected>${escapeHtml(existing.character)}（档案已不存在）</option>`,
      );
    }
    return options.join("");
  }

  window.VoiceCommon = {
    LANGUAGES,
    POLL_ACTIVE_MS,
    POLL_IDLE_MS,
    escapeHtml,
    formatBytes,
    formatNumber,
    formatDate,
    languageName,
    errorMessage,
    createRun,
    quietly,
    base64Blob,
    audioKey,
    cachedAudio,
    cacheAudio,
    exportRequest,
    discardPreview,
    confirmPreview,
    languageOptions,
    fetchLanguageChecks,
    applyFetchDefaults,
    archiveOptions,
  };
})();
