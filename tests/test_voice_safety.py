"""Offline regressions. All writes use temporary directories, never plugin data."""
import ast
import asyncio
import hashlib
import importlib
import io
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[1]
package = types.ModuleType("safety_plugin")
package.__path__ = [str(REPO)]
sys.modules[package.__name__] = package
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("safety_tests")
sys.modules["astrbot"] = types.ModuleType("astrbot")
sys.modules[api.__name__] = api
web = types.ModuleType("astrbot.api.web")
web.PluginUploadFile = type("PluginUploadFile", (), {})
web.error_response = lambda message, **kw: {"error": message, **kw}
web.json_response = lambda data, **kw: data
web.file_response = lambda *args, **kw: None
web.request = None
sys.modules[web.__name__] = web
data = importlib.import_module("safety_plugin.data_source")
page = importlib.import_module("safety_plugin.voice_page")
VM = data.VoiceManager


def wav_bytes(sample=b"\x01\x02"):
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(sample * 100)
    return output.getvalue()


def write_wav(path, sample=b"\x01\x02"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(sample))
    return path


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self.payload = payload if payload is not None else wav_bytes()
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.payload


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mrfz-safety-")
        self.root = Path(self.temp.name)
        self.mgr = VM(self.root / "data", REPO)
        self.titles = list(VM.VOICE_RESOURCE_IDS)
        self.record = {"paths": {"\u4e2d\u6587": "voice_cn/operator"},
                       "files": {self.titles[0]: "cn_001.wav"}, "texts": {},
                       "avatar_url": "https://prts.wiki/avatar.png"}
        self.pm = page.VoicePageManager.__new__(page.VoicePageManager)
        self.pm.voice_mgr = self.mgr
        self.pm.voices_dir = self.mgr.voices_dir
        self.pm.backup_dir = self.root / "backups"
        self.pm.preview_dir = self.root / "previews"
        self.pm.upload_dir = self.root / "uploads"
        self.pm.preview_dir.mkdir()
        self.pm.upload_dir.mkdir()
        self.pm._mutation_lock = self.mgr.mutation_lock
        self.pm._preview_cleanup_lock = threading.Lock()
        self.pm._operation_previews = {}
        self.pm.scan_callback = AsyncMock()
        self.pm._audit = AsyncMock()

    def tearDown(self):
        self.temp.cleanup()

    def target(self, voice=None, character="Operator", lang="cn"):
        return self.mgr.voices_dir / character / lang / f"{voice or self.titles[0]}.wav"

    def test_reject_truncated_wav(self):
        valid = wav_bytes()
        self.assertTrue(VM._looks_like_wav(valid))
        for invalid in (b"RIFF\0\0\0\0WAVE", valid[:44], valid[:-1], b"not audio"):
            self.assertFalse(VM._looks_like_wav(invalid))

    def test_language_rank_compatibility_and_exact_tokens(self):
        parse = data.constants.parse_language_ranks
        self.assertEqual(parse("123456"), list("123456"))
        self.assertEqual(parse("10"), ["10"])
        self.assertEqual(parse("7,8,9,10"), ["7", "8", "9", "10"])
        self.assertEqual(parse("10 2，10,99"), ["10", "2"])
        self.mgr.voice_index["Operator"] = ["fy", "cn", "fr"]
        self.assertEqual(self.mgr.choose_language("Operator", "10,2,1"), "fr")
        self.assertEqual(self.mgr.choose_language("Operator", "123456"), "fy")

    def test_minority_languages_share_custom_folder_without_collision(self):
        paths = {"\u4fc4\u8bed": "voice_custom/russian",
                 "\u5fb7\u8bed": "voice_custom/german",
                 "\u897f\u73ed\u7259\u8bed": "voice_custom/spanish",
                 "\u6cd5\u8bed": "voice_custom/french",
                 "\u672a\u77e5\u8bed\u8a00": "voice_custom/unknown",
                 "\u4e2d\u6587-\u65b9\u8a00": "voice_custom/dialect"}
        record = {**self.record, "paths": paths}
        plan = self.mgr.build_download_plan("Operator", record, True, "7,8,9,10")
        self.assertEqual({item["language"] for item in plan}, {"ru", "de", "es", "fr"})
        french = self.mgr.build_download_plan("Operator", record, True, "10")
        self.assertEqual([item["language"] for item in french], ["fr"])
        self.assertTrue(all("/voice_custom/" in item["url"] for item in plan))
        for language in ("ru", "de", "es", "fr"):
            write_wav(self.target(lang=language))
        self.mgr.scan_voice_files()
        for language in ("ru", "de", "es", "fr"):
            self.assertEqual(self.mgr.get_voice_path("Operator", self.titles[0], language), self.target(lang=language))

    def test_minority_text_labels_and_stale_caches(self):
        text = "|\u8def\u5f84=\u6cd5\u8bed:voice_custom/french\n|\u6807\u98981=" + self.titles[0] + "\n|\u8bed\u97f31=CN_001.wav\n|\u53f0\u8bcd1="
        text += "{{VoiceData/word|\u4fc4\u6587|Russian}}{{VoiceData/word|\u5fb7\u6587|German}}{{VoiceData/word|\u897f\u73ed\u7259\u6587|Spanish}}{{VoiceData/word|\u6cd5\u6587|French}}"
        record = data.prts.parse_voice_record(text)
        self.assertEqual(set(record["texts"][""][self.titles[0]]), {"ru", "de", "es", "fr"})
        self.mgr._voice_records["Operator"] = {"version": 1, "fetchedAt": 9999999999, "record": self.record}
        self.assertIsNone(self.mgr._cached_voice_record("Operator"))

    def test_page_fetch_selection_keeps_french_rank_whole(self):
        self.pm.default_download_langs = "123"
        self.pm.default_download_skin = True
        operation = self.pm._normalize_fetch_payload({"character": "Operator", "languages": "10,7"})
        self.assertEqual(operation["languageCodes"], ["fr", "ru"])
        self.assertEqual(operation["languages"], ["fr", "ru"])
        legacy = self.pm._normalize_fetch_payload({"character": "Operator", "languages": "123"})
        self.assertEqual(legacy["languageCodes"], ["fy", "cn", "jp"])

    async def test_fetch_french_does_not_download_dialect(self):
        record = {**self.record, "paths": {"\u6cd5\u8bed": "voice_custom/french",
                                           "\u4e2d\u6587-\u65b9\u8a00": "voice_custom/dialect"}}
        with patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=record)), \
             patch.object(self.mgr, "fetch_character_image", AsyncMock(return_value=(True, "ok"))), \
             patch.object(data.aiohttp, "ClientSession") as session:
            session.return_value.__aenter__.return_value = types.SimpleNamespace(get=lambda *a, **kw: Response())
            ok, _ = await self.mgr.fetch_character_voices("Operator", False, "10", require_no_failures=True)
        self.assertTrue(ok)
        self.assertIsNotNone(self.mgr.get_voice_path("Operator", self.titles[0], "fr"))
        self.assertFalse(self.target(lang="fy").exists())
        self.assertFalse(self.target(lang="cn").exists())

    async def test_verified_minority_audio_migrates_to_exact_language(self):
        for label, language in (("\u4fc4\u8bed", "ru"), ("\u5fb7\u8bed", "de"),
                                ("\u897f\u73ed\u7259\u8bed", "es"), ("\u6cd5\u8bed", "fr")):
            with self.subTest(language=language):
                character = "Operator_" + language
                target = write_wav(self.target(character=character))
                before = target.read_bytes()
                record = {**self.record, "paths": {label: "voice_custom/" + character}}
                moved = {}
                with patch.object(self.mgr, "_remote_voice_sizes", AsyncMock(return_value={1: len(before)})), \
                     patch.object(self.mgr, "_remote_voice_digest", AsyncMock(return_value=hashlib.sha256(before).digest())):
                    complete = await self.mgr._fix_character_routing(None, character, record, moved, {},
                                                                  {"moved": 0, "quarantined": 0})
                self.assertTrue(complete)
                self.assertFalse(target.exists())
                self.assertEqual(self.target(character=character, lang=language).read_bytes(), before)
                self.assertEqual(moved, {character: {"cn": language}})

    async def test_old_text_cache_is_refreshed_for_new_languages(self):
        self.mgr._atomic_write_json(self.mgr._voice_text_path("Operator"),
                                   {"fetchedAt": __import__("time").time(), "texts": {"old": {}}})
        with patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value={"texts": {"new": {}}})) as lookup:
            self.assertEqual(await self.mgr.get_voice_texts("Operator"), {"new": {}})
            lookup.assert_awaited_once()

    async def test_active_lock_not_evicted(self):
        lock = self.mgr._lock_for("Busy")
        async with lock:
            for index in range(500):
                self.mgr._lock_for(f"Other{index}")
            self.assertIs(lock, self.mgr._lock_for("Busy"))
        self.assertLess(len(self.mgr._download_locks), 3)

    def test_backup_failure_preserves_original(self):
        target = write_wav(self.target())
        incoming = write_wav(self.root / "incoming.wav", b"\x03\x04")
        before = target.read_bytes()
        with patch.object(page.shutil, "copy2", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                self.pm._replace_voice_file(incoming, target, "replace")
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(incoming.exists())

    def import_files(self):
        stage = self.pm.preview_dir / "stage"
        staged, targets = {}, {}
        for title in self.titles[:2]:
            targets[title] = write_wav(self.target(title))
            staged[title] = write_wav(stage / f"{title}.wav", b"\x03\x04")
        return stage, staged, targets

    def test_import_failure_rolls_back_all_files(self):
        stage, staged, targets = self.import_files()
        original = {v: p.read_bytes() for v, p in targets.items()}
        real = page.os.replace
        def replace(source, target):
            if Path(source) == staged[self.titles[1]] and Path(target) == targets[self.titles[1]]:
                raise PermissionError("second denied")
            return real(source, target)
        with patch.object(page.os, "replace", replace):
            with self.assertRaises(OSError):
                self.pm._commit_import_files("Operator", "cn", staged)
        self.assertEqual({v: p.read_bytes() for v, p in targets.items()}, original)
        self.assertTrue(all(p.exists() for p in staged.values()))
        self.assertFalse((stage / "recovery.json").exists())

    def test_failed_rollback_keeps_recovery_material(self):
        stage, staged, targets = self.import_files()
        real = page.os.replace
        def replace(source, target):
            if (Path(source) == staged[self.titles[1]] or
                    Path(source) == targets[self.titles[0]]):
                raise PermissionError("rollback denied")
            return real(source, target)
        with patch.object(page.os, "replace", replace):
            with self.assertRaises(OSError):
                self.pm._commit_import_files("Operator", "cn", staged)
        self.assertTrue((stage / "recovery.json").exists())
        self.pm._remove_preview_staging({"stagingDir": str(stage)})
        self.pm._cleanup_operation_previews(remove_orphans=True)
        self.assertTrue(stage.exists())
        manifest = json.loads((stage / "recovery.json").read_text(encoding="utf-8"))
        self.assertTrue(Path(manifest["files"][0]["backup"]).exists())

    def test_import_rechecks_target_signature(self):
        stage, staged, targets = self.import_files()
        entries = [{"voice": v, "action": "overwrite", "targetSignature": self.pm._path_signature(p)}
                   for v, p in targets.items()]
        write_wav(targets[self.titles[0]], b"\x05\x06")
        before = targets[self.titles[0]].read_bytes()
        with self.assertRaises(ValueError):
            self.pm._commit_import_files("Operator", "cn", staged, entries)
        self.assertEqual(targets[self.titles[0]].read_bytes(), before)

    async def test_upload_limit_applies_while_streaming(self):
        upload = page.PluginUploadFile()
        upload.content_length = None
        upload.read = AsyncMock(side_effect=[b"x" * 8, b"x" * 8, b""])
        with self.assertRaises(ValueError):
            await self.pm._save_upload(upload, suffix=".zip", max_bytes=10)
        self.assertEqual(upload.read.await_count, 2)
        self.assertFalse(list(self.pm.upload_dir.iterdir()))

    def test_preview_cap_and_expiry(self):
        self.pm.MAX_OPERATION_PREVIEWS = 8
        with patch.object(page, "request", types.SimpleNamespace(username="tester")):
            for _ in range(12):
                self.pm._issue_operation_preview(action="fetch", payload={}, summary={})
        self.assertEqual(len(self.pm._operation_previews), 8)
        for record in self.pm._operation_previews.values():
            record["expiresEpoch"] = 0
        self.pm._cleanup_operation_previews()
        self.assertFalse(self.pm._operation_previews)

    async def test_download_cancel_removes_temporary_file(self):
        session = types.SimpleNamespace(get=lambda *a, **kw: Response())
        await self.mgr.mutation_lock.acquire()
        task = asyncio.create_task(self.mgr._download_single_voice(
            session, "Operator", "unused", "cn", self.titles[0]))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                if list(self.target().parent.glob("*.tmp")):
                    break
            self.assertTrue(list(self.target().parent.glob("*.tmp")))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(list(self.target().parent.glob("*.tmp")))
        finally:
            self.mgr.mutation_lock.release()

    async def test_download_does_not_overwrite_concurrent_upload(self):
        target = write_wav(self.target())
        await self.mgr.mutation_lock.acquire()
        task = asyncio.create_task(self.mgr._download_single_voice(
            types.SimpleNamespace(get=lambda *a, **kw: Response()), "Operator", "unused",
            "cn", self.titles[0], force_redownload=True))
        for _ in range(20):
            await asyncio.sleep(0)
            if list(target.parent.glob("*.tmp")):
                break
        write_wav(target, b"\x07\x08")
        before = target.read_bytes()
        self.mgr.mutation_lock.release()
        status, _ = await task
        self.assertEqual(status, "failed")
        self.assertEqual(target.read_bytes(), before)

    async def test_cancel_waits_for_file_worker(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def operation():
            started.set()
            release.wait(5)
            finished.set()
        task = asyncio.create_task(self.pm._run_file_operation(operation))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_same_size_different_audio_is_not_migrated(self):
        target = write_wav(self.target())
        before = target.read_bytes()
        record = {**self.record, "paths": {"\u8054\u52a8": "voice/operator"}}
        with patch.object(self.mgr, "_remote_voice_sizes", AsyncMock(return_value={1: len(before)})), \
             patch.object(self.mgr, "_remote_voice_digest", AsyncMock(return_value=hashlib.sha256(wav_bytes(b"\x03\x04")).digest())):
            complete = await self.mgr._fix_character_routing(None, "Operator", record, {}, {},
                                                          {"moved": 0, "quarantined": 0})
        self.assertTrue(complete)
        self.assertEqual(target.read_bytes(), before)

    async def test_verified_audio_is_moved_and_refill_is_durable(self):
        target = write_wav(self.target())
        before = target.read_bytes()
        record = {**self.record, "paths": {"\u8054\u52a8": "voice/operator"}}
        moved = {}
        with patch.object(self.mgr, "_remote_voice_sizes", AsyncMock(return_value={1: len(before)})), \
             patch.object(self.mgr, "_remote_voice_digest", AsyncMock(return_value=hashlib.sha256(before).digest())):
            complete = await self.mgr._fix_character_routing(None, "Operator", record, moved, {},
                                                          {"moved": 0, "quarantined": 0})
        self.assertTrue(complete)
        self.assertFalse(target.exists())
        self.assertEqual(self.target(lang="jp").read_bytes(), before)
        self.assertEqual(moved, {"Operator": {"cn": "jp"}})
        restarted = VM(self.mgr.data_dir, REPO)
        self.assertTrue(restarted._routing_refills)
        self.assertEqual(restarted._routing_moves, moved)

    async def test_unresolved_skin_scope_keeps_migration_pending(self):
        write_wav(self.mgr.voices_dir / "Operator" / "skin" / "Unknown" / "cn" / f"{self.titles[0]}.wav")
        record = {**self.record, "paths": {"\u8054\u52a8(Outfit)": "voice/skin_a"}}
        self.assertFalse(await self.mgr._fix_character_routing(None, "Operator", record, {}, {},
                                                             {"moved": 0, "quarantined": 0}))

    async def test_network_failure_preserves_old_remap_file(self):
        target = write_wav(self.target())
        before = target.read_bytes()
        self.mgr._voice_remap_pending = {("Operator", "cn")}
        with patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=self.record)), \
             patch.object(self.mgr, "_download_with_retries", AsyncMock(return_value=("failed", "HTTP 403"))), \
             patch.object(self.mgr, "fetch_character_image", AsyncMock(return_value=(True, "ok"))):
            ok, _ = await self.mgr.fetch_character_voices("Operator", False, "2", require_no_failures=True)
        self.assertFalse(ok)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.mgr.needs_voice_resource_remap("Operator", "cn"))

    async def test_missing_record_does_not_mark_migration_complete(self):
        write_wav(self.target())
        self.mgr.scan_voice_files()
        with patch.object(self.mgr, "get_voice_records", AsyncMock(return_value={})):
            await self.mgr.migrate_language_routing()
        self.assertLess(self.mgr._language_routing_version, data.constants.LANGUAGE_ROUTING_VERSION)

    async def test_failed_refill_survives_restart(self):
        source = data.prts.voice_sources(self.record)[0]
        self.mgr._queue_routing_refill("Operator", source, self.titles[0], self.mgr.voices_dir / "Operator")
        with patch.object(self.mgr, "_download_with_retries", AsyncMock(return_value=("failed", "offline"))):
            await self.mgr.migrate_language_routing()
        restarted = VM(self.mgr.data_dir, REPO)
        self.assertTrue(restarted._routing_refills)
        self.assertLess(restarted._language_routing_version, data.constants.LANGUAGE_ROUTING_VERSION)
        with patch.object(restarted, "_download_with_retries", AsyncMock(return_value=("downloaded", "ok"))):
            await restarted.migrate_language_routing()
        self.assertFalse(restarted._routing_refills)
        self.assertEqual(restarted._language_routing_version, data.constants.LANGUAGE_ROUTING_VERSION)

    async def test_head_without_length_is_failure(self):
        session = types.SimpleNamespace(head=lambda *a, **kw: Response())
        self.assertIsNone(await self.mgr._remote_voice_sizes(session, "voice/operator", [1]))

    async def test_remap_requests_and_quarantines_obsolete_title(self):
        stale = write_wav(self.target(self.titles[-1]))
        self.mgr._voice_remap_pending = {("Operator", "cn")}
        seen = []
        def get(url, **kw):
            seen.append(url)
            return Response(status=200 if url.endswith("cn_001.wav") else 404)
        with patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=self.record)), \
             patch.object(self.mgr, "fetch_character_image", AsyncMock(return_value=(True, "ok"))), \
             patch.object(data.aiohttp, "ClientSession") as session:
            session.return_value.__aenter__.return_value = types.SimpleNamespace(get=get)
            ok, _ = await self.mgr.fetch_character_voices("Operator", False, "2", require_no_failures=True)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 38)
        self.assertTrue(any(url.endswith("cn_044.wav") for url in seen))
        self.assertFalse(stale.exists())
        self.assertFalse(self.mgr.needs_voice_resource_remap("Operator", "cn"))

    def test_remap_queue_uses_base_names(self):
        write_wav(self.mgr.voices_dir / "Operator" / "skin" / "Outfit" / "cn" / f"{self.titles[0]}.wav")
        self.mgr._voice_resource_map_version = 0
        self.mgr.scan_voice_files()
        self.assertEqual(self.mgr._voice_remap_pending, {("Operator", "cn")})

    async def test_record_cache_merges_single_and_batch_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(session, names):
            entered.set()
            await release.wait()
            return {name: self.record for name in names}
        with patch.object(data.prts, "fetch_voice_records", AsyncMock(side_effect=fetch)) as fetcher:
            batch = asyncio.create_task(self.mgr.get_voice_records(None, ["Operator"]))
            await entered.wait()
            single = asyncio.create_task(self.mgr._get_voice_record("Operator", session=object()))
            release.set()
            records, record = await asyncio.gather(batch, single)
            self.assertEqual(record, records["Operator"])
            self.assertEqual(fetcher.await_count, 1)
        restarted = VM(self.mgr.data_dir, REPO)
        with patch.object(data.prts, "fetch_voice_record", AsyncMock()) as fetcher:
            self.assertEqual(await restarted._get_voice_record("Operator", session=object()), self.record)
            fetcher.assert_not_awaited()

    async def test_avatar_uses_cached_url_and_existing_png(self):
        self.mgr._cache_voice_record("Operator", self.record)
        image = io.BytesIO()
        data.PILImage.new("RGB", (2, 2)).save(image, format="PNG")
        calls = []
        def get(url, **kw):
            calls.append(url)
            return Response(payload=image.getvalue())
        with patch.object(data.prts, "fetch_file_url", AsyncMock()) as lookup:
            session = types.SimpleNamespace(get=get)
            self.assertTrue((await self.mgr.fetch_character_image("Operator", session=session))[0])
            self.assertTrue((await self.mgr.fetch_character_image("Operator", session=session))[0])
            lookup.assert_not_awaited()
        self.assertEqual(calls, [self.record["avatar_url"]])

    def test_plan_reserves_distinct_same_named_skin_directories(self):
        record = {**self.record, "paths": {"\u4e2d\u6587(Outfit)": "voice_cn/skin_a",
                                           "\u65e5\u6587(Outfit)": "voice/skin_b"}}
        plan = self.mgr.build_download_plan("Operator", record, True, "23")
        self.assertEqual(len({item["skin_directory"] for item in plan}), 2)
        for source in data.prts.voice_sources(record):
            self.mgr._register_skin_metadata("Operator", source["label"], source["voice_key"], source["language"])
        for item in plan:
            self.assertEqual(item["skin_directory"], self.mgr.skin_metadata["Operator"][item["resource_id"]]["directory"])

    async def test_preview_uses_upstream_slots_and_new_skin(self):
        record = {**self.record, "paths": {"\u4e2d\u6587": "voice_cn/operator",
                                           "\u4e2d\u6587(Outfit)": "voice_cn/skin_a"}}
        self.pm.default_download_langs = "2"
        self.pm.default_download_skin = True
        request = types.SimpleNamespace(username="tester", json=AsyncMock(return_value={"character": "Operator", "languages": "2", "includeSkin": True}))
        with patch.object(page, "request", request), \
             patch.object(self.mgr, "_get_voice_record", AsyncMock(return_value=record)):
            preview = await self.pm.preview_fetch()
        self.assertEqual(preview["knownSlots"], 2)
        self.assertEqual(preview["knownArchives"], 2)
        self.assertEqual(preview["missing"], 2)
        self.assertFalse(self.mgr.skin_metadata)

    def test_plan_matches_legacy_local_skin_registration(self):
        self.mgr.skin_metadata = {"Operator": {"local_abc": {"name": "Outfit", "directory": "Outfit", "voice_keys": {}}}}
        record = {**self.record, "paths": {"\u4e2d\u6587(Outfit)": "voice_cn/skin_a"}}
        item = self.mgr.build_download_plan("Operator", record, True, "2")[0]
        _, _, directory = self.mgr._register_skin_metadata("Operator", item["label"], item["voice_key"], "cn")
        self.assertEqual(item["skin_directory"], directory)
        self.assertEqual(directory, "Outfit")

    def test_skin_name_compatibility_for_text_lookup_and_long_names(self):
        self.assertEqual(data.prts.skin_name_from_label("(Outfit)", "unused"), "Outfit")
        raw = "Outfit" * 20
        expected = raw[:71] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]
        self.assertEqual(data.prts.skin_name_from_label(f"\u4e2d\u6587({raw})", "unused"), expected)

    async def test_prts_batches_records_and_avatars_together(self):
        text = "|\u8def\u5f84=\u4e2d\u6587:voice_cn/operator\n|\u6807\u98981=" + self.titles[0] + "\n|\u8bed\u97f31=CN_001.wav\n"
        async def query(session, params):
            self.assertEqual(params["prop"], "revisions|imageinfo")
            pages = []
            for title in params["titles"].split("|"):
                if title.endswith(data.prts.VOICE_PAGE_SUFFIX):
                    pages.append({"title": title, "revisions": [{"slots": {"main": {"content": text}}}]})
                else:
                    pages.append({"title": title, "imageinfo": [{"url": "https://prts.wiki/a.png"}]})
            return {"query": {"pages": pages}}
        with patch.object(data.prts, "api_query", AsyncMock(side_effect=query)) as query_mock:
            records = await data.prts.fetch_voice_records(None, [f"Operator{i}" for i in range(21)])
        self.assertEqual(len(records), 21)
        self.assertEqual(query_mock.await_count, 2)
        self.assertTrue(all(r["avatar_url"] for r in records.values()))

    async def test_prts_redirect_and_malformed_page(self):
        text = "|\u8def\u5f84=\u4e2d\u6587:voice_cn/operator\n|\u6807\u98981=" + self.titles[0] + "\n|\u8bed\u97f31=CN_001.wav\n"
        response = {"query": {
            "normalized": [{"from": "alias/" + data.prts.VOICE_PAGE_SUFFIX[1:], "to": "Alias" + data.prts.VOICE_PAGE_SUFFIX}],
            "redirects": [{"from": "Alias" + data.prts.VOICE_PAGE_SUFFIX, "to": "Operator" + data.prts.VOICE_PAGE_SUFFIX}],
            "pages": [{"title": "Operator" + data.prts.VOICE_PAGE_SUFFIX,
                       "revisions": [{"slots": {"main": {"content": text}}}]}]}}
        with patch.object(data.prts, "api_query", AsyncMock(return_value=response)):
            record = await data.prts.fetch_voice_record(None, "alias")
        self.assertEqual(record["character"], "Operator")
        response["query"]["pages"][0].pop("revisions")
        with patch.object(data.prts, "api_query", AsyncMock(return_value=response)):
            with self.assertRaises(data.PRTSLookupError):
                await data.prts.fetch_voice_records(None, ["alias"])

    def test_binding_save_failure_rolls_back_memory(self):
        tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_retarget_bindings")
        namespace = {"Dict": dict, "logger": api.logger}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), namespace)
        binding = {"character": "Operator", "voice": self.titles[0], "lang": "cn"}
        plugin = types.SimpleNamespace(custom_mappings={"trigger": binding}, voice_mgr=self.mgr,
                                       _save_custom_commands=lambda: False)
        with patch.object(self.mgr, "get_voice_path", side_effect=lambda c, v, lang: self.root if lang == "jp" else None):
            self.assertFalse(namespace["_retarget_bindings"](plugin, {"Operator": {"cn": "jp"}}))
        self.assertEqual(binding["lang"], "cn")


if __name__ == "__main__":
    unittest.main()
