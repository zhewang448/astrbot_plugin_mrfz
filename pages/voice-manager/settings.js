/* Shared configuration editor for both Page styles. */
window.VoiceSettings = function createSettings({ bridge, root, onChange }) {
  let snapshot = null;
  let draft = null;
  let busy = false;
  const fields = [
    ["auto_download", "自动下载缺少的角色语音"],
    ["allow_public_auto_download", "允许普通用户触发自动下载"],
    ["auto_download_skin", "自动下载时包含皮肤语音"],
  ];
  const escape = value => String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  // Lucide arrow-up/arrow-down/x paths, ISC license (see icons-LICENSE.txt).
  const icon = name => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${{
    up: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    down: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  }[name]}</svg>`;
  root.innerHTML = `
    <form class="settings-form">
      <div class="settings-heading"><h2>插件设置</h2><div class="settings-actions">
        <button type="button" data-config-reload>重新加载</button>
        <button type="submit" data-config-save disabled>保存设置</button>
      </div></div>
      <p data-config-status role="status" aria-live="polite">正在读取配置…</p>
      <fieldset data-config-fields disabled>
        <section class="settings-section"><h3>自动下载</h3>
          ${fields.map(([key, label]) => `<label class="settings-toggle"><span>${label}</span><input type="checkbox" data-config-key="${key}" /></label>`).join("")}
          <fieldset class="settings-language-field"><legend>自动下载的语言</legend><div class="settings-language-checks" data-config-download></div></fieldset>
        </section>
        <section class="settings-section"><h3>播放语言优先级</h3>
          <ol class="settings-priority" data-config-priority></ol>
          <label class="settings-add"><span>添加语言</span><select data-config-add aria-label="添加优先播放语言"></select></label>
        </section>
        <section class="settings-section"><h3>管理页面</h3>
          <label class="settings-style"><span>界面样式</span><select data-config-key="page_style"><option value="modern">新版</option><option value="classic">经典版</option></select></label>
          <a href="../index.html${escape(window.location.search)}">重新打开管理页面</a>
        </section>
      </fieldset>
    </form>`;
  const $ = selector => root.querySelector(selector);
  const status = (message, error = false) => {
    $("[data-config-status]").textContent = message;
    $("[data-config-status]").dataset.error = String(error);
  };
  const updateSave = () => {
    $("[data-config-save]").disabled = busy || !snapshot || JSON.stringify(draft) === JSON.stringify(snapshot.config);
  };
  const render = () => {
    fields.forEach(([key]) => { $(`[data-config-key="${key}"]`).checked = draft[key]; });
    $('[data-config-key="page_style"]').value = draft.page_style;
    $("[data-config-download]").innerHTML = snapshot.languages.map(({name}) => `
      <label><input type="checkbox" data-config-language="${escape(name)}" ${draft.auto_download_language.includes(name) ? "checked" : ""} /><span>${escape(name)}</span></label>`).join("");
    $("[data-config-priority]").innerHTML = draft.default_language_rank.map((name, index) => `
      <li><span>${escape(name)}</span><div class="settings-order-actions">
        <button type="button" data-config-move="up" data-index="${index}" title="提高${escape(name)}优先级" aria-label="提高${escape(name)}优先级" ${index === 0 ? "disabled" : ""}>${icon("up")}</button>
        <button type="button" data-config-move="down" data-index="${index}" title="降低${escape(name)}优先级" aria-label="降低${escape(name)}优先级" ${index === draft.default_language_rank.length - 1 ? "disabled" : ""}>${icon("down")}</button>
        <button type="button" data-config-remove="${index}" title="移除${escape(name)}" aria-label="移除${escape(name)}">${icon("x")}</button>
      </div></li>`).join("");
    const available = snapshot.languages.filter(({name}) => !draft.default_language_rank.includes(name));
    $("[data-config-add]").innerHTML = '<option value="">选择语言</option>' + available.map(({name}) => `<option>${escape(name)}</option>`).join("");
    $("[data-config-add]").disabled = !available.length;
    root.querySelectorAll('[data-config-key="allow_public_auto_download"], [data-config-key="auto_download_skin"], [data-config-language]').forEach(input => { input.disabled = !draft.auto_download; });
    updateSave();
  };
  async function load({force = false} = {}) {
    if (busy || (snapshot && !force)) return;
    busy = true;
    $("[data-config-fields]").disabled = true;
    $("[data-config-reload]").disabled = true;
    updateSave();
    status("正在读取配置…");
    try {
      const data = await bridge.apiGet("page/config");
      snapshot = data;
      draft = structuredClone(data.config);
      render();
      onChange(data.config);
      status(data.migrationWarning || "", Boolean(data.migrationWarning));
    } catch (error) {
      status(error.message || "配置读取失败，请重新加载", true);
    } finally {
      busy = false;
      $("[data-config-fields]").disabled = !snapshot;
      $("[data-config-reload]").disabled = false;
      updateSave();
    }
  }
  root.addEventListener("change", event => {
    const input = event.target;
    if (!draft || busy) return;
    if (input.dataset.configKey) draft[input.dataset.configKey] = input.type === "checkbox" ? input.checked : input.value;
    if (input.dataset.configLanguage) {
      const names = draft.auto_download_language;
      if (input.checked && !names.includes(input.dataset.configLanguage)) names.push(input.dataset.configLanguage);
      if (!input.checked) draft.auto_download_language = names.filter(name => name !== input.dataset.configLanguage);
    }
    if (input.matches("[data-config-add]") && input.value) draft.default_language_rank.push(input.value);
    render();
    // render 会重建语言复选框，键盘操作时把焦点还给对应的新节点。
    if (input.dataset.configLanguage) {
      $(`[data-config-language="${CSS.escape(input.dataset.configLanguage)}"]`)?.focus();
    }
  });
  root.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button || busy) return;
    if (button.hasAttribute("data-config-reload")) { load({force: true}); return; }
    if (!draft) return;
    const names = draft.default_language_rank;
    if (button.hasAttribute("data-config-remove")) names.splice(Number(button.dataset.configRemove), 1);
    if (button.dataset.configMove) {
      const index = Number(button.dataset.index);
      const next = index + (button.dataset.configMove === "up" ? -1 : 1);
      if (next >= 0 && next < names.length) [names[index], names[next]] = [names[next], names[index]];
    }
    render();
    if (button.dataset.configMove) {
      const index = Number(button.dataset.index) + (button.dataset.configMove === "up" ? -1 : 1);
      $(`[data-config-move="${button.dataset.configMove}"][data-index="${index}"]`)?.focus();
    }
    if (button.hasAttribute("data-config-remove")) {
      // 焦点移到顶替上来的那一项；删的是最后一项就移到上一项，列表空了就移到“添加语言”。
      const index = Number(button.dataset.configRemove);
      ($(`[data-config-remove="${index}"]`) || $(`[data-config-remove="${index - 1}"]`) || $("[data-config-add]"))?.focus();
    }
  });
  $("form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !snapshot) return;
    busy = true;
    $("[data-config-fields]").disabled = true;
    $("[data-config-reload]").disabled = true;
    updateSave();
    status("正在保存…");
    try {
      const previousStyle = snapshot.config.page_style;
      snapshot = await bridge.apiPost("page/config", {config: draft, revision: snapshot.revision});
      draft = structuredClone(snapshot.config);
      onChange(snapshot.config);
      render();
      status(previousStyle === draft.page_style ? "已保存并生效" : "已保存并生效；界面样式在重新打开管理页面后切换");
    } catch (error) {
      status(error.message || "配置保存失败，请重试", true);
    } finally {
      busy = false;
      $("[data-config-fields]").disabled = false;
      $("[data-config-reload]").disabled = false;
      updateSave();
    }
  });
  return {load};
};
