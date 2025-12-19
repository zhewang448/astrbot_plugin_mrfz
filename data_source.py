import os
import json
import asyncio
import re
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
import aiohttp
from bs4 import BeautifulSoup
from astrbot.api import logger


class VoiceManager:
    # 静态常量
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://prts.wiki/",
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

    # 语言映射配置 - 颜色已调深，以便白色文字显示清晰
    LANGUAGE_MAP = {
        "cn": {"name": "中文", "rank": "2", "color": (46, 125, 50)},
        "jp": {"name": "日语", "rank": "3", "color": (21, 101, 192)},
        "us": {"name": "英语", "rank": "4", "color": (198, 40, 40)},
        "kr": {"name": "韩语", "rank": "5", "color": (97, 97, 97)},
        "fy": {"name": "方言", "rank": "1", "color": (230, 81, 0)},
        "it": {"name": "意语", "rank": "6", "color": (0, 131, 143)},
    }

    # 别名映射
    LANG_ALIAS = {
        "中文": "cn",
        "普通话": "cn",
        "cn": "cn",
        "2": "cn",
        "日语": "jp",
        "jp": "jp",
        "3": "jp",
        "英语": "us",
        "us": "us",
        "4": "us",
        "韩语": "kr",
        "kr": "kr",
        "5": "kr",
        "方言": "fy",
        "fy": "fy",
        "1": "fy",
        "意语": "it",
        "意大利语": "it",
        "it": "it",
        "6": "it",
    }

    def __init__(self, data_dir: Path, plugin_dir: Union[str, Path]):
        self.data_dir = data_dir
        # 强制转换为 Path 对象，防止路径拼接报错
        self.plugin_dir = Path(plugin_dir)
        self.voices_dir = data_dir / "voices"
        self.assets_dir = data_dir / "assets"
        self.voice_index: Dict[str, List[str]] = {}

        # 确保目录存在
        for d in [self.data_dir, self.voices_dir, self.assets_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self.scan_voice_files()

    def scan_voice_files(self) -> None:
        """扫描本地语音文件并建立索引"""
        self.voice_index.clear()
        if not self.voices_dir.exists():
            return

        for char_dir in self.voices_dir.iterdir():
            if not char_dir.is_dir():
                continue
            character = char_dir.name

            # 1. 扫描普通语音
            langs = [
                d.name
                for d in char_dir.iterdir()
                if d.is_dir() and d.name != "skin" and any(d.iterdir())
            ]
            if langs:
                self.voice_index[character] = langs

            # 2. 扫描皮肤语音
            skin_dir = char_dir / "skin"
            if skin_dir.exists() and skin_dir.is_dir():
                skin_char = f"{character}皮肤"
                skin_langs = [
                    d.name
                    for d in skin_dir.iterdir()
                    if d.is_dir() and any(d.iterdir())
                ]
                if skin_langs:
                    if skin_char not in self.voice_index:
                        self.voice_index[skin_char] = []
                    self.voice_index[skin_char].extend(skin_langs)

        # 持久化索引
        try:
            with open(self.data_dir / "voice_index.json", "w", encoding="utf-8") as f:
                json.dump(self.voice_index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存语音索引失败: {e}")

    def get_voice_path(
        self, character: str, voice_name: str, language: str
    ) -> Optional[Path]:
        """获取具体的语音文件路径"""
        real_char = character.replace("皮肤", "")
        base_path = self.voices_dir / real_char

        if character.endswith("皮肤"):
            target = base_path / "skin" / language / f"{voice_name}.wav"
        else:
            target = base_path / language / f"{voice_name}.wav"

        return target if target.exists() else None

    async def choose_language(self, character: str, rank_config: str) -> str:
        """根据配置优先级自动选择语言"""
        available = self.voice_index.get(character, [])
        if not available:
            return "nodownload"

        # 解析配置的优先级顺序
        rank_codes = []
        rank_map_rev = {v["rank"]: k for k, v in self.LANGUAGE_MAP.items()}
        for r in rank_config:
            if r in rank_map_rev:
                rank_codes.append(rank_map_rev[r])

        # 按优先级查找
        for lang in rank_codes:
            if lang in available:
                return lang

        # 兜底：如果配置的都没找到，返回第一个存在的
        return available[0] if available else "nodownload"

    async def fetch_character_voices(
        self, character: str, auto_download_skin: bool, download_langs: str
    ) -> Tuple[bool, str]:
        """爬取并下载语音"""
        try:
            base_char = character.replace("皮肤", "")

            # 1. 获取语音元数据
            char_id_map = await self._get_character_id_map(character)
            if not char_id_map:
                return False, f"未在PRTS Wiki找到角色 {character} 的语音记录"

            base_url = "https://torappu.prts.wiki/assets/audio"
            total_success = 0

            # 2. 遍历语言进行下载
            for lang_key, key in char_id_map.items():
                if lang_key == "语音key":
                    continue

                # 解析语言代码
                target_lang = "cn"
                if "日" in lang_key:
                    target_lang = "jp"
                elif "英" in lang_key:
                    target_lang = "us"
                elif "韩" in lang_key:
                    target_lang = "kr"
                elif "方" in lang_key:
                    target_lang = "fy"
                elif "意" in lang_key:
                    target_lang = "it"

                # 检查是否需要下载该语言
                lang_rank = self.LANGUAGE_MAP[target_lang]["rank"]
                if lang_rank not in download_langs:
                    continue

                # 检查皮肤逻辑
                is_skin_voice = "(" in lang_key
                if is_skin_voice and not auto_download_skin:
                    continue

                current_char_name = f"{base_char}皮肤" if is_skin_voice else base_char

                # 下载该语言下的所有语音
                file_idx = 1
                desc_idx = 0

                logger.info(f"正在下载 {current_char_name} 的 {target_lang} 语音...")

                # 尝试下载直到连续失败或达到上限
                while desc_idx < len(self.VOICE_DESCRIPTIONS) and file_idx <= 50:
                    desc = self.VOICE_DESCRIPTIONS[desc_idx]
                    fname = f"cn_{file_idx:03d}.wav"
                    voice_url = f"{base_url}/{key}/{fname}"

                    if await self._download_single_voice(
                        current_char_name, voice_url, target_lang, desc
                    ):
                        total_success += 1
                        desc_idx += 1
                    else:
                        pass

                    file_idx += 1

            self.scan_voice_files()
            return True, f"下载完成，共新增 {total_success} 条语音"

        except Exception as e:
            logger.error(f"下载语音异常: {e}")
            return False, str(e)

    async def _download_single_voice(
        self, character: str, url: str, lang: str, filename: str
    ) -> bool:
        """下载单个文件 helper"""
        real_char = character.replace("皮肤", "")
        base_dir = self.voices_dir / real_char
        if character.endswith("皮肤"):
            save_dir = base_dir / "skin" / lang
        else:
            save_dir = base_dir / lang

        save_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", filename)
        path = save_dir / f"{safe_name}.wav"

        if path.exists():
            return True

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.DEFAULT_HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        with open(path, "wb") as f:
                            f.write(data)
                        return True
        except:
            return False
        return False

    async def _get_character_id_map(self, character: str) -> Optional[Dict]:
        """爬取 PRTS Wiki 获取语音 Key 映射"""
        try:
            real_char = character.replace("皮肤", "")
            url = f"https://prts.wiki/w/{real_char}/语音记录"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.DEFAULT_HEADERS) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            voice_div = soup.find("div", {"data-voice-base": True})
            if not voice_div:
                return None

            voice_data = voice_div["data-voice-base"]
            result = {}
            for item in voice_data.split(","):
                if ":" not in item:
                    continue
                lang, path = item.split(":", 1)
                result[lang.strip()] = path.strip()

            return result
        except Exception as e:
            logger.error(f"解析Wiki失败: {e}")
            return None

    async def ensure_assets(self):
        """确保已有角色的头像存在"""
        try:
            # 1. 找出缺头像的角色
            missing_chars = []
            for char in self.voice_index.keys():
                real_char = char.replace("皮肤", "")
                if not (self.assets_dir / f"{real_char}.png").exists():
                    missing_chars.append(real_char)

            if not missing_chars:
                return

            # 2. 逐个获取
            for char in set(missing_chars):
                url = f"https://prts.wiki/w/文件:头像_{char}.png"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            # 正则获取 og:image 的内容
                            match = re.search(
                                r'<meta property="og:image" content="([^"]+)"', html
                            )
                            if match:
                                img_url = match.group(1)
                                # 处理 URL 格式 (PRTS wiki 有时返回相对路径或无协议路径)
                                if img_url.startswith("//"):
                                    img_url = "https:" + img_url
                                elif not img_url.startswith("http"):
                                    img_url = "https://prts.wiki" + img_url

                                # 下载图片
                                try:
                                    async with session.get(img_url) as img_resp:
                                        if img_resp.status == 200:
                                            img_data = await img_resp.read()
                                            with open(
                                                self.assets_dir / f"{real_char}.png",
                                                "wb",
                                            ) as f:
                                                f.write(img_data)
                                            # logger.info(f"已缓存头像: {real_char}")
                                except Exception as e:
                                    logger.warning(f"头像下载失败 {real_char}: {e}")

        except Exception as e:
            logger.warning(f"资源检查过程出现异常: {e}")
