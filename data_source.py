import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from astrbot.api import logger
from bs4 import BeautifulSoup


class VoiceManager:
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Referer": "https://prts.wiki/",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    VOICE_DESCRIPTIONS = [
        "任命助理",
        "交谈1",
        "交谈2",
        "交谈3",
        "晋升后交谈1",
        "晋升后交谈2",
        "信赖提升后交谈1",
        "信赖提升后交谈2",
        "信赖提升后交谈3",
        "闲置",
        "干员报到",
        "观看作战记录",
        "精英化晋升1",
        "精英化晋升2",
        "编入队伍",
        "任命队长",
        "行动出发",
        "行动开始",
        "选中干员1",
        "选中干员2",
        "部署1",
        "部署2",
        "作战中1",
        "作战中2",
        "作战中3",
        "作战中4",
        "完成高难行动",
        "3星结束行动",
        "非3星结束行动",
        "行动失败",
        "进驻设施",
        "戳一下",
        "信赖触摸",
        "标题",
        "新年祝福",
        "问候",
        "生日",
        "周年庆典",
    ]

    LANGUAGE_MAP = {
        "cn": {
            "name": "中文",
            "rank": "2",
            "color": (46, 125, 50),
        },
        "jp": {
            "name": "日语",
            "rank": "3",
            "color": (21, 101, 192),
        },
        "us": {
            "name": "英语",
            "rank": "4",
            "color": (198, 40, 40),
        },
        "kr": {
            "name": "韩语",
            "rank": "5",
            "color": (97, 97, 97),
        },
        "fy": {
            "name": "方言",
            "rank": "1",
            "color": (230, 81, 0),
        },
        "it": {
            "name": "意语",
            "rank": "6",
            "color": (0, 131, 143),
        },
    }

    LANG_ALIAS = {
        "中文": "cn",
        "普通话": "cn",
        "中": "cn",
        "汉语": "cn",
        "cn": "cn",
        "2": "cn",
        "日文": "jp",
        "日语": "jp",
        "日": "jp",
        "jp": "jp",
        "3": "jp",
        "英文": "us",
        "英语": "us",
        "英": "us",
        "us": "us",
        "4": "us",
        "韩文": "kr",
        "韩语": "kr",
        "韩": "kr",
        "kr": "kr",
        "5": "kr",
        "方言": "fy",
        "方": "fy",
        "fy": "fy",
        "1": "fy",
        "意文": "it",
        "意语": "it",
        "意大利语": "it",
        "意": "it",
        "it": "it",
        "6": "it",
    }

    MAX_CHARACTER_LENGTH = 80
    MAX_SKIN_ID_LENGTH = 80
    MAX_VOICE_BYTES = 20 * 1024 * 1024
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    DOWNLOAD_RETRIES = 3

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

        self._download_locks: Dict[
            str,
            asyncio.Lock,
        ] = {}

        for directory in (
            self.data_dir,
            self.voices_dir,
            self.assets_dir,
        ):
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

        self.scan_voice_files()

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
                        and path.stat().st_size > 0
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

            # 兼容旧目录：
            # 角色/skin/语言/*.wav
            legacy_languages = {}

            for language in self.LANGUAGE_MAP:
                voices = self._scan_language_dir(skin_root / language)
                if voices:
                    legacy_languages[language] = voices

            if legacy_languages:
                packages["legacy"] = legacy_languages

            # 新目录：
            # 角色/skin/皮肤ID/语言/*.wav
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
                    packages[skin_dir.name] = languages

            if not packages:
                continue

            self.skin_voice_index[character] = packages

            aggregate: Dict[
                str,
                List[str],
            ] = {}

            for skin_id, languages in packages.items():
                for language, voices in languages.items():
                    aggregate.setdefault(
                        language,
                        [],
                    ).extend(voices)

                if skin_id != "legacy":
                    self._record_flat_character(
                        (f"{character}皮肤[{skin_id}]"),
                        languages,
                    )

            for language, voices in aggregate.items():
                aggregate[language] = self._sort_voice_names(voices)

            self._record_flat_character(
                f"{character}皮肤",
                aggregate,
            )

        payload = {
            "version": 2,
            "voice_index": self.voice_index,
            "voice_files": self.voice_files,
            "skins": self.skin_voice_index,
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

        candidates = []

        if not is_skin:
            candidate = self._safe_path(
                character_root,
                language,
                f"{voice_name}.wav",
            )

            if candidate:
                candidates.append(candidate)

        elif skin_id:
            candidate = self._safe_path(
                character_root,
                "skin",
                skin_id,
                language,
                f"{voice_name}.wav",
            )

            if candidate:
                candidates.append(candidate)

            if skin_id == "legacy":
                legacy = self._safe_path(
                    character_root,
                    "skin",
                    language,
                    f"{voice_name}.wav",
                )

                if legacy:
                    candidates.append(legacy)

        else:
            legacy = self._safe_path(
                character_root,
                "skin",
                language,
                f"{voice_name}.wav",
            )

            if legacy:
                candidates.append(legacy)

            package_ids = sorted(
                self.skin_voice_index.get(
                    base_character,
                    {},
                ).keys()
            )

            for package_id in package_ids:
                if package_id == "legacy":
                    continue

                candidate = self._safe_path(
                    character_root,
                    "skin",
                    package_id,
                    language,
                    f"{voice_name}.wav",
                )

                if candidate:
                    candidates.append(candidate)

        for candidate in candidates:
            try:
                if (
                    candidate.is_file()
                    and candidate.stat().st_size > 0
                    and self._path_is_within(
                        candidate,
                        character_root,
                    )
                ):
                    return candidate
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
    def _skin_id_from_label(
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

    def _lock_for(
        self,
        character: str,
    ) -> asyncio.Lock:
        lock = self._download_locks.get(character)

        if lock is None:
            lock = asyncio.Lock()
            self._download_locks[character] = lock

        return lock

    async def fetch_character_voices(
        self,
        character: str,
        auto_download_skin: bool,
        download_langs: str,
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
                            (f"未在PRTS Wiki找到角色 {base_character} 的语音记录"),
                        )

                    base_url = "https://torappu.prts.wiki/assets/audio"

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
                            continue

                        skin_id = (
                            self._skin_id_from_label(
                                language_label,
                                str(voice_key),
                            )
                            if is_skin
                            else None
                        )

                        encoded_key = quote(
                            str(voice_key).strip(),
                            safe="",
                        )

                        if not encoded_key:
                            counts["failed"] += len(self.VOICE_DESCRIPTIONS)
                            continue

                        display_name = (
                            (f"{base_character}皮肤[{skin_id}]")
                            if skin_id
                            else base_character
                        )

                        logger.info(f"正在下载 {display_name} 的 {language} 语音...")

                        for (
                            file_number,
                            description,
                        ) in enumerate(
                            self.VOICE_DESCRIPTIONS,
                            start=1,
                        ):
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
                                    skin_id=skin_id,
                                )

                                if status != "failed":
                                    break

                                if attempt + 1 < self.DOWNLOAD_RETRIES:
                                    await asyncio.sleep(0.4 * (2**attempt))

                            counts[status] += 1

                            if status == "failed":
                                logger.warning(
                                    "下载失败 "
                                    f"{display_name}/"
                                    f"{language}/"
                                    f"{description}: "
                                    f"{message}"
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

            success = counts["downloaded"] > 0 or counts["existed"] > 0

            summary = (
                "下载完成："
                f"新增 {counts['downloaded']}，"
                f"已存在 {counts['existed']}，"
                f"不存在 {counts['not_found']}，"
                f"失败 {counts['failed']}"
            )

            return success, summary

    async def _download_single_voice(
        self,
        session: aiohttp.ClientSession,
        character: str,
        url: str,
        lang: str,
        filename: str,
        *,
        skin_id: Optional[str] = None,
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

        if skin_id and not self._is_safe_component(
            skin_id,
            self.MAX_SKIN_ID_LENGTH,
        ):
            return (
                "failed",
                "皮肤 ID 不合法",
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

        if skin_id:
            save_dir = self._safe_path(
                character_root,
                "skin",
                skin_id,
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

            if path.is_file() and path.stat().st_size > 0:
                return (
                    "existed",
                    "文件已存在",
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

        url = f"https://prts.wiki/w/{encoded_character}/语音记录"

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

            async with session.get(url) as response:
                if response.status != 200:
                    return None

                html = await response.text()

            soup = BeautifulSoup(
                html,
                "html.parser",
            )
            voice_div = soup.find(
                "div",
                attrs={"data-voice-base": True},
            )

            if not voice_div:
                return None

            voice_data = str(
                voice_div.get(
                    "data-voice-base",
                    "",
                )
            )

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

            return result or None

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            logger.error(f"获取角色ID映射失败 {base_character}: {exc}")
            return None
        except Exception as exc:
            logger.error(f"解析Wiki失败: {exc}")
            return None
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

                if not avatar_path.is_file():
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

        page_url = f"https://prts.wiki/w/文件:头像_{encoded_character}.png"

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
                "https://prts.wiki/",
                str(meta["content"]),
            )
            parsed_url = urlparse(image_url)

            if (
                parsed_url.scheme != "https"
                or not parsed_url.hostname
                or not (
                    parsed_url.hostname == "prts.wiki"
                    or parsed_url.hostname.endswith(".prts.wiki")
                )
            ):
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

            png_header = b"\x89PNG\r\n\x1a\n"

            if not bytes(image_data).startswith(png_header):
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
