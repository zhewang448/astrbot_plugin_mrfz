from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.config.astrbot_config import AstrBotConfig
import random
import re
from typing import List, Optional, Dict, Tuple
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path
import aiohttp
from astrbot.api.star import StarTools
import json


@register("astrbot_plugin_mrfz", "bushikq", "明日方舟角色语音插件", "1.5.0")
class MyPlugin(Star):
    # 语音描述列表
    VOICE_DESCRIPTIONS = [
        "任命助理", "交谈1", "交谈2", "交谈3", "晋升后交谈1", "晋升后交谈2",
        "信赖提升后交谈1", "信赖提升后交谈2", "信赖提升后交谈3", "闲置", "干员报到",
        "观看作战记录", "精英化晋升1", "精英化晋升2", "编入队伍", "任命队长",
        "行动出发", "行动开始", "选中干员1", "选中干员2", "部署1", "部署2",
        "作战中1", "作战中2", "作战中3", "作战中4", "完成高难行动", "3星结束行动",
        "非3星结束行动", "行动失败", "进驻设施", "戳一下", "信赖触摸", "标题",
        "新年祝福", "问候", "生日", "周年庆典"
    ]

    # 已知角色ID映射
    KNOWN_CHARACTER_IDS = {
        "阿": "char_225_haak",
        "维什戴尔皮肤":"char_1035_wisdel_sale__14"
        # 可以添加更多已知的角色ID
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_mrfz")
        self.voices_dir = self.data_dir / "voices"
        self.voice_index: Dict[str, Dict[str, List[str]]] = {}
        
        # 确保数据目录存在
        self.data_dir.mkdir(exist_ok=True)
        
        # 移动并处理配置文件
        self._handle_config_schema()
        
        # 从配置中读取设置
        self.config = config
        self.auto_download = self.config.get("auto_download", True)
        self.language_list=["fy","jp", "cn"]
        self.default_language_rank = self.config.get("default_language_rank", "123")
        
        # 创建voices目录并扫描文件
        self.voices_dir.mkdir(exist_ok=True)
        self.scan_voice_files()

    def _handle_config_schema(self) -> None:
        """处理配置文件,确保它在正确的位置"""
        schema_content ={
            "auto_download": {
                "description": "是否自动下载未找到的角色语音",
                "type": "bool",
                "hint": "true/false",
                "default": True
            },
            "default_language_rank": {
                "type": "string",
                "description": "设置语言优先级     1:方言, 2:汉语, 3:日语",
                "hint": "将对应的语音序号优先级输入，默认为123",
                "default": "123"
            }
} 
        
        # 配置文件路径
        config_path = self.data_dir / "_conf_schema.json"
        
        # 如果配置文件不存在,创建它
        if not config_path.exists():
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(schema_content, f, ensure_ascii=False, indent=4)

    def scan_voice_files(self) -> None:
        """扫描已下载的语音文件并建立索引"""
        self.voice_index.clear()
        if not self.voices_dir.exists():
            return

        for char_dir in self.voices_dir.iterdir():
            if not char_dir.is_dir():
                continue

            character = char_dir.name
            self.voice_index[character] = {}
            
            for lang_dir in char_dir.iterdir():
                if not lang_dir.is_dir():
                    continue

                language = lang_dir.name
                self.voice_index[character][language] = [
                    f.name for f in lang_dir.iterdir()
                    if f.is_file() and f.suffix.lower() == '.wav'
                ]

    def _get_character_dir(self, character: str) -> Path:
        """获取角色语音目录"""
        return self.voices_dir / character

    def _get_voice_files(self, character: str, language: str = "jp") -> List[str]:
        """获取指定角色的语音文件列表"""
        if character not in self.voice_index:
            return []
        # 合并所有语言下的文件名
        files = set()
        for lang_files in self.voice_index[character].values():
            files.update(lang_files)
        return sorted(files)

    def _get_voice_path(self, character: str, voice_name: str, language: str = "jp") -> Optional[Path]:
        """获取语音文件的完整路径"""
        char_dir = self._get_character_dir(character)
        
        # 优先查找指定语言
        path = char_dir / language / voice_name
        if path.exists():
            return path
            
        # 如果指定语言没有，查找所有语言
        for lang_dir in char_dir.iterdir():
            if not lang_dir.is_dir():
                continue
            path = lang_dir / voice_name
            if path.exists():
                return path
        return None

    async def download_voice(self, character: str, voice_url: str, language: str, description: str) -> Tuple[bool, str]:
        """下载单个语音文件"""
        try:
            # 创建保存目录
            save_dir = self._get_character_dir(character) / language
            save_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成安全的文件名
            safe_description = re.sub(r'[\\/:*?"<>|]', '_', description)
            filename = f"{safe_description}.wav"
            save_path = save_dir / filename
            
            # 检查文件是否已存在
            if save_path.exists():
                if character not in self.voice_index:
                    self.voice_index[character] = {}
                if language not in self.voice_index[character]:
                    self.voice_index[character][language] = []
                if filename not in self.voice_index[character][language]:
                    self.voice_index[character][language].append(filename)
                return True, f"文件已存在: {filename}"
            
            # 下载文件
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://prts.wiki/"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(voice_url, headers=headers) as response:
                    if response.status != 200:
                        return False, f"下载失败: HTTP {response.status}"
                    
                    content_type = response.headers.get('content-type', '')
                    if not any(media_type in content_type.lower() for media_type in ['audio', 'video', 'application/octet-stream']):
                        return False, f"非音频文件: {content_type}"
                    
                    content = await response.read()
                    
            # 保存文件
            save_path.write_bytes(content)
            
            # 更新语音索引
            if character not in self.voice_index:
                self.voice_index[character] = {}
            if language not in self.voice_index[character]:
                self.voice_index[character][language] = []
            if filename not in self.voice_index[character][language]:
                self.voice_index[character][language].append(filename)
                
            return True, filename
            
        except aiohttp.ClientError as e:
            return False, f"网络错误: {str(e)}"
        except IOError as e:
            return False, f"文件系统错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"

    async def get_character_id(self, character: str) -> Optional[str]:
        """获取角色的语音ID"""
        try:
            # 1. 检查已知映射
            if character in self.KNOWN_CHARACTER_IDS:
                return self.KNOWN_CHARACTER_IDS[character]
            
            # 2. 尝试从PRTS Wiki获取
            wiki_url = f"https://prts.wiki/w/{character}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(wiki_url, headers=headers) as response:
                    if response.status != 200:
                        return None
                    
                    content = await response.text()
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    # 查找包含角色ID的元素
                    for element in soup.find_all(['div', 'span', 'a']):
                        if 'data-char-id' in element.attrs:
                            return f"char_{element['data-char-id']}"
                    
                    # 查找语音相关链接
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        if 'voice' in href and 'char_' in href:
                            match = re.search(r'char_\d+_\w+', href)
                            if match:
                                return match.group(0)
                    
                    # 在页面内容中搜索可能的ID
                    matches = re.findall(r'char_\d+_[a-zA-Z0-9]+', str(soup))
                    if matches:
                        return matches[0]
            
            return None
            
        except Exception as e:
            print(f"获取角色ID时出错: {str(e)}")
            return None

    async def fetch_character_voices(self, character: str) -> Tuple[bool, str]:
        """获取角色语音"""
        try:
            char_id = await self.get_character_id(character)
            if not char_id:
                return False, f"无法获取角色 {character} 的ID"
            print(f"获取到角色ID: {char_id}")
            
            voice_configs = {
                "cn": {
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice_cn/{char_id}",
                    "patterns": ["cn_{num:03d}.wav"]
                },
                "jp": {
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice/{char_id}",
                    "patterns": ["cn_{num:03d}.wav"]
                },
                "fy":{
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice_custom/{char_id}_cn_topolect",
                    "patterns": ["cn_{num:03d}.wav"]
                }
            }
            total_voices = 0
            failed_voices = 0
            
            for lang, config in voice_configs.items():
                base_url = config["base_url"]
                found_pattern = None
                for pattern in config["patterns"]:
                    test_url = f"{base_url}/{pattern.format(num=1)}"
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.head(test_url, headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                            }) as response:
                                if response.status == 200:
                                    found_pattern = pattern
                                    break
                    except:
                        continue
                        
                if not found_pattern:
                    print(f"未找到{lang}语音文件格式，尝试下一种语言")
                    continue
                    
                print(f"开始下载{lang}语音...")
                desc_idx = 0  # 当前描述索引
                file_idx = 1  # 当前文件索引
                
                while desc_idx < len(self.VOICE_DESCRIPTIONS) and file_idx <= 50:
                    description = self.VOICE_DESCRIPTIONS[desc_idx]
                    fname = found_pattern.format(num=file_idx)
                    voice_url = f"{base_url}/{fname}"
                    
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.head(voice_url, headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                            }) as response:
                                if response.status == 200:
                                    print(f"正在下载: {lang}语音{file_idx} ({voice_url}) -> {description}.wav")
                                    #await asyncio.sleep(0.1)
                                    success, message = await self.download_voice(character, voice_url, lang, description)
                                    if success:
                                        total_voices += 1
                                        desc_idx += 1  # 只有下载成功才移动到下一个描述
                                        print(f"成功下载: {lang}语音{file_idx}")
                                    else:
                                        failed_voices += 1
                                        print(f"下载失败 ({lang}语音{file_idx}): {message}")
                                else:
                                    print(f"语音{file_idx}不存在，跳过")
                    except Exception as e:
                        print(f"下载{lang}语音{file_idx}时出错: {str(e)}")
                        failed_voices += 1
                    
                    file_idx += 1  # 无论成功与否都尝试下一个文件
                    
            if total_voices == 0:
                return False, "未能成功下载任何语音"
                
            return True, f"下载完成: 成功 {total_voices} 个, 失败 {failed_voices} 个"
            
        except Exception as e:
            print(f"获取语音时出错: {str(e)}")
            return False, f"获取语音失败: {str(e)}"

    @filter.command("mrfz")
    async def mrfz_handler(self, event: AstrMessageEvent, character: str = None, voice_name: str = None, language: str = None):
        """/mrfz [角色名] [语音名] [jp/cn/fy] 随机播放指定角色的语音。不指定语音名则随机播放。"""
        try:
            # 处理语言参数
            if not language:
                lang = self.language_list[int(self.default_language_rank[0])-1]
            elif language.lower() in ["cn", "1"]:
                lang = "cn"
            elif language.lower() in ["jp", "0"]:
                lang = "jp" 
            elif language.lower() in ["fy"]:
                lang = "fy"
            else:
                yield event.plain_result("语言参数错误,请使用 jp 或 cn")
                return

            # 如果未指定角色,随机选择一个角色
            if not character:
                if not self.voice_index:
                    yield event.plain_result("还没有任何角色的语音文件")
                    return
                character = random.choice(list(self.voice_index.keys()))

            # 获取语音文件列表
            voice_files = self._get_voice_files(character, lang)
            if not voice_files:
                if not self.auto_download:
                    yield event.plain_result(f"未找到角色 {character} 的语音文件。自动下载已禁用，请使用 /mrfz_fetch 手动获取。")
                    return
                    
                # 角色语音不存在，自动执行fetch
                yield event.plain_result(f"未找到角色 {character} 的语音文件，正在自动获取...")
                fetch_success, fetch_msg = await self.fetch_character_voices(character)
                if not fetch_success:
                    yield event.plain_result(f"自动获取失败: {fetch_msg}")
                    return
                # 重新获取语音文件列表
                voice_files = self._get_voice_files(character, lang)
                if not voice_files:
                    yield event.plain_result(f"获取后仍未找到角色 {character} 的语音文件")
                    return
                yield event.plain_result(f"成功获取角色 {character} 的语音文件")

            # 处理语音文件名
            if not voice_name:
                voice_name = random.choice(voice_files)
            elif not voice_name.endswith('.wav'):
                voice_name = f"{voice_name}.wav"
                
            if voice_name not in voice_files:
                yield event.plain_result(f"未找到语音：{voice_name}")
                return

            yield event.plain_result(f"正在播放 {character} 的语音：{voice_name[:-4]}")
            # 获取并播放语音
            voice_path = self._get_voice_path(character, voice_name, lang)
            if not voice_path:
                yield event.plain_result("语音文件不存在")
                return
            async for msg in self.send_voice_message(event, str(voice_path)):
                yield msg
                
        except Exception as e:
            yield event.plain_result(f"播放语音时出错：{str(e)}")

    @filter.command("mrfz_list")
    async def mrfz_list_handler(self, event: AstrMessageEvent):
        """/mrfz_list 显示所有可用语音和已下载角色列表"""
        try:
            # 显示所有可用的语音类型
            voice_list_str = "可用语音列表：" + " / ".join(self.VOICE_DESCRIPTIONS)
            
            # 显示已下载的角色列表
            downloaded_chars = {}
            for char in self.voice_index:
                langs = []
                if "jp" in self.voice_index[char]:
                    langs.append("日语")
                if "cn" in self.voice_index[char]:
                    langs.append("中文")
                if "fy" in self.voice_index[char]:
                    langs.append("方言")
                if langs:
                    downloaded_chars[char] = langs
            
            if downloaded_chars:
                char_list_str = "\n\n已下载的角色：" + " / ".join(
                    f"{char}（{', '.join(langs)}）" 
                    for char, langs in downloaded_chars.items()
                )
            else:
                char_list_str = "\n\n还没有下载任何角色的语音"
            
            yield event.plain_result(voice_list_str + char_list_str)

        except Exception as e:
            yield event.plain_result(f"获取语音列表时出错：{str(e)}")

    @filter.command("mrfz_fetch")
    async def mrfz_fetch_handler(self, event: AstrMessageEvent, character: str):
        """/mrfz_fetch [角色名] 从网络获取并下载指定角色的全部语音。"""
        try:
            yield event.plain_result(f"开始获取角色 {character} 的语音文件,这可能需要一些时间...")
            
            success, result = await self.fetch_character_voices(character)
            if success:
                yield event.plain_result(f"获取完成: {result}")
            else:
                yield event.plain_result(f"获取失败: {result}")

        except Exception as e:
            yield event.plain_result(f"获取语音文件时出错：{str(e)}")

    async def send_voice_message(self, event: AstrMessageEvent, voice_file_path: str):
        """发送语音消息"""
        try:
            chain = [Record.fromFileSystem(voice_file_path)]
            yield event.chain_result(chain)
        except Exception as e:
            yield event.plain_result(f"发送语音消息时出错：{str(e)}")
            
