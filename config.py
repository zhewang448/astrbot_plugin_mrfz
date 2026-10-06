"""直观配置、旧值迁移与 AstrBot 配置持久化。"""
import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import constants


class ConfigConflict(ValueError):
    pass


@dataclass
class PluginConfig:
    auto_download: bool = True
    allow_public_auto_download: bool = True
    auto_download_skin: bool = True
    language_priority: list[str] = field(default_factory=lambda: list(constants.DEFAULT_LANGUAGE_PRIORITY))
    download_languages: list[str] = field(default_factory=lambda: list(constants.DEFAULT_DOWNLOAD_LANGUAGES))
    page_style: str = "modern"

    @classmethod
    def from_dict(cls, config) -> "PluginConfig":
        booleans = {}
        for key in ("auto_download", "allow_public_auto_download", "auto_download_skin"):
            value = config.get(key, True)
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是开关值")
            booleans[key] = value
        style = config.get("page_style", "modern")
        if style not in ("modern", "classic"):
            raise ValueError("管理页面样式必须是新版或经典版")
        return cls(
            **booleans,
            language_priority=constants.normalize_languages(
                config.get("default_language_rank", constants.DEFAULT_LANGUAGE_PRIORITY)
            ),
            download_languages=constants.normalize_languages(
                config.get("auto_download_language", constants.DEFAULT_DOWNLOAD_LANGUAGES)
            ),
            page_style=style,
        )

    def to_dict(self) -> dict:
        # 保留原键名：AstrBot 在插件构造前会清除 schema 未声明的键。
        return {
            "auto_download": self.auto_download,
            "allow_public_auto_download": self.allow_public_auto_download,
            "auto_download_skin": self.auto_download_skin,
            "default_language_rank": [constants.LANGUAGE_MAP[code]["name"] for code in self.language_priority],
            "auto_download_language": [constants.LANGUAGE_MAP[code]["name"] for code in self.download_languages],
            "page_style": self.page_style,
        }


class ConfigStore:
    """只通过 AstrBotConfig 保存；失败回滚，旧值迁移前留备份。"""

    def __init__(self, config, backup_dir: Path):
        self.config = config
        self.backup_dir = Path(backup_dir)
        self._lock = threading.RLock()
        self.current = PluginConfig.from_dict(config)
        self.migration_error = ""
        normalized = self.current.to_dict()
        if any(config.get(key) != value for key, value in normalized.items()):
            try:
                self._persist(normalized, backup=True)
            except Exception as exc:
                self.migration_error = f"旧配置迁移尚未保存，将在下次加载重试：{exc}"

    @staticmethod
    def _revision(config) -> str:
        raw = json.dumps(dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "config": PluginConfig.from_dict(self.config).to_dict(),
                "revision": self._revision(self.config),
                "migrationWarning": self.migration_error,
                "languages": [
                    {"code": code, "name": info["name"]}
                    for code, info in sorted(
                        constants.LANGUAGE_MAP.items(), key=lambda pair: int(pair[1]["rank"])
                    )
                ],
            }

    def _persist(self, values: dict, *, backup: bool = False) -> None:
        previous = copy.deepcopy(dict(self.config))
        if backup:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            path = self.backup_dir / f"before-language-config-{uuid4().hex}.json"
            with path.open("x", encoding="utf-8") as handle:
                json.dump(previous, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            self.config.update(values)
            self.config.save_config()
        except BaseException:
            self.config.clear()
            self.config.update(previous)
            raise

    def save(self, payload: dict, revision: str) -> PluginConfig:
        with self._lock:
            if not isinstance(payload, dict) or set(payload) != set(self.current.to_dict()):
                raise ValueError("请提供完整的插件配置")
            if not isinstance(revision, str) or revision != self._revision(self.config):
                raise ConfigConflict("配置已在其他页面更改，请重新加载后再保存")
            names = {info["name"] for info in constants.LANGUAGE_MAP.values()}
            for key in ("default_language_rank", "auto_download_language"):
                if not isinstance(payload[key], list):
                    raise ValueError("请选择语言，不能使用数字串")
                if any(not isinstance(item, str) or item not in names for item in payload[key]):
                    raise ValueError("请选择支持的语言名称")
            candidate = PluginConfig.from_dict(payload)
            self._persist(candidate.to_dict(), backup=bool(self.migration_error))
            self.current = candidate
            self.migration_error = ""
            return candidate
