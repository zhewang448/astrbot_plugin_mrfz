"""Regression tests for the performance and correctness fixes from the v3.8.3 code review."""
import ast
import asyncio
import importlib
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_voice_safety import REPO, VM, data, page, wav_bytes, write_wav

renderer = importlib.import_module("safety_plugin.renderer")


class Query(dict):
    def get(self, key, default=None, type=None):
        return super().get(key, default)


def main_method(name, namespace):
    """从 main.py 里单独取出一个方法；main 依赖的 AstrBot 事件模块在测试里不可用。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace[name]


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mrfz-review-")
        self.root = Path(self.temp.name)
        self.mgr = VM(self.root / "data", REPO)
        self.titles = list(VM.VOICE_RESOURCE_IDS)
        self.pm = page.VoicePageManager.__new__(page.VoicePageManager)
        self.pm.voice_mgr = self.mgr
        self.pm.data_dir = self.mgr.data_dir
        self.pm.voices_dir = self.mgr.voices_dir
        self.pm.page_dir = self.root / "page"
        self.pm.backup_dir = self.pm.page_dir / "backups"
        self.pm.trash_dir = self.pm.page_dir / "trash"
        self.pm.preview_dir = self.pm.page_dir / "previews"
        self.pm.audit_file = self.pm.page_dir / "audit.jsonl"
        self.pm.integrity_file = self.pm.page_dir / "integrity.json"
        for directory in (self.pm.backup_dir, self.pm.trash_dir, self.pm.preview_dir):
            directory.mkdir(parents=True)
        self.pm.scan_callback = AsyncMock()
        self.pm._audit_lock = asyncio.Lock()
        self.pm._audit_lines = 0
        self.pm._last_page_cleanup = time.monotonic()

    def tearDown(self):
        self.temp.cleanup()

    def voice(self, index, *, lang="cn", skin=None, valid=True):
        root = self.mgr.voices_dir / "Operator"
        if skin:
            root = root / "skin" / skin
        path = root / lang / f"{self.titles[index]}.wav"
        if valid:
            return write_wav(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF\0\0\0\0WAVEbroken")
        return path

    async def test_detail_statuses_from_one_directory_listing(self):
        own = self.voice(0, skin="Outfit")
        self.voice(2, skin="Outfit", valid=False)
        fallback = self.voice(1)
        self.voice(0)
        self.mgr.scan_voice_files()
        character = "Operator皮肤[Outfit]"
        with patch.object(page, "request", types.SimpleNamespace(query=Query(character=character, language="cn"))), \
             patch.object(self.mgr, "_path_is_within", wraps=self.mgr._path_is_within) as within:
            detail = await self.pm.archive_detail()
        statuses = {item["voice"]: item["status"] for item in detail["voices"]}
        self.assertEqual([statuses[title] for title in self.titles[:4]], ["own", "fallback", "damaged", "missing"])
        self.assertTrue(all(item["status"] == "missing" for item in detail["voices"][4:]))
        # 目录各校验一次，不再按语音条数逐个 resolve。
        self.assertLess(within.call_count, 10)
        paths = {item["voice"]: path for item, path in self.pm._voice_statuses(character, "cn")}
        self.assertEqual(paths[self.titles[0]], own)
        self.assertEqual(paths[self.titles[1]], fallback)
        self.assertIsNone(paths[self.titles[2]])

    def test_integrity_does_not_hold_file_lock_while_checking(self):
        write_wav(self.mgr.voices_dir / "Operator" / "cn" / f"{self.titles[0]}.wav")
        broken = self.voice(1, valid=False)
        unknown = write_wav(self.mgr.voices_dir / "Operator" / "cn" / "unknown.wav")
        real = self.mgr._is_valid_wav_file
        acquired = []

        def probe(path):
            result = []

            def other():
                ok = self.mgr.file_lock.acquire(timeout=1)
                result.append(ok)
                if ok:
                    self.mgr.file_lock.release()

            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
            acquired.append(result[0])
            return real(path)

        with patch.object(self.mgr, "_is_valid_wav_file", side_effect=probe):
            report = self.pm._run_integrity(True)
        self.assertTrue(acquired and all(acquired))
        self.assertEqual((report["checked"], report["valid"], report["issueCount"], report["isolated"]), (3, 1, 2, 2))
        self.assertEqual({item["issue"] for item in report["issues"]}, {"WAV 文件损坏", "未知语音名称"})
        self.assertFalse(broken.exists())
        self.assertFalse(unknown.exists())
        self.assertEqual(json.loads(self.pm.integrity_file.read_text(encoding="utf-8"))["isolated"], 2)

    def test_integrity_skips_quarantine_when_file_changed_after_check(self):
        broken = self.voice(0, valid=False)

        def rewrite(path):
            broken.write_bytes(b"RIFF\0\0\0\0WAVEchanged-after-check")
            return False

        with patch.object(self.mgr, "_is_valid_wav_file", side_effect=rewrite):
            report = self.pm._run_integrity(True)
        self.assertEqual(report["issueCount"], 1)
        self.assertEqual(report["isolated"], 0)
        self.assertTrue(broken.exists())

    def test_expired_backups_and_trash_are_removed(self):
        old = time.time() - page.constants.PAGE_RETENTION_SECONDS - 60
        backups = {}
        for name in ("old", "referenced", "recent"):
            backups[name] = self.pm.backup_dir / name
            write_wav(backups[name] / "Operator" / "cn" / "x.wav")
        stage = self.pm.preview_dir / "stage"
        stage.mkdir()
        (stage / "recovery.json").write_text(json.dumps({"files": [
            {"backup": str(backups["referenced"] / "Operator" / "cn" / "x.wav")},
            {"backup": "None"},
        ]}), encoding="utf-8")
        trash_old = self.pm.trash_dir / ("a" * 32)
        trash_new = self.pm.trash_dir / ("b" * 32)
        unrelated = self.pm.trash_dir / "keep-me"
        for directory in (trash_old, trash_new, unrelated):
            directory.mkdir()
            (directory / "file.wav").write_bytes(wav_bytes())
        for directory in (backups["old"], backups["referenced"], trash_old, unrelated):
            os.utime(directory, (old, old))
        self.pm._cleanup_expired_page_data()
        self.assertFalse(backups["old"].exists())
        self.assertTrue(backups["referenced"].exists())
        self.assertTrue(backups["recent"].exists())
        self.assertFalse(trash_old.exists())
        self.assertTrue(trash_new.exists())
        self.assertTrue(unrelated.exists())

    async def test_audit_log_is_trimmed_to_recent_entries(self):
        self.pm.MAX_AUDIT_ITEMS = 3
        with patch.object(page.constants, "MAX_AUDIT_LINES", 5), \
             patch.object(page, "request", types.SimpleNamespace(username="tester")):
            for index in range(6):
                await self.pm._audit("rescan", f"target-{index}")
        lines = self.pm.audit_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["target"] for line in lines], ["target-3", "target-4", "target-5"])
        self.assertEqual(self.pm._audit_lines, 3)

    def test_trash_count_does_not_parse_metadata(self):
        item = self.pm.trash_dir / ("c" * 32)
        item.mkdir()
        (item / "file.wav").write_bytes(wav_bytes())
        (item / "metadata.json").write_text("{}", encoding="utf-8")
        (self.pm.trash_dir / ("d" * 32)).mkdir()
        with patch.object(self.pm, "_read_trash_item") as reader:
            self.assertEqual(self.pm._count_trash_items(), 1)
        reader.assert_not_called()

    def test_voice_path_requires_scanned_index(self):
        path = self.voice(0)
        self.assertIsNone(self.mgr.get_voice_path("Operator", self.titles[0], "cn"))
        self.mgr.scan_voice_files()
        self.assertEqual(self.mgr.get_voice_path("Operator", self.titles[0], "cn"), path)
        path.write_bytes(b"not audio")
        self.assertIsNone(self.mgr.get_voice_path("Operator", self.titles[0], "cn"))

    def test_atomic_write_failure_does_not_close_fd_twice(self):
        target = self.root / "out" / "payload.json"
        with patch.object(data.os, "fsync", side_effect=OSError("disk full")), \
             patch.object(data.os, "close", Mock(side_effect=AssertionError("closed twice"))):
            with self.assertRaises(OSError):
                VM._atomic_write_json(target, {"value": 1})
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.iterdir()))

    def test_unchanged_voice_index_is_not_rewritten(self):
        self.voice(0)
        self.mgr.scan_voice_files()
        with patch.object(self.mgr, "_atomic_write_bytes", wraps=self.mgr._atomic_write_bytes) as writer:
            self.mgr.scan_voice_files()
            self.mgr.save_voice_index()
            self.assertEqual(writer.call_count, 0)
            (self.mgr.data_dir / "voice_index.json").unlink()
            self.mgr.save_voice_index()
            self.assertEqual(writer.call_count, 1)
            self.voice(1)
            self.mgr.scan_voice_files()
            self.assertEqual(writer.call_count, 2)

    async def test_downloads_run_with_bounded_concurrency(self):
        record = {"paths": {"中文": "voice_cn/operator"},
                  "files": {title: f"cn_{number:03d}.wav" for title, number in VM.VOICE_RESOURCE_IDS.items()},
                  "texts": {}, "avatar_url": None}
        active = peak = 0

        async def download(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return "downloaded", "ok"

        progress = []
        with patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=record)), \
             patch.object(self.mgr, "_download_with_retries", side_effect=download), \
             patch.object(self.mgr, "fetch_character_image", AsyncMock(return_value=(True, "ok"))), \
             patch.object(data.aiohttp, "ClientSession"):
            ok, summary = await self.mgr.fetch_character_voices(
                "Operator", False, "2", progress=lambda done, total, label: progress.append((done, total)))
        self.assertTrue(ok)
        self.assertEqual(peak, VM.DOWNLOAD_CONCURRENCY)
        self.assertEqual(progress[-1], (len(self.titles), len(self.titles)))
        self.assertEqual(sorted(done for done, _ in progress), list(range(1, len(self.titles) + 1)))
        self.assertIn(f"新增 {len(self.titles)}", summary)

    async def test_fetch_preview_counts_existing_damaged_and_missing(self):
        self.voice(0)
        self.voice(1, valid=False)
        record = {"paths": {"中文": "voice_cn/operator"},
                  "files": {title: f"cn_{index + 1:03d}.wav" for index, title in enumerate(self.titles[:3])},
                  "texts": {}}
        self.pm.default_download_langs = ["cn"]
        self.pm.default_download_skin = False
        self.pm._preview_cleanup_lock = threading.Lock()
        self.pm._operation_previews = {}
        request = types.SimpleNamespace(username="tester", json=AsyncMock(return_value={"character": "Operator", "languages": "2"}))
        with patch.object(page, "request", request), \
             patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=record)):
            preview = await self.pm.preview_fetch()
        self.assertEqual((preview["existing"], preview["damaged"], preview["missing"]), (1, 1, 1))

    async def test_help_image_is_rendered_once(self):
        output = self.root / "render"
        image = renderer.VoiceRenderer(output_dir=str(output))
        rendered = output / "help.png"
        rendered.write_bytes(b"png")
        with patch.object(image, "_render_help_logic", Mock(return_value=str(rendered))) as render:
            self.assertEqual(await image.render_help(), str(rendered))
            self.assertEqual(await image.render_help(), str(rendered))
            self.assertEqual(render.call_count, 1)
            rendered.unlink()
            await image.render_help()
            self.assertEqual(render.call_count, 2)

    def test_binding_validation_and_cooldown_cleanup(self):
        valid_binding = main_method("_valid_binding", {})
        plugin = types.SimpleNamespace(voice_mgr=self.mgr)
        self.assertTrue(valid_binding(plugin, {"character": "Operator", "voice": self.titles[0], "lang": None}))
        self.assertFalse(valid_binding(plugin, {"character": "../x", "voice": self.titles[0]}))
        self.assertFalse(valid_binding(plugin, {"character": "Operator", "voice": "nope"}))
        self.assertFalse(valid_binding(plugin, {"character": "Operator", "voice": self.titles[0], "lang": "xx"}))

        remaining = main_method("_cooldown_remaining", {"time": time, "AstrMessageEvent": object})
        now = time.monotonic()
        plugin = types.SimpleNamespace(_cooldowns={(str(index), "bind"): now - 1 for index in range(300)})
        plugin._cooldowns[("active", "bind")] = now + 60
        event = types.SimpleNamespace(get_sender_id=lambda: "new")
        self.assertEqual(remaining(plugin, event, "bind", 2.0), 0.0)
        self.assertEqual(set(plugin._cooldowns), {("active", "bind"), ("new", "bind")})

    def test_skin_selector_matching_is_shared(self):
        self.voice(0, skin="Outfit")
        self.mgr.scan_voice_files()
        resource_id = next(iter(self.mgr.skin_voice_index["Operator"]))
        for selector in ("Outfit", resource_id):
            self.assertEqual(list(self.mgr._match_skin_packages("Operator", selector)), [resource_id])
            resolved, _ = self.mgr.resolve_character_reference(f"Operator皮肤[{selector}]")
            self.assertEqual(resolved, "Operator皮肤[Outfit]")
        self.assertEqual(self.pm._skin_package("Operator皮肤[Outfit]")[1], resource_id)
        self.assertFalse(self.mgr._match_skin_packages("Operator", "Missing"))


if __name__ == "__main__":
    unittest.main()
