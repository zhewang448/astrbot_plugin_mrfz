import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import weakref
from contextlib import AsyncExitStack
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urlparse

import aiohttp
from astrbot.api import logger
from PIL import Image as PILImage

from . import constants, prts
from .prts import PRTSLookupError, PRTSNotFoundError

__all__ = ["PRTSLookupError", "PRTSNotFoundError", "VoiceManager"]


class VoiceManager:
    # 使用 constants 模块中的常量
    DEFAULT_HEADERS = constants.DEFAULT_HEADERS
    VOICE_RESOURCE_IDS = constants.VOICE_RESOURCE_IDS
    VOICE_DESCRIPTIONS = constants.VOICE_DESCRIPTIONS
    LANGUAGE_MAP = constants.LANGUAGE_MAP
    LANG_ALIAS = constants.LANG_ALIAS

    MAX_CHARACTER_LENGTH = 80
    MAX_SKIN_ID_LENGTH = 80
    MAX_VOICE_BYTES = constants.MAX_VOICE_BYTES
    MAX_IMAGE_BYTES = constants.MAX_IMAGE_BYTES
    DOWNLOAD_RETRIES = constants.DOWNLOAD_RETRIES
    CHARACTER_PAGE_RETRIES = constants.CHARACTER_PAGE_RETRIES
    RETRYABLE_PAGE_STATUSES = constants.RETRYABLE_PAGE_STATUSES
    VOICE_RESOURCE_MAP_VERSION = constants.VOICE_RESOURCE_MAP_VERSION
    MAX_WAV_VALIDITY_CACHE = constants.MAX_WAV_VALIDITY_CACHE
    DOWNLOAD_CONCURRENCY = constants.DOWNLOAD_CONCURRENCY
    # 语音名 -> 展示顺序，排序时直接查表。
    _VOICE_ORDER = {name: index for index, name in enumerate(constants.VOICE_DESCRIPTIONS)}

    _SAFE_COMPONENT_RE = re.compile(
        r"^[\w\- .·()（）]+$",
        re.UNICODE,
    )
    _SKIN_REFERENCE_RE = re.compile(r"^(?P<base>.+?)皮肤(?:\[(?P<skin>.+)\])?$")

    def __init__(
        self,
        data_dir: Path,
        plugin_dir: Union[str, Path],
    ):
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.voices_dir = self.data_dir / "voices"
        self.assets_dir = self.data_dir / "assets"
        self.operator_alias_file = self.data_dir / "operator_aliases.json"
        self.operator_catalog_file = self.data_dir / "operator_catalog.json"
        self.voice_text_dir = self.data_dir / "voice_texts"
        self.voice_record_dir = self.data_dir / "voice_records"
        self._voice_records: Dict[str, Dict[str, Any]] = {}
        self._record_locks = weakref.WeakValueDictionary()
        self.file_lock = threading.RLock()
        self.mutation_lock = asyncio.Lock()
        self._operator_catalog: Optional[Dict[str, Any]] = None
        self._catalog_lock = asyncio.Lock()
        self.operator_aliases: Dict[str, str] = dict(constants.OPERATOR_ALIAS)
        self._custom_operator_aliases: Dict[str, str] = {}

        # 兼容原 main.py。
        self.voice_index: Dict[
            str,
            List[str],
        ] = {}

        # 角色 -> 语言 -> 实际存在语音。
        self.voice_files: Dict[
            str,
            Dict[str, List[str]],
        ] = {}

        # 角色 -> 皮肤 ID -> 语言 -> 语音。
        self.skin_voice_index: Dict[
            str,
            Dict[
                str,
                Dict[str, List[str]],
            ],
        ] = {}

        # 角色 -> 稳定皮肤资源 ID -> 展示名、目录名、各语言 PRTS voice key。
        self.skin_metadata: Dict[
            str,
            Dict[str, Dict[str, Any]],
        ] = {}

        # 持有者和等待者保留强引用；空闲锁自动回收，不淘汰正在使用的锁。
        self._download_locks = weakref.WeakValueDictionary()

        # v3 及更早版本按连续编号下载过语音，已有 WAV 可能内容与名称错位。
        # 迁移按“角色 + 语言”记录，只有整组请求没有真实失败时才清除。
        self._voice_resource_map_version = self.VOICE_RESOURCE_MAP_VERSION
        self._voice_remap_pending: set[Tuple[str, str]] = set()

        # 路径 -> ((大小, 修改时间), 是否为有效 WAV)，供重复扫描跳过读文件头。
        self._wav_validity_cache: Dict[str, Tuple[Tuple[int, int], bool]] = {}
        # 每次扫描完成后递增，管理页据此判断缓存的档案汇总是否过期。
        self.scan_generation = 0
        self.last_scan_time = 0.0
        # 已完成的语言目录修正版本，见 migrate_language_routing。
        self._language_routing_version = 0
        self._routing_refills: Dict[str, Dict[str, Any]] = {}
        self._routing_moves: Dict[str, Dict[str, str]] = {}
        # 上次写入 voice_index.json 的内容，用于跳过没有变化的保存。
        self._saved_index_bytes: Optional[bytes] = None

        for directory in (
            self.data_dir,
            self.voices_dir,
            self.assets_dir,
            self.voice_text_dir,
            self.voice_record_dir,
        ):
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

        self._load_skin_metadata()
        self._load_operator_aliases()
        # 首次扫描要读每个 WAV 的文件头，由调用方放到线程里执行（见 main._initialize_resources）。

    def _load_operator_aliases(self) -> None:
        """加载用户自定义别称，并保留内置别称作为默认值。"""
        if not self.operator_alias_file.is_file():
            return
        try:
            with self.operator_alias_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return
            for alias, character in payload.items():
                if (
                    isinstance(alias, str)
                    and isinstance(character, str)
                    and self._is_safe_component(alias, self.MAX_CHARACTER_LENGTH)
                    and self._is_safe_component(character, self.MAX_CHARACTER_LENGTH)
                ):
                    alias = alias.strip()
                    character = character.strip()
                    self._custom_operator_aliases[alias] = character
                    self.operator_aliases[alias] = character
        except (OSError, json.JSONDecodeError):
            logger.warning(f"读取干员别称文件失败: {self.operator_alias_file}")

    def _save_operator_aliases(self) -> bool:
        """原子保存用户自定义别称。"""
        temp_path = self.operator_alias_file.with_name(
            f".{self.operator_alias_file.name}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(
                    self._custom_operator_aliases,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
            temp_path.replace(self.operator_alias_file)
            return True
        except OSError as exc:
            logger.error(f"保存干员别称失败: {exc}")
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def resolve_operator_alias(self, character: str) -> str:
        """按原样或忽略大小写解析干员别称，最多展开一层。"""
        value = character.strip()
        if value in self.operator_aliases:
            return self.operator_aliases[value]
        folded = value.casefold()
        for alias, target in self.operator_aliases.items():
            if alias.casefold() == folded:
                return target
        return value

    def add_operator_alias(self, alias: str, character: str) -> Tuple[bool, str]:
        """添加并持久化一个干员别称。"""
        alias = alias.strip()
        character = self.resolve_operator_alias(character)
        if not self._is_safe_component(alias, self.MAX_CHARACTER_LENGTH):
            return False, "别称不能为空，且不能包含路径或特殊字符"
        if not self._is_safe_component(character, self.MAX_CHARACTER_LENGTH):
            return False, "干员名称不合法"
        if alias == character:
            return False, "别称不能与干员标准名相同"
        previous = self.operator_aliases.get(alias)
        previous_custom = self._custom_operator_aliases.get(alias)
        self.operator_aliases[alias] = character
        self._custom_operator_aliases[alias] = character
        if not self._save_operator_aliases():
            if previous is None:
                self.operator_aliases.pop(alias, None)
            else:
                self.operator_aliases[alias] = previous
            if previous_custom is None:
                self._custom_operator_aliases.pop(alias, None)
            else:
                self._custom_operator_aliases[alias] = previous_custom
            return False, "别称保存失败，请检查数据目录权限"
        return True, f"已添加干员别称: {alias} -> {character}"

    def remove_operator_alias(self, alias: str) -> Tuple[bool, str]:
        """删除自定义别称；内置别称删除后恢复默认映射。"""
        alias = alias.strip()
        if alias not in self.operator_aliases:
            return False, "干员别称不存在"

        previous = self.operator_aliases[alias]
        previous_custom = self._custom_operator_aliases.get(alias)
        if alias in constants.OPERATOR_ALIAS:
            self.operator_aliases[alias] = constants.OPERATOR_ALIAS[alias]
            self._custom_operator_aliases.pop(alias, None)
            message = f"已恢复内置干员别称: {alias} -> {self.operator_aliases[alias]}"
        else:
            self.operator_aliases.pop(alias, None)
            self._custom_operator_aliases.pop(alias, None)
            message = f"已删除干员别称: {alias}"

        if not self._save_operator_aliases():
            # 保存失败时恢复内存状态，避免页面与运行时不一致。
            self.operator_aliases[alias] = previous
            if previous_custom is None:
                self._custom_operator_aliases.pop(alias, None)
            else:
                self._custom_operator_aliases[alias] = previous_custom
            return False, "别称保存失败，请检查数据目录权限"
        return True, message

    def _load_skin_metadata(self) -> None:
        index_path = self.data_dir / "voice_index.json"

        if not index_path.is_file():
            return

        try:
            with index_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)

            if not isinstance(payload, dict) or payload.get("version") not in constants.SUPPORTED_VOICE_INDEX_VERSIONS:
                return

            try:
                self._voice_resource_map_version = int(
                    payload.get("voice_resource_map_version", 0)
                )
            except (TypeError, ValueError):
                self._voice_resource_map_version = 0

            try:
                self._language_routing_version = int(
                    payload.get("language_routing_version", 0)
                )
            except (TypeError, ValueError):
                self._language_routing_version = 0

            raw_pending = payload.get("voice_remap_pending", [])
            if isinstance(raw_pending, list):
                self._voice_remap_pending = {
                    (self._base_character(item[0]), item[1])
                    for item in raw_pending
                    if (
                        isinstance(item, list)
                        and len(item) == 2
                        and self.validate_character(item[0])
                        and isinstance(item[1], str)
                        and item[1] in self.LANGUAGE_MAP
                    )
                }

            raw_refills = payload.get("routing_refills", {})
            if not isinstance(raw_refills, dict):
                raw_refills = {}
            for key, job in raw_refills.items():
                if isinstance(job, dict) and self._valid_refill(job):
                    self._routing_refills[key] = job
            moves = payload.get("routing_moves", {})
            if isinstance(moves, dict):
                self._routing_moves = {
                    character: {old: new for old, new in languages.items()
                                if old in self.LANGUAGE_MAP and isinstance(new, str) and new in self.LANGUAGE_MAP}
                    for character, languages in moves.items()
                    if self.validate_character(character) and isinstance(languages, dict)
                }

            raw_metadata = payload.get("skin_metadata", {})

            if not isinstance(raw_metadata, dict):
                return

            for character, packages in raw_metadata.items():
                if not self._is_safe_component(
                    character, self.MAX_CHARACTER_LENGTH
                ) or not isinstance(packages, dict):
                    continue

                for resource_id, info in packages.items():
                    if not self._is_safe_component(
                        resource_id, self.MAX_SKIN_ID_LENGTH
                    ) or not isinstance(info, dict):
                        continue

                    name = str(info.get("name", "")).strip()
                    directory = str(info.get("directory", "")).strip()
                    raw_voice_keys = info.get("voice_keys", {})

                    if (
                        not self._is_safe_component(name, self.MAX_SKIN_ID_LENGTH)
                        or not self._is_safe_component(
                            directory,
                            self.MAX_SKIN_ID_LENGTH,
                        )
                        or not isinstance(raw_voice_keys, dict)
                    ):
                        continue

                    voice_keys = {
                        language: str(voice_key).strip().strip("/")
                        for language, voice_key in raw_voice_keys.items()
                        if language in self.LANGUAGE_MAP
                        and isinstance(voice_key, str)
                        and voice_key.strip().strip("/")
                    }

                    self.skin_metadata.setdefault(character, {})[resource_id] = {
                        "name": name,
                        "directory": directory,
                        "voice_keys": voice_keys,
                    }
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"加载语音索引失败，将从本地目录重建: {exc}")

    @classmethod
    def _is_safe_component(
        cls,
        value: str,
        max_length: int,
    ) -> bool:
        if not isinstance(value, str):
            return False

        value = value.strip()

        if not value or len(value) > max_length or value in {".", ".."}:
            return False

        if "/" in value or "\\" in value or "\x00" in value:
            return False

        return bool(cls._SAFE_COMPONENT_RE.fullmatch(value))

    @classmethod
    def _parse_character_reference(
        cls,
        character: str,
    ) -> Optional[Tuple[str, bool, Optional[str]]]:
        if not isinstance(character, str):
            return None

        character = character.strip()
        match = cls._SKIN_REFERENCE_RE.fullmatch(character)

        if match:
            base = match.group("base").strip()
            skin_id = match.group("skin")
            skin_id = skin_id.strip() if skin_id else None

            if not cls._is_safe_component(
                base,
                cls.MAX_CHARACTER_LENGTH,
            ):
                return None

            if skin_id and not cls._is_safe_component(
                skin_id,
                cls.MAX_SKIN_ID_LENGTH,
            ):
                return None

            return base, True, skin_id

        if not cls._is_safe_component(
            character,
            cls.MAX_CHARACTER_LENGTH,
        ):
            return None

        return character, False, None

    @classmethod
    def validate_character(cls, character: Any) -> bool:
        """检查角色或皮肤角色名称是否合法。"""
        return cls._parse_character_reference(character) is not None

    @classmethod
    def _base_character(cls, character: Any) -> str:
        """从角色引用中取得基础角色名。"""
        parsed = cls._parse_character_reference(character)
        return parsed[0] if parsed else ""

    @staticmethod
    def _path_is_within(
        path: Path,
        root: Path,
    ) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):
            return False

    def _safe_path(
        self,
        root: Path,
        *parts: str,
    ) -> Optional[Path]:
        path = root.joinpath(*parts)

        if self._path_is_within(
            path,
            root,
        ):
            return path

        return None

    def _scan_language_dir(
        self,
        directory: Path,
    ) -> List[str]:
        if not directory.is_dir() or not self._path_is_within(
            directory,
            self.voices_dir,
        ):
            return []

        found = []
        allowed = set(self.VOICE_DESCRIPTIONS)

        # 目录本身已校验在语音树内；条目来自 scandir，名字不含路径分隔符，
        # 只需排除符号链接即可保证不越界，避免对每个文件做开销很大的 resolve()。
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        name, suffix = os.path.splitext(entry.name)
                        if (
                            suffix.lower() == ".wav"
                            and name in allowed
                            and not entry.is_symlink()
                            and entry.is_file(follow_symlinks=False)
                            and self._is_valid_wav_entry(entry)
                        ):
                            found.append(name)
                    except OSError:
                        continue
        except OSError:
            return []

        return sorted(
            set(found),
            key=self._VOICE_ORDER.__getitem__,
        )

    @classmethod
    def _is_valid_wav_file(cls, path: Path) -> bool:
        """验证 RIFF chunk 边界，兼容 PCM 与带扩展 fmt 的 WAV。"""
        try:
            info = path.stat()

            if not S_ISREG(info.st_mode) or info.st_size < 12:
                return False

            with path.open("rb") as handle:
                return cls._valid_wav_stream(handle, info.st_size)
        except OSError:
            return False

    def _is_valid_wav_entry(self, entry: os.DirEntry) -> bool:
        """按 (大小, 修改时间) 缓存 WAV 头校验结果，重复扫描时只需 stat。"""
        stat = entry.stat(follow_symlinks=False)
        signature = (stat.st_size, stat.st_mtime_ns)
        cached = self._wav_validity_cache.get(entry.path)

        if cached is not None and cached[0] == signature:
            return cached[1]

        valid = False

        if stat.st_size >= 12:
            try:
                with open(entry.path, "rb") as handle:
                    valid = self._valid_wav_stream(handle, stat.st_size)
            except OSError:
                valid = False

        if len(self._wav_validity_cache) >= self.MAX_WAV_VALIDITY_CACHE:
            self._wav_validity_cache.clear()

        self._wav_validity_cache[entry.path] = (signature, valid)
        return valid

    @staticmethod
    def _is_valid_png_file(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False

            with PILImage.open(path) as image:
                image.verify()
                return image.format == "PNG"
        except (OSError, ValueError):
            return False

    def _quarantine_wav(
        self,
        path: Path,
        suffix: str,
        reason: str,
    ) -> Optional[Path]:
        """把不能继续使用的 WAV 移出语音树，保留原文件供排查。"""
        try:
            relative = path.relative_to(self.voices_dir)
            target = self.data_dir / "quarantine" / "voices" / relative
            target = target.with_name(f"{target.name}.{suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)

            candidate = target
            attempt = 1

            while candidate.exists():
                candidate = target.with_name(f"{target.name}.{attempt}")
                attempt += 1

            path.replace(candidate)
            logger.warning(f"已隔离{reason}的语音文件: {path} -> {candidate}")
            return candidate
        except (OSError, ValueError) as exc:
            logger.warning(f"隔离{reason}语音文件失败 {path}: {exc}")
            return None

    def _quarantine_invalid_wav(self, path: Path) -> Optional[Path]:
        return self._quarantine_wav(path, "invalid", "损坏")

    def _quarantine_stale_wav(self, path: Path) -> Optional[Path]:
        return self._quarantine_wav(path, "stale-map", "旧编号错位")

    def _record_flat_character(
        self,
        character: str,
        languages: Dict[
            str,
            List[str],
        ],
        voice_files: Dict[str, Dict[str, List[str]]],
        voice_index: Dict[str, List[str]],
    ) -> None:
        languages = {
            language: voices for language, voices in languages.items() if voices
        }

        if not languages:
            return

        voice_files[character] = languages
        voice_index[character] = sorted(
            languages,
            key=lambda language: int(
                self.LANGUAGE_MAP.get(
                    language,
                    {},
                ).get(
                    "rank",
                    "99",
                )
            ),
        )

    def _skin_playable_languages(
        self,
        base_languages: Dict[str, List[str]],
        skin_languages: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:
        """
        皮肤包未覆盖的单条语音回退到同语言的角色基础语音。

        只有皮肤包自身存在的语言才会登记，避免把整套不存在的皮肤语言
        误报为可用。
        """
        playable = {}

        for language, skin_voices in skin_languages.items():
            playable[language] = self._sort_voice_names(
                list(skin_voices) + list(base_languages.get(language, []))
            )

        return playable

    @staticmethod
    def _local_skin_resource_id(
        character: str,
        directory: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{character}\0{directory}".encode("utf-8")
        ).hexdigest()[:12]
        return f"local_{digest}"

    def _metadata_for_directory(
        self,
        character: str,
        directory: str,
    ) -> Tuple[str, Dict[str, Any]]:
        packages = self.skin_metadata.setdefault(character, {})

        for resource_id, info in packages.items():
            if info.get("directory") == directory:
                return resource_id, info

        resource_id = self._local_skin_resource_id(character, directory)
        info = {
            "name": directory,
            "directory": directory,
            "voice_keys": {},
        }
        packages[resource_id] = info
        return resource_id, info

    def _skin_reference(
        self,
        character: str,
        resource_id: str,
        packages: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """packages 为 None 时使用当前索引；扫描过程中需传入正在构建的皮肤包。"""
        info = self.skin_metadata.get(character, {}).get(resource_id)

        if not info:
            return None

        name = str(info.get("name", "")).strip()

        if not name:
            return None

        same_name_ids = [
            current_id
            for current_id in (
                self.skin_voice_index.get(character, {})
                if packages is None
                else packages
            )
            if self.skin_metadata.get(character, {}).get(current_id, {}).get("name")
            == name
        ]
        selector = name if len(same_name_ids) <= 1 else f"{name} · {resource_id}"
        return f"{character}皮肤[{selector}]"

    def _match_skin_packages(
        self,
        character: str,
        selector: Optional[str],
    ) -> Dict[str, Optional[str]]:
        """本地皮肤包里能被 selector 匹配上的项：{资源 ID: 皮肤引用}，按索引顺序。

        selector 可以是资源 ID、展示名、目录名，或重名时带资源 ID 的引用选择器。
        """
        matched = {}
        packages = self.skin_metadata.get(character, {})

        for resource_id in self.skin_voice_index.get(character, {}):
            info = packages.get(resource_id, {})
            reference = self._skin_reference(character, resource_id)
            aliases = {
                resource_id,
                str(info.get("name", "")).strip(),
                str(info.get("directory", "")).strip(),
                self._parse_character_reference(reference)[2] if reference else None,
            }

            if selector in aliases:
                matched[resource_id] = reference

        return matched

    def get_skin_options(self, character: str) -> List[str]:
        """返回某角色当前确实有文件的具体皮肤引用。"""
        options = []

        for resource_id in self.skin_voice_index.get(character, {}):
            reference = self._skin_reference(character, resource_id)

            if reference:
                options.append(reference)

        return sorted(set(options))

    def get_skin_name_matches(self, skin_name: str) -> List[str]:
        """按皮肤展示名反查当前本地可播放的皮肤引用。"""
        skin_name = skin_name.strip()

        if not skin_name:
            return []

        matches = []

        for character, packages in self.skin_voice_index.items():
            for resource_id in packages:
                info = self.skin_metadata.get(character, {}).get(resource_id, {})
                aliases = {
                    str(info.get("name", "")).strip(),
                    str(info.get("directory", "")).strip(),
                    resource_id,
                }

                if skin_name not in aliases:
                    continue

                reference = self._skin_reference(character, resource_id)
                if reference:
                    matches.append(reference)

        return sorted(set(matches))

    def resolve_character_reference(
        self,
        character: str,
    ) -> Tuple[Optional[str], List[str]]:
        """
        把皮肤引用规范化为具体展示名。

        返回 (规范引用, 候选项)。多皮肤未指定或名称无法唯一匹配时，
        规范引用为 None，并通过候选项提示用户。
        """
        parsed = self._parse_character_reference(character)

        if not parsed:
            return None, []

        base_character, is_skin, selector = parsed
        canonical_base = self.resolve_operator_alias(base_character)
        alias_used = canonical_base != base_character
        base_character = canonical_base

        if not is_skin:
            reference = base_character
            if reference in self.voice_index or alias_used:
                return reference, []

            # 兼容直接输入皮肤展示名，例如“超新星”，无需再输入
            # “维什戴尔皮肤[超新星]”。重名皮肤保留候选列表，交给上层提示用户选择。
            skin_matches = self.get_skin_name_matches(reference)
            if len(skin_matches) == 1:
                return skin_matches[0], []

            return None, skin_matches

        packages = self.skin_voice_index.get(base_character, {})
        options = self.get_skin_options(base_character)

        if not packages:
            return None, []

        if selector is None:
            if len(options) == 1:
                return options[0], []

            return None, options

        matches = sorted(
            {
                reference
                for reference in self._match_skin_packages(base_character, selector).values()
                if reference
            }
        )

        if len(matches) == 1:
            return matches[0], []

        return None, matches or options

    def scan_voice_files(self) -> None:
        with self.file_lock:
            self._scan_voice_files()

    def _scan_voice_files(self) -> None:
        """扫描真实、非空且名称合法的 WAV。

        新索引先在局部变量里建好再整体替换：扫描可能在线程里执行，
        事件循环上的读取方不会看到清空到一半的索引。
        """
        voice_index: Dict[str, List[str]] = {}
        voice_files: Dict[str, Dict[str, List[str]]] = {}
        skin_voice_index: Dict[str, Dict[str, Dict[str, List[str]]]] = {}

        if not self.voices_dir.is_dir():
            self.voice_index = voice_index
            self.voice_files = voice_files
            self.skin_voice_index = skin_voice_index
            self.scan_generation += 1
            return

        try:
            character_dirs = sorted(
                self.voices_dir.iterdir(),
                key=lambda path: path.name,
            )
        except OSError as exc:
            logger.warning(f"扫描语音目录失败: {exc}")
            return

        for character_dir in character_dirs:
            if not character_dir.is_dir() or not self._is_safe_component(
                character_dir.name,
                self.MAX_CHARACTER_LENGTH,
            ):
                continue

            character = character_dir.name
            normal_languages = {}

            for language in self.LANGUAGE_MAP:
                voices = self._scan_language_dir(character_dir / language)
                if voices:
                    normal_languages[language] = voices

            self._record_flat_character(
                character,
                normal_languages,
                voice_files,
                voice_index,
            )

            skin_root = character_dir / "skin"

            if not skin_root.is_dir():
                continue

            packages: Dict[
                str,
                Dict[str, List[str]],
            ] = {}

            # 新目录：
            # 角色/skin/实际目录名/语言/*.wav
            # 角色/skin/语言/*.wav 属于待迁移旧结构，不再登记或播放。
            try:
                skin_dirs = sorted(
                    skin_root.iterdir(),
                    key=lambda path: path.name,
                )
            except OSError:
                skin_dirs = []

            for skin_dir in skin_dirs:
                if (
                    not skin_dir.is_dir()
                    or skin_dir.name in self.LANGUAGE_MAP
                    or not self._is_safe_component(
                        skin_dir.name,
                        self.MAX_SKIN_ID_LENGTH,
                    )
                ):
                    continue

                languages = {}

                for language in self.LANGUAGE_MAP:
                    voices = self._scan_language_dir(skin_dir / language)
                    if voices:
                        languages[language] = voices

                if languages:
                    resource_id, _ = self._metadata_for_directory(
                        character,
                        skin_dir.name,
                    )
                    packages[resource_id] = languages

            if not packages:
                continue

            skin_voice_index[character] = packages

            aggregate: Dict[
                str,
                List[str],
            ] = {}

            for resource_id, languages in packages.items():
                playable_languages = self._skin_playable_languages(
                    normal_languages,
                    languages,
                )

                for language, voices in playable_languages.items():
                    aggregate.setdefault(
                        language,
                        [],
                    ).extend(voices)

                reference = self._skin_reference(
                    character,
                    resource_id,
                    packages,
                )

                if reference:
                    self._record_flat_character(
                        reference,
                        playable_languages,
                        voice_files,
                        voice_index,
                    )

            for language, voices in aggregate.items():
                aggregate[language] = self._sort_voice_names(voices)

            self._record_flat_character(
                f"{character}皮肤",
                aggregate,
                voice_files,
                voice_index,
            )

        self.voice_index = voice_index
        self.voice_files = voice_files
        self.skin_voice_index = skin_voice_index
        self.scan_generation += 1
        self.last_scan_time = time.monotonic()

        if self._voice_resource_map_version < self.VOICE_RESOURCE_MAP_VERSION:
            if not self._voice_remap_pending:
                remap_targets = set()
                for character, languages in self.voice_files.items():
                    remap_targets.update(
                        (self._base_character(character), language) for language in languages
                    )

                for character, packages in self.skin_voice_index.items():
                    for languages in packages.values():
                        remap_targets.update(
                            (character, language) for language in languages
                        )

                self._voice_remap_pending = remap_targets

        try:
            self.save_voice_index()
        except OSError as exc:
            logger.warning(f"保存语音索引失败: {exc}")

    def save_voice_index(self) -> None:
        with self.file_lock:
            self._save_voice_index()

    def _save_voice_index(self) -> None:
        payload = {
            "version": constants.VOICE_INDEX_VERSION,
            "voice_resource_map_version": (
                self._voice_resource_map_version
                if self._voice_remap_pending
                else self.VOICE_RESOURCE_MAP_VERSION
            ),
            "voice_remap_pending": sorted(self._voice_remap_pending),
            "language_routing_version": self._language_routing_version,
            "routing_refills": self._routing_refills,
            "routing_moves": self._routing_moves,
            "voice_index": self.voice_index,
            "voice_files": self.voice_files,
            "skins": self.skin_voice_index,
            "skin_metadata": self.skin_metadata,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        path = self.data_dir / "voice_index.json"

        # 定时扫描通常不改变索引，内容相同就不再整份重写并 fsync。
        if data == self._saved_index_bytes and path.is_file():
            return

        self._atomic_write_bytes(path, data)
        self._saved_index_bytes = data

    def _sort_voice_names(
        self,
        voices: List[str],
    ) -> List[str]:
        return sorted(
            {voice for voice in voices if voice in self._VOICE_ORDER},
            key=self._VOICE_ORDER.__getitem__,
        )

    @staticmethod
    def _write_temp_file(
        directory: Path,
        name: str,
        data: bytes,
    ) -> str:
        """把 data 写入 directory 下的临时文件并落盘，返回临时文件路径；失败时不留残片。"""
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{name}.",
            suffix=".tmp",
            dir=str(directory),
        )

        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            Path(temp_name).unlink(missing_ok=True)
            raise

        # fdopen 接管 fd 后由 with 关闭；不能再手动 os.close，
        # 否则可能关掉别的线程刚分配到同一编号的文件。
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

        return temp_name

    @classmethod
    def _atomic_write_json(
        cls,
        path: Path,
        payload: Any,
    ) -> None:
        cls._atomic_write_bytes(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    @classmethod
    def _atomic_write_bytes(
        cls,
        path: Path,
        data: bytes,
    ) -> None:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        temp_name = cls._write_temp_file(path.parent, path.name, data)

        try:
            os.replace(
                temp_name,
                path,
            )
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def get_available_voices(
        self,
        character: str,
        language: Optional[str] = None,
    ) -> List[str]:
        """返回角色真实拥有的语音。"""
        parsed = self._parse_character_reference(character)

        if not parsed:
            return []

        language = language.lower() if isinstance(language, str) else None

        if language is not None and language not in self.LANGUAGE_MAP:
            return []

        base_character, is_skin, _ = parsed
        base_character = self.resolve_operator_alias(base_character)
        reference = (
            f"{base_character}皮肤[{parsed[2]}]"
            if is_skin and parsed[2]
            else base_character
        )
        languages = self.voice_files.get(reference, {})

        if language:
            return list(
                languages.get(
                    language,
                    [],
                )
            )

        voices = []

        for names in languages.values():
            voices.extend(names)

        return self._sort_voice_names(voices)

    def get_voice_path(
        self,
        character: str,
        voice_name: str,
        language: str,
    ) -> Optional[Path]:
        """安全解析语音路径。"""
        parsed = self._parse_character_reference(character)

        if not parsed or voice_name not in self.VOICE_DESCRIPTIONS:
            return None

        if not isinstance(language, str):
            return None

        language = language.lower()

        if language not in self.LANGUAGE_MAP:
            return None

        (
            base_character,
            is_skin,
            skin_id,
        ) = parsed
        base_character = self.resolve_operator_alias(base_character)

        if is_skin:
            resolved, _ = self.resolve_character_reference(character)

            if not resolved:
                return None
        else:
            resolved = base_character

        # 扫描时已校验过的语音才可能播放；不在索引里就不必解析路径、读文件头。
        if voice_name not in self.voice_files.get(resolved, {}).get(language, ()):
            return None

        character_root = self._safe_path(
            self.voices_dir,
            base_character,
        )

        if character_root is None:
            return None

        if not is_skin:
            candidate = self._safe_path(
                character_root,
                language,
                f"{voice_name}.wav",
            )
        else:
            resolved_selector = self._parse_character_reference(resolved)[2]
            resource_id = next(
                iter(self._match_skin_packages(base_character, resolved_selector)),
                None,
            )
            directory = (
                self.skin_metadata.get(base_character, {})
                .get(resource_id, {})
                .get("directory")
            )

            if (
                resource_id is None
                or not isinstance(directory, str)
                or language
                not in self.skin_voice_index.get(base_character, {}).get(
                    resource_id,
                    {},
                )
            ):
                return None

            candidate = self._safe_path(
                character_root,
                "skin",
                directory,
                language,
                f"{voice_name}.wav",
            )

        # 候选路径都由 _safe_path 生成，已确认位于角色目录内。
        if candidate is not None and self._is_valid_wav_file(candidate):
            return candidate

        if is_skin:
            fallback = self._safe_path(
                character_root,
                language,
                f"{voice_name}.wav",
            )

            if fallback is not None and self._is_valid_wav_file(fallback):
                return fallback

        return None

    def choose_language(
        self,
        character: str,
        rank_config: str | List[str],
    ) -> str:
        available = self.voice_index.get(
            character,
            [],
        )

        if not available:
            return "nodownload"

        for language in constants.normalize_languages(rank_config):
            if language in available:
                return language

        return available[0]

    @staticmethod
    def _legacy_language_from_label(label: str) -> str:
        """3.8.0 之前的语言判定：只看标签里有没有“日/英/韩/方/意”，其余都当中文。

        仅供迁移识别旧版本放错目录的语音；下载请用 prts.language_from_label。
        """
        for keyword, language in (
            ("日", "jp"),
            ("英", "us"),
            ("韩", "kr"),
            ("方", "fy"),
            ("意", "it"),
        ):
            if keyword in label:
                return language

        return "cn"

    @staticmethod
    def _is_skin_label(
        label: str,
    ) -> bool:
        return "(" in label or "（" in label

    @classmethod
    def _skin_name_from_label(
        cls,
        label: str,
        voice_key: str,
    ) -> str:
        return prts.skin_name_from_label(label, voice_key)

    @classmethod
    def _skin_resource_id_from_key(
        cls,
        voice_key: str,
    ) -> str:
        return prts.skin_resource_id(voice_key)

    def _skin_directory_name(
        self,
        character: str,
        resource_id: str,
        display_name: str,
        packages: Optional[Dict[str, Any]] = None,
    ) -> str:
        if packages is None:
            packages = self.skin_metadata.get(character, {})
        existing = packages.get(resource_id)

        if existing and self._is_safe_component(
            str(existing.get("directory", "")),
            self.MAX_SKIN_ID_LENGTH,
        ):
            return str(existing["directory"])

        used = {
            str(info.get("directory", ""))
            for current_id, info in packages.items()
            if current_id != resource_id
        }

        if display_name not in used:
            return display_name

        digest = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:8]
        suffix = f"_{digest}"
        return f"{display_name[: self.MAX_SKIN_ID_LENGTH - len(suffix)]}{suffix}"

    def _register_skin_metadata(
        self,
        character: str,
        language_label: str,
        voice_key: str,
        language: str,
    ) -> Tuple[str, str, str]:
        with self.file_lock:
            return self._register_skin_metadata_locked(character, language_label, voice_key, language)

    def _register_skin_metadata_locked(
        self, character: str, language_label: str, voice_key: str, language: str,
    ) -> Tuple[str, str, str]:
        display_name = self._skin_name_from_label(language_label, voice_key)
        resource_id = self._skin_resource_id_from_key(voice_key)
        packages = self.skin_metadata.setdefault(character, {})

        for current_id, info in list(packages.items()):
            if (
                current_id.startswith("local_")
                and info.get("name") == display_name
                and current_id != resource_id
            ):
                packages.pop(current_id, None)

        directory = self._skin_directory_name(
            character,
            resource_id,
            display_name,
        )
        info = packages.setdefault(
            resource_id,
            {
                "name": display_name,
                "directory": directory,
                "voice_keys": {},
            },
        )
        info["name"] = display_name
        info["directory"] = directory
        info.setdefault("voice_keys", {})[language] = voice_key.strip().strip("/")
        return resource_id, display_name, directory

    # ================== PRTS 干员列表与台词缓存 ==================

    @staticmethod
    def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None

        return payload if isinstance(payload, dict) else None

    async def get_operator_catalog(
        self,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """PRTS 上所有带语音记录的干员，缓存到数据目录。

        返回 {"items": [{"name", "addedAt"}], "fetchedAt": 时间戳, "stale": bool,
        "error": str | None}。刷新失败时继续返回旧缓存并标记 stale。
        """
        async with self._catalog_lock:
            cached = self._operator_catalog

            if cached is None:
                cached = self._read_json_file(self.operator_catalog_file)

                if cached is not None and not isinstance(cached.get("items"), list):
                    cached = None

                self._operator_catalog = cached

            fresh = cached is not None and (
                time.time() - float(cached.get("fetchedAt", 0))
                < constants.OPERATOR_CATALOG_TTL
            )

            if fresh and not force_refresh:
                return {**cached, "stale": False, "error": None}

            try:
                async with aiohttp.ClientSession(
                    headers=self.DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                ) as session:
                    items = await prts.fetch_operator_catalog(session)
            except PRTSLookupError as exc:
                logger.warning(f"获取 PRTS 干员列表失败: {exc}")

                if cached is None:
                    return {"items": [], "fetchedAt": None, "stale": True, "error": str(exc)}

                return {**cached, "stale": True, "error": str(exc)}

            items = [
                item
                for item in items
                if self._is_safe_component(item["name"], self.MAX_CHARACTER_LENGTH)
            ]
            payload = {"fetchedAt": time.time(), "items": items}

            try:
                self._atomic_write_json(self.operator_catalog_file, payload)
            except OSError as exc:
                logger.warning(f"保存干员列表缓存失败: {exc}")

            self._operator_catalog = payload
            return {**payload, "stale": False, "error": None}

    def cached_operator_names(self) -> List[str]:
        """不发请求，只返回已缓存的干员名，用于聊天里的拼写提示。"""
        catalog = self._operator_catalog

        if catalog is None:
            catalog = self._read_json_file(self.operator_catalog_file) or {}

        return [
            str(item.get("name", ""))
            for item in catalog.get("items", [])
            if isinstance(item, dict) and item.get("name")
        ]

    def _voice_text_path(self, character: str) -> Optional[Path]:
        if not self._is_safe_component(character, self.MAX_CHARACTER_LENGTH):
            return None

        return self._safe_path(self.voice_text_dir, f"{character}.json")

    def _store_voice_texts(self, character: str, texts: Dict[str, Any]) -> None:
        """下载或查询语音页时顺带缓存台词，管理页查看时就不必再请求 PRTS。"""
        path = self._voice_text_path(character)

        if path is None or not texts:
            return

        try:
            self._atomic_write_json(path, {"version": constants.VOICE_RECORD_VERSION,
                                           "fetchedAt": time.time(), "texts": texts})
        except OSError as exc:
            logger.debug(f"保存 {character} 的台词缓存失败: {exc}")

    async def get_voice_texts(self, character: str) -> Dict[str, Any]:
        """返回 {皮肤名或 "": {语音标题: {语言代码: 台词}}}，过期或缺失时从 PRTS 获取。"""
        base_character = self.resolve_operator_alias(character)
        path = self._voice_text_path(base_character)

        if path is None:
            return {}

        cached = self._read_json_file(path)

        if cached is not None and (
            cached.get("version") == constants.VOICE_RECORD_VERSION and
            time.time() - float(cached.get("fetchedAt", 0)) < constants.VOICE_TEXT_TTL
        ):
            return cached.get("texts") or {}

        try:
            record = await self._get_voice_record(base_character)
        except PRTSLookupError as exc:
            logger.debug(f"获取 {base_character} 的台词失败: {exc}")
            return (cached or {}).get("texts") or {}

        return record["texts"]

    def _lock_for(
        self,
        character: str,
    ) -> asyncio.Lock:
        """等待者使用同一个锁，空闲后自动释放。"""
        lock = self._download_locks.get(character)
        if lock is None:
            lock = asyncio.Lock()
            self._download_locks[character] = lock
        return lock

    def needs_voice_resource_remap(
        self,
        character: str,
        language: Optional[str] = None,
    ) -> bool:
        parsed = self._parse_character_reference(character)

        if not parsed:
            return False

        base_character = self.resolve_operator_alias(parsed[0])

        if language is not None:
            return (base_character, language) in self._voice_remap_pending

        return any(
            current_character == base_character
            for current_character, _ in self._voice_remap_pending
        )

    def build_download_plan(
        self, character: str, record: Dict[str, Any], include_skin: bool, ranks: str | List[str],
    ) -> List[Dict[str, Any]]:
        """预览和下载共用计划；本地编号表仅用于完整的旧缓存修复。"""
        base = self.resolve_operator_alias(self._base_character(character))
        selected_languages = set(constants.normalize_languages(ranks))
        plan = []
        packages = dict(self.skin_metadata.get(base, {}))
        for source in prts.voice_sources(record):
            language = source["language"]
            if language is None:
                continue
            directory = None
            if source["is_skin"]:
                packages = {resource_id: info for resource_id, info in packages.items()
                            if not (resource_id.startswith("local_")
                                    and info.get("name") == source["skin_name"]
                                    and resource_id != source["resource_id"])}
                directory = self._skin_directory_name(base, source["resource_id"], source["skin_name"], packages)
                packages[source["resource_id"]] = {"directory": directory}
            if language not in selected_languages or (source["is_skin"] and not include_skin):
                continue
            repairing = (base, language) in self._voice_remap_pending
            files = ({title: f"cn_{number:03d}.wav" for title, number in self.VOICE_RESOURCE_IDS.items()}
                     if repairing else record["files"])
            for title in self.VOICE_DESCRIPTIONS:
                if title not in files:
                    continue
                filename = str(files[title]).lower()
                if not re.fullmatch(r"cn_\d{3}\.wav", filename):
                    raise PRTSLookupError(f"语音文件名无效: {title}")
                plan.append({**source, "character": base, "voice": title,
                             "skin_directory": directory, "force_redownload": repairing,
                             "url": self._voice_url(source["voice_key"], filename)})
        return plan

    @staticmethod
    def _voice_url(voice_key: str, filename: str) -> str:
        parts = str(voice_key).strip().strip("/").split("/")
        if not all(parts) or any(part in {".", ".."} or "\\" in part for part in parts):
            raise PRTSLookupError("PRTS 语音资源路径无效")
        return f"{constants.PRTS_AUDIO_BASE_URL}/{quote('/'.join(parts), safe='/')}/{filename}"

    async def _download_with_retries(self, session, character, url, language, voice, **options):
        for attempt in range(self.DOWNLOAD_RETRIES):
            status, message = await self._download_single_voice(
                session, character, url, language, voice, **options,
            )
            if status != "failed" or message.startswith("目标文件在下载期间发生变化"):
                return status, message
            if attempt + 1 < self.DOWNLOAD_RETRIES:
                await asyncio.sleep(0.4 * (2**attempt))
        return status, message

    async def fetch_character_voices(
        self,
        character: str,
        auto_download_skin: bool,
        download_langs: str | List[str],
        *,
        require_no_failures: bool = False,
        progress: Optional[Callable[[int, int, str], None]] = None,
        follow_redirect: bool = True,
    ) -> Tuple[bool, str]:
        """下载角色语音。

        progress(已处理, 总数, 当前档案) 会在每条语音处理后调用。
        名称在 PRTS 不存在时，沿 PRTS 重定向（PRTS 维护的别称）找到标准名，
        记录为自定义别称后按标准名重新下载。
        """
        parsed = self._parse_character_reference(character)

        if not parsed:
            return False, "角色名称不合法"

        base_character = self.resolve_operator_alias(parsed[0])
        redirect_target = None

        selected_languages = constants.normalize_languages(download_langs)

        if not selected_languages:
            return (
                False,
                "没有选择任何有效语言",
            )

        async with self._lock_for(base_character):
            counts = {
                "downloaded": 0,
                "existed": 0,
                "not_found": 0,
                "failed": 0,
            }
            remap_failed_languages = set()
            remap_skipped_languages = set()
            remap_seen_languages = set()

            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=10,
            )

            try:
                async with aiohttp.ClientSession(
                    headers=self.DEFAULT_HEADERS,
                    timeout=timeout,
                ) as session:
                    try:
                        record = await self._get_voice_record(
                            base_character,
                            session=session,
                        )
                    except PRTSNotFoundError:
                        if not follow_redirect:
                            raise

                        redirect_target = await prts.resolve_title(
                            session,
                            base_character,
                        )

                        if (
                            not redirect_target
                            or redirect_target == base_character
                            or not self._is_safe_component(
                                redirect_target,
                                self.MAX_CHARACTER_LENGTH,
                            )
                        ):
                            raise

                        record = None

                    if record is not None and record.get("character", base_character) != base_character:
                        redirect_target = record["character"]
                        if not self._is_safe_component(redirect_target, self.MAX_CHARACTER_LENGTH):
                            raise PRTSLookupError("PRTS 返回的角色名称不合法")
                        record = None
                    plan = []
                    if record is not None:
                        sources = prts.voice_sources(record)
                        remap_skipped_languages = {
                            source["language"] for source in sources
                            if source["is_skin"] and not auto_download_skin
                        }
                        plan = self.build_download_plan(
                            base_character, record, auto_download_skin, selected_languages,
                        )
                        for source in sources:
                            if source["is_skin"] and source["language"] and auto_download_skin:
                                self._register_skin_metadata(
                                    base_character, source["label"], source["voice_key"], source["language"],
                                )

                    # 各条目标文件互不相同，有限并发下载；进度按完成顺序上报。
                    semaphore = asyncio.Semaphore(self.DOWNLOAD_CONCURRENCY)
                    done = 0

                    async def download(item: Dict[str, Any]) -> None:
                        nonlocal done
                        language = item["language"]
                        remap_seen_languages.add(language)
                        async with semaphore:
                            status, message = await self._download_with_retries(
                                session, base_character, item["url"], language, item["voice"],
                                skin_directory=item["skin_directory"],
                                force_redownload=item["force_redownload"],
                            )
                        done += 1
                        counts[status] += 1
                        display_name = (f"{base_character}皮肤[{item['skin_name']}]"
                                        if item["is_skin"] else base_character)
                        if progress:
                            progress(done, len(plan), f"{display_name} {self.LANGUAGE_MAP[language]['name']}")
                        if status == "failed":
                            remap_failed_languages.add(language)
                            logger.warning(f"下载失败 {display_name}/{language}/{item['voice']}: {message}")

                    await asyncio.gather(*(download(item) for item in plan))

                    for language in remap_seen_languages:
                        if language not in remap_failed_languages and (
                            language not in remap_skipped_languages
                        ):
                            self._voice_remap_pending.discard(
                                (base_character, language)
                            )

                    if not self._voice_remap_pending:
                        self._voice_resource_map_version = (
                            self.VOICE_RESOURCE_MAP_VERSION
                        )

                    if redirect_target is None:
                        (
                            image_ok,
                            image_message,
                        ) = await self.fetch_character_image(
                            base_character,
                            session=session,
                            image_url=record.get("avatar_url"),
                        )

                        if not image_ok:
                            logger.debug(f"获取头像跳过 {base_character}: {image_message}")

            except PRTSLookupError as exc:
                logger.warning(f"获取 {base_character} 的 PRTS 记录失败: {exc}")
                return False, str(exc)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                logger.warning(f"下载 {base_character} 时网络异常: {exc}")
                return (
                    False,
                    f"网络请求失败: {exc}",
                )
            except Exception as exc:
                logger.exception(f"下载语音或头像异常: {exc}")
                return False, str(exc)

            if redirect_target is None:
                await asyncio.to_thread(self.scan_voice_files)

        if redirect_target is not None:
            original = parsed[0].strip()
            logger.info(f"PRTS 将 {original} 重定向到 {redirect_target}，按标准名下载")
            # 记为自定义别称，之后播放、绑定都能继续用原来的叫法。
            self.add_operator_alias(original, redirect_target)
            success, summary = await self.fetch_character_voices(
                redirect_target,
                auto_download_skin,
                download_langs,
                require_no_failures=require_no_failures,
                progress=progress,
                follow_redirect=False,
            )
            return success, f"{original} 即 {redirect_target}。{summary}"

        success = (counts["downloaded"] > 0 or counts["existed"] > 0) and (
            not require_no_failures or counts["failed"] == 0
        )

        summary = (
            "下载完成："
            f"新增 {counts['downloaded']}，"
            f"已存在 {counts['existed']}，"
            f"不存在 {counts['not_found']}，"
            f"失败 {counts['failed']}"
        )

        return success, summary

    async def migrate_legacy_skin_directories(
        self,
        download_langs: str | List[str],
    ) -> None:
        """
        把 角色/skin/语言/*.wav 旧结构迁移到具名皮肤目录。

        旧文件只用于确认需要迁移的语言。只有 PRTS 下载成功且新结构中
        已有对应语音后，才删除该语言的旧目录；失败时保留供下次启动重试。
        """
        migrations: Dict[str, Dict[str, List[str]]] = {}

        try:
            character_dirs = list(self.voices_dir.iterdir())
        except OSError as exc:
            logger.warning(f"检查旧版皮肤目录失败: {exc}")
            return

        for character_dir in character_dirs:
            if not character_dir.is_dir() or not self._is_safe_component(
                character_dir.name,
                self.MAX_CHARACTER_LENGTH,
            ):
                continue

            skin_root = character_dir / "skin"
            legacy_languages = {}

            for language in self.LANGUAGE_MAP:
                voices = self._scan_language_dir(skin_root / language)

                if voices:
                    legacy_languages[language] = voices

            if legacy_languages:
                migrations[character_dir.name] = legacy_languages

        if not migrations:
            return

        configured_languages = constants.normalize_languages(download_langs)

        for character, legacy_languages in migrations.items():
            selected_languages = list(dict.fromkeys([*configured_languages, *legacy_languages]))

            logger.info(f"检测到 {character} 的旧版皮肤目录，正在从 PRTS 迁移具名皮肤")
            success, message = await self.fetch_character_voices(
                character,
                True,
                selected_languages,
                require_no_failures=True,
            )

            if not success:
                logger.warning(f"{character} 旧版皮肤迁移暂缓，已保留原文件: {message}")
                continue

            await asyncio.to_thread(self.scan_voice_files)
            packages = self.skin_voice_index.get(character, {})
            skin_root = self.voices_dir / character / "skin"

            for language, old_voices in legacy_languages.items():
                new_voice_sets = [
                    set(languages.get(language, []))
                    for languages in packages.values()
                    if languages.get(language)
                ]
                skin_voices = set().union(*new_voice_sets) if new_voice_sets else set()
                missing_voices = [
                    voice for voice in old_voices if voice not in skin_voices
                ]
                verified = bool(new_voice_sets) and not missing_voices

                if not verified:
                    package_names = [
                        str(
                            self.skin_metadata.get(character, {})
                            .get(resource_id, {})
                            .get("name", resource_id)
                        )
                        for resource_id in packages
                        if packages[resource_id].get(language)
                    ]
                    logger.warning(
                        f"{character}/{language} 的新皮肤资源未完整确认，"
                        f"旧文件 {len(old_voices)} 个，"
                        f"皮肤文件 {len(skin_voices)} 个，"
                        f"缺失 {len(missing_voices)} 个，保留旧目录；"
                        f"新皮肤: {', '.join(package_names) or '无'}；"
                        f"缺失项: {', '.join(missing_voices[:8])}"
                        f"{' ...' if len(missing_voices) > 8 else ''}"
                    )
                    continue

                legacy_dir = skin_root / language

                logger.info(
                    f"{character}/{language} 的新皮肤资源已完整确认："
                    f"旧文件 {len(old_voices)} 个，"
                    f"皮肤文件 {len(skin_voices)} 个"
                )

                try:
                    resolved_legacy = legacy_dir.resolve()
                    resolved_skin_root = skin_root.resolve()
                    resolved_legacy.relative_to(resolved_skin_root)

                    if (
                        resolved_legacy.parent != resolved_skin_root
                        or resolved_legacy.name != language
                        or language not in self.LANGUAGE_MAP
                    ):
                        raise ValueError("旧目录路径校验失败")

                    shutil.rmtree(resolved_legacy)
                    logger.info(f"已迁移并移除旧版皮肤目录: {legacy_dir}")
                except (OSError, RuntimeError, ValueError) as exc:
                    logger.warning(f"移除旧版皮肤目录失败 {legacy_dir}: {exc}")

        await asyncio.to_thread(self.scan_voice_files)

    async def refresh_local_skin_metadata(self) -> None:
        """
        为旧版已分目录、但尚无 PRTS 稳定 ID 的皮肤补齐元数据。

        local_* ID 只用于离线过渡。这里仅请求角色语音页补齐映射，
        不重新下载音频；后续正常下载仍会复用合法 WAV，只补缺失或损坏项。
        """
        pending = []

        for character, packages in self.skin_voice_index.items():
            if any(resource_id.startswith("local_") for resource_id in packages):
                pending.append(character)

        if not pending:
            return

        timeout = aiohttp.ClientTimeout(
            total=30,
            connect=10,
        )

        async with aiohttp.ClientSession(
            headers=self.DEFAULT_HEADERS,
            timeout=timeout,
        ) as session:
            records = await self.get_voice_records(session, pending)
            for character in pending:
                logger.info(f"正在为 {character} 的现有皮肤目录补齐 PRTS 稳定索引")

                try:
                    record = records.get(character)
                    if record is None:
                        raise PRTSNotFoundError(f"未找到 {character} 的语音记录")
                    for source in prts.voice_sources(record):
                        if not source["is_skin"] or source["language"] is None:
                            continue
                        self._register_skin_metadata(
                            character, source["label"], source["voice_key"], source["language"],
                        )
                except (
                    PRTSLookupError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ) as exc:
                    logger.warning(
                        f"{character} 的皮肤稳定索引暂未补齐，将在下次启动重试: {exc}"
                    )

        await asyncio.to_thread(self.scan_voice_files)

        for character in pending:
            if any(
                resource_id.startswith("local_")
                for resource_id in self.skin_voice_index.get(character, {})
            ):
                logger.warning(
                    f"{character} 仍有无法与 PRTS 对应的本地皮肤目录，已保留离线索引"
                )

    # ================== 语言目录修正 ==================

    async def migrate_language_routing(self) -> Dict[str, Dict[str, str]]:
        """修正 3.8.0 之前语言判定错误、放错目录的语音。

        旧版只认标签里的“日/英/韩/方/意”，其余一律当中文，造成：
        - 联动干员只有一条“联动”标签，日语语音被存进 cn/；
        - 西班牙语、俄语等额外配音被当成中文，可能覆盖 cn/ 里的中文语音；
        - 皮肤名含“方”等字的皮肤语音（如令的“方遒卷”）进了错误的语言目录。

        按 PRTS 当前的标签找出旧规则可能放错的目录，再用 CDN 文件的内容摘要逐个核对，
        确认是放错的文件才移到正确目录或隔离，并补下载被腾空的语音。内容对不上的文件
        （例如用户自己导入的）保持原样。

        返回 {干员: {旧语言: 新语言}}，供上层修正快捷绑定。网络失败时不标记完成，
        下次启动重试；已处理过的文件不会被重复处理。
        """
        if self._language_routing_version >= constants.LANGUAGE_ROUTING_VERSION and not self._routing_refills:
            return self._routing_moves

        characters = sorted(
            {
                name
                for name in self.voice_files
                if (parsed := self._parse_character_reference(name)) and not parsed[1]
            }
            | set(self.skin_voice_index)
        )
        moved = self._routing_moves
        counts = {"moved": 0, "quarantined": 0}
        complete = True

        async with aiohttp.ClientSession(
            headers=self.DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=60, connect=10),
        ) as session:
            records = await self.get_voice_records(session, characters)
            for character in characters:
                record = records.get(character)
                if record is None:
                    complete = False
                    continue
                async with self._lock_for(character):
                    complete = await self._fix_character_routing(
                        session, character, record, moved, {}, counts,
                    ) and complete

            for key, job in list(self._routing_refills.items()):
                async with self._lock_for(job["character"]):
                    status, message = await self._download_with_retries(
                        session, job["character"], job["url"], job["language"], job["voice"],
                        skin_directory=job.get("skin_directory"),
                    )
                if status in {"downloaded", "existed", "not_found"}:
                    with self.file_lock:
                        self._routing_refills.pop(key, None)
                        self.save_voice_index()
                else:
                    complete = False
                    logger.warning(f"迁移补下载失败，保留重试任务: {message}")

        await asyncio.to_thread(self.scan_voice_files)

        # 文件已经挪走的语言不再需要旧编号校正，避免这条记录永远清不掉。
        for character in {self._base_character(name) for name in moved}:
            present = set(self.voice_files.get(character, {}))

            for languages in self.skin_voice_index.get(character, {}).values():
                present.update(languages)

            self._voice_remap_pending = {
                (current, language)
                for current, language in self._voice_remap_pending
                if current != character or language in present
            }

        with self.file_lock:
            self._routing_moves = moved
            if complete and not self._routing_refills:
                self._language_routing_version = constants.LANGUAGE_ROUTING_VERSION
            self.save_voice_index()

        if counts["moved"] or counts["quarantined"]:
            logger.info(
                "语言目录修正完成："
                f"移回正确目录 {counts['moved']} 条，"
                f"隔离 {counts['quarantined']} 条"
            )

        if not complete:
            logger.warning("语言目录修正有部分文件未能核对，将在下次启动重试")

        return moved

    def _valid_refill(self, job: Dict[str, Any]) -> bool:
        return (
            self._is_safe_component(job.get("character"), self.MAX_CHARACTER_LENGTH)
            and isinstance(job.get("language"), str)
            and job["language"] in self.LANGUAGE_MAP
            and isinstance(job.get("voice"), str)
            and job["voice"] in self.VOICE_RESOURCE_IDS
            and (job.get("skin_directory") is None or self._is_safe_component(
                job["skin_directory"], self.MAX_SKIN_ID_LENGTH))
            and isinstance(job.get("url"), str)
            and job["url"].startswith(constants.PRTS_AUDIO_BASE_URL + "/")
            and self._is_trusted_prts_url(job["url"])
        )

    def _queue_routing_refill(self, character, source, voice, scope):
        filename = f"cn_{self.VOICE_RESOURCE_IDS[voice]:03d}.wav"
        job = {"character": character, "language": source["language"], "voice": voice,
               "skin_directory": scope.name if source["is_skin"] else None,
               "url": self._voice_url(source["voice_key"], filename)}
        key = hashlib.sha256(json.dumps(job, sort_keys=True).encode("utf-8")).hexdigest()
        self._routing_refills[key] = job
        self.save_voice_index()

    def _routing_scope_dir(
        self,
        character: str,
        label: str,
        voice_key: str,
    ) -> Optional[Path]:
        """语言标签对应的本地目录：基础语音为干员目录，皮肤语音为对应的皮肤目录。"""
        root = self._safe_path(self.voices_dir, character)

        if root is None or not self._is_skin_label(label):
            return root

        resource_id = self._skin_resource_id_from_key(voice_key)
        info = self.skin_metadata.get(character, {}).get(resource_id) or {}
        directory = str(info.get("directory", "")).strip()

        if not self._is_safe_component(directory, self.MAX_SKIN_ID_LENGTH):
            return None

        return self._safe_path(root, "skin", directory)

    def _local_voice_files(self, directory: Path) -> Dict[str, Tuple[Path, int]]:
        """目录下名称合法的 WAV：{语音: (路径, 字节数)}，不跟随符号链接。"""
        result = {}

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    name, suffix = os.path.splitext(entry.name)

                    try:
                        if (
                            suffix.lower() == ".wav"
                            and name in self.VOICE_RESOURCE_IDS
                            and not entry.is_symlink()
                            and entry.is_file(follow_symlinks=False)
                        ):
                            result[name] = (
                                Path(entry.path),
                                entry.stat(follow_symlinks=False).st_size,
                            )
                    except OSError:
                        continue
        except OSError:
            return {}

        return result

    async def _remote_voice_sizes(
        self,
        session: aiohttp.ClientSession,
        voice_key: str,
        numbers: List[int],
    ) -> Optional[Dict[int, int]]:
        """用 HEAD 查询 PRTS 上各编号语音的字节数。

        404 的编号不出现在结果里；有请求在重试后仍失败时返回 None，调用方不能据此下结论。
        """
        encoded_key = quote(str(voice_key).strip().strip("/"), safe="/")
        semaphore = asyncio.Semaphore(constants.ROUTING_HEAD_CONCURRENCY)
        failed = False

        async def head(number: int) -> Tuple[int, Optional[int]]:
            nonlocal failed
            url = f"{constants.PRTS_AUDIO_BASE_URL}/{encoded_key}/cn_{number:03d}.wav"

            async with semaphore:
                for attempt in range(self.DOWNLOAD_RETRIES):
                    try:
                        # 必须声明不压缩，否则 CDN 返回的 Content-Length 不是原始文件大小。
                        async with session.head(
                            url,
                            headers={"Accept-Encoding": "identity"},
                            allow_redirects=True,
                        ) as response:
                            if response.status == 404:
                                return number, None

                            if response.status == 200:
                                length = response.headers.get("Content-Length", "")
                                if length.isdigit():
                                    return number, int(length)
                                failed = True
                                return number, None
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass

                    if attempt + 1 < self.DOWNLOAD_RETRIES:
                        await asyncio.sleep(0.4 * (2**attempt))

            failed = True
            return number, None

        results = await asyncio.gather(*(head(number) for number in numbers))

        if failed:
            return None

        return {number: size for number, size in results if size is not None}

    async def _remote_voice_digest(self, session, voice_key, number):
        url = self._voice_url(voice_key, f"cn_{number:03d}.wav")
        for attempt in range(self.DOWNLOAD_RETRIES):
            try:
                async with session.get(url, headers={"Accept-Encoding": "identity"}) as response:
                    if response.status != 200:
                        raise PRTSLookupError(f"内容核对 HTTP {response.status}")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > self.MAX_VOICE_BYTES:
                            raise PRTSLookupError("内容核对文件过大")
                    if not self._looks_like_wav(data):
                        raise PRTSLookupError("内容核对收到无效 WAV")
                    return hashlib.sha256(data).digest()
            except (PRTSLookupError, aiohttp.ClientError, asyncio.TimeoutError):
                if attempt + 1 < self.DOWNLOAD_RETRIES:
                    await asyncio.sleep(0.4 * (2**attempt))
        return None

    @staticmethod
    def _local_digest(path):
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
            return digest.digest()

    async def _fix_character_routing(
        self,
        session: aiohttp.ClientSession,
        character: str,
        record: Dict[str, Any],
        moved: Dict[str, Dict[str, str]],
        refills: Dict[str, set],
        counts: Dict[str, int],
    ) -> bool:
        """处理单个干员，返回是否所有可疑文件都核对完毕。"""
        numbers = sorted(self.VOICE_RESOURCE_IDS.values())
        size_cache: Dict[str, Optional[Dict[int, int]]] = {}
        digest_cache = {}

        async def matches(voice_key, sizes, size, digest):
            for number, remote_size in sizes.items():
                if remote_size != size:
                    continue
                key = (voice_key, number)
                if key not in digest_cache:
                    digest_cache[key] = await self._remote_voice_digest(session, voice_key, number)
                if digest_cache[key] is None:
                    return None
                if digest_cache[key] == digest:
                    return number
            return False

        async def sizes_for(voice_key: str) -> Optional[Dict[int, int]]:
            if voice_key not in size_cache:
                size_cache[voice_key] = await self._remote_voice_sizes(
                    session,
                    voice_key,
                    numbers,
                )
            return size_cache[voice_key]

        labels = []
        complete = True

        sources = prts.voice_sources(record)
        for source in sources:
            label, voice_key = source["label"], source["voice_key"]
            scope = self._routing_scope_dir(character, label, voice_key)
            if scope is None and source["is_skin"] and (self.voices_dir / character / "skin").exists():
                complete = False

            if scope is not None:
                labels.append(
                    (
                        label,
                        voice_key,
                        scope,
                        self._legacy_language_from_label(label),
                        source["language"],
                    )
                )

        for label, voice_key, scope, old, new in labels:
            if old == new:
                continue

            local = self._local_voice_files(scope / old)

            if not local:
                continue

            wrong = await sizes_for(voice_key)

            if wrong is None:
                complete = False
                continue

            wrong_sizes = set(wrong.values())
            candidates = {
                voice: item for voice, item in local.items() if item[1] in wrong_sizes
            }

            if not candidates:
                continue

            # 同一目录按新规则本该存放的来源；大小与它们相同的文件无法区分，按原样保留。
            expected_keys = [
                other_key
                for _, other_key, other_scope, _, other_new in labels
                if other_scope == scope and other_new == old and other_key != voice_key
            ]
            expected_records = []
            expected_ok = True

            for expected_key in expected_keys:
                sizes = await sizes_for(expected_key)

                if sizes is None:
                    expected_ok = False
                    break

                expected_records.append((expected_key, sizes))

            if not expected_ok:
                complete = False
                continue

            for voice, (path, size) in candidates.items():
                expected_ok = True
                try:
                    signature = self._file_signature(path)
                    digest = await asyncio.to_thread(self._local_digest, path)
                except OSError:
                    complete = False
                    continue
                correct = False
                for expected_key, expected in expected_records:
                    match = await matches(expected_key, expected, size, digest)
                    if match is None:
                        expected_ok = False
                        break
                    if match:
                        correct = True
                        break
                if not expected_ok:
                    complete = False
                    continue
                if correct:
                    continue
                matched = await matches(voice_key, wrong, size, digest)
                if matched is None:
                    complete = False
                    continue
                if matched is False:
                    continue
                same_number = matched == self.VOICE_RESOURCE_IDS[voice]
                target = (
                    self._safe_path(scope, new, f"{voice}.wav")
                    if new is not None
                    else None
                )

                async with self.mutation_lock:
                    with self.file_lock:
                        if self._file_signature(path) != signature:
                            complete = False
                            continue
                        source = next(item for item in sources if item["label"] == label)
                        if target is not None and (not same_number or not self._is_valid_wav_file(target)):
                            self._queue_routing_refill(character, source, voice, scope)
                        for expected_key in expected_keys:
                            expected_source = next(item for item in sources if item["voice_key"] == expected_key)
                            self._queue_routing_refill(character, expected_source, voice, scope)
                        if target is not None:
                            reference = (f"{character}皮肤[{source['resource_id']}]"
                                         if source["is_skin"] else character)
                            moved.setdefault(reference, {})[old] = new
                            self._routing_moves = moved
                            self.save_voice_index()
                        try:
                            if target is not None and same_number and not target.exists():
                                target.parent.mkdir(parents=True, exist_ok=True)
                                os.replace(path, target)
                                counts["moved"] += 1
                            else:
                                if self._quarantine_wav(path, "misrouted", "放错语言目录") is None:
                                    complete = False
                                    continue
                                counts["quarantined"] += 1
                        except OSError as exc:
                            logger.warning(f"移动放错目录的语音失败 {path}: {exc}")
                            complete = False
                            continue
        return complete

    async def _download_single_voice(
        self,
        session: aiohttp.ClientSession,
        character: str,
        url: str,
        lang: str,
        filename: str,
        *,
        skin_directory: Optional[str] = None,
        force_redownload: bool = False,
    ) -> Tuple[str, str]:
        """
        返回 downloaded/existed/not_found/failed。
        """
        parsed = self._parse_character_reference(character)

        if not parsed or parsed[1]:
            return (
                "failed",
                "角色名称不合法",
            )

        base_character = self.resolve_operator_alias(parsed[0])

        if lang not in self.LANGUAGE_MAP or filename not in self.VOICE_DESCRIPTIONS:
            return (
                "failed",
                "语言或语音名称不合法",
            )

        if skin_directory and not self._is_safe_component(
            skin_directory,
            self.MAX_SKIN_ID_LENGTH,
        ):
            return (
                "failed",
                "皮肤目录名不合法",
            )

        character_root = self._safe_path(
            self.voices_dir,
            base_character,
        )

        if character_root is None:
            return (
                "failed",
                "目标路径越界",
            )

        if skin_directory:
            save_dir = self._safe_path(
                character_root,
                "skin",
                skin_directory,
                lang,
            )
        else:
            save_dir = self._safe_path(
                character_root,
                lang,
            )

        if save_dir is None:
            return (
                "failed",
                "目标路径越界",
            )

        path = self._safe_path(
            save_dir,
            f"{filename}.wav",
        )

        if path is None:
            return (
                "failed",
                "目标文件路径越界",
            )

        try:
            with self.file_lock:
                save_dir.mkdir(parents=True, exist_ok=True)
                if path.is_file() and not force_redownload:
                    if self._is_valid_wav_file(path):
                        return "existed", "文件已存在"
                    if self._quarantine_invalid_wav(path) is None:
                        return "failed", "现有 WAV 已损坏且无法隔离"
                signature = self._file_signature(path)
        except OSError as exc:
            return (
                "failed",
                f"无法访问目标目录: {exc}",
            )

        try:
            async with session.get(
                url,
                allow_redirects=True,
            ) as response:
                if response.status == 404:
                    async with self.mutation_lock:
                        with self.file_lock:
                            if self._file_signature(path) != signature:
                                return "failed", "目标文件在下载期间发生变化，请重新操作"
                            if force_redownload and path.is_file() and self._quarantine_stale_wav(path) is None:
                                return "failed", "新编号资源不存在，且旧编号缓存无法隔离"

                    return (
                        "not_found",
                        "文件不存在(404)",
                    )

                if response.status != 200:
                    return (
                        "failed",
                        f"HTTP错误: {response.status}",
                    )

                content_length = response.headers.get("Content-Length")

                if content_length:
                    try:
                        if int(content_length) > self.MAX_VOICE_BYTES:
                            return (
                                "failed",
                                "音频文件过大",
                            )
                    except ValueError:
                        pass

                data = bytearray()

                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)

                    if len(data) > self.MAX_VOICE_BYTES:
                        return (
                            "failed",
                            "音频文件过大",
                        )

                if not self._looks_like_wav(data):
                    return (
                        "failed",
                        "响应内容不是有效 WAV",
                    )

            temp_name = self._write_temp_file(save_dir, path.name, bytes(data))

            try:
                async with self.mutation_lock:
                    with self.file_lock:
                        if self._file_signature(path) != signature:
                            return "failed", "目标文件在下载期间发生变化，请重新操作"
                        os.replace(temp_name, path)
            finally:
                Path(temp_name).unlink(missing_ok=True)

            return (
                "downloaded",
                "下载成功",
            )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            return (
                "failed",
                f"网络请求失败: {exc}",
            )
        except OSError as exc:
            return (
                "failed",
                f"写入文件失败: {exc}",
            )
        except Exception as exc:
            logger.warning(f"下载语音失败 {url}: {exc}")
            return (
                "failed",
                f"未知错误: {exc}",
            )

    @staticmethod
    def _looks_like_wav(
        data: Union[bytes, bytearray],
    ) -> bool:
        return VoiceManager._valid_wav_stream(BytesIO(data), len(data))

    @staticmethod
    def _valid_wav_stream(handle, size: int) -> bool:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            return False
        end = int.from_bytes(header[4:8], "little") + 8
        if end > size or end < 44:
            return False
        has_format = has_audio = False
        while handle.tell() < end:
            chunk = handle.read(8)
            if len(chunk) != 8:
                return False
            length = int.from_bytes(chunk[4:], "little")
            next_chunk = handle.tell() + length + (length & 1)
            if next_chunk > end:
                return False
            if chunk[:4] == b"fmt ":
                if length < 16:
                    return False
                fmt = handle.read(16)
                if len(fmt) != 16 or not all((
                    int.from_bytes(fmt[0:2], "little"),
                    int.from_bytes(fmt[2:4], "little"),
                    int.from_bytes(fmt[4:8], "little"),
                    int.from_bytes(fmt[8:12], "little"),
                    int.from_bytes(fmt[12:14], "little"),
                )):
                    return False
                has_format = True
            elif chunk[:4] == b"data":
                has_audio = length > 0
            handle.seek(next_chunk)
        return has_format and has_audio

    @staticmethod
    def _file_signature(path: Path) -> Optional[Tuple[int, int, int]]:
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_ino
        except FileNotFoundError:
            return None

    @classmethod
    def _is_trusted_prts_url(cls, url: str) -> bool:
        """校验 URL 是否指向 PRTS 白名单主机。

        同时拒绝携带 userinfo 的地址，避免 https://prts.wiki@evil.com
        这类写法绕过主机判断。
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return False

        if parsed.username or parsed.password or parsed.scheme != "https":
            return False

        hostname = parsed.hostname

        if not hostname:
            return False

        return any(
            hostname == allowed or hostname.endswith(f".{allowed}")
            for allowed in constants.PRTS_ALLOWED_HOSTS
        )

    async def _get_voice_record(
        self,
        character: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Dict[str, Any]:
        """通过 PRTS API 读取语音记录页，见 prts.parse_voice_record。"""
        parsed = self._parse_character_reference(character)

        if not parsed:
            raise PRTSLookupError("角色名称不合法")

        base_character = self.resolve_operator_alias(parsed[0])
        lock = self._record_lock_for(base_character)
        async with lock:
            cached = self._cached_voice_record(base_character)
            if cached is not None:
                return cached
            if session is None:
                async with aiohttp.ClientSession(
                    headers=self.DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                ) as owned_session:
                    record = await prts.fetch_voice_record(owned_session, base_character)
            else:
                record = await prts.fetch_voice_record(session, base_character)
            self._cache_voice_record(base_character, record)
            return record

    def _record_lock_for(self, base_character):
        lock = self._record_locks.get(base_character)
        if lock is None:
            lock = asyncio.Lock()
            self._record_locks[base_character] = lock
        return lock

    def _cached_voice_record(self, character: str) -> Optional[Dict[str, Any]]:
        cached = self._voice_records.get(character)
        if cached is None:
            cached = self._read_json_file(self.voice_record_dir / f"{character}.json")
        if not isinstance(cached, dict) or cached.get("version") != constants.VOICE_RECORD_VERSION:
            return None
        try:
            fresh = 0 <= time.time() - float(cached.get("fetchedAt", 0)) < constants.VOICE_RECORD_TTL
        except (TypeError, ValueError):
            return None
        record = cached.get("record")
        if fresh and isinstance(record, dict) and record.get("paths") and record.get("files"):
            self._voice_records[character] = cached
            return record
        return None

    def _cache_voice_record(self, character: str, record: Dict[str, Any]) -> None:
        payload = {"version": constants.VOICE_RECORD_VERSION, "fetchedAt": time.time(), "record": record}
        self._voice_records[character] = payload
        self._store_voice_texts(character, record.get("texts", {}))
        try:
            self._atomic_write_json(self.voice_record_dir / f"{character}.json", payload)
        except OSError as exc:
            logger.warning(f"保存资源记录缓存失败 {character}: {exc}")

    async def get_voice_records(self, session, characters: List[str]) -> Dict[str, Dict[str, Any]]:
        result = {}
        missing = []
        for character in dict.fromkeys(characters):
            if not self._is_safe_component(character, self.MAX_CHARACTER_LENGTH):
                continue
            record = self._cached_voice_record(character)
            if record is None:
                missing.append(character)
            else:
                result[character] = record
        if missing:
            async with AsyncExitStack() as stack:
                for character in sorted(missing):
                    await stack.enter_async_context(self._record_lock_for(character))
                remaining = []
                for character in missing:
                    record = self._cached_voice_record(character)
                    if record is None:
                        remaining.append(character)
                    else:
                        result[character] = record
                if remaining:
                    fetched = await prts.fetch_voice_records(session, remaining)
                    for character, record in fetched.items():
                        self._cache_voice_record(character, record)
                    result.update(fetched)
        return result

    async def ensure_assets(self) -> None:
        try:
            missing = set()

            for character in self.voice_index:
                parsed = self._parse_character_reference(character)

                if not parsed:
                    continue

                base_character = self.resolve_operator_alias(parsed[0])
                avatar_path = self.assets_dir / f"{base_character}.png"

                if not self._is_valid_png_file(avatar_path):
                    missing.add(base_character)

            if not missing:
                return

            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=10,
            )

            async with aiohttp.ClientSession(
                headers=self.DEFAULT_HEADERS,
                timeout=timeout,
            ) as session:
                for character in sorted(missing):
                    (
                        success,
                        message,
                    ) = await self.fetch_character_image(
                        character,
                        session=session,
                    )

                    if not success:
                        logger.debug(f"获取头像跳过 {character}: {message}")

        except Exception as exc:
            logger.warning(f"资源检查过程出现异常: {exc}")

    async def fetch_character_image(
        self,
        base_char: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
        image_url: Optional[str] = None,
    ) -> Tuple[bool, str]:
        parsed = self._parse_character_reference(base_char)

        if not parsed:
            return (
                False,
                "角色名称不合法",
            )

        base_char = self.resolve_operator_alias(parsed[0])
        existing = self._safe_path(self.assets_dir, f"{base_char}.png")
        if existing is not None and self._is_valid_png_file(existing):
            return True, "头像已存在"

        owns_session = session is None

        if owns_session:
            session = aiohttp.ClientSession(
                headers=self.DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                ),
            )

        try:
            assert session is not None

            try:
                if image_url is None:
                    cached = self._cached_voice_record(base_char)
                    image_url = (cached or {}).get("avatar_url")
                if image_url is None:
                    image_url = await prts.fetch_file_url(
                        session, constants.PRTS_AVATAR_FILE_TITLE.format(character=base_char),
                    )
            except PRTSLookupError as exc:
                return False, f"获取头像地址失败: {exc}"

            if not image_url:
                return (
                    False,
                    "未找到头像图片链接",
                )

            if not self._is_trusted_prts_url(image_url):
                return (
                    False,
                    "头像链接来源不可信",
                )

            async with session.get(image_url) as image_response:
                if image_response.status != 200:
                    return (
                        False,
                        (f"下载头像失败: HTTP {image_response.status}"),
                    )

                content_length = image_response.headers.get("Content-Length")

                if content_length:
                    try:
                        if int(content_length) > self.MAX_IMAGE_BYTES:
                            return (
                                False,
                                "头像文件过大",
                            )
                    except ValueError:
                        pass

                image_data = bytearray()

                async for chunk in image_response.content.iter_chunked(64 * 1024):
                    image_data.extend(chunk)

                    if len(image_data) > self.MAX_IMAGE_BYTES:
                        return (
                            False,
                            "头像文件过大",
                        )

            try:
                with PILImage.open(BytesIO(image_data)) as image:
                    image.verify()

                    if image.format != "PNG":
                        raise ValueError("图片格式不是 PNG")
            except (OSError, ValueError):
                return (
                    False,
                    "响应内容不是有效 PNG",
                )

            save_path = self._safe_path(
                self.assets_dir,
                f"{base_char}.png",
            )

            if save_path is None:
                return (
                    False,
                    "头像保存路径越界",
                )

            self._atomic_write_bytes(save_path, bytes(image_data))

            logger.info(f"下载 {base_char} 头像成功")

            return True, "下载成功"

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            return (
                False,
                f"网络错误: {exc}",
            )
        except OSError as exc:
            return (
                False,
                f"写入头像失败: {exc}",
            )
        except Exception as exc:
            logger.warning(f"获取头像失败 {base_char}: {exc}")
            return False, str(exc)
        finally:
            if owns_session and session is not None:
                await session.close()
