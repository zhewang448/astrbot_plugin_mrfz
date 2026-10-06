"""Configuration migration and Page saves; all files stay in temporary folders."""
import asyncio
import ast
import copy
import importlib
import json
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_voice_safety import page

config = importlib.import_module("safety_plugin.config")


class SavedConfig(dict):
    def __init__(self, path, values):
        super().__init__(copy.deepcopy(values))
        self.path = path
        self.fail = False
        self.writes = 0
        self.save_config()

    def save_config(self):
        if self.fail:
            raise OSError("read only")
        self.path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")
        self.writes += 1


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix="mrfz-config-")
        self.root = Path(self.temp.name)
        self.backups = self.root / "backups"
        self.old = {"auto_download": False, "allow_public_auto_download": False,
                    "auto_download_skin": False, "default_language_rank": "10,2,7",
                    "auto_download_language": "123", "page_style": "classic"}
        self.raw = SavedConfig(self.root / "config.json", self.old)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_preserves_choices_order_and_is_idempotent(self):
        store = config.ConfigStore(self.raw, self.backups)
        self.assertEqual(store.current.language_priority, ["fr", "cn", "ru"])
        self.assertEqual(self.raw["default_language_rank"], ["\u6cd5\u8bed", "\u4e2d\u6587", "\u4fc4\u8bed"])
        self.assertEqual(self.raw["auto_download_language"], ["\u65b9\u8a00", "\u4e2d\u6587", "\u65e5\u8bed"])
        backup = list(self.backups.glob("*.json"))
        self.assertEqual(len(backup), 1)
        self.assertEqual(json.loads(backup[0].read_text(encoding="utf-8")), self.old)
        writes = self.raw.writes
        config.ConfigStore(self.raw, self.backups)
        self.assertEqual(self.raw.writes, writes)
        self.assertEqual(self.raw["auto_download"], False)
        self.assertEqual(self.raw["page_style"], "classic")

    def test_failed_migration_keeps_disk_memory_and_retries(self):
        before = self.raw.path.read_bytes()
        self.raw.fail = True
        store = config.ConfigStore(self.raw, self.backups)
        self.assertTrue(store.migration_error)
        self.assertEqual(dict(self.raw), self.old)
        self.assertEqual(self.raw.path.read_bytes(), before)
        self.assertEqual(store.current.language_priority, ["fr", "cn", "ru"])
        self.raw.fail = False
        restarted = config.ConfigStore(self.raw, self.backups)
        self.assertFalse(restarted.migration_error)
        self.assertIsInstance(self.raw["default_language_rank"], list)

    def test_backup_failure_does_not_write_configuration(self):
        with patch.object(Path, "open", side_effect=OSError("backup denied")):
            store = config.ConfigStore(self.raw, self.backups)
        self.assertTrue(store.migration_error)
        self.assertEqual(dict(self.raw), self.old)
        self.assertEqual(self.raw.writes, 1)

    def test_empty_lists_are_preserved_and_defaults_match_schema(self):
        defaults = config.PluginConfig().to_dict()
        from test_voice_safety import REPO
        schema = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(defaults, {key: meta["default"] for key, meta in schema.items()})
        values = {**defaults, "default_language_rank": [], "auto_download_language": []}
        self.assertEqual(config.PluginConfig.from_dict(values).to_dict(), values)

    def test_save_failure_rolls_back_runtime_disk_and_revision(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        values = {**before["config"], "auto_download": True}
        disk = self.raw.path.read_bytes()
        self.raw.fail = True
        with self.assertRaises(OSError):
            store.save(values, before["revision"])
        self.assertEqual(store.snapshot(), before)
        self.assertEqual(self.raw.path.read_bytes(), disk)
        self.assertFalse(store.current.auto_download)

    def test_concurrent_pages_reject_stale_write(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        values = {**before["config"], "auto_download": True}
        store.save(values, before["revision"])
        with self.assertRaises(config.ConfigConflict):
            store.save(before["config"], before["revision"])
        self.assertTrue(store.current.auto_download)
        self.assertTrue(json.loads(self.raw.path.read_text(encoding="utf-8"))["auto_download"])

    def test_invalid_values_are_rejected_without_writes(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        for key, value in (("auto_download", "false"), ("page_style", "invalid"),
                           ("auto_download_language", "10"), ("default_language_rank", ["unknown"]),
                           ("default_language_rank", [{}])):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                store.save({**before["config"], key: value}, before["revision"])
        self.assertEqual(store.snapshot(), before)

    async def test_page_save_applies_only_after_success_and_updates_snapshot(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        pm = page.VoicePageManager.__new__(page.VoicePageManager)
        pm.config_store = store
        pm._config_lock = asyncio.Lock()
        applied = []
        pm.apply_config = applied.append
        request = types.SimpleNamespace(json=AsyncMock(return_value={
            "config": {**before["config"], "auto_download": True}, "revision": before["revision"]}))
        with patch.object(page, "request", request):
            saved = await pm.save_config()
        self.assertTrue(saved["config"]["auto_download"])
        self.assertEqual(len(applied), 1)
        with patch.object(page, "request", request):
            rejected = await pm.save_config()
        self.assertEqual(rejected["status_code"], 409)
        self.assertEqual(len(applied), 1)

    async def test_page_save_failure_keeps_active_config(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        pm = page.VoicePageManager.__new__(page.VoicePageManager)
        pm.config_store = store
        pm._config_lock = asyncio.Lock()
        applied = []
        pm.apply_config = applied.append
        self.raw.fail = True
        request = types.SimpleNamespace(json=AsyncMock(return_value={
            "config": {**before["config"], "auto_download": True}, "revision": before["revision"]}))
        with patch.object(page, "request", request):
            rejected = await pm.save_config()
        self.assertEqual(rejected["status_code"], 500)
        self.assertFalse(applied)
        self.assertEqual(store.snapshot(), before)

    def test_runtime_apply_updates_command_and_page_settings(self):
        from test_voice_safety import REPO
        tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_apply_config")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), namespace)
        previous = config.PluginConfig()
        candidate = config.PluginConfig(auto_download=False, auto_download_skin=False,
                                        language_priority=["fr", "ru"], download_languages=["fr"], page_style="classic")
        plugin = types.SimpleNamespace(plugin_config=previous, voice_page=types.SimpleNamespace())
        namespace["_apply_config"](plugin, candidate)
        self.assertIs(plugin.plugin_config, candidate)
        self.assertEqual(plugin.voice_page.default_download_langs, ["fr"])
        self.assertFalse(plugin.voice_page.default_download_skin)
        self.assertEqual(plugin.voice_page.page_style, "classic")
        self.assertEqual(previous.download_languages, ["fy", "cn", "jp"])

    async def test_cancelled_save_finishes_persistence_and_runtime_update(self):
        store = config.ConfigStore(self.raw, self.backups)
        before = store.snapshot()
        pm = page.VoicePageManager.__new__(page.VoicePageManager)
        pm.config_store = store
        pm._config_lock = asyncio.Lock()
        applied = []
        pm.apply_config = applied.append
        started, release = threading.Event(), threading.Event()
        persist = self.raw.save_config
        def blocked_save():
            started.set()
            release.wait(5)
            persist()
        self.raw.save_config = blocked_save
        request = types.SimpleNamespace(json=AsyncMock(return_value={
            "config": {**before["config"], "auto_download": True}, "revision": before["revision"]}))
        with patch.object(page, "request", request):
            task = asyncio.create_task(pm.save_config())
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(applied), 1)
        self.assertTrue(applied[0].auto_download)
        self.assertTrue(json.loads(self.raw.path.read_text(encoding="utf-8"))["auto_download"])
