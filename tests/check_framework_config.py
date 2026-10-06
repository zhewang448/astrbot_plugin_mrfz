"""Exercise the installed AstrBotConfig class with temporary plugin config files.

Run: python -X utf8 tests/check_framework_config.py ASTRBOT_ROOT
Only the configuration class is loaded; no bot, database, or production file is opened.
"""
import ast
import asyncio
import copy
import enum
import importlib
import json
import logging
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_config_class(root):
    config_dir = root / "astrbot" / "core" / "config"
    source = ast.parse((config_dir / "astrbot_config.py").read_text(encoding="utf-8"))
    class_node = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "AstrBotConfig")
    defaults_tree = ast.parse((config_dir / "default.py").read_text(encoding="utf-8"))
    defaults_node = next(node for node in defaults_tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "DEFAULT_VALUE_MAP" for target in node.targets))
    namespace = {"asyncio": asyncio, "copy": copy, "enum": enum, "json": json,
                 "logging": logging, "os": os, "tempfile": tempfile, "threading": threading,
                 "Path": Path, "logger": logging.getLogger("framework-config-check"),
                 "DEFAULT_CONFIG": {}, "DEFAULT_VALUE_MAP": ast.literal_eval(defaults_node.value),
                 "ASTRBOT_CONFIG_PATH": "unused-global-config.json",
                 "DASHBOARD_RESET_PASSWORD_ENV": "ASTRBOT_RESET_DASHBOARD_PASSWORD",
                 "DASHBOARD_INITIAL_PASSWORD_ENV": "ASTRBOT_DASHBOARD_INITIAL_PASSWORD"}
    exec(compile(ast.Module(body=[class_node], type_ignores=[]), "astrbot_config.py", "exec"), namespace)
    return namespace["AstrBotConfig"]


def main(root):
    AstrBotConfig = load_config_class(root)
    package = types.ModuleType("config_check_plugin")
    package.__path__ = [str(REPO)]
    sys.modules[package.__name__] = package
    config_module = importlib.import_module("config_check_plugin.config")
    schema = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="mrfz-framework-") as directory:
        config_path = Path(directory) / "plugin_config.json"
        old = {"auto_download": False, "allow_public_auto_download": True,
               "auto_download_skin": False, "default_language_rank": "10,2,7",
               "auto_download_language": "78", "page_style": "classic"}
        config_path.write_text(json.dumps(old), encoding="utf-8")
        raw = AstrBotConfig(config_path=str(config_path), schema=schema)
        assert raw["default_language_rank"] == "10,2,7", "Framework discarded legacy preference"
        store = config_module.ConfigStore(raw, Path(directory) / "backups")
        assert not store.migration_error, store.migration_error
        assert store.current.language_priority == ["fr", "cn", "ru"]
        assert store.current.download_languages == ["ru", "de"]
        assert len(list((Path(directory) / "backups").glob("*.json"))) == 1
        disk = json.loads(config_path.read_text(encoding="utf-8-sig"))
        assert isinstance(disk["default_language_rank"], list)
        before = store.snapshot()
        store.save({**before["config"], "auto_download": True}, before["revision"])
        reloaded = AstrBotConfig(config_path=str(config_path), schema=schema)
        current = config_module.PluginConfig.from_dict(reloaded)
        assert current.auto_download and current.language_priority == ["fr", "cn", "ru"]
        assert current.page_style == "classic" and not current.auto_download_skin
        fresh = AstrBotConfig(config_path=str(Path(directory) / "fresh.json"), schema=schema)
        assert config_module.PluginConfig.from_dict(fresh).to_dict() == config_module.PluginConfig().to_dict()
        print("Installed AstrBotConfig: legacy schema load, migration, backup, save, reload and fresh defaults passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
