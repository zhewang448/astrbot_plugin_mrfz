"""Manual UI checks with a mocked bridge; never contacts AstrBot or PRTS.

Run: python -X utf8 tests/check_language_ui.py OUTPUT_DIR
Requires Playwright and an installed Chromium browser.
"""
import importlib.util
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_images(output):
    constants = load_module("constants")
    renderer = load_module("renderer").VoiceRenderer(output_dir=str(output))
    languages = [{"display": info["name"], "color": info["color"]}
                 for info in constants.LANGUAGE_MAP.values()]
    renderer._render_help_logic()
    renderer.render_image({"operators": [{"name": "Operator", "languages": languages}],
                           "skin_operators": [{"name": "Operator\u76ae\u80a4 \u00b7 Outfit", "languages": languages}]},
                          constants.VOICE_DESCRIPTIONS)


BRIDGE = """(() => {
  window.testPosts = [];
  window.testSettings = {
    config: {auto_download:true, allow_public_auto_download:true, auto_download_skin:true,
      default_language_rank:['\u65b9\u8a00','\u4e2d\u6587','\u65e5\u8bed','\u82f1\u8bed','\u97e9\u8bed','\u610f\u8bed'],
      auto_download_language:['\u65b9\u8a00','\u4e2d\u6587','\u65e5\u8bed'], page_style:'modern'},
    revision: 'initial',
    languages: ['\u65b9\u8a00','\u4e2d\u6587','\u65e5\u8bed','\u82f1\u8bed','\u97e9\u8bed','\u610f\u8bed','\u4fc4\u8bed','\u5fb7\u8bed','\u897f\u73ed\u7259\u8bed','\u6cd5\u8bed'].map((name,i) => ({name,code:String(i)}))
  };
  window.AstrBotPluginPage = {
    ready: async () => {},
    apiGet: async (route) => {
      if (route === 'page/config') return structuredClone(testSettings);
      if (route === 'page/overview') return { version: '3.8.2', summary: {} };
      if (route === 'page/archive') return {
        character: 'Operator', language: 'fr', availableLanguages: ['fy','cn','jp','us','kr','it','ru','de','es','fr'],
        voices: [], packages: [], permissions: {}
      };
      return { items: [], summary: {} };
    },
    apiPost: async (route, payload) => {
      window.testPosts.push({route, payload});
      if (route === 'page/config') {
        if (window.rejectConfigSave) throw new Error('Mock config conflict');
        testSettings.config = structuredClone(payload.config);
        testSettings.revision = 'saved';
        return structuredClone(testSettings);
      }
      if (route === 'page/fetch/preview') return {
        languageNames: ['French'], previewToken: 'mock', existing: 0, missing: 1, warnings: []
      };
      return {};
    }
  };
})();"""


