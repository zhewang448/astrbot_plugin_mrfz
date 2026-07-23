from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)


PLUGIN_NAME = "astrbot_plugin_mrfz"
PAGE_PREFIX = f"/{PLUGIN_NAME}/page"


class VoicePageManager:
    """AstrBot Plugin Page backend for safe voice archive management."""

    MAX_PREVIEW_BYTES = 12 * 1024 * 1024
    MAX_UPLOAD_BYTES = 24 * 1024 * 1024
    MAX_IMPORT_BYTES = 220 * 1024 * 1024
    MAX_IMPORT_MEMBERS = 160
    MAX_AUDIT_ITEMS = 500
    MAX_TASK_ITEMS = 100
    MAX_OPERATION_PREVIEWS = 8
    OPERATION_PREVIEW_TTL = 15 * 60
    _TRASH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
    _PREVIEW_ID_RE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(
        self,
        *,
        context,
        voice_mgr,
        custom_mappings: Dict[str, dict],
        save_custom_commands: Callable[[], bool],
        scan_callback: Callable[[bool], Awaitable[None]],
        valid_trigger: Callable[[object], bool],
        default_language_rank: str,
        default_download_langs: str,
        default_download_skin: bool,
    ) -> None:
        self.context = context
        self.voice_mgr = voice_mgr
        self.custom_mappings = custom_mappings
        self.save_custom_commands = save_custom_commands
        self.scan_callback = scan_callback
        self.valid_trigger = valid_trigger
        self.default_language_rank = str(default_language_rank)
        self.default_download_langs = str(default_download_langs)
        self.default_download_skin = bool(default_download_skin)

        self.data_dir = Path(self.voice_mgr.data_dir)
        self.voices_dir = Path(self.voice_mgr.voices_dir)
        self.page_dir = self.data_dir / "page_manager"
        self.trash_dir = self.page_dir / "trash"
        self.backup_dir = self.page_dir / "backups"
        self.export_dir = self.page_dir / "exports"
        self.upload_dir = self.page_dir / "uploads"
        self.preview_dir = self.page_dir / "previews"
        self.audit_file = self.page_dir / "audit.jsonl"
        self.integrity_file = self.page_dir / "integrity_report.json"

        for directory in (
            self.page_dir,
            self.trash_dir,
            self.backup_dir,
            self.export_dir,
            self.upload_dir,
            self.preview_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._mutation_lock = asyncio.Lock()
        self._audit_lock = asyncio.Lock()
        self._fetch_semaphore = asyncio.Semaphore(2)
        self._tasks: Dict[str, dict] = {}
        self._task_handles: Dict[str, asyncio.Task] = {}
        self._operation_previews: Dict[str, dict] = {}
        self._latest_integrity: dict = self._load_integrity_report()
        self._cleanup_operation_previews(remove_orphans=True)
        self._register_routes()

    def _register_routes(self) -> None:
        routes = [
            ("/overview", self.overview, ["GET"], "Voice archive overview"),
            ("/archives", self.archives, ["GET"], "List voice archives"),
            ("/archive", self.archive_detail, ["GET"], "Voice archive details"),
            ("/audio", self.audio_preview, ["GET"], "Preview a voice file"),
            ("/export", self.export_archive, ["GET"], "Export voice files"),
            (
                "/replace/<token>",
                self.replace_voice,
                ["POST"],
                "Replace one voice file",
            ),
            (
                "/import/<token>",
                self.import_archive,
                ["POST"],
                "Import a voice ZIP",
            ),
            (
                "/import/preview/<token>",
                self.preview_import,
                ["POST"],
                "Preview a voice ZIP import",
            ),
            (
                "/import/commit",
                self.commit_import,
                ["POST"],
                "Commit a previewed voice ZIP import",
            ),
            ("/remove", self.remove_voice, ["POST"], "Move a voice to trash"),
            (
                "/remove/batch/preview",
                self.preview_batch_remove,
                ["POST"],
                "Preview moving voice files to trash",
            ),
            (
                "/remove/batch",
                self.batch_remove,
                ["POST"],
                "Move previewed voice files to trash",
            ),
            ("/trash", self.trash_items, ["GET"], "List recoverable files"),
            ("/restore", self.restore_voice, ["POST"], "Restore a trashed voice"),
            ("/purge", self.purge_voice, ["POST"], "Permanently delete trash"),
            (
                "/fetch/preview",
                self.preview_fetch,
                ["POST"],
                "Preview a PRTS download task",
            ),
            ("/fetch", self.start_fetch, ["POST"], "Start a PRTS download task"),
            (
                "/preview/discard",
                self.discard_preview,
                ["POST"],
                "Discard a pending operation preview",
            ),
            ("/tasks", self.tasks, ["GET"], "List Page background tasks"),
            (
                "/task/cancel",
                self.cancel_task,
                ["POST"],
                "Cancel a Page background task",
            ),
            ("/rescan", self.rescan, ["POST"], "Rebuild the voice index"),
            (
                "/integrity",
                self.integrity,
                ["GET", "POST"],
                "Run or read an integrity check",
            ),
            ("/bindings", self.bindings, ["GET"], "List custom voice bindings"),
            (
                "/bindings/save",
                self.save_binding,
                ["POST"],
                "Create or update a voice binding",
            ),
            (
                "/bindings/remove",
                self.remove_binding,
                ["POST"],
                "Remove a voice binding",
            ),
            ("/audit", self.audit, ["GET"], "Read Page audit records"),
        ]

        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"{PAGE_PREFIX}{suffix}",
                handler,
                methods,
                description,
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _safe_filename(value: str, fallback: str = "voice") -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
        return (value[:100] or fallback).strip()

    @staticmethod
    def _encode_token(payload: dict) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_token(token: str) -> dict:
        if not isinstance(token, str) or not token or len(token) > 1200:
            raise ValueError("无效的上传目标")

        try:
            padding = "=" * (-len(token) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(token + padding).decode("utf-8")
            )
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("无效的上传目标") from exc

        if not isinstance(payload, dict):
            raise ValueError("无效的上传目标")

        return payload

    @staticmethod
    def _path_signature(path: Path) -> Optional[tuple[int, int]]:
        try:
            stat = path.stat()
        except OSError:
            return None

        if not path.is_file():
            return None

        return stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _files_equal(first: Path, second: Path) -> bool:
        try:
            if first.stat().st_size != second.stat().st_size:
                return False

            with first.open("rb") as first_file, second.open("rb") as second_file:
                while True:
                    first_chunk = first_file.read(1024 * 1024)
                    second_chunk = second_file.read(1024 * 1024)

                    if first_chunk != second_chunk:
                        return False

                    if not first_chunk:
                        return True

        except OSError:
            return False

    def _cleanup_operation_previews(self, *, remove_orphans: bool = False) -> None:
        now = time.time()
        expired = [
            preview_id
            for preview_id, record in self._operation_previews.items()
            if float(record.get("expiresEpoch", 0)) <= now
        ]

        for preview_id in expired:
            record = self._operation_previews.pop(preview_id, None)
            self._remove_preview_staging(record)

        if remove_orphans:
            active_staging = {
                Path(record["stagingDir"]).resolve()
                for record in self._operation_previews.values()
                if record.get("stagingDir")
            }

            try:
                paths = list(self.preview_dir.iterdir())
            except OSError:
                paths = []

            for path in paths:
                try:
                    if path.resolve() not in active_staging:
                        shutil.rmtree(path)
                except OSError:
                    continue

        if len(self._operation_previews) <= self.MAX_OPERATION_PREVIEWS:
            return

        oldest = sorted(
            self._operation_previews,
            key=lambda current: float(
                self._operation_previews[current].get("createdEpoch", 0)
            ),
        )

        for preview_id in oldest[
            : len(self._operation_previews) - self.MAX_OPERATION_PREVIEWS
        ]:
            record = self._operation_previews.pop(preview_id, None)
            self._remove_preview_staging(record)

    def _remove_preview_staging(self, record: Optional[dict]) -> None:
        if not record or not record.get("stagingDir"):
            return

        path = Path(record["stagingDir"])

        try:
            if self._path_within(path, self.preview_dir) and path != self.preview_dir:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def _issue_operation_preview(
        self,
        *,
        action: str,
        payload: dict,
        summary: dict,
        staging_dir: Optional[Path] = None,
        preview_id: Optional[str] = None,
    ) -> dict:
        self._cleanup_operation_previews()
        preview_id = preview_id or uuid4().hex

        if not self._PREVIEW_ID_RE.fullmatch(preview_id):
            raise ValueError("无法创建操作预览")

        now = time.time()
        expires_epoch = now + self.OPERATION_PREVIEW_TTL
        record = {
            "id": preview_id,
            "action": action,
            "username": request.username or "dashboard",
            "createdEpoch": now,
            "expiresEpoch": expires_epoch,
            "payload": payload,
            "summary": summary,
            "stagingDir": str(staging_dir) if staging_dir else None,
        }
        self._operation_previews[preview_id] = record
        self._cleanup_operation_previews()

        return {
            **summary,
            "previewToken": preview_id,
            "expiresAt": datetime.fromtimestamp(
                expires_epoch,
                timezone.utc,
            ).isoformat(timespec="seconds"),
        }

    def _take_operation_preview(self, token: Any, action: str) -> dict:
        self._cleanup_operation_previews()
        token = str(token or "")

        if not self._PREVIEW_ID_RE.fullmatch(token):
            raise ValueError("操作预览无效或已过期，请重新预览")

        record = self._operation_previews.pop(token, None)

        if (
            record is None
            or record.get("action") != action
            or record.get("username") != (request.username or "dashboard")
        ):
            self._remove_preview_staging(record)
            raise ValueError("操作预览无效或已过期，请重新预览")

        return record

    async def discard_preview(self):
        payload = await request.json(default={})
        token = payload.get("previewToken") if isinstance(payload, dict) else None
        token = str(token or "")

        if not self._PREVIEW_ID_RE.fullmatch(token):
            return json_response({"discarded": False})

        record = self._operation_previews.get(token)

        if record and record.get("username") == (request.username or "dashboard"):
            self._operation_previews.pop(token, None)
            self._remove_preview_staging(record)
            return json_response({"discarded": True})

        return json_response({"discarded": False})

    def _path_within(self, path: Path, root: Path) -> bool:
        return bool(self.voice_mgr._path_is_within(path, root))

    def _canonical_character(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("缺少档案名称")

        resolved, options = self.voice_mgr.resolve_character_reference(value.strip())

        if resolved:
            return resolved

        if options:
            raise ValueError("皮肤名称不明确，请从档案列表重新选择")

        raise ValueError("未找到该语音档案")

    def _skin_package(
        self,
        character: str,
    ) -> tuple[str, str, str, str]:
        parsed = self.voice_mgr._parse_character_reference(character)

        if not parsed or not parsed[1]:
            raise ValueError("目标不是皮肤语音档案")

        base, _, selector = parsed

        for resource_id in self.voice_mgr.skin_voice_index.get(base, {}):
            info = self.voice_mgr.skin_metadata.get(base, {}).get(resource_id, {})
            reference = self.voice_mgr._skin_reference(base, resource_id)
            reference_parsed = (
                self.voice_mgr._parse_character_reference(reference)
                if reference
                else None
            )
            reference_selector = reference_parsed[2] if reference_parsed else None
            aliases = {
                resource_id,
                str(info.get("name", "")).strip(),
                str(info.get("directory", "")).strip(),
                reference_selector,
            }

            if selector in aliases or character == reference:
                directory = str(info.get("directory", "")).strip()
                skin_name = str(info.get("name", "")).strip() or directory

                if not self.voice_mgr._is_safe_component(
                    directory,
                    self.voice_mgr.MAX_SKIN_ID_LENGTH,
                ):
                    break

                return base, resource_id, skin_name, directory

        raise ValueError("皮肤语音档案已失效，请重新扫描")

    def _own_voice_path(
        self,
        character: str,
        language: str,
        voice: str,
    ) -> Path:
        if language not in self.voice_mgr.LANGUAGE_MAP:
            raise ValueError("语言代码无效")

        if voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
            raise ValueError("语音类型无效")

        parsed = self.voice_mgr._parse_character_reference(character)

        if not parsed:
            raise ValueError("档案名称无效")

        base, is_skin, _ = parsed
        character_root = self.voice_mgr._safe_path(self.voices_dir, base)

        if character_root is None:
            raise ValueError("档案路径无效")

        if is_skin:
            _, _, _, directory = self._skin_package(character)
            target = self.voice_mgr._safe_path(
                character_root,
                "skin",
                directory,
                language,
                f"{voice}.wav",
            )
        else:
            target = self.voice_mgr._safe_path(
                character_root,
                language,
                f"{voice}.wav",
            )

        if target is None or not self._path_within(target, self.voices_dir):
            raise ValueError("档案路径越界")

        return target

    def _fallback_voice_path(
        self,
        character: str,
        language: str,
        voice: str,
    ) -> Optional[Path]:
        parsed = self.voice_mgr._parse_character_reference(character)

        if not parsed or not parsed[1]:
            return None

        candidate = self.voice_mgr._safe_path(
            self.voices_dir,
            parsed[0],
            language,
            f"{voice}.wav",
        )

        if candidate is None or not self._path_within(candidate, self.voices_dir):
            return None

        return candidate

    @staticmethod
    def _file_metadata(path: Path) -> dict:
        stat = path.stat()
        return {
            "bytes": stat.st_size,
            "updatedAt": datetime.fromtimestamp(
                stat.st_mtime,
                timezone.utc,
            ).isoformat(timespec="seconds"),
        }

    def _voice_status(
        self,
        character: str,
        language: str,
        voice: str,
    ) -> dict:
        own = self._own_voice_path(character, language, voice)

        try:
            own_exists = own.is_file()
            own_valid = own_exists and self.voice_mgr._is_valid_wav_file(own)
        except OSError:
            own_exists = False
            own_valid = False

        if own_valid:
            return {
                "voice": voice,
                "status": "own",
                "source": "当前档案",
                "deletable": True,
                "replaceable": True,
                **self._file_metadata(own),
            }

        if own_exists:
            return {
                "voice": voice,
                "status": "damaged",
                "source": "当前档案（损坏）",
                "deletable": True,
                "replaceable": True,
                **self._file_metadata(own),
            }

        fallback = self._fallback_voice_path(character, language, voice)

        try:
            fallback_valid = bool(
                fallback
                and fallback.is_file()
                and self.voice_mgr._is_valid_wav_file(fallback)
            )
        except OSError:
            fallback_valid = False

        if fallback_valid and fallback is not None:
            return {
                "voice": voice,
                "status": "fallback",
                "source": "基础语音回退",
                "deletable": False,
                "replaceable": True,
                **self._file_metadata(fallback),
            }

        return {
            "voice": voice,
            "status": "missing",
            "source": "缺失",
            "deletable": False,
            "replaceable": True,
            "bytes": 0,
            "updatedAt": None,
        }

    def _archive_summary(
        self,
        character: str,
        *,
        resource_id: Optional[str] = None,
    ) -> dict:
        parsed = self.voice_mgr._parse_character_reference(character)

        if not parsed:
            raise ValueError("档案名称无效")

        base, is_skin, _ = parsed
        skin_name = None
        own_voice_count = 0
        total_bytes = 0
        latest_mtime = 0.0

        if is_skin:
            base, resource_id, skin_name, directory = self._skin_package(character)
            package = self.voice_mgr.skin_voice_index.get(base, {}).get(
                resource_id,
                {},
            )
            archive_root = self.voice_mgr._safe_path(
                self.voices_dir,
                base,
                "skin",
                directory,
            )
        else:
            package = self.voice_mgr.voice_files.get(character, {})
            archive_root = self.voice_mgr._safe_path(self.voices_dir, base)

        for voices in package.values():
            own_voice_count += len(voices)

        if archive_root is not None and archive_root.is_dir():
            try:
                if is_skin:
                    paths = archive_root.rglob("*.wav")
                else:
                    paths = (
                        path
                        for language in self.voice_mgr.LANGUAGE_MAP
                        for path in (archive_root / language).glob("*.wav")
                    )

                for path in paths:
                    if not path.is_file() or not self._path_within(
                        path,
                        archive_root,
                    ):
                        continue
                    stat = path.stat()
                    total_bytes += stat.st_size
                    latest_mtime = max(latest_mtime, stat.st_mtime)
            except OSError:
                pass

        languages = list(self.voice_mgr.voice_index.get(character, []))
        playable_voice_count = sum(
            len(self.voice_mgr.get_available_voices(character, language))
            for language in languages
        )

        return {
            "id": self._encode_token({"character": character}),
            "character": character,
            "base": base,
            "kind": "skin" if is_skin else "operator",
            "skinName": skin_name,
            "resourceId": resource_id,
            "languages": languages,
            "languageNames": [
                self.voice_mgr.LANGUAGE_MAP[language]["name"]
                for language in languages
                if language in self.voice_mgr.LANGUAGE_MAP
            ],
            "ownVoiceCount": own_voice_count,
            "playableVoiceCount": playable_voice_count,
            "bytes": total_bytes,
            "updatedAt": (
                datetime.fromtimestamp(
                    latest_mtime,
                    timezone.utc,
                ).isoformat(timespec="seconds")
                if latest_mtime
                else None
            ),
        }

    def _all_archives(self) -> list[dict]:
        result = []

        for character in sorted(self.voice_mgr.voice_files):
            parsed = self.voice_mgr._parse_character_reference(character)

            if not parsed or parsed[1]:
                continue

            result.append(self._archive_summary(character))

        for base in sorted(self.voice_mgr.skin_voice_index):
            for resource_id in sorted(self.voice_mgr.skin_voice_index[base]):
                reference = self.voice_mgr._skin_reference(base, resource_id)

                if not reference:
                    continue

                result.append(
                    self._archive_summary(
                        reference,
                        resource_id=resource_id,
                    )
                )

        return result

    def _storage_stats(self) -> dict:
        total_bytes = 0
        wav_files = 0

        try:
            paths = self.voices_dir.rglob("*.wav")
        except OSError:
            paths = []

        for path in paths:
            try:
                if path.is_file() and self._path_within(path, self.voices_dir):
                    wav_files += 1
                    total_bytes += path.stat().st_size
            except OSError:
                continue

        trash_items = len(self._list_trash_items())
        return {
            "bytes": total_bytes,
            "wavFiles": wav_files,
            "trashItems": trash_items,
        }

    async def _audit(
        self,
        action: str,
        target: str,
        *,
        details: Optional[dict] = None,
        username: Optional[str] = None,
    ) -> None:
        entry = {
            "id": uuid4().hex,
            "time": self._utc_now(),
            "username": username or request.username or "dashboard",
            "action": action,
            "target": target,
            "details": details or {},
        }

        async with self._audit_lock:
            try:
                with self.audit_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                logger.warning(f"写入语音管理审计日志失败: {exc}")

    def _read_audit(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), self.MAX_AUDIT_ITEMS))

        if not self.audit_file.is_file():
            return []

        records = []

        try:
            with self.audit_file.open("r", encoding="utf-8") as handle:
                lines = deque(handle, maxlen=limit)
        except OSError:
            return []

        for line in reversed(lines):
            try:
                item = json.loads(line)

                if isinstance(item, dict):
                    records.append(item)
            except json.JSONDecodeError:
                continue

        return records

    def _load_integrity_report(self) -> dict:
        if not self.integrity_file.is_file():
            return {}

        try:
            with self.integrity_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def overview(self):
        await self.scan_callback(False)
        archives = self._all_archives()
        storage = self._storage_stats()
        operators = sum(item["kind"] == "operator" for item in archives)
        skins = sum(item["kind"] == "skin" for item in archives)
        languages = sorted(
            {
                language
                for item in archives
                for language in item.get("languages", [])
            }
        )
        return json_response(
            {
                "version": "3.7.1",
                "operators": operators,
                "skins": skins,
                "languages": languages,
                "languageCount": len(languages),
                "voiceTypes": len(self.voice_mgr.VOICE_DESCRIPTIONS),
                "bindings": len(self.custom_mappings),
                "storage": storage,
                "runningTasks": sum(
                    item.get("status") in {"queued", "running"}
                    for item in self._tasks.values()
                ),
                "integrity": self._latest_integrity,
                "recentAudit": self._read_audit(8),
            }
        )

    async def archives(self):
        await self.scan_callback(False)
        query = str(request.query.get("q", "")).strip().casefold()
        kind = str(request.query.get("kind", "all")).strip()
        language = str(request.query.get("language", "all")).strip()
        items = self._all_archives()

        if query:
            items = [
                item
                for item in items
                if query
                in " ".join(
                    [
                        item["character"],
                        item["base"],
                        item.get("skinName") or "",
                    ]
                ).casefold()
            ]

        if kind in {"operator", "skin"}:
            items = [item for item in items if item["kind"] == kind]

        if language in self.voice_mgr.LANGUAGE_MAP:
            items = [
                item for item in items if language in item.get("languages", [])
            ]

        return json_response(
            {
                "items": items,
                "total": len(items),
                "languages": [
                    {
                        "code": code,
                        "name": info["name"],
                    }
                    for code, info in self.voice_mgr.LANGUAGE_MAP.items()
                ],
            }
        )

    async def archive_detail(self):
        await self.scan_callback(False)

        try:
            character = self._canonical_character(request.query.get("character"))
        except ValueError as exc:
            return error_response(str(exc), status_code=404)

        languages = list(self.voice_mgr.voice_index.get(character, []))
        requested_language = str(request.query.get("language", "")).strip().lower()
        language = (
            requested_language
            if requested_language in self.voice_mgr.LANGUAGE_MAP
            else (languages[0] if languages else "cn")
        )
        summary = self._archive_summary(character)

        if language is None:
            voices = []
        else:
            voices = [
                self._voice_status(character, language, voice)
                for voice in self.voice_mgr.VOICE_DESCRIPTIONS
            ]

        for item in voices:
            item["replaceToken"] = self._encode_token(
                {
                    "character": character,
                    "language": language,
                    "voice": item["voice"],
                }
            )

        import_token = (
            self._encode_token(
                {
                    "character": character,
                    "language": language,
                }
            )
            if language
            else None
        )
        return json_response(
            {
                **summary,
                "language": language,
                "availableLanguages": languages,
                "voices": voices,
                "importToken": import_token,
                "voiceTypeCount": len(self.voice_mgr.VOICE_DESCRIPTIONS),
            }
        )

    async def audio_preview(self):
        try:
            character = self._canonical_character(request.query.get("character"))
            language = str(request.query.get("language", "")).strip().lower()
            voice = str(request.query.get("voice", "")).strip()
            path = self.voice_mgr.get_voice_path(character, voice, language)

            if path is None:
                raise ValueError("语音文件不存在")

            size = path.stat().st_size

            if size > self.MAX_PREVIEW_BYTES:
                return error_response(
                    "文件过大，无法在线预览，请直接下载",
                    status_code=413,
                )

            encoded = await asyncio.to_thread(
                lambda: base64.b64encode(path.read_bytes()).decode("ascii")
            )
            return json_response(
                {
                    "mime": "audio/wav",
                    "base64": encoded,
                    "bytes": size,
                    "filename": self._safe_filename(
                        f"{character}-{language}-{voice}.wav"
                    ),
                }
            )
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=404)

    def _build_export(
        self,
        character: str,
        language: str,
    ) -> Path:
        self._cleanup_old_exports()
        safe_base = self._safe_filename(character, "archive")
        output = self.export_dir / f"{safe_base}-{language}-{uuid4().hex[:8]}.zip"
        manifest = {
            "plugin": PLUGIN_NAME,
            "createdAt": self._utc_now(),
            "character": character,
            "language": language,
            "voices": [],
        }

        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for voice in self.voice_mgr.VOICE_DESCRIPTIONS:
                status = self._voice_status(character, language, voice)

                if status["status"] not in {"own", "fallback"}:
                    continue

                path = self.voice_mgr.get_voice_path(character, voice, language)

                if path is None:
                    continue

                folder = "fallback" if status["status"] == "fallback" else "voices"
                archive.write(path, f"{folder}/{voice}.wav")
                manifest["voices"].append(
                    {
                        "voice": voice,
                        "source": status["status"],
                        "path": f"{folder}/{voice}.wav",
                    }
                )

            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        return output

    def _cleanup_old_exports(self) -> None:
        cutoff = time.time() - 24 * 60 * 60

        try:
            paths = list(self.export_dir.glob("*.zip"))
        except OSError:
            return

        for path in paths:
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    async def export_archive(self):
        try:
            character = self._canonical_character(request.query.get("character"))
            language = str(request.query.get("language", "")).strip().lower()
            voice = str(request.query.get("voice", "")).strip()

            if language not in self.voice_mgr.LANGUAGE_MAP:
                raise ValueError("语言代码无效")

            if voice:
                if voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
                    raise ValueError("语音类型无效")

                path = self.voice_mgr.get_voice_path(character, voice, language)

                if path is None:
                    raise ValueError("语音文件不存在")

                filename = self._safe_filename(
                    f"{character}-{language}-{voice}.wav"
                )
                await self._audit(
                    "export_voice",
                    f"{character}/{language}/{voice}",
                )
                return file_response(
                    path,
                    filename=filename,
                    content_type="audio/wav",
                )

            output = await asyncio.to_thread(
                self._build_export,
                character,
                language,
            )
            await self._audit(
                "export_archive",
                f"{character}/{language}",
            )
            return file_response(
                output,
                filename=self._safe_filename(f"{character}-{language}.zip"),
                content_type="application/zip",
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return error_response(str(exc), status_code=400)

    def _backup_existing(self, target: Path, reason: str) -> Optional[Path]:
        if not target.is_file():
            return None

        relative = target.resolve().relative_to(self.voices_dir.resolve())
        backup_root = self.backup_dir / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + f"-{reason}-{uuid4().hex[:8]}"
        )
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        return destination

    async def _save_upload(
        self,
        upload: PluginUploadFile,
        *,
        suffix: str,
        max_bytes: int,
    ) -> Path:
        if upload.content_length is not None and upload.content_length > max_bytes:
            raise ValueError("上传文件超过大小限制")

        fd, temp_name = tempfile.mkstemp(
            prefix="upload-",
            suffix=suffix,
            dir=str(self.upload_dir),
        )
        os.close(fd)
        path = Path(temp_name)

        try:
            await upload.save(path)

            if path.stat().st_size > max_bytes:
                raise ValueError("上传文件超过大小限制")

            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    async def replace_voice(self, token: str):
        temp_path = None

        try:
            payload = self._decode_token(token)
            character = self._canonical_character(payload.get("character"))
            language = str(payload.get("language", "")).strip().lower()
            voice = str(payload.get("voice", "")).strip()
            target = self._own_voice_path(character, language, voice)
            files = await request.files()
            upload = files.get("file")

            if not isinstance(upload, PluginUploadFile):
                raise ValueError("请选择 WAV 文件")

            if Path(upload.filename or "").suffix.lower() != ".wav":
                raise ValueError("仅支持 WAV 文件")

            temp_path = await self._save_upload(
                upload,
                suffix=".wav",
                max_bytes=self.MAX_UPLOAD_BYTES,
            )

            if not self.voice_mgr._is_valid_wav_file(temp_path):
                raise ValueError("WAV 文件头无效或文件为空")

            async with self._mutation_lock:
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = self._backup_existing(target, "replace")
                os.replace(temp_path, target)
                temp_path = None

            await self.scan_callback(True)
            await self._audit(
                "replace_voice",
                f"{character}/{language}/{voice}",
                details={
                    "backup": bool(backup),
                    "bytes": target.stat().st_size,
                },
            )
            return json_response(
                {
                    "saved": True,
                    "character": character,
                    "language": language,
                    "voice": voice,
                    "backupCreated": bool(backup),
                }
            )
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _stage_import(
        self,
        zip_path: Path,
        staging_dir: Path,
    ) -> dict[str, Path]:
        if not zipfile.is_zipfile(zip_path):
            raise ValueError("上传文件不是有效 ZIP")

        staged: dict[str, Path] = {}

        with zipfile.ZipFile(zip_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]

            if len(members) > self.MAX_IMPORT_MEMBERS:
                raise ValueError("ZIP 文件条目过多")

            total_size = sum(item.file_size for item in members)

            if total_size > self.MAX_IMPORT_BYTES:
                raise ValueError("ZIP 解压后体积超过限制")

            for member in members:
                if member.flag_bits & 0x1:
                    raise ValueError("不支持加密 ZIP")

                source_name = Path(member.filename.replace("\\", "/")).name

                if Path(source_name).suffix.lower() != ".wav":
                    continue

                voice = Path(source_name).stem

                if voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
                    continue

                if voice in staged:
                    raise ValueError(f"ZIP 中存在重复语音：{voice}")

                if member.file_size > self.MAX_UPLOAD_BYTES:
                    raise ValueError(f"单个语音文件过大：{voice}")

                target = staging_dir / f"{voice}.wav"

                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)

                if not self.voice_mgr._is_valid_wav_file(target):
                    raise ValueError(f"WAV 文件无效：{voice}")

                staged[voice] = target

        if not staged:
            raise ValueError("ZIP 中没有可识别的语音 WAV")

        return staged

    async def preview_import(self, token: str):
        zip_path = None
        staging_dir = None

        try:
            payload = self._decode_token(token)
            character = self._canonical_character(payload.get("character"))
            language = str(payload.get("language", "")).strip().lower()

            if language not in self.voice_mgr.LANGUAGE_MAP:
                raise ValueError("语言代码无效")

            files = await request.files()
            upload = files.get("file")

            if not isinstance(upload, PluginUploadFile):
                raise ValueError("请选择 ZIP 文件")

            if Path(upload.filename or "").suffix.lower() != ".zip":
                raise ValueError("仅支持 ZIP 文件")

            zip_path = await self._save_upload(
                upload,
                suffix=".zip",
                max_bytes=self.MAX_IMPORT_BYTES,
            )
            preview_id = uuid4().hex
            staging_dir = self.preview_dir / preview_id
            staging_dir.mkdir(parents=True)
            staged = await asyncio.to_thread(
                self._stage_import,
                zip_path,
                staging_dir,
            )
            entries = []
            added = 0
            overwritten = 0
            skipped = 0
            incoming_bytes = 0
            backup_bytes = 0

            for voice, staged_path in staged.items():
                target = self._own_voice_path(character, language, voice)
                staged_bytes = staged_path.stat().st_size
                target_signature = self._path_signature(target)
                incoming_bytes += staged_bytes

                if target_signature is None:
                    action = "add"
                    added += 1
                elif await asyncio.to_thread(
                    self._files_equal,
                    staged_path,
                    target,
                ):
                    action = "skip"
                    skipped += 1
                else:
                    action = "overwrite"
                    overwritten += 1
                    backup_bytes += target_signature[0]

                entries.append(
                    {
                        "voice": voice,
                        "action": action,
                        "targetSignature": target_signature,
                        "incomingBytes": staged_bytes,
                    }
                )

            summary = {
                "action": "import",
                "title": f"导入 {character} / {self.voice_mgr.LANGUAGE_MAP[language]['name']}",
                "character": character,
                "language": language,
                "total": len(entries),
                "added": added,
                "overwritten": overwritten,
                "skipped": skipped,
                "incomingBytes": incoming_bytes,
                "backupBytes": backup_bytes,
                "warnings": (
                    [
                        "同名文件会在覆盖前备份；"
                        "内容完全相同的文件不会重复写入。"
                    ]
                    if overwritten
                    else ["内容完全相同的文件不会重复写入。"]
                ),
                "sample": [
                    {
                        "voice": entry["voice"],
                        "action": entry["action"],
                    }
                    for entry in entries[:12]
                ],
            }
            result = self._issue_operation_preview(
                action="import",
                payload={
                    "character": character,
                    "language": language,
                    "entries": entries,
                },
                summary=summary,
                staging_dir=staging_dir,
                preview_id=preview_id,
            )
            staging_dir = None
            return json_response(result)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            if zip_path is not None:
                zip_path.unlink(missing_ok=True)

            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)

    async def commit_import(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        record = None

        try:
            record = self._take_operation_preview(
                payload.get("previewToken"),
                "import",
            )
            operation = record["payload"]
            character = self._canonical_character(operation.get("character"))
            language = str(operation.get("language", "")).strip().lower()
            staging_dir = Path(record["stagingDir"])
            entries = operation.get("entries", [])

            if (
                language not in self.voice_mgr.LANGUAGE_MAP
                or not self._path_within(staging_dir, self.preview_dir)
                or not isinstance(entries, list)
            ):
                raise ValueError("操作预览内容无效，请重新预览")

            plan = []

            for entry in entries:
                voice = str(entry.get("voice", ""))
                action = str(entry.get("action", ""))
                staged_path = staging_dir / f"{voice}.wav"
                target = self._own_voice_path(character, language, voice)
                expected_signature = entry.get("targetSignature")
                current_signature = self._path_signature(target)

                if expected_signature is not None:
                    expected_signature = tuple(expected_signature)

                if current_signature != expected_signature:
                    raise ValueError("档案状态在预览后发生变化，请重新预览")

                if not staged_path.is_file() or action not in {
                    "add",
                    "overwrite",
                    "skip",
                }:
                    raise ValueError("预览文件已失效，请重新预览")

                if action == "skip" and not await asyncio.to_thread(
                    self._files_equal,
                    staged_path,
                    target,
                ):
                    raise ValueError("档案状态在预览后发生变化，请重新预览")

                plan.append((voice, action, staged_path, target))

            backups = 0
            imported = 0
            skipped = 0

            async with self._mutation_lock:
                for _, action, staged_path, target in plan:
                    if action == "skip":
                        skipped += 1
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)

                    if action == "overwrite":
                        if self._backup_existing(target, "import") is not None:
                            backups += 1

                    os.replace(staged_path, target)
                    imported += 1

            if imported:
                await self.scan_callback(True)

            await self._audit(
                "import_archive",
                f"{character}/{language}",
                details={
                    "imported": imported,
                    "backups": backups,
                    "skipped": skipped,
                    "previewed": True,
                },
            )
            return json_response(
                {
                    "imported": imported,
                    "backups": backups,
                    "skipped": skipped,
                    "character": character,
                    "language": language,
                }
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            self._remove_preview_staging(record)

    async def import_archive(self, token: str):
        zip_path = None

        try:
            payload = self._decode_token(token)
            character = self._canonical_character(payload.get("character"))
            language = str(payload.get("language", "")).strip().lower()

            if language not in self.voice_mgr.LANGUAGE_MAP:
                raise ValueError("语言代码无效")

            files = await request.files()
            upload = files.get("file")

            if not isinstance(upload, PluginUploadFile):
                raise ValueError("请选择 ZIP 文件")

            if Path(upload.filename or "").suffix.lower() != ".zip":
                raise ValueError("仅支持 ZIP 文件")

            zip_path = await self._save_upload(
                upload,
                suffix=".zip",
                max_bytes=self.MAX_IMPORT_BYTES,
            )

            with tempfile.TemporaryDirectory(
                prefix="stage-",
                dir=str(self.upload_dir),
            ) as stage_name:
                staged = await asyncio.to_thread(
                    self._stage_import,
                    zip_path,
                    Path(stage_name),
                )
                backups = 0

                async with self._mutation_lock:
                    for voice, staged_path in staged.items():
                        target = self._own_voice_path(
                            character,
                            language,
                            voice,
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)

                        if self._backup_existing(target, "import") is not None:
                            backups += 1

                        os.replace(staged_path, target)

            await self.scan_callback(True)
            await self._audit(
                "import_archive",
                f"{character}/{language}",
                details={
                    "imported": len(staged),
                    "backups": backups,
                },
            )
            return json_response(
                {
                    "imported": len(staged),
                    "backups": backups,
                    "character": character,
                    "language": language,
                }
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            if zip_path is not None:
                zip_path.unlink(missing_ok=True)

    async def remove_voice(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        try:
            character = self._canonical_character(payload.get("character"))
            language = str(payload.get("language", "")).strip().lower()
            voice = str(payload.get("voice", "")).strip()
            target = self._own_voice_path(character, language, voice)

            if not target.is_file():
                fallback = self._fallback_voice_path(
                    character,
                    language,
                    voice,
                )

                if (
                    fallback is not None
                    and fallback.is_file()
                    and self.voice_mgr._is_valid_wav_file(fallback)
                ):
                    raise ValueError("该条目来自基础语音回退，不能在皮肤档案中删除")

                raise ValueError("当前档案中没有这个文件")

            trash_id = uuid4().hex
            trash_item = self.trash_dir / trash_id
            trash_item.mkdir(parents=True)
            destination = trash_item / "file.wav"
            relative = target.resolve().relative_to(self.voices_dir.resolve())
            metadata = {
                "id": trash_id,
                "deletedAt": self._utc_now(),
                "username": request.username or "dashboard",
                "character": character,
                "language": language,
                "voice": voice,
                "relativePath": relative.as_posix(),
                "bytes": target.stat().st_size,
            }

            async with self._mutation_lock:
                self.voice_mgr._atomic_write_json(
                    trash_item / "metadata.json",
                    metadata,
                )
                target.replace(destination)

            await self.scan_callback(True)
            await self._audit(
                "trash_voice",
                f"{character}/{language}/{voice}",
                details={"trashId": trash_id},
            )
            return json_response(metadata)
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def preview_batch_remove(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        try:
            character = self._canonical_character(payload.get("character"))
            language = str(payload.get("language", "")).strip().lower()
            requested_voices = payload.get("voices", [])

            if language not in self.voice_mgr.LANGUAGE_MAP:
                raise ValueError("语言代码无效")

            if not isinstance(requested_voices, list):
                raise ValueError("请选择要回收的语音")

            voices = list(
                dict.fromkeys(
                    str(voice).strip()
                    for voice in requested_voices
                    if str(voice).strip() in self.voice_mgr.VOICE_DESCRIPTIONS
                )
            )

            if not voices:
                raise ValueError("请选择要回收的语音")

            entries = []
            unavailable = []
            total_bytes = 0

            for voice in voices:
                target = self._own_voice_path(character, language, voice)
                signature = self._path_signature(target)

                if signature is None:
                    unavailable.append(voice)
                    continue

                entries.append(
                    {
                        "voice": voice,
                        "targetSignature": signature,
                    }
                )
                total_bytes += signature[0]

            if not entries:
                raise ValueError("所选语音已不在当前档案中，请刷新后重试")

            summary = {
                "action": "batch_remove",
                "title": (
                    f"批量回收 {character} / "
                    f"{self.voice_mgr.LANGUAGE_MAP[language]['name']}"
                ),
                "selected": len(voices),
                "affected": len(entries),
                "unavailable": len(unavailable),
                "bytes": total_bytes,
                "warnings": [
                    "文件会移入可恢复回收站，不会立即释放磁盘空间。",
                    *(
                        [f"{len(unavailable)} 个条目已变化，将不会处理。"]
                        if unavailable
                        else []
                    ),
                ],
                "sample": [entry["voice"] for entry in entries[:12]],
            }
            result = self._issue_operation_preview(
                action="batch_remove",
                payload={
                    "character": character,
                    "language": language,
                    "entries": entries,
                },
                summary=summary,
            )
            return json_response(result)
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def batch_remove(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        try:
            record = self._take_operation_preview(
                payload.get("previewToken"),
                "batch_remove",
            )
            operation = record["payload"]
            character = self._canonical_character(operation.get("character"))
            language = str(operation.get("language", "")).strip().lower()
            entries = operation.get("entries", [])

            if (
                language not in self.voice_mgr.LANGUAGE_MAP
                or not isinstance(entries, list)
            ):
                raise ValueError("操作预览内容无效，请重新预览")

            plan = []
            username = request.username or "dashboard"

            for entry in entries:
                voice = str(entry.get("voice", ""))
                target = self._own_voice_path(character, language, voice)
                current_signature = self._path_signature(target)
                expected_signature = entry.get("targetSignature")

                if expected_signature is not None:
                    expected_signature = tuple(expected_signature)

                if current_signature is None or current_signature != expected_signature:
                    raise ValueError("档案状态在预览后发生变化，请重新预览")

                relative = target.resolve().relative_to(self.voices_dir.resolve())
                trash_id = uuid4().hex
                trash_item = self.trash_dir / trash_id
                destination = trash_item / "file.wav"
                metadata = {
                    "id": trash_id,
                    "deletedAt": self._utc_now(),
                    "username": username,
                    "character": character,
                    "language": language,
                    "voice": voice,
                    "relativePath": relative.as_posix(),
                    "bytes": current_signature[0],
                    "batch": record["id"],
                }
                plan.append(
                    {
                        "target": target,
                        "trashItem": trash_item,
                        "destination": destination,
                        "metadata": metadata,
                    }
                )

            moved = []

            async with self._mutation_lock:
                try:
                    for item in plan:
                        item["trashItem"].mkdir(parents=True)
                        self.voice_mgr._atomic_write_json(
                            item["trashItem"] / "metadata.json",
                            item["metadata"],
                        )
                        item["target"].replace(item["destination"])
                        moved.append(item)
                except Exception:
                    for item in reversed(moved):
                        try:
                            item["target"].parent.mkdir(parents=True, exist_ok=True)
                            item["destination"].replace(item["target"])
                        except OSError:
                            logger.exception(
                                "批量回收回滚失败: "
                                f"{item['metadata'].get('relativePath', '')}"
                            )

                    for item in plan:
                        if item not in moved or item["target"].is_file():
                            shutil.rmtree(item["trashItem"], ignore_errors=True)

                    raise

            await self.scan_callback(True)
            total_bytes = sum(item["metadata"]["bytes"] for item in plan)
            await self._audit(
                "trash_batch",
                f"{character}/{language}",
                details={
                    "batchId": record["id"],
                    "count": len(plan),
                    "bytes": total_bytes,
                    "trashIds": [
                        item["metadata"]["id"] for item in plan
                    ],
                },
            )
            return json_response(
                {
                    "removed": len(plan),
                    "bytes": total_bytes,
                    "batchId": record["id"],
                }
            )
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    def _read_trash_item(self, trash_id: str) -> tuple[Path, dict]:
        if not self._TRASH_ID_RE.fullmatch(str(trash_id)):
            raise ValueError("回收站条目标识无效")

        item_dir = self.trash_dir / trash_id

        if not self._path_within(item_dir, self.trash_dir):
            raise ValueError("回收站路径越界")

        metadata_path = item_dir / "metadata.json"
        file_path = item_dir / "file.wav"

        try:
            with metadata_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("回收站记录损坏") from exc

        if not isinstance(metadata, dict) or not file_path.is_file():
            raise ValueError("回收站文件不存在")

        return item_dir, metadata

    def _list_trash_items(self) -> list[dict]:
        result = []

        try:
            directories = sorted(
                self.trash_dir.iterdir(),
                key=lambda item: item.name,
                reverse=True,
            )
        except OSError:
            return result

        for item_dir in directories:
            if not item_dir.is_dir():
                continue

            try:
                _, metadata = self._read_trash_item(item_dir.name)
                result.append(metadata)
            except ValueError:
                continue

        return result

    async def trash_items(self):
        return json_response({"items": self._list_trash_items()})

    async def restore_voice(self):
        payload = await request.json(default={})

        try:
            trash_id = payload.get("id") if isinstance(payload, dict) else None
            item_dir, metadata = self._read_trash_item(str(trash_id))
            relative = Path(str(metadata.get("relativePath", "")))
            target = self.voices_dir / relative

            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not self._path_within(target, self.voices_dir)
            ):
                raise ValueError("恢复目标路径无效")

            source = item_dir / "file.wav"

            async with self._mutation_lock:
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = self._backup_existing(target, "restore")
                source.replace(target)
                shutil.rmtree(item_dir)

            await self.scan_callback(True)
            await self._audit(
                "restore_voice",
                str(metadata.get("relativePath", "")),
                details={
                    "trashId": trash_id,
                    "backupCreated": bool(backup),
                },
            )
            return json_response(
                {
                    "restored": True,
                    "backupCreated": bool(backup),
                }
            )
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def purge_voice(self):
        payload = await request.json(default={})

        try:
            trash_id = payload.get("id") if isinstance(payload, dict) else None
            item_dir, metadata = self._read_trash_item(str(trash_id))

            async with self._mutation_lock:
                shutil.rmtree(item_dir)

            await self._audit(
                "purge_voice",
                str(metadata.get("relativePath", "")),
                details={"trashId": trash_id},
            )
            return json_response({"purged": True})
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    def _trim_tasks(self) -> None:
        finished = [
            item
            for item in sorted(
                self._tasks.values(),
                key=lambda current: current.get("createdAt", ""),
            )
            if item.get("status") not in {"queued", "running"}
        ]

        while len(self._tasks) > self.MAX_TASK_ITEMS and finished:
            item = finished.pop(0)
            self._tasks.pop(item["id"], None)
            self._task_handles.pop(item["id"], None)

    def _start_task(
        self,
        *,
        kind: str,
        target: str,
        username: str,
        runner: Callable[[], Awaitable[dict]],
    ) -> dict:
        task_id = uuid4().hex
        record = {
            "id": task_id,
            "kind": kind,
            "target": target,
            "status": "queued",
            "createdAt": self._utc_now(),
            "startedAt": None,
            "finishedAt": None,
            "message": "等待执行",
            "result": None,
        }
        self._tasks[task_id] = record

        async def wrapped() -> None:
            record["status"] = "running"
            record["startedAt"] = self._utc_now()
            record["message"] = "正在执行"

            try:
                result = await runner()
                record["result"] = result
                record["status"] = "completed"
                record["message"] = str(result.get("message", "已完成"))
            except asyncio.CancelledError:
                record["status"] = "cancelled"
                record["message"] = "已取消"
                raise
            except Exception as exc:
                logger.exception(f"语音管理后台任务失败: {exc}")
                record["status"] = "failed"
                record["message"] = str(exc)
            finally:
                record["finishedAt"] = self._utc_now()
                await self._audit(
                    f"task_{record['status']}",
                    target,
                    details={
                        "taskId": task_id,
                        "kind": kind,
                        "message": record["message"],
                    },
                    username=username,
                )
                self._trim_tasks()

        handle = asyncio.create_task(wrapped())
        self._task_handles[task_id] = handle
        self._trim_tasks()
        return dict(record)

    def _normalize_fetch_payload(self, payload: dict) -> dict:
        character = str(payload.get("character", "")).strip()

        if not self.voice_mgr.validate_character(character):
            raise ValueError("角色名称不合法")

        rank_to_language = {
            str(info["rank"]): code
            for code, info in self.voice_mgr.LANGUAGE_MAP.items()
        }
        requested = str(
            payload.get("languages", self.default_download_langs)
        ).strip()
        ranks = "".join(
            dict.fromkeys(rank for rank in requested if rank in rank_to_language)
        )

        if not ranks:
            raise ValueError("请选择至少一种下载语言")

        return {
            "character": character,
            "languages": ranks,
            "languageCodes": [rank_to_language[rank] for rank in ranks],
            "includeSkin": bool(
                payload.get("includeSkin", self.default_download_skin)
            ),
        }

    async def preview_fetch(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        try:
            operation = self._normalize_fetch_payload(payload)
            character = operation["character"]
            language_codes = operation["languageCodes"]
            references = [character]

            if operation["includeSkin"]:
                for resource_id in self.voice_mgr.skin_voice_index.get(
                    character,
                    {},
                ):
                    reference = self.voice_mgr._skin_reference(
                        character,
                        resource_id,
                    )

                    if reference:
                        references.append(reference)

            existing = 0
            overwritten = 0
            missing = 0
            damaged = 0

            for reference in references:
                for language in language_codes:
                    force_redownload = self.voice_mgr.needs_voice_resource_remap(
                        character,
                        language,
                    )

                    for voice in self.voice_mgr.VOICE_DESCRIPTIONS:
                        status = self._voice_status(reference, language, voice)

                        if status["status"] == "own":
                            if force_redownload:
                                overwritten += 1
                            else:
                                existing += 1
                        elif status["status"] == "damaged":
                            damaged += 1
                        else:
                            missing += 1

            language_names = [
                self.voice_mgr.LANGUAGE_MAP[language]["name"]
                for language in language_codes
            ]
            summary = {
                "action": "fetch",
                "title": f"获取 {character} 的语音资源",
                "character": character,
                "languageCodes": language_codes,
                "languageNames": language_names,
                "includeSkin": operation["includeSkin"],
                "knownArchives": len(references),
                "knownSlots": (
                    len(references)
                    * len(language_codes)
                    * len(self.voice_mgr.VOICE_DESCRIPTIONS)
                ),
                "existing": existing,
                "overwritten": overwritten,
                "missing": missing,
                "damaged": damaged,
                "warnings": [
                    (
                        "有效本地文件会跳过；损坏、缺失或"
                        "待编号修复的条目会重新请求。"
                    ),
                    *(
                        [
                            "PRTS 中尚未登记到本地的新皮肤包会在"
                            "任务执行时加入，未计入上述数量。"
                        ]
                        if operation["includeSkin"]
                        else []
                    ),
                ],
            }
            result = self._issue_operation_preview(
                action="fetch",
                payload=operation,
                summary=summary,
            )
            return json_response(result)
        except (OSError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    async def start_fetch(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        try:
            if payload.get("previewToken"):
                preview = self._take_operation_preview(
                    payload.get("previewToken"),
                    "fetch",
                )
                operation = self._normalize_fetch_payload(preview["payload"])
            else:
                operation = self._normalize_fetch_payload(payload)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        character = operation["character"]
        languages = operation["languages"]
        include_skin = operation["includeSkin"]
        username = request.username or "dashboard"

        async def runner() -> dict:
            async with self._fetch_semaphore:
                success, message = await self.voice_mgr.fetch_character_voices(
                    character,
                    include_skin,
                    languages,
                )

            await self.scan_callback(True)

            if not success:
                raise RuntimeError(message)

            return {
                "message": message,
                "character": character,
            }

        record = self._start_task(
            kind="fetch",
            target=character,
            username=username,
            runner=runner,
        )
        return json_response(record, status_code=202)

    async def tasks(self):
        items = sorted(
            (dict(item) for item in self._tasks.values()),
            key=lambda item: item.get("createdAt", ""),
            reverse=True,
        )
        return json_response({"items": items})

    async def cancel_task(self):
        payload = await request.json(default={})
        task_id = payload.get("id") if isinstance(payload, dict) else None
        handle = self._task_handles.get(str(task_id))
        record = self._tasks.get(str(task_id))

        if handle is None or record is None:
            return error_response("任务不存在", status_code=404)

        if handle.done() or record.get("status") not in {"queued", "running"}:
            return error_response("任务已经结束")

        handle.cancel()
        return json_response({"cancelled": True, "id": task_id})

    async def rescan(self):
        await self.scan_callback(True)
        await self._audit("rescan", "voice_index")
        return json_response(
            {
                "rescanned": True,
                "archives": len(self._all_archives()),
                "storage": self._storage_stats(),
            }
        )

    def _run_integrity(self, quarantine: bool) -> dict:
        checked = 0
        valid = 0
        issues = []
        isolated = 0
        quarantine_root = (
            self.data_dir
            / "quarantine"
            / "page-integrity"
            / (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + f"-{uuid4().hex[:8]}"
            )
        )

        try:
            paths = list(self.voices_dir.rglob("*.wav"))
        except OSError:
            paths = []

        for path in paths:
            checked += 1
            issue = None

            try:
                if path.is_symlink():
                    issue = "符号链接"
                elif not self._path_within(path, self.voices_dir):
                    issue = "路径越界"
                elif path.stem not in self.voice_mgr.VOICE_DESCRIPTIONS:
                    issue = "未知语音名称"
                elif not self.voice_mgr._is_valid_wav_file(path):
                    issue = "WAV 文件损坏"
            except OSError:
                issue = "文件不可读"

            if issue is None:
                valid += 1
                continue

            try:
                relative = path.resolve().relative_to(self.voices_dir.resolve())
            except (OSError, ValueError):
                relative = Path(path.name)

            item = {
                "path": relative.as_posix(),
                "issue": issue,
                "isolated": False,
            }

            if (
                quarantine
                and path.is_file()
                and not path.is_symlink()
                and self._path_within(path, self.voices_dir)
            ):
                destination = quarantine_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                path.replace(destination)
                isolated += 1
                item["isolated"] = True

            issues.append(item)

        report = {
            "checkedAt": self._utc_now(),
            "checked": checked,
            "valid": valid,
            "issues": issues,
            "issueCount": len(issues),
            "isolated": isolated,
            "quarantineMode": quarantine,
        }
        self.voice_mgr._atomic_write_json(self.integrity_file, report)
        return report

    async def integrity(self):
        if request.method == "GET":
            return json_response(self._latest_integrity)

        payload = await request.json(default={})
        quarantine = bool(
            payload.get("quarantine", False)
            if isinstance(payload, dict)
            else False
        )
        username = request.username or "dashboard"

        async def runner() -> dict:
            report = await asyncio.to_thread(
                self._run_integrity,
                quarantine,
            )
            self._latest_integrity = report

            if report["isolated"]:
                await self.scan_callback(True)

            return {
                **report,
                "message": (
                    f"检查 {report['checked']} 个文件，"
                    f"发现 {report['issueCount']} 个问题，"
                    f"隔离 {report['isolated']} 个"
                ),
            }

        record = self._start_task(
            kind="integrity",
            target="voice_library",
            username=username,
            runner=runner,
        )
        return json_response(record, status_code=202)

    async def bindings(self):
        items = []

        for trigger, info in sorted(self.custom_mappings.items()):
            if not isinstance(info, dict):
                continue

            character = str(info.get("character", ""))
            voice = str(info.get("voice", ""))
            language = info.get("lang")
            path = (
                self.voice_mgr.get_voice_path(character, voice, language)
                if language in self.voice_mgr.LANGUAGE_MAP
                else None
            )
            items.append(
                {
                    "trigger": trigger,
                    "character": character,
                    "voice": voice,
                    "language": language,
                    "languageName": (
                        self.voice_mgr.LANGUAGE_MAP.get(language, {}).get("name")
                        if language
                        else "自动"
                    ),
                    "available": bool(path)
                    if language
                    else bool(self.voice_mgr.get_available_voices(character)),
                }
            )

        return json_response({"items": items})

    async def save_binding(self):
        payload = await request.json(default={})

        if not isinstance(payload, dict):
            return error_response("请求格式无效")

        trigger = str(payload.get("trigger", "")).strip()
        voice = str(payload.get("voice", "")).strip()
        language_value = payload.get("language")
        language = (
            str(language_value).strip().lower()
            if language_value not in {None, "", "auto"}
            else None
        )

        try:
            if not self.valid_trigger(trigger):
                raise ValueError("触发词不能为空且最长 64 个字符")

            character = self._canonical_character(payload.get("character"))

            if voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
                raise ValueError("语音类型无效")

            if language is not None and language not in self.voice_mgr.LANGUAGE_MAP:
                raise ValueError("语言代码无效")

            if language is not None:
                if self.voice_mgr.get_voice_path(character, voice, language) is None:
                    raise ValueError("所选语言下没有该语音")
            elif voice not in self.voice_mgr.get_available_voices(character):
                raise ValueError("当前档案没有该语音")

            previous = self.custom_mappings.get(trigger)
            new_value = {
                "character": character,
                "voice": voice,
                "lang": language,
            }

            async with self._mutation_lock:
                self.custom_mappings[trigger] = new_value

                if not self.save_custom_commands():
                    if previous is None:
                        self.custom_mappings.pop(trigger, None)
                    else:
                        self.custom_mappings[trigger] = previous

                    raise ValueError("保存快捷绑定失败")

            await self._audit(
                "save_binding",
                trigger,
                details=new_value,
            )
            return json_response(
                {
                    "saved": True,
                    "trigger": trigger,
                    **new_value,
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def remove_binding(self):
        payload = await request.json(default={})
        trigger = (
            str(payload.get("trigger", "")).strip()
            if isinstance(payload, dict)
            else ""
        )

        if trigger not in self.custom_mappings:
            return error_response("快捷绑定不存在", status_code=404)

        previous = self.custom_mappings[trigger]

        async with self._mutation_lock:
            self.custom_mappings.pop(trigger, None)

            if not self.save_custom_commands():
                self.custom_mappings[trigger] = previous
                return error_response("保存快捷绑定失败", status_code=500)

        await self._audit("remove_binding", trigger)
        return json_response({"removed": True, "trigger": trigger})

    async def audit(self):
        limit = request.query.get("limit", 100, type=int)
        return json_response({"items": self._read_audit(limit)})

    async def terminate(self) -> None:
        handles = [handle for handle in self._task_handles.values() if not handle.done()]

        for handle in handles:
            handle.cancel()

        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
