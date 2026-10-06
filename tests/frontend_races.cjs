const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");

function functionSource(source, name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert(start >= 0, `Missing ${name}`);
  const tail = source.slice(start);
  const match = /\n(?:async )?function /.exec(tail);
  return match ? tail.slice(0, match.index) : tail;
}

function fixture(style) {
  const source = fs.readFileSync(path.join(__dirname, "..", "pages", "voice-manager", style, "app.js"), "utf8");
  const pending = [];
  const nodes = new Map();
  let plays = 0;
  const state = { detailSequence: 0, archiveSequence: 0, audioSequence: 0, selected: new Set() };
  const $ = (selector) => {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {
        hidden: true, value: "", style: {},
        classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
        setAttribute() {}, removeAttribute() {}, focus() {}, pause() {}, load() {},
        play: async () => { plays += 1; },
      });
    }
    return nodes.get(selector);
  };
  const context = vm.createContext({
    state, $, run: action => action(),
    bridge: { apiGet(route, query) { return new Promise(resolve => pending.push({ route, query, resolve })); } },
    renderDetail: data => { state.detail = data; },
    renderArchiveDetail: data => { state.archiveDetail = data; },
    renderArchiveCards: () => {}, loadVoiceTexts: async () => {},
    base64Blob: x => x,
    URL: { createObjectURL: data => `blob:${data}`, revokeObjectURL() {} },
    document: { body: { style: {}, classList: { add() {}, remove() {} } } },
    window: { requestAnimationFrame: action => action(), setTimeout: action => action() },
  });
  for (const name of ["stopAudio", "closeArchive", "openArchive", style === "modern" ? "fetchDetail" : "loadArchives",
    style === "modern" ? "playVoice" : "previewVoice"]) {
    vm.runInContext(functionSource(source, name), context);
  }
  return { context, state, pending, $, plays: () => plays };
}

async function check(style) {
  const open = style === "modern" ? "fetchDetail" : "openArchive";
  const play = style === "modern" ? "playVoice" : "previewVoice";
  const detailKey = style === "modern" ? "detail" : "archiveDetail";
  {
    const f = fixture(style);
    const slow = f.context[open]("First");
    const fast = f.context[open]("Second");
    f.pending[1].resolve({ character: "Second" });
    await fast;
    f.pending[0].resolve({ character: "First" });
    await slow;
    assert.equal(f.state[detailKey].character, "Second");
  }
  {
    const f = fixture(style);
    const loading = f.context.openArchive("First");
    f.context.closeArchive();
    f.pending[0].resolve({ character: "First" });
    await loading;
    assert(!f.$(style === "modern" ? "#drawer" : "#archive-drawer").classList.contains("is-open"));
  }
  {
    const f = fixture(style);
    f.state[detailKey] = { character: "First", language: "cn" };
    f.state.current = "voice";
    const loading = f.context[play]("voice");
    f.context.closeArchive();
    f.pending[0].resolve({ base64: "old" });
    await loading;
    assert.equal(f.plays(), 0);
  }
  {
    const f = fixture(style);
    f.state[detailKey] = { character: "First", language: "cn" };
    f.state.current = "voice";
    const slow = f.context[play]("voice");
    const fast = f.context[play]("voice");
    f.pending[1].resolve({ base64: "latest" });
    await fast;
    f.pending[0].resolve({ base64: "old" });
    await slow;
    assert.equal(f.plays(), 1);
    assert.equal(f.$("#audio-player").src, "blob:latest");
  }
  {
    const f = fixture(style);
    f.state[detailKey] = { character: "First", language: "cn" };
    f.state.current = "voice";
    const loading = f.context[play]("voice");
    f.context.stopAudio();
    f.pending[0].resolve({ base64: "old" });
    await loading;
    assert.equal(f.plays(), 0);
  }
  if (style === "classic") {
    const f = fixture(style);
    const detail = f.context.openArchive("First");
    const list = f.context.loadArchives();
    f.pending[1].resolve({ items: [] });
    await list;
    f.pending[0].resolve({ character: "First" });
    await detail;
    assert.equal(f.state.archiveDetail.character, "First");
  }
  console.log(`${style}: response ordering, close and audio cancellation passed`);
}

(async () => {
  await check("modern");
  await check("classic");
  console.log("11 frontend race regressions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
