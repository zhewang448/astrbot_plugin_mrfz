import asyncio
import difflib
import json
import random
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, StarTools, register

# 引入拆分后的模块
from .data_source import VoiceManager
from .renderer import VoiceRenderer
from .voice_page import VoicePageManager


# 常量定义
FUZZY_MATCH_THRESHOLD = 0.6
SCAN_CACHE_DURATION = 60


@register(
    "astrbot_plugin_mrfz",
    "bushikq",
    "明日方舟角色语音插件",
    "3.7.0",
)
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 1. 初始化路径
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_mrfz")
        self.plugin_dir = Path(__file__).parent
        self.custom_cmd_file = self.data_dir / "custom_commands.json"

        # 2. 初始化核心模块
        self.voice_mgr = VoiceManager(self.data_dir, self.plugin_dir)
        self.renderer = VoiceRenderer(
            font_path=self.plugin_dir / "SourceHanSerifCN-Medium-6.otf",
            output_dir=self.data_dir / "render_cache",
        )

        # 3. 加载普通配置
        self.auto_download = self.config.get("auto_download", True)
        self.allow_public_auto_download = self.config.get(
            "allow_public_auto_download",
            True,
        )
        self.auto_download_skin = self.config.get(
            "auto_download_skin",
            True,
        )
        self.download_langs = self.config.get(
            "auto_download_language",
            "123",
        )
        self.default_lang_rank = self.config.get(
            "default_language_rank",
            "123456",
        )

        # 4. 加载自定义指令
        self.custom_mappings = self._load_custom_commands()

        # 5. 文件扫描缓存
        self._last_scan_time = 0
        self._scan_lock = asyncio.Lock()
        self._cooldowns: Dict[Tuple[str, str], float] = {}

        # 6. 启动后台迁移与资源检查
        self._startup_task = asyncio.create_task(self._initialize_resources())

        # 7. 注册 AstrBot Plugin Page 管理端
        self.voice_page = VoicePageManager(
            context=context,
            voice_mgr=self.voice_mgr,
            custom_mappings=self.custom_mappings,
            save_custom_commands=self._save_custom_commands,
            scan_callback=self._scan_if_needed,
            valid_trigger=self._valid_trigger,
            default_language_rank=self.default_lang_rank,
            default_download_langs=self.download_langs,
            default_download_skin=self.auto_download_skin,
        )

    # ================== 持久化存储逻辑 ==================

    def _load_custom_commands(self) -> Dict[str, dict]:
        """从 JSON 加载自定义指令。"""
        if not self.custom_cmd_file.exists():
            return {}

        try:
            with open(
                self.custom_cmd_file,
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            if not isinstance(data, dict):
                logger.warning("自定义指令文件格式错误，应为字典类型")
                return {}

            result = {}

            for trigger, info in data.items():
                if not self._valid_trigger(trigger) or not isinstance(info, dict):
                    logger.warning(f"忽略格式错误的自定义指令: {trigger!r}")
                    continue

                character = info.get("character")
                voice = info.get("voice")
                lang = info.get("lang")

                if (
                    not self.voice_mgr.validate_character(character)
                    or voice not in self.voice_mgr.VOICE_DESCRIPTIONS
                    or (lang is not None and lang not in self.voice_mgr.LANGUAGE_MAP)
                ):
                    logger.warning(f"忽略字段无效的自定义指令: {trigger!r}")
                    continue

                result[trigger.strip()] = {
                    "character": character.strip(),
                    "voice": voice,
                    "lang": lang,
                }

            return result

        except json.JSONDecodeError as exc:
            logger.error(f"自定义指令 JSON 解析失败: {exc}，文件可能已损坏")
            return {}
        except PermissionError:
            logger.error(f"无权限读取自定义指令文件: {self.custom_cmd_file}")
            return {}
        except Exception as exc:
            logger.error(
                f"加载自定义指令时发生未知错误: {exc}",
                exc_info=True,
            )
            return {}

    def _save_custom_commands(self) -> bool:
        """原子保存自定义指令。"""
        temp_path = None

        try:
            self.custom_cmd_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp_path = self.custom_cmd_file.with_name(
                f".{self.custom_cmd_file.name}.{uuid4().hex}.tmp"
            )

            with open(
                temp_path,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    self.custom_mappings,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()

            temp_path.replace(self.custom_cmd_file)
            return True

        except Exception as exc:
            logger.error(
                f"保存自定义指令时发生未知错误: {exc}",
                exc_info=True,
            )
            return False

        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _initialize_resources(self) -> None:
        try:
            await self.voice_mgr.migrate_legacy_skin_directories(
                self.download_langs,
            )
            await self.voice_mgr.refresh_local_skin_metadata()
            await self.voice_mgr.ensure_assets()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"启动资源迁移或检查失败，将在下次启动重试: {exc}")

    async def _scan_if_needed(
        self,
        force: bool = False,
    ) -> None:
        """缓存过期或强制刷新时扫描语音文件。"""
        startup_task = getattr(self, "_startup_task", None)

        if (
            startup_task is not None
            and startup_task is not asyncio.current_task()
            and not startup_task.done()
        ):
            await startup_task

        async with self._scan_lock:
            current_time = time.monotonic()

            if force or current_time - self._last_scan_time > SCAN_CACHE_DURATION:
                await asyncio.to_thread(self.voice_mgr.scan_voice_files)
                self._last_scan_time = current_time

                logger.debug(f"执行文件扫描，下次扫描时间: {SCAN_CACHE_DURATION}秒后")

    async def _repair_voice_mapping_if_needed(
        self,
        character: str,
        language: str,
    ) -> Tuple[bool, str]:
        if not self.voice_mgr.needs_voice_resource_remap(character, language):
            return True, ""

        language_info = self.voice_mgr.LANGUAGE_MAP.get(language)

        if not language_info:
            return False, "语言代码无效"

        logger.info(f"正在校正 {character}/{language} 的旧版语音资源编号")
        success, message = await self.voice_mgr.fetch_character_voices(
            character,
            True,
            language_info["rank"],
            require_no_failures=True,
        )

        repaired = success and not self.voice_mgr.needs_voice_resource_remap(
            character,
            language,
        )
        return repaired, message

    @staticmethod
    def _skin_choice_message(
        character: str,
        options: list[str],
    ) -> str:
        base = VoiceManager._base_character(character) or character
        option_lines = "\n".join(f"- {option}" for option in options)
        reason = (
            f"检测到 {base} 有多套皮肤语音，请指定具体皮肤"
            if len(options) > 1
            else f"未找到指定的 {base} 皮肤，当前可用项如下"
        )
        return f"{reason}：\n{option_lines}\n例如：/mrfz {options[0]} 问候 中文"

    @staticmethod
    def _valid_trigger(trigger: object) -> bool:
        """检查自定义触发词是否合法。"""
        return (
            isinstance(trigger, str)
            and bool(trigger.strip())
            and len(trigger.strip()) <= 64
            and "\x00" not in trigger
        )

    def _cooldown_remaining(
        self,
        event: AstrMessageEvent,
        action: str,
        seconds: float,
    ) -> float:
        """获取用户操作剩余冷却时间。"""
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            sender_id = "unknown"

        key = (sender_id, action)
        now = time.monotonic()
        remaining = self._cooldowns.get(key, 0.0) - now

        if remaining <= 0:
            self._cooldowns[key] = now + seconds
            return 0.0

        return remaining

    async def _get_list_render_data(self) -> dict:
        """构建列表图片所需的数据。"""
        await self._scan_if_needed()

        render_data = {
            "custom_commands": [],
            "operators": [],
            "skin_operators": [],
            "voice_types": self.voice_mgr.VOICE_DESCRIPTIONS,
        }

        # 填充自定义指令
        for trigger, info in self.custom_mappings.items():
            if not isinstance(info, dict):
                logger.warning(f"自定义指令格式错误: {trigger} -> {info}")
                continue

            if "character" not in info or "voice" not in info:
                logger.warning(f"自定义指令缺少必要字段: {trigger} -> {info}")
                continue

            resolved_character, _ = self.voice_mgr.resolve_character_reference(
                info["character"]
            )
            display_character = resolved_character or info["character"]
            base = self.voice_mgr._base_character(display_character)
            lang_code = info.get("lang")
            lang_display = "Auto"

            if lang_code:
                lang_conf = self.voice_mgr.LANGUAGE_MAP.get(lang_code)
                lang_display = lang_conf["name"] if lang_conf else lang_code
            else:
                auto_code = self.voice_mgr.choose_language(
                    display_character,
                    self.default_lang_rank,
                )

                if auto_code == "nodownload":
                    lang_display = "Auto(无)"
                else:
                    name = self.voice_mgr.LANGUAGE_MAP.get(
                        auto_code,
                        {},
                    ).get("name", auto_code)
                    lang_display = f"Auto({name})"

            render_data["custom_commands"].append(
                {
                    "trigger": trigger,
                    "target": (f"{display_character} · {info['voice']}"),
                    "lang_display": lang_display,
                    "avatar_path": str(self.voice_mgr.assets_dir / f"{base}.png"),
                }
            )

        # 填充干员及皮肤数据
        for character, languages in self.voice_mgr.voice_index.items():
            parsed = self.voice_mgr._parse_character_reference(character)

            if not parsed:
                logger.warning(f"忽略无法解析的语音索引项: {character!r}")
                continue

            base, is_skin, skin_id = parsed

            # 聚合索引只用于识别“角色皮肤”输入，列表仅展示具体皮肤。
            if is_skin and skin_id is None:
                continue

            lang_items = []

            for lang_code in languages:
                lang_conf = self.voice_mgr.LANGUAGE_MAP.get(
                    lang_code,
                    {
                        "name": lang_code,
                        "color": (100, 100, 100),
                    },
                )

                lang_items.append(
                    {
                        "code": lang_code,
                        "display": lang_conf["name"],
                        "color": lang_conf["color"],
                    }
                )

            item = {
                "name": (
                    f"{base}皮肤 · {skin_id}" if is_skin and skin_id else character
                ),
                "avatar_path": str(self.voice_mgr.assets_dir / f"{base}.png"),
                "languages": lang_items,
            }

            if is_skin:
                render_data["skin_operators"].append(item)
            else:
                render_data["operators"].append(item)

        return render_data

    # ================== 事件监听 ==================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(
        self,
        event: AstrMessageEvent,
    ) -> None:
        """监听所有消息并处理自定义触发词。"""
        msg = event.message_str.strip()

        if msg not in self.custom_mappings:
            return

        cfg = self.custom_mappings[msg]

        if not isinstance(cfg, dict):
            logger.warning(f"忽略格式错误的自定义指令: {msg!r}")
            return

        character = cfg.get("character")
        voice = cfg.get("voice")
        lang_code = cfg.get("lang")

        if (
            not self.voice_mgr.validate_character(character)
            or voice not in self.voice_mgr.VOICE_DESCRIPTIONS
            or (lang_code is not None and lang_code not in self.voice_mgr.LANGUAGE_MAP)
        ):
            logger.warning(f"忽略字段无效的自定义指令: {msg!r}")
            return

        resolved_character, options = self.voice_mgr.resolve_character_reference(
            character
        )

        if not resolved_character:
            if options:
                logger.warning(
                    f"自定义语音绑定未指定具体皮肤，已跳过: {msg!r} -> {character}"
                )
            return

        character = resolved_character

        if not lang_code:
            lang_code = self.voice_mgr.choose_language(
                character,
                self.default_lang_rank,
            )

        if lang_code == "nodownload":
            logger.warning(f"自定义语音尚未下载: {character} {voice}")
            return

        repaired, message = await self._repair_voice_mapping_if_needed(
            character,
            lang_code,
        )

        if not repaired:
            logger.warning(
                f"自定义语音旧缓存校正失败: {character}/{lang_code}: {message}"
            )
            return

        path = self.voice_mgr.get_voice_path(
            character,
            voice,
            lang_code,
        )

        if path:
            logger.info(f"触发自定义语音: {msg} -> {character} {voice}")
            await event.send(
                MessageChain(
                    [
                        Record.fromFileSystem(str(path)),
                    ]
                )
            )
        else:
            logger.warning(f"自定义语音文件缺失: {character} {voice}")

    # ================== 指令处理 ==================

    @filter.command(
        "mrfz",
        alias={
            "播放明日方舟语音",
            "播放方舟语音",
        },
    )
    async def mrfz_handler(
        self,
        event: AstrMessageEvent,
        character: Optional[str] = None,
        voice: Optional[str] = None,
        lang: Optional[str] = None,
    ):
        """播放明日方舟角色语音。"""
        await self._scan_if_needed()

        # 未指定角色时随机选择
        if not character:
            playable_characters = []

            for name in self.voice_mgr.voice_index:
                parsed = self.voice_mgr._parse_character_reference(name)

                if parsed and not (parsed[1] and parsed[2] is None):
                    playable_characters.append(name)

            if not playable_characters:
                yield event.plain_result("本地暂无语音，请先使用 /mrfz_fetch 下载")
                return

            character = random.choice(playable_characters)

        character = character.strip()

        if not self.voice_mgr.validate_character(character):
            yield event.plain_result("角色名称不合法")
            return

        resolved_character, skin_options = self.voice_mgr.resolve_character_reference(
            character
        )

        if skin_options and not resolved_character:
            yield event.plain_result(
                self._skin_choice_message(
                    character,
                    skin_options,
                )
            )
            return

        if resolved_character:
            character = resolved_character

        # 检查角色是否存在
        if character not in self.voice_mgr.voice_index:
            all_names = list(self.voice_mgr.voice_index.keys())

            matches = difflib.get_close_matches(
                character,
                all_names,
                n=1,
                cutoff=FUZZY_MATCH_THRESHOLD,
            )

            guessed_character = None

            if matches:
                guessed_character = matches[0]

                yield event.plain_result(
                    f"本地未找到「{character}」，"
                    f"猜测您是指「{guessed_character}」"
                    "...已自动切换。"
                )

                character = guessed_character

                resolved_character, skin_options = (
                    self.voice_mgr.resolve_character_reference(character)
                )

                if skin_options and not resolved_character:
                    yield event.plain_result(
                        self._skin_choice_message(
                            character,
                            skin_options,
                        )
                    )
                    return

                if resolved_character:
                    character = resolved_character

            if not guessed_character:
                if not self.auto_download:
                    yield event.plain_result(f"未找到角色 {character} (自动下载已关闭)")
                    return

                if not self.allow_public_auto_download and not event.is_admin():
                    yield event.plain_result(
                        f"未找到角色 {character} "
                        "（普通用户自动下载已关闭，请联系管理员下载）"
                    )
                    return

                yield event.plain_result(f"未找到 {character}，正在尝试从 PRTS 获取...")

                success, message = await self.voice_mgr.fetch_character_voices(
                    character,
                    self.auto_download_skin,
                    self.download_langs,
                )

                if not success:
                    yield event.plain_result(f"获取失败: {message}")
                    return

                await self._scan_if_needed(force=True)

                resolved_character, skin_options = (
                    self.voice_mgr.resolve_character_reference(character)
                )

                if skin_options and not resolved_character:
                    yield event.plain_result(
                        self._skin_choice_message(
                            character,
                            skin_options,
                        )
                    )
                    return

                if resolved_character:
                    character = resolved_character

                if character not in self.voice_mgr.voice_index:
                    yield event.plain_result("下载完成，但没有发现可播放的语音文件。")
                    return

        # 处理语言参数
        if lang:
            target_lang = self.voice_mgr.LANG_ALIAS.get(lang.strip().lower())

            if not target_lang:
                yield event.plain_result(f"不支持的语言参数: {lang}")
                return
        else:
            target_lang = self.voice_mgr.choose_language(
                character,
                self.default_lang_rank,
            )

        if target_lang == "nodownload":
            yield event.plain_result("该角色没有符合当前语言配置的语音文件。")
            return

        if self.voice_mgr.needs_voice_resource_remap(character, target_lang):
            yield event.plain_result(
                f"检测到 {character} 的旧版语音缓存，正在校正资源编号..."
            )
            repaired, message = await self._repair_voice_mapping_if_needed(
                character,
                target_lang,
            )

            if not repaired:
                yield event.plain_result(f"旧版语音缓存校正失败: {message}")
                return

        # 根据实际文件列表选择语音
        if voice:
            voice = voice.strip()

        available_voices = self.voice_mgr.get_available_voices(
            character,
            target_lang,
        )

        if not available_voices:
            yield event.plain_result("该角色在所选语言下没有可播放的语音文件。")
            return

        if not voice:
            voice = random.choice(available_voices)
        elif voice not in (self.voice_mgr.VOICE_DESCRIPTIONS):
            yield event.plain_result(f"不支持的语音名称: {voice}")
            return

        path = self.voice_mgr.get_voice_path(
            character,
            voice,
            target_lang,
        )

        if path:
            yield event.plain_result(f"播放 {character}: {voice}")
            yield event.chain_result(
                [
                    Record.fromFileSystem(str(path)),
                ]
            )
        else:
            yield event.plain_result(f"文件未找到: {voice}")

    @filter.command(
        "mrfz_list",
        alias={"明日方舟语音列表"},
    )
    async def mrfz_list_handler(
        self,
        event: AstrMessageEvent,
    ):
        """生成并发送本地语音列表图片。"""
        yield event.plain_result("正在读取 PRTS 终端数据...")

        render_data = await self._get_list_render_data()

        try:
            img_path = await asyncio.to_thread(
                self.renderer.render_image,
                render_data,
                self.voice_mgr.VOICE_DESCRIPTIONS,
            )

            yield event.image_result(str(img_path))

        except Exception as exc:
            logger.error(
                f"渲染错误: {exc}",
                exc_info=True,
            )
            yield event.plain_result(f"终端渲染模块故障: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "mrfz_bind",
        alias={
            "绑定语音",
            "语音绑定",
        },
    )
    async def mrfz_bind(
        self,
        event: AstrMessageEvent,
        trigger: str,
        character: str,
        voice: str,
        lang: Optional[str] = None,
    ):
        """将语音绑定到自定义触发词。"""
        remaining = self._cooldown_remaining(
            event,
            "bind",
            2.0,
        )

        if remaining > 0:
            yield event.plain_result(f"操作过于频繁，请 {remaining:.1f} 秒后重试")
            return

        trigger = trigger.strip()
        character = character.strip()

        if not self._valid_trigger(trigger):
            yield event.plain_result("触发词不能为空且不能超过 64 个字符")
            return

        if not self.voice_mgr.validate_character(character):
            yield event.plain_result("角色名称不合法")
            return

        await self._scan_if_needed()

        resolved_character, skin_options = self.voice_mgr.resolve_character_reference(
            character
        )

        if skin_options and not resolved_character:
            yield event.plain_result(
                self._skin_choice_message(
                    character,
                    skin_options,
                ).replace("/mrfz ", "/mrfz_bind 触发词 ", 1)
            )
            return

        if resolved_character:
            character = resolved_character

        if character not in self.voice_mgr.voice_index:
            yield event.plain_result("角色语音尚未下载，无法绑定")
            return

        lang_code = None

        if lang:
            lang_code = self.voice_mgr.LANG_ALIAS.get(lang.strip().lower())

            if not lang_code:
                yield event.plain_result("语言代码错误")
                return

        if voice not in (self.voice_mgr.VOICE_DESCRIPTIONS):
            yield event.plain_result("语音名称错误")
            return

        resolved_lang = lang_code or self.voice_mgr.choose_language(
            character,
            self.default_lang_rank,
        )

        if resolved_lang != "nodownload":
            repaired, message = await self._repair_voice_mapping_if_needed(
                character,
                resolved_lang,
            )

            if not repaired:
                yield event.plain_result(f"旧版语音缓存校正失败: {message}")
                return

        if resolved_lang == "nodownload" or not self.voice_mgr.get_voice_path(
            character,
            voice,
            resolved_lang,
        ):
            yield event.plain_result("对应的角色、语言或语音文件不存在，无法绑定")
            return

        marker = object()
        previous = self.custom_mappings.get(
            trigger,
            marker,
        )

        self.custom_mappings[trigger] = {
            "character": character,
            "voice": voice,
            "lang": lang_code,
        }

        if not self._save_custom_commands():
            if previous is marker:
                self.custom_mappings.pop(
                    trigger,
                    None,
                )
            else:
                self.custom_mappings[trigger] = previous

            yield event.plain_result("绑定失败：无法保存配置，请检查权限")
            return

        yield event.plain_result(f"绑定成功: 「{trigger}」 -> {character} {voice}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "mrfz_unbind",
        alias={
            "解绑语音",
            "语音解绑",
        },
    )
    async def mrfz_unbind(
        self,
        event: AstrMessageEvent,
        trigger: str,
    ):
        """解除自定义触发词绑定。"""
        remaining = self._cooldown_remaining(
            event,
            "unbind",
            2.0,
        )

        if remaining > 0:
            yield event.plain_result(f"操作过于频繁，请 {remaining:.1f} 秒后重试")
            return

        trigger = trigger.strip()

        if trigger not in self.custom_mappings:
            yield event.plain_result("未找到该触发词")
            return

        previous = self.custom_mappings.pop(trigger)

        if not self._save_custom_commands():
            self.custom_mappings[trigger] = previous

            yield event.plain_result("解绑失败：无法保存配置，请检查权限")
            return

        yield event.plain_result(f"已解绑: {trigger}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "mrfz_fetch",
        alias={
            "下载语音",
            "获取语音",
        },
    )
    async def mrfz_fetch(
        self,
        event: AstrMessageEvent,
        character: str,
    ):
        """从 PRTS Wiki 下载指定角色的语音。"""
        remaining = self._cooldown_remaining(
            event,
            "fetch",
            30.0,
        )

        if remaining > 0:
            yield event.plain_result(f"操作过于频繁，请 {remaining:.0f} 秒后重试")
            return

        character = character.strip()

        if not self.voice_mgr.validate_character(character):
            yield event.plain_result("角色名称不合法")
            return

        yield event.plain_result(f"开始获取 {character} 的语音文件...")

        success, message = await self.voice_mgr.fetch_character_voices(
            character,
            True,
            "123456",
        )

        if success:
            await self._scan_if_needed(force=True)

        yield event.plain_result(message)

    @filter.command(
        "mrfz_help",
        alias={"明日方舟语音帮助"},
    )
    async def mrfz_help(
        self,
        event: AstrMessageEvent,
    ):
        """生成并发送帮助图片和语音索引图片。"""
        try:
            render_data = await self._get_list_render_data()

            # render_help 是异步函数，必须直接 await。
            # render_image 是同步函数，在线程中运行。
            help_img_path, list_img_path = await asyncio.gather(
                self.renderer.render_help(),
                asyncio.to_thread(
                    self.renderer.render_image,
                    render_data,
                    self.voice_mgr.VOICE_DESCRIPTIONS,
                ),
            )

            chain = [
                Plain("已生成帮助文档与索引列表：\n"),
                Image.fromFileSystem(str(help_img_path)),
                Image.fromFileSystem(str(list_img_path)),
            ]

            yield event.chain_result(chain)

        except FileNotFoundError as exc:
            logger.error(f"帮助图片文件未找到: {exc}")
            yield event.plain_result("帮助生成失败: 图片文件未找到")

        except PermissionError as exc:
            logger.error(f"无权限访问图片文件: {exc}")
            yield event.plain_result("帮助生成失败: 权限不足")

        except Exception as exc:
            logger.error(
                f"帮助图片生成失败: {exc}",
                exc_info=True,
            )
            yield event.plain_result(f"帮助生成失败: {exc}")

    async def terminate(self):
        """插件停用或重载时停止后台迁移与资源检查任务。"""
        voice_page = getattr(self, "voice_page", None)

        if voice_page is not None:
            await voice_page.terminate()

        task = getattr(self, "_startup_task", None)

        if task is None or task.done():
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