def check_pages(output):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for style in ("modern", "classic"):
            for width in (1440, 390):
                page = browser.new_page(viewport={"width": width, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frontend = REPO / "pages" / "voice-manager" / style
                html = (frontend / "index.html").read_text(encoding="utf-8")
                html = html.replace('<script type="module" src="./app.js"></script>', "")
                html = html.replace('<script src="../settings.js"></script>', "")
                html = html.replace('<link rel="stylesheet" href="./style.css" />', "")
                html = html.replace('<link rel="stylesheet" href="../settings.css" />', "")
                page.set_content(html)
                page.add_style_tag(content=(frontend / "style.css").read_text(encoding="utf-8"))
                page.add_style_tag(content=(frontend.parent / "settings.css").read_text(encoding="utf-8"))
                page.evaluate(BRIDGE)
                page.add_script_tag(content=(frontend.parent / "settings.js").read_text(encoding="utf-8"))
                page.add_script_tag(content=(frontend / "app.js").read_text(encoding="utf-8"))
                page.wait_for_function("document.querySelectorAll('input[name=fetch-language]').length === 10")
                page.wait_for_function("document.querySelector('[data-config-fields]').disabled === false")
                page.evaluate("switchView('tasks')")
                page.locator("#fetch-character").fill("Operator")
                page.locator('input[name="fetch-language"]').evaluate_all("nodes => nodes.forEach(n => n.checked = false)")
                page.locator('input[name="fetch-language"][value="fr"]').check()
                assert page.locator('input[name="fetch-language"]:checked').count() == 1
                page.evaluate("modalConfirm = async () => true")
                page.evaluate("submitFetch({preventDefault() {}})")
                posts = page.evaluate("testPosts")
                assert posts[0]["payload"]["languages"] == ["fr"], posts
                page.locator("#fetch-character").fill("Operator")
                page.locator('input[name="fetch-language"][value="ru"]').check()
                page.evaluate("submitFetch({preventDefault() {}})")
                posts = page.evaluate("testPosts")
                assert posts[2]["payload"]["languages"] == ["ru", "fr"], posts
                if style == "modern":
                    page.locator('input[name="fetch-language"][value="ru"]').uncheck()
                    page.evaluate("""state.rosterSelected.add('Operator');
                      modalConfirm = async options => {window.testConfirmation = options; return false;};
                      downloadRoster();""")
                    message = page.evaluate("testConfirmation.message")
                    assert "\u6cd5\u8bed" in message and "\u65b9\u8a00" not in message, message
                boxes = page.locator('#fetch-languages label').evaluate_all("""nodes => nodes.map(n => {
                    const r = n.getBoundingClientRect();
                    return {x:r.x, right:r.right, width:r.width};
                })""")
                assert len(boxes) == 10 and all(0 <= b["x"] < b["right"] <= width for b in boxes), boxes
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(output / f"{style}-{width}.png"), full_page=True)
                check_settings(page, style, width, output)
                assert not errors, errors
                print(json.dumps({"style": style, "width": width, "languages": len(boxes), "payloads": "fr;ru,fr", "settings": "saved;reordered;reload;conflict"}))
                page.close()
        browser.close()


def check_settings(page, style, width, output):
    page.evaluate("switchView('settings')")
    assert page.locator('[data-config-save]').is_disabled()
    page.evaluate("document.querySelectorAll('#toasts > *, #toast-stack > *').forEach(node => node.remove())")
    if style == "modern":
        page.locator('#tab-settings').evaluate("node => node.scrollIntoView({block:'nearest',inline:'nearest'})")
    page.locator('[data-config-add]').select_option("\u6cd5\u8bed")
    page.locator('[data-config-move="up"][data-index="6"]').click()
    priorities = page.locator('[data-config-priority] li > span').all_text_contents()
    assert priorities[5] == "\u6cd5\u8bed", priorities
    page.locator('[data-config-language="\u6cd5\u8bed"]').check()
    page.locator('[data-config-key="auto_download_skin"]').uncheck()
    page.locator('[data-config-save]').click()
    page.wait_for_function("document.querySelector('[data-config-status]').textContent.includes('\u5df2\u4fdd\u5b58')")
    saved = page.evaluate("testSettings.config")
    assert saved["default_language_rank"][5] == "\u6cd5\u8bed"
    assert "\u6cd5\u8bed" in saved["auto_download_language"]
    assert not saved["auto_download_skin"]
    assert page.locator('input[name="fetch-language"][value="fr"]').is_checked()
    assert not page.locator('#fetch-skin').is_checked()
    page.locator('[data-config-key="auto_download"]').uncheck()
    assert page.locator('[data-config-language="\u6cd5\u8bed"]').is_disabled()
    page.evaluate("window.rejectConfigSave = true")
    page.locator('[data-config-save]').click()
    page.wait_for_function("document.querySelector('[data-config-status]').textContent === 'Mock config conflict'")
    assert saved == page.evaluate("testSettings.config")
    assert not page.locator('[data-config-key="auto_download"]').is_checked()
    page.locator('[data-config-reload]').click()
    page.wait_for_function("document.querySelector('[data-config-fields]').disabled === false")
    assert page.locator('[data-config-key="auto_download"]').is_checked()
    assert page.locator('[data-config-save]').is_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    boxes = page.locator('.settings-form button, .settings-form select').evaluate_all("""nodes => nodes.map(n => {
      const r=n.getBoundingClientRect(); return {left:r.left,right:r.right};
    })""")
    assert all(0 <= b["left"] < b["right"] <= width for b in boxes), boxes
    page.screenshot(path=str(output / f"settings-{style}-{width}.png"), full_page=True)


if __name__ == "__main__":
    output_dir = Path(sys.argv[1]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    check_images(output_dir)
    check_pages(output_dir)
