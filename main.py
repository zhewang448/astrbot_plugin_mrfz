import json
import random
import asyncio
import difflib
import time
from pathlib import Path
from typing import Optional, Dict, Tuple
from uuid import uuid4
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.star import StarTools
from astrbot.api import logger

# 引入拆分后的模块
from .data_source import VoiceManager
from .renderer import VoiceRenderer

# 常量定义
FUZZY_MATCH_THRESHOLD = 0.6  # 模糊匹配相似度阈值
SCAN_CACHE_DURATION = 60  # 文件扫描缓存时间(秒)


@register("astrbot_plugin_mrfz", "bushikq", "明日方舟角色语音插件", "3.5.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 1. 初始化路径
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_mrfz")
        self.plugin_dir = Path(__file__).parent
        self.custom_cmd_file = (
            self.data_dir / "custom_commands.json"
        )  # 自定义指令存储路径

        # 2. 初始化核心模块
        self.voice_mgr = VoiceManager(self.data_dir, self.plugin_dir)
        self.renderer = VoiceRenderer(
            font_path=self.plugin_dir / "SourceHanSerifCN-Medium-6.otf",
            output_dir=self.data_dir / "render_cache",
        )

        # 3. 加载普通配置
        self.auto_download = self.config.get("auto_download", True)
        self.auto_download_skin = self.config.get("auto_download_skin", True)
        self.download_langs = self.config.get("auto_download_language", "123")
        self.default_lang_rank = self.config.get("default_language_rank", "123456")

        # 4. 加载自定义指令 (从 JSON 文件)
        self.custom_mappings = self._load_custom_commands()

        # 5. 文件扫描缓存
        self._last_scan_time = 0  # 上次扫描时间戳
        self._scan_lock = asyncio.Lock()
        self._cooldowns: Dict[Tuple[str, str], float] = {}

        # 6. 启动后台资源检查
        asyncio.create_task(self.voice_mgr.ensure_assets())

    # ================== 持久化存储逻辑 ==================

    def _load_custom_commands(self) -> Dict[str, dict]:
        """从 JSON 加载自定义指令

        Returns:
            Dict[str, dict]: 自定义指令映射字典，格式为 {触发词: {character, voice, lang}}
        """
        if not self.custom_cmd_file.exists():
            return {}

        try:
            with open(self.custom_cmd_file, "r", encoding="utf-8") as f:
                data = json.load(f)
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
        except json.JSONDecodeError as e:
            logger.error(f"自定义指令 JSON 解析失败: {e}，文件可能已损坏")
            return {}
        except PermissionError:
            logger.error(f"无权限读取自定义指令文件: {self.custom_cmd_file}")
            return {}
        except Exception as e:
            logger.error(f"加载自定义指令时发生未知错误: {e}", exc_info=True)
            return {}

    def _save_custom_commands(self) -> bool:
        """保存自定义指令到 JSON

        Returns:
            bool: 保存是否成功
        """
        try:
            self.custom_cmd_file.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.custom_cmd_file.with_name(
                f".{self.custom_cmd_file.name}.{uuid4().hex}.tmp"
            )
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.custom_mappings, f, ensure_ascii=False, indent=2)
                f.flush()
            temp_path.replace(self.custom_cmd_file)
            return True
        except Exception as e:
            logger.error(f"保存自定义指令时发生未知错误: {e}", exc_info=True)
            return False
        finally:
            try:
                if "temp_path" in locals():
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _scan_if_needed(self, force: bool = False) -> None:
        """智能扫描：仅在缓存过期或强制时才扫描

        Args:
            force: 是否强制扫描，忽略缓存
        """
        async with self._scan_lock:
            current_time = time.monotonic()
            if force or (current_time - self._last_scan_time) > SCAN_CACHE_DURATION:
                await asyncio.to_thread(self.voice_mgr.scan_voice_files)
                self._last_scan_time = current_time
                logger.debug(f"执行文件扫描，下次扫描时间: {SCAN_CACHE_DURATION}秒后")

    @staticmethod
    def _valid_trigger(trigger: object) -> bool:
        return (
            isinstance(trigger, str)
            and bool(trigger.strip())
            and len(trigger.strip()) <= 64
            and "\x00" not in trigger
        )

    def _cooldown_remaining(
        self, event: AstrMessageEvent, action: str, seconds: float
    ) -> float:
        """Return remaining per-user cooldown and record a permitted action."""
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
        """构建列表渲染所需的数据字典

        Returns:
            dict: 包含 custom_commands, operators, skin_operators, voice_types 的字典
        """
        # 使用智能扫描
        await self._scan_if_needed()

        # 组装数据供 Renderer 使用
        render_data = {
            "custom_commands": [],
            "operators": [],
            "skin_operators": [],
            "voice_types": self.voice_mgr.VOICE_DESCRIPTIONS,
        }

        # 填充自定义指令
        for trigger, info in self.custom_mappings.items():
            # 安全检查：确保 info 是字典且包含必要字段
            if not isinstance(info, dict):
                logger.warning(f"自定义指令格式错误: {trigger} -> {info}")
                continue
            if "character" not in info or "voice" not in info:
                logger.warning(f"自定义指令缺少必要字段: {trigger} -> {info}")
                continue

            base = self.voice_mgr._base_character(info["character"])
            lang_code = info.get("lang")
            lang_display = "Auto"
            if lang_code:
                # 指定了语言
                lang_conf = self.voice_mgr.LANGUAGE_MAP.get(lang_code)
                lang_display = lang_conf["name"] if lang_conf else lang_code
            else:
                auto_code = self.voice_mgr.choose_language(
                    info["character"], self.default_lang_rank
                )
                if auto_code == "nodownload":
                    lang_display = "Auto(无)"
                else:
                    name = self.voice_mgr.LANGUAGE_MAP.get(auto_code, {}).get(
                        "name", auto_code
                    )
                    lang_display = f"Auto({name})"
            render_data["custom_commands"].append(
                {
                    "trigger": trigger,
                    "target": f"{info['character']} · {info['voice']}",
                    "lang_display": lang_display,
                    "avatar_path": str(self.voice_mgr.assets_dir / f"{base}.png"),
                }
            )

        # 填充干员
        for char, langs in self.voice_mgr.voice_index.items():
            is_skin = char.endswith("皮肤")
            base = self.voice_mgr._base_character(char)

            # 构建语言标签数据
            lang_items = []
            for l in langs:
                l_conf = self.voice_mgr.LANGUAGE_MAP.get(
                    l, {"name": l, "color": (100, 100, 100)}
                )
                lang_items.append(
                    {"code": l, "display": l_conf["name"], "color": l_conf["color"]}
                )

            item = {
                "name": char,
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
    async def on_message(self, event: AstrMessageEvent) -> None:
        """监听所有消息，检测并响应自定义触发词

        Args:
            event: 消息事件对象
        """
        msg = event.message_str.strip()
        if msg in self.custom_mappings:
            cfg = self.custom_mappings[msg]
            if not isinstance(cfg, dict):
                logger.warning(f"忽略格式错误的自定义指令: {msg!r}")
                return
            char = cfg.get("character")
            voice = cfg.get("voice")
            lang_code = cfg.get("lang")

            if (
                not self.voice_mgr.validate_character(char)
                or voice not in self.voice_mgr.VOICE_DESCRIPTIONS
                or (lang_code is not None and lang_code not in self.voice_mgr.LANGUAGE_MAP)
            ):
                logger.warning(f"忽略字段无效的自定义指令: {msg!r}")
                return

            if not lang_code:
                lang_code = self.voice_mgr.choose_language(char, self.default_lang_rank)

            if lang_code == "nodownload":
                logger.warning(f"自定义语音尚未下载: {char} {voice}")
                return

            path = self.voice_mgr.get_voice_path(char, voice, lang_code)
            if path:
                logger.info(f"触发自定义语音: {msg} -> {char} {voice}")
                await event.send(MessageChain([Record.fromFileSystem(str(path))]))
            else:
                logger.warning(f"自定义语音文件缺失: {char} {voice}")

    # ================== 指令处理 ==================

    @filter.command("mrfz", alias={"播放明日方舟语音", "播放方舟语音"})
    async def mrfz_handler(
        self,
        event: AstrMessageEvent,
        character: Optional[str] = None,
        voice: Optional[str] = None,
        lang: Optional[str] = None,
    ):
        """播放明日方舟角色语音

        Args:
            event: 消息事件对象
            character: 角色名称，支持模糊匹配，不填则随机
            voice: 语音类型(如"问候"、"交谈1")，不填则随机
            lang: 语言代码(中文/日语/英语等)，不填则按优先级自动选择

        Examples:
            /mrfz 凯尔希 问候 中文
            /mrfz 阿米娅
            /mrfz
        """
        # 使用智能扫描
        await self._scan_if_needed()

        # 1. 角色处理：如果没输入角色，随机选一个
        if not character:
            if not self.voice_mgr.voice_index:
                yield event.plain_result("本地暂无语音，请先使用 /mrfz_fetch 下载")
                return
            character = random.choice(list(self.voice_mgr.voice_index.keys()))

        # ==================== 模糊匹配逻辑 ====================
        # 2. 检查角色是否存在
        character = character.strip()
        if not self.voice_mgr.validate_character(character):
            yield event.plain_result("角色名称不合法")
            return

        if character not in self.voice_mgr.voice_index:
            # 先尝试从本地已有的角色模糊匹配
            all_names = list(self.voice_mgr.voice_index.keys())
            # n=1 表示只找最像的一个
            matches = difflib.get_close_matches(
                character, all_names, n=1, cutoff=FUZZY_MATCH_THRESHOLD
            )

            guessed_char = None
            if matches:
                guessed_char = matches[0]
                yield event.plain_result(
                    f"本地未找到「{character}」，猜测您是指「{guessed_char}」...已自动切换。"
                )
                # 修正角色名为匹配到的名字
                character = guessed_char

            # 2.2 如果模糊匹配也没找到，再尝试自动下载
            if not guessed_char:
                if self.auto_download:
                    yield event.plain_result(
                        f"未找到 {character}，正在尝试从 PRTS 获取..."
                    )
                    success, msg = await self.voice_mgr.fetch_character_voices(
                        character, self.auto_download_skin, self.download_langs
                    )
                    if not success:
                        yield event.plain_result(f"获取失败: {msg}")
                        return
                    await self._scan_if_needed(force=True)
                    if character not in self.voice_mgr.voice_index:
                        yield event.plain_result("下载完成，但没有发现可播放的语音文件。")
                        return
                else:
                    yield event.plain_result(f"未找到角色 {character} (自动下载已关闭)")
                    return
        # ==================== 逻辑结束 ====================

        # 3. 语言处理
        target_lang = None
        if lang:
            target_lang = self.voice_mgr.LANG_ALIAS.get(lang.strip().lower())
            if not target_lang:
                yield event.plain_result(f"不支持的语言参数: {lang}")
                return
        else:
            target_lang = self.voice_mgr.choose_language(character, self.default_lang_rank)

        if target_lang == "nodownload":
            yield event.plain_result("该角色没有符合当前语言配置的语音文件。")
            return

        # 4. 语音名处理
        if voice:
            voice = voice.strip()
        available_voices = self.voice_mgr.get_available_voices(character, target_lang)
        if not available_voices:
            yield event.plain_result("该角色在所选语言下没有可播放的语音文件。")
            return
        if not voice:
            voice = random.choice(available_voices)
        elif voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
            yield event.plain_result(f"不支持的语音名称: {voice}")
            return

        # 5. 播放
        path = self.voice_mgr.get_voice_path(character, voice, target_lang)
        if path:
            yield event.plain_result(f"播放 {character}: {voice}")
            yield event.chain_result([Record.fromFileSystem(str(path))])
        else:
            yield event.plain_result(f"文件未找到: {voice}")

    @filter.command("mrfz_list", alias={"明日方舟语音列表"})
    async def mrfz_list_handler(self, event: AstrMessageEvent):
        """生成并发送语音列表图片

        显示所有已下载的干员、皮肤、自定义指令等信息

        Args:
            event: 消息事件对象
        """
        yield event.plain_result("正在读取 PRTS 终端数据...")

        # 使用辅助方法获取数据
        render_data = await self._get_list_render_data()

        # 渲染
        try:
            img_path = await asyncio.to_thread(
                self.renderer.render_image,
                render_data,
                self.voice_mgr.VOICE_DESCRIPTIONS,
            )
            yield event.image_result(str(img_path))
        except Exception as e:
            logger.error(f"渲染错误: {e}", exc_info=True)
            yield event.plain_result(f"终端渲染模块故障: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mrfz_bind", alias={"绑定语音", "语音绑定"})
    async def mrfz_bind(
        self,
        event: AstrMessageEvent,
        trigger: str,
        character: str,
        voice: str,
        lang: Optional[str] = None,
    ):
        """将语音绑定到自定义触发词

        Args:
            event: 消息事件对象
            trigger: 触发词(如"早安")
            character: 角色名称
            voice: 语音类型
            lang: 语言代码(可选)

        Examples:
            /mrfz_bind 早安 阿米娅 问候 中文
            /mrfz_bind 晚安 凯尔希 交谈1
        """
        remaining = self._cooldown_remaining(event, "bind", 2.0)
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
        if character not in self.voice_mgr.voice_index:
            yield event.plain_result("角色语音尚未下载，无法绑定")
            return

        lang_code = None
        if lang:
            lang_code = self.voice_mgr.LANG_ALIAS.get(lang.strip().lower())
            if not lang_code:
                yield event.plain_result("语言代码错误")
                return
        if voice not in self.voice_mgr.VOICE_DESCRIPTIONS:
            yield event.plain_result("语音名称错误")
            return

        resolved_lang = lang_code or self.voice_mgr.choose_language(
            character, self.default_lang_rank
        )
        if resolved_lang == "nodownload" or not self.voice_mgr.get_voice_path(
            character, voice, resolved_lang
        ):
            yield event.plain_result("对应的角色、语言或语音文件不存在，无法绑定")
            return

        marker = object()
        previous = self.custom_mappings.get(trigger, marker)
        self.custom_mappings[trigger] = {
            "character": character,
            "voice": voice,
            "lang": lang_code,
        }

        if not self._save_custom_commands():
            if previous is marker:
                self.custom_mappings.pop(trigger, None)
            else:
                self.custom_mappings[trigger] = previous
            yield event.plain_result("绑定失败：无法保存配置，请检查权限")
            return

        yield event.plain_result(f"绑定成功: 「{trigger}」 -> {character} {voice} ")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mrfz_unbind", alias={"解绑语音", "语音解绑"})
    async def mrfz_unbind(self, event: AstrMessageEvent, trigger: str):
        """解除自定义触发词绑定

        Args:
            event: 消息事件对象
            trigger: 要解绑的触发词

        Examples:
            /mrfz_unbind 早安
        """
        remaining = self._cooldown_remaining(event, "unbind", 2.0)
        if remaining > 0:
            yield event.plain_result(f"操作过于频繁，请 {remaining:.1f} 秒后重试")
            return
        trigger = trigger.strip()
        if trigger in self.custom_mappings:
            previous = self.custom_mappings.pop(trigger)

            if not self._save_custom_commands():
                self.custom_mappings[trigger] = previous
                yield event.plain_result("解绑失败：无法保存配置，请检查权限")
                return

            yield event.plain_result(f"已解绑: {trigger}")
        else:
            yield event.plain_result("未找到该触发词")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mrfz_fetch", alias={"下载语音", "获取语音"})
    async def mrfz_fetch(self, event: AstrMessageEvent, character: str):
        """从 PRTS Wiki 下载指定角色的语音文件

        Args:
            event: 消息事件对象
            character: 角色名称

        Examples:
            /mrfz_fetch 凯尔希
            /mrfz_fetch 陈
        """
        remaining = self._cooldown_remaining(event, "fetch", 30.0)
        if remaining > 0:
            yield event.plain_result(f"操作过于频繁，请 {remaining:.0f} 秒后重试")
            return
        character = character.strip()
        if not self.voice_mgr.validate_character(character):
            yield event.plain_result("角色名称不合法")
            return

        yield event.plain_result(f"开始获取 {character} 的语音文件...")
        success, msg = await self.voice_mgr.fetch_character_voices(
            character, True, "123456"
        )
        # 下载完成后强制刷新缓存
        if success:
            await self._scan_if_needed(force=True)
        yield event.plain_result(msg)

    @filter.command("mrfz_help", alias={"明日方舟语音帮助"})
    async def mrfz_help(self, event: AstrMessageEvent):
        """显示插件帮助信息和语音列表

        生成帮助图片和干员列表图片并发送

        Args:
            event: 消息事件对象
        """
        try:
            # 获取数据
            render_data = await self._get_list_render_data()
            # 两张图均在线程中渲染，且输出唯一文件名，避免并发覆盖。
            help_img_path, list_img_path = await asyncio.gather(
                asyncio.to_thread(self.renderer.render_help),
                asyncio.to_thread(
                    self.renderer.render_image,
                    render_data,
                    self.voice_mgr.VOICE_DESCRIPTIONS,
                ),
            )

            # 构造消息链发送两张图片
            chain = [
                Plain("已生成帮助文档与索引列表：\n"),
                Image.fromFileSystem(help_img_path),
                Image.fromFileSystem(list_img_path),
            ]
            yield event.chain_result(chain)
        except FileNotFoundError as e:
            logger.error(f"帮助图片文件未找到: {e}")
            yield event.plain_result(f"帮助生成失败: 图片文件未找到")
        except PermissionError as e:
            logger.error(f"无权限访问图片文件: {e}")
            yield event.plain_result(f"帮助生成失败: 权限不足")
        except Exception as e:
            logger.error(f"帮助图片生成失败: {e}", exc_info=True)
            yield event.plain_result(f"帮助生成失败: {e}")
