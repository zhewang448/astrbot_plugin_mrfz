import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup
from PIL import Image as PILImage

from . import constants


class PRTSLookupError(Exception):
    """PRTS 角色页请求或解析失败，并携带可直接展示的原因。"""


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

        # 使用 OrderedDict 实现 LRU 缓存，避免无限增长
        self._download_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_locks = constants.MAX_DOWNLOAD_LOCKS

        # v3 及更早版本按连续编号下载过语音，已有 WAV 可能内容与名称错位。
        # 迁移按“角色 + 语言”记录，只有整组请求没有真实失败时才清除。
        self._voice_resource_map_version = self.VOICE_RESOURCE_MAP_VERSION
        self._voice_remap_pending: set[Tuple[str, str]] = set()

        for directory in (
            self.data_dir,
            self.voices_dir,
            self.assets_dir,
        ):
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

        self._load_skin_metadata()
        self.scan_voice_files()

    def _load_skin_metadata(self) -> None:
        index_path = self.data_dir / "voice_index.json"

        if not index_path.is_file():
            return

        try:
            with index_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)

            if payload.get("version") not in constants.SUPPORTED_VOICE_INDEX_VERSIONS:
                return

            try:
                self._voice_resource_map_version = int(
                    payload.get("voice_resource_map_version", 0)
                )
            except (TypeError, ValueError):
                self._voice_resource_map_version = 0

            raw_pending = payload.get("voice_remap_pending", [])
            if isinstance(raw_pending, list):
                self._voice_remap_pending = {
                    (item[0], item[1])
                    for item in raw_pending
                    if (
                        isinstance(item, list)
                        and len(item) == 2
                        and self._is_safe_component(
                            item[0],
                            self.MAX_CHARACTER_LENGTH,
                        )
                        and item[1] in self.LANGUAGE_MAP
                    )
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

        try:
            for path in directory.iterdir():
                try:
                    if (
                        path.is_file()
                        and path.suffix.lower() == ".wav"
                        and path.stem in allowed
                        and self._is_valid_wav_file(path)
                        and self._path_is_within(
                            path,
                            directory,
                        )
                    ):
                        found.append(path.stem)
                except OSError:
                    continue
        except OSError:
            return []

        order = {name: index for index, name in enumerate(self.VOICE_DESCRIPTIONS)}

        return sorted(
            set(found),
            key=lambda name: order[name],
        )

    @classmethod
    def _is_valid_wav_file(cls, path: Path) -> bool:
        """以最小 RIFF/WAVE 头校验兼容现有合法 WAV。"""
        try:
            if not path.is_file() or path.stat().st_size < 12:
                return False

            with path.open("rb") as handle:
                return cls._looks_like_wav(handle.read(12))
        except OSError:
            return False

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
            suffix = 1

            while candidate.exists():
                candidate = target.with_name(f"{target.name}.{suffix}")
                suffix += 1

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
    ) -> None:
        languages = {
            language: voices for language, voices in languages.items() if voices
        }

        if not languages:
            return

        self.voice_files[character] = languages
        self.voice_index[character] = sorted(
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
    ) -> Optional[str]:
        info = self.skin_metadata.get(character, {}).get(resource_id)

        if not info:
            return None

        name = str(info.get("name", "")).strip()

        if not name:
            return None

        same_name_ids = [
            current_id
            for current_id in self.skin_voice_index.get(character, {})
            if self.skin_metadata.get(character, {}).get(current_id, {}).get("name")
            == name
        ]
        selector = name if len(same_name_ids) <= 1 else f"{name} · {resource_id}"
        return f"{character}皮肤[{selector}]"

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

        if not is_skin:
            reference = base_character
            if reference in self.voice_index:
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

        matches = []

        for resource_id in packages:
            info = self.skin_metadata.get(base_character, {}).get(resource_id, {})
            reference = self._skin_reference(base_character, resource_id)
            reference_selector = (
                self._parse_character_reference(reference)[2] if reference else None
            )
            aliases = {
                resource_id,
                str(info.get("name", "")).strip(),
                str(info.get("directory", "")).strip(),
                reference_selector,
            }

            if selector in aliases:
                if reference:
                    matches.append(reference)

        matches = sorted(set(matches))

        if len(matches) == 1:
            return matches[0], []

        return None, matches or options

    def scan_voice_files(self) -> None:
        """扫描真实、非空且名称合法的 WAV。"""
        self.voice_index.clear()
        self.voice_files.clear()
        self.skin_voice_index.clear()

        if not self.voices_dir.is_dir():
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

            self.skin_voice_index[character] = packages

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
                )

                if reference:
                    self._record_flat_character(
                        reference,
                        playable_languages,
                    )

            for language, voices in aggregate.items():
                aggregate[language] = self._sort_voice_names(voices)

            self._record_flat_character(
                f"{character}皮肤",
                aggregate,
            )

        if self._voice_resource_map_version < self.VOICE_RESOURCE_MAP_VERSION:
            if not self._voice_remap_pending:
                remap_targets = set()
                for character, languages in self.voice_files.items():
                    remap_targets.update(
                        (character, language) for language in languages
                    )

                for character, packages in self.skin_voice_index.items():
                    for languages in packages.values():
                        remap_targets.update(
                            (character, language) for language in languages
                        )

                self._voice_remap_pending = remap_targets

        payload = {
            "version": constants.VOICE_INDEX_VERSION,
            "voice_resource_map_version": (
                self._voice_resource_map_version
                if self._voice_remap_pending
                else self.VOICE_RESOURCE_MAP_VERSION
            ),
            "voice_remap_pending": sorted(self._voice_remap_pending),
            "voice_index": self.voice_index,
            "voice_files": self.voice_files,
            "skins": self.skin_voice_index,
            "skin_metadata": self.skin_metadata,
        }

        try:
            self._atomic_write_json(
                self.data_dir / "voice_index.json",
                payload,
            )
        except OSError as exc:
            logger.warning(f"保存语音索引失败: {exc}")

    def _sort_voice_names(
        self,
        voices: List[str],
    ) -> List[str]:
        order = {name: index for index, name in enumerate(self.VOICE_DESCRIPTIONS)}

        return sorted(
            {voice for voice in voices if voice in order},
            key=lambda voice: order[voice],
        )

    @staticmethod
    def _atomic_write_json(
        path: Path,
        payload: Any,
    ) -> None:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )

        try:
            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(
                temp_name,
                path,
            )
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass

            Path(temp_name).unlink(missing_ok=True)
            raise

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

        languages = self.voice_files.get(
            character.strip(),
            {},
        )

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
            resolved, _ = self.resolve_character_reference(character)

            if not resolved:
                return None

            resolved_selector = self._parse_character_reference(resolved)[2]
            resource_id = None
            directory = None

            for current_id in self.skin_voice_index.get(base_character, {}):
                info = self.skin_metadata.get(base_character, {}).get(current_id, {})
                reference = self._skin_reference(base_character, current_id)
                reference_selector = (
                    self._parse_character_reference(reference)[2] if reference else None
                )

                if resolved_selector in {
                    current_id,
                    info.get("name"),
                    info.get("directory"),
                    reference_selector,
                }:
                    resource_id = current_id
                    directory = info.get("directory")
                    break

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

        candidates = [candidate]

        if is_skin:
            candidates.append(
                self._safe_path(
                    character_root,
                    language,
                    f"{voice_name}.wav",
                )
            )

        for current_candidate in candidates:
            if current_candidate is None:
                continue

            try:
                if (
                    current_candidate.is_file()
                    and self._is_valid_wav_file(current_candidate)
                    and self._path_is_within(
                        current_candidate,
                        character_root,
                    )
                ):
                    return current_candidate
            except OSError:
                continue

        return None

    def choose_language(
        self,
        character: str,
        rank_config: str,
    ) -> str:
        available = self.voice_index.get(
            character,
            [],
        )

        if not available:
            return "nodownload"

        rank_to_language = {
            value["rank"]: language for language, value in self.LANGUAGE_MAP.items()
        }

        for rank in str(rank_config):
            language = rank_to_language.get(rank)

            if language in available:
                return language

        return available[0]

    @classmethod
    def _language_from_label(
        cls,
        label: str,
    ) -> str:
        if "日" in label:
            return "jp"
        if "英" in label:
            return "us"
        if "韩" in label:
            return "kr"
        if "方" in label:
            return "fy"
        if "意" in label:
            return "it"

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
        match = re.search(
            r"[（(]([^()（）]+)[）)]",
            label,
        )

        raw = match.group(1).strip() if match else ""

        raw = re.sub(
            r"[^\w\- .·()（）]",
            "_",
            raw,
            flags=re.UNICODE,
        ).strip(" ._")

        if not raw:
            digest = hashlib.sha256(voice_key.encode("utf-8")).hexdigest()[:10]

            raw = f"skin_{digest}"

        if len(raw) > cls.MAX_SKIN_ID_LENGTH:
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

            raw = f"{raw[: cls.MAX_SKIN_ID_LENGTH - 9]}_{digest}"

        return raw

    @classmethod
    def _skin_resource_id_from_key(
        cls,
        voice_key: str,
    ) -> str:
        normalized = str(voice_key).strip().strip("/")
        raw = normalized.rsplit("/", 1)[-1]
        raw = re.sub(
            r"[^\w\- .·()（）]",
            "_",
            raw,
            flags=re.UNICODE,
        ).strip(" ._")

        if not raw:
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
            raw = f"skin_{digest}"

        if len(raw) > cls.MAX_SKIN_ID_LENGTH:
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
            raw = f"{raw[: cls.MAX_SKIN_ID_LENGTH - 9]}_{digest}"

        return raw

    def _skin_directory_name(
        self,
        character: str,
        resource_id: str,
        display_name: str,
    ) -> str:
        packages = self.skin_metadata.setdefault(character, {})
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

    def _lock_for(
        self,
        character: str,
    ) -> asyncio.Lock:
        """获取角色下载锁，使用 LRU 缓存避免内存泄漏。"""
        if character in self._download_locks:
            # 移到末尾表示最近使用
            self._download_locks.move_to_end(character)
            return self._download_locks[character]

        lock = asyncio.Lock()
        self._download_locks[character] = lock

        # 超过限制时移除最老的锁
        if len(self._download_locks) > self._max_locks:
            self._download_locks.popitem(last=False)

        return lock

    def needs_voice_resource_remap(
        self,
        character: str,
        language: Optional[str] = None,
    ) -> bool:
        parsed = self._parse_character_reference(character)

        if not parsed:
            return False

        base_character = parsed[0]

        if language is not None:
            return (base_character, language) in self._voice_remap_pending

        return any(
            current_character == base_character
            for current_character, _ in self._voice_remap_pending
        )

    async def fetch_character_voices(
        self,
        character: str,
        auto_download_skin: bool,
        download_langs: str,
        *,
        require_no_failures: bool = False,
    ) -> Tuple[bool, str]:
        parsed = self._parse_character_reference(character)

        if not parsed:
            return False, "角色名称不合法"

        base_character = parsed[0]

        valid_ranks = {item["rank"] for item in self.LANGUAGE_MAP.values()}
        selected_ranks = {rank for rank in str(download_langs) if rank in valid_ranks}

        if not selected_ranks:
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
                    character_map = await self._get_character_id_map(
                        base_character,
                        session=session,
                    )

                    if not character_map:
                        return (
                            False,
                            (f"PRTS 返回了角色 {base_character} 的空语音记录"),
                        )

                    base_url = constants.PRTS_AUDIO_BASE_URL

                    for (
                        language_label,
                        voice_key,
                    ) in character_map.items():
                        if language_label == "语音key":
                            continue

                        language = self._language_from_label(language_label)

                        if self.LANGUAGE_MAP[language]["rank"] not in selected_ranks:
                            continue

                        is_skin = self._is_skin_label(language_label)

                        if is_skin and not auto_download_skin:
                            if (base_character, language) in self._voice_remap_pending:
                                remap_skipped_languages.add(language)
                            continue

                        remap_seen_languages.add(language)
                        force_redownload = (
                            base_character,
                            language,
                        ) in self._voice_remap_pending

                        skin_resource_id = None
                        skin_name = None
                        skin_directory = None

                        if is_skin:
                            (
                                skin_resource_id,
                                skin_name,
                                skin_directory,
                            ) = self._register_skin_metadata(
                                base_character,
                                language_label,
                                str(voice_key),
                                language,
                            )

                        encoded_key = quote(
                            str(voice_key).strip().strip("/"),
                            safe="/",
                        )

                        if not encoded_key:
                            counts["failed"] += len(self.VOICE_DESCRIPTIONS)
                            if force_redownload:
                                remap_failed_languages.add(language)
                            continue

                        display_name = (
                            (f"{base_character}皮肤[{skin_name}]")
                            if skin_resource_id
                            else base_character
                        )

                        logger.info(f"正在下载 {display_name} 的 {language} 语音...")

                        for description, file_number in self.VOICE_RESOURCE_IDS.items():
                            file_name = f"cn_{file_number:03d}.wav"
                            voice_url = f"{base_url}/{encoded_key}/{file_name}"

                            status = "failed"
                            message = ""

                            for attempt in range(self.DOWNLOAD_RETRIES):
                                (
                                    status,
                                    message,
                                ) = await self._download_single_voice(
                                    session,
                                    base_character,
                                    voice_url,
                                    language,
                                    description,
                                    skin_directory=skin_directory,
                                    force_redownload=force_redownload,
                                )

                                if status != "failed":
                                    break

                                if attempt + 1 < self.DOWNLOAD_RETRIES:
                                    await asyncio.sleep(0.4 * (2**attempt))

                            counts[status] += 1

                            if status == "failed":
                                if force_redownload:
                                    remap_failed_languages.add(language)
                                logger.warning(
                                    "下载失败 "
                                    f"{display_name}/"
                                    f"{language}/"
                                    f"{description}: "
                                    f"{message}"
                                )

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

                    (
                        image_ok,
                        image_message,
                    ) = await self.fetch_character_image(
                        base_character,
                        session=session,
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

            self.scan_voice_files()

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
        download_langs: str,
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

        configured_ranks = {
            rank
            for rank in str(download_langs)
            if rank in {item["rank"] for item in self.LANGUAGE_MAP.values()}
        }

        for character, legacy_languages in migrations.items():
            required_ranks = {
                self.LANGUAGE_MAP[language]["rank"] for language in legacy_languages
            }
            selected_ranks = "".join(
                sorted(
                    configured_ranks | required_ranks,
                    key=int,
                )
            )

            logger.info(f"检测到 {character} 的旧版皮肤目录，正在从 PRTS 迁移具名皮肤")
            success, message = await self.fetch_character_voices(
                character,
                True,
                selected_ranks,
                require_no_failures=True,
            )

            if not success:
                logger.warning(f"{character} 旧版皮肤迁移暂缓，已保留原文件: {message}")
                continue

            self.scan_voice_files()
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

        self.scan_voice_files()

    async def refresh_local_skin_metadata(self) -> None:
        """
        为旧版已分目录、但尚无 PRTS 稳定 ID 的皮肤补齐元数据。

        local_* ID 只用于离线过渡。这里仅请求角色语音页补齐映射，
        不重新下载音频；后续正常下载仍会复用合法 WAV，只补缺失或损坏项。
        """
        self.scan_voice_files()
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
            for character in pending:
                logger.info(f"正在为 {character} 的现有皮肤目录补齐 PRTS 稳定索引")

                try:
                    character_map = await self._get_character_id_map(
                        character,
                        session=session,
                    )

                    for language_label, voice_key in (character_map or {}).items():
                        if language_label == "语音key" or not self._is_skin_label(
                            language_label
                        ):
                            continue

                        language = self._language_from_label(language_label)
                        self._register_skin_metadata(
                            character,
                            language_label,
                            str(voice_key),
                            language,
                        )
                except (
                    PRTSLookupError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ) as exc:
                    logger.warning(
                        f"{character} 的皮肤稳定索引暂未补齐，将在下次启动重试: {exc}"
                    )

        self.scan_voice_files()

        for character in pending:
            if any(
                resource_id.startswith("local_")
                for resource_id in self.skin_voice_index.get(character, {})
            ):
                logger.warning(
                    f"{character} 仍有无法与 PRTS 对应的本地皮肤目录，已保留离线索引"
                )

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

        base_character = parsed[0]

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
            save_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            if path.is_file() and not force_redownload:
                if self._is_valid_wav_file(path):
                    return (
                        "existed",
                        "文件已存在",
                    )

                if self._quarantine_invalid_wav(path) is None:
                    return (
                        "failed",
                        "现有 WAV 已损坏且无法隔离",
                    )
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
                    if (
                        force_redownload
                        and path.is_file()
                        and self._quarantine_stale_wav(path) is None
                    ):
                        return (
                            "failed",
                            "新编号资源不存在，且旧编号缓存无法隔离",
                        )

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

            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=str(save_dir),
            )

            try:
                with os.fdopen(
                    fd,
                    "wb",
                ) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(
                    temp_name,
                    path,
                )
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass

                Path(temp_name).unlink(missing_ok=True)
                raise

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
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

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

    async def _get_character_id_map(
        self,
        character: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Optional[Dict[str, str]]:
        parsed = self._parse_character_reference(character)

        if not parsed:
            return None

        base_character = parsed[0]
        encoded_character = quote(
            base_character,
            safe="",
        )

        url = constants.PRTS_VOICE_PAGE_URL.format(character=encoded_character)

        owns_session = session is None

        if owns_session:
            session = aiohttp.ClientSession(
                headers=self.DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(
                    total=15,
                    connect=10,
                ),
            )

        try:
            assert session is not None
            html = None

            for attempt in range(self.CHARACTER_PAGE_RETRIES):
                try:
                    async with session.get(url) as response:
                        status = response.status

                        if status == 200:
                            html = await response.text()
                            break

                        if status == 404:
                            raise PRTSLookupError(
                                f"PRTS 未找到角色 {base_character} 的语音记录（HTTP 404）"
                            )

                        if status == 403:
                            raise PRTSLookupError(
                                "PRTS 拒绝访问（HTTP 403），请稍后重试或检查网络出口"
                            )

                        if status not in self.RETRYABLE_PAGE_STATUSES:
                            raise PRTSLookupError(f"PRTS 请求失败（HTTP {status}）")

                        if attempt + 1 >= self.CHARACTER_PAGE_RETRIES:
                            if status == 429:
                                raise PRTSLookupError(
                                    "PRTS 请求过于频繁（HTTP 429），请稍后重试"
                                )

                            raise PRTSLookupError(
                                f"PRTS 服务暂时异常（HTTP {status}），请稍后重试"
                            )

                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt + 1 >= self.CHARACTER_PAGE_RETRIES:
                        raise PRTSLookupError(f"访问 PRTS 时网络异常: {exc}") from exc

                await asyncio.sleep(0.4 * (2**attempt))

            if html is None:
                raise PRTSLookupError("PRTS 页面请求未返回内容")

            soup = BeautifulSoup(
                html,
                "html.parser",
            )
            voice_div = soup.find(
                "div",
                attrs={"data-voice-base": True},
            )

            if not voice_div:
                raise PRTSLookupError("PRTS 页面结构可能已变化：未找到语音记录节点")

            voice_data = voice_div.get("data-voice-base") or ""
            voice_data = str(voice_data).strip()

            result = {}

            for item in voice_data.split(","):
                if ":" not in item:
                    continue

                language, path = item.split(
                    ":",
                    1,
                )

                language = language.strip()
                path = path.strip()

                if language and path:
                    result[language] = path

            if not result:
                raise PRTSLookupError("PRTS 页面结构可能已变化：语音记录内容为空")

            return result

        except PRTSLookupError:
            raise
        except Exception as exc:
            logger.error(f"解析 PRTS 页面失败: {exc}")
            raise PRTSLookupError(f"解析 PRTS 页面失败: {exc}") from exc
        finally:
            if owns_session and session is not None:
                await session.close()

    async def ensure_assets(self) -> None:
        try:
            missing = set()

            for character in self.voice_index:
                parsed = self._parse_character_reference(character)

                if not parsed:
                    continue

                base_character = parsed[0]
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
    ) -> Tuple[bool, str]:
        parsed = self._parse_character_reference(base_char)

        if not parsed:
            return (
                False,
                "角色名称不合法",
            )

        base_char = parsed[0]
        encoded_character = quote(
            base_char,
            safe="",
        )

        page_url = constants.PRTS_AVATAR_PAGE_URL.format(character=encoded_character)

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

            async with session.get(page_url) as response:
                if response.status != 200:
                    return (
                        False,
                        (f"获取头像页面失败: HTTP {response.status}"),
                    )

                html = await response.text()

            soup = BeautifulSoup(
                html,
                "html.parser",
            )
            meta = soup.find(
                "meta",
                attrs={"property": "og:image"},
            )

            if not meta or not meta.get("content"):
                return (
                    False,
                    "未找到头像图片链接",
                )

            image_url = urljoin(
                constants.PRTS_BASE_URL,
                str(meta["content"]),
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

            fd, temp_name = tempfile.mkstemp(
                prefix=f".{save_path.name}.",
                suffix=".tmp",
                dir=str(self.assets_dir),
            )

            try:
                with os.fdopen(
                    fd,
                    "wb",
                ) as handle:
                    handle.write(image_data)
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(
                    temp_name,
                    save_path,
                )
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass

                Path(temp_name).unlink(missing_ok=True)
                raise

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
