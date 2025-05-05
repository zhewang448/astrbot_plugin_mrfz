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


@register("astrbot_plugin_mrfz", "bushikq", "明日方舟角色语音插件", "2.0.0")
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
        "维什戴尔皮肤":"char_1035_wisdel_sale__14",
        "年":"char_2014_nian"
        # 可以添加更多已知的角色ID
    }

    # 皮肤语音配置
    SKIN_VOICE_CONFIGS = {
        "维什戴尔皮肤": {
            "base_url": "https://torappu.prts.wiki/assets/audio/voice",
            "patterns": ["cn_{num:03d}.wav"]
        }
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
        self.auto_download_skin = self.config.get("auto_download_skin", True)
        # 修改语言列表顺序，使其与优先级对应：1:方言, 2:汉语, 3:日语
        self.language_list = ["fy", "cn", "jp"]
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
            "auto_download_skin": {
                "description": "是否自动下载角色的皮肤语音",
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
        # 如果是皮肤，使用基础角色名
        if character.endswith("皮肤"):
            base_character = character.replace("皮肤", "")
            return self.voices_dir / base_character
        return self.voices_dir / character

    def _get_voice_files(self, character: str, language: str = "jp") -> List[str]:
        if character not in self.voice_index:
            return []
        if language not in self.voice_index[character]:
            return []
        return sorted(self.voice_index[character][language])

    def _get_voice_path(self, character: str, voice_name: str, language: str = "jp") -> Optional[Path]:
        """获取语音文件的完整路径"""
        char_dir = self._get_character_dir(character)
        
        # 如果是皮肤，使用skin子目录
        if character.endswith("皮肤"):
            path = char_dir / "skin" / language / voice_name
        else:
            path = char_dir / language / voice_name
            
        if path.exists():
            return path
            
        # 如果指定语言没有，查找所有语言
        if character.endswith("皮肤"):
            base_path = char_dir / "skin"
        else:
            base_path = char_dir
            
        for lang_dir in base_path.iterdir():
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
            char_dir = self._get_character_dir(character)
            if character.endswith("皮肤"):
                save_dir = char_dir / "skin" / language
            else:
                save_dir = char_dir / language
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

    async def get_skin_voice_url(self, char_id: str) -> Optional[str]:
        """获取皮肤语音的URL"""
        try:
            # 从char_id中提取基础ID（去掉可能的skin后缀）
            base_id = char_id.split('_skin')[0] if '_skin' in char_id else char_id
            
            # 尝试不同的皮肤语音URL模式
            patterns = [
                # 标准皮肤语音路径
                f"https://torappu.prts.wiki/assets/audio/voice/{base_id}_skin",
                # 中文皮肤语音路径
                f"https://torappu.prts.wiki/assets/audio/voice_cn/{base_id}_skin",
                # 特殊皮肤路径（如维什戴尔）
                f"https://torappu.prts.wiki/assets/audio/voice/{base_id}",
                # 备用路径
                f"https://torappu.prts.wiki/assets/audio/voice_cn/{base_id}",
                # 超新星皮肤路径
                f"https://torappu.prts.wiki/assets/audio/voice/{base_id}_sale__14",
                f"https://torappu.prts.wiki/assets/audio/voice_cn/{base_id}_sale__14"
            ]
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://prts.wiki/"
            }
            
            async with aiohttp.ClientSession() as session:
                for base_url in patterns:
                    test_url = f"{base_url}/cn_001.wav"
                    try:
                        async with session.head(test_url, headers=headers) as response:
                            if response.status == 200:
                                print(f"找到皮肤语音URL: {base_url}")
                                return base_url
                    except:
                        continue
            
            return None
            
        except Exception as e:
            print(f"获取皮肤语音URL时出错: {str(e)}")
            return None

    async def get_character_id(self, character: str) -> Optional[str]:
        """获取角色的语音ID"""
        try:
            # 1. 检查已知映射
            if character in self.KNOWN_CHARACTER_IDS:
                return self.KNOWN_CHARACTER_IDS[character]
            
            # 2. 尝试从PRTS Wiki获取
            # 对角色名进行URL编码
            encoded_character = character.replace("皮肤", "")
            wiki_url = f"https://prts.wiki/w/{encoded_character}/语音记录"
            print(f"正在访问URL: {wiki_url}")
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://prts.wiki/"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(wiki_url, headers=headers) as response:
                    if response.status != 200:
                        print(f"访问页面失败: HTTP {response.status}")
                        return None
                    
                    content = await response.text()
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    # 如果是皮肤，尝试从页面中提取皮肤ID
                    if character.endswith("皮肤"):
                        print("正在查找皮肤ID...")
                        # 1. 首先查找语音表格
                        voice_table = soup.find('table', class_='wikitable')
                        if voice_table:
                            print("找到语音表格")
                            # 查找所有语音行
                            for row in voice_table.find_all('tr'):
                                # 查找包含语音信息的单元格
                                voice_cell = row.find('td', class_='voice-cell')
                                if voice_cell:
                                    print("找到语音单元格")
                                    # 查找语音按钮
                                    voice_btn = voice_cell.find('button', class_='voice-btn')
                                    if voice_btn and 'data-voice-base' in voice_btn.attrs:
                                        voice_base = voice_btn['data-voice-base']
                                        print(f"找到语音信息: {voice_base}")
                                        # 解析语音信息
                                        for voice_part in voice_base.split(','):
                                            if 'voice' in voice_part and 'char_' in voice_part:
                                                # 提取完整的皮肤ID
                                                match = re.search(r'char_\d+_[^,]+', voice_part)
                                                if match:
                                                    skin_id = match.group(0)
                                                    # 检查是否是皮肤ID（以下划线加数字结尾）
                                                    if re.search(r'_\d+$', skin_id):
                                                        print(f"找到皮肤ID: {skin_id}")
                                                        return skin_id
                        # 2. 查找皮肤信息区域
                        skin_section = soup.find('div', class_='skin-info')
                        if skin_section:
                            print("找到皮肤信息区域")
                            # 查找所有语音相关元素
                            for element in skin_section.find_all(['div', 'span', 'a']):
                                if 'data-voice-id' in element.attrs:
                                    skin_id = element['data-voice-id']
                                    # 检查是否是皮肤ID（以下划线加数字结尾）
                                    if re.search(r'_\d+$', skin_id):
                                        print(f"从皮肤信息区域找到ID: {skin_id}")
                                        return skin_id
                        # 3. 查找所有可能包含皮肤ID的元素
                        print("搜索所有可能包含皮肤ID的元素...")
                        for element in soup.find_all(['div', 'span', 'a', 'button']):
                            # 检查所有可能包含皮肤ID的属性
                            for attr in ['data-voice-id', 'data-voice-base', 'data-skin-id']:
                                if attr in element.attrs:
                                    value = element[attr]
                                    print(f"找到属性 {attr}: {value}")
                                    if 'char_' in value:
                                        # 解析语音信息
                                        for voice_part in value.split(','):
                                            if 'voice' in voice_part and 'char_' in voice_part:
                                                match = re.search(r'char_\d+_[^,]+', voice_part)
                                                if match:
                                                    skin_id = match.group(0)
                                                    # 检查是否是皮肤ID（以下划线加数字结尾）
                                                    if re.search(r'_\d+$', skin_id):
                                                        print(f"找到皮肤ID: {skin_id}")
                                                        return skin_id
                        # 4. 在页面内容中搜索可能的ID
                        print("在页面内容中搜索ID...")
                        content_str = str(soup)
                        matches = re.findall(r'char_\d+_[^,]+', content_str)
                        if matches:
                            print(f"在页面内容中找到可能的ID: {matches}")
                            # 过滤出以下划线加数字结尾的ID
                            skin_matches = [m for m in matches if re.search(r'_\d+$', m)]
                            if skin_matches:
                                print(f"找到皮肤ID: {skin_matches[0]}")
                                return skin_matches[0]
                        # 没有找到合法皮肤ID，直接返回None
                        print("未找到合法皮肤ID")
                        return None
                    # 如果不是皮肤，查找普通角色ID
                    for element in soup.find_all(['div', 'span', 'a']):
                        if 'data-char-id' in element.attrs:
                            char_id = element['data-char-id']
                            print(f"找到普通角色ID: char_{char_id}")
                            return f"char_{char_id}"
                    
                    # 查找语音相关链接
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        if 'voice' in href and 'char_' in href:
                            match = re.search(r'char_\d+_\w+', href)
                            if match:
                                print(f"从链接中找到ID: {match.group(0)}")
                                return match.group(0)
                    
                    # 在页面内容中搜索可能的ID
                    matches = re.findall(r'char_\d+_\w+', str(soup))
                    if matches:
                        print(f"在页面内容中找到ID: {matches[0]}")
                        return matches[0]
            
            print("未找到任何ID")
            return None
            
        except Exception as e:
            print(f"获取角色ID时出错: {str(e)}")
            return None

    async def fetch_character_voices(self, character: str) -> Tuple[bool, str]:
        """获取角色语音"""
        try:
            # 获取基础角色名（去掉"皮肤"后缀）
            base_character = character.replace("皮肤", "")
            
            # 获取角色ID
            char_id = await self.get_character_id(character)
            # 如果是皮肤且没找到合法皮肤ID，直接跳过
            if character.endswith("皮肤") and not char_id:
                return False, f"{character} 没有专属皮肤语音"
            if not char_id:
                return False, f"无法获取角色 {character} 的ID"
            print(f"获取到角色ID: {char_id}")
            
            # 检查是否是皮肤语音
            is_skin = character.endswith("皮肤")
            
            # 构建语音配置
            voice_configs = {
                "cn": {
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice_cn/{char_id}",
                    "patterns": ["cn_{num:03d}.wav"]
                },
                "jp": {
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice/{char_id}",
                    "patterns": ["cn_{num:03d}.wav"]
                },
                "fy": {
                    "base_url": f"https://torappu.prts.wiki/assets/audio/voice_custom/{char_id}_cn_topolect",
                    "patterns": ["cn_{num:03d}.wav"]
                }
            }
            
            total_voices = 0
            failed_voices = 0
            skin_total_voices = 0
            skin_failed_voices = 0
            
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
                                    success, message = await self.download_voice(character, voice_url, lang, description)
                                    if success:
                                        if is_skin:
                                            skin_total_voices += 1
                                        else:
                                            total_voices += 1
                                        desc_idx += 1  # 只有下载成功才移动到下一个描述
                                        print(f"成功下载: {lang}语音{file_idx}")
                                    else:
                                        if is_skin:
                                            skin_failed_voices += 1
                                        else:
                                            failed_voices += 1
                                        print(f"下载失败 ({lang}语音{file_idx}): {message}")
                                else:
                                    print(f"语音{file_idx}不存在，跳过")
                    except Exception as e:
                        print(f"下载{lang}语音{file_idx}时出错: {str(e)}")
                        if is_skin:
                            skin_failed_voices += 1
                        else:
                            failed_voices += 1
                    
                    file_idx += 1  # 无论成功与否都尝试下一个文件
            
            # 如果不是皮肤且启用了自动下载皮肤语音，尝试下载皮肤语音
            if not is_skin and self.auto_download_skin:
                skin_character = f"{base_character}皮肤"
                print(f"尝试下载{skin_character}的语音...")
                skin_success, skin_result = await self.fetch_character_voices(skin_character)
                if skin_success:
                    # 从皮肤下载结果中提取统计信息
                    if isinstance(skin_result, str):
                        # 处理合并后的消息
                        match = re.search(r'皮肤语音: (\d+)个(?:\(失败(\d+)个\))?', skin_result)
                        if match:
                            skin_total_voices += int(match.group(1))
                            if match.group(2):
                                skin_failed_voices += int(match.group(2))
                    else:
                        # 处理消息列表
                        for msg in skin_result:
                            if "皮肤语音:" in msg:
                                match = re.search(r'皮肤语音: (\d+)个(?:\(失败(\d+)个\))?', msg)
                                if match:
                                    skin_total_voices += int(match.group(1))
                                    if match.group(2):
                                        skin_failed_voices += int(match.group(2))
                else:
                    print(f"下载{skin_character}的语音失败: {skin_result}")
            
            # 构建返回消息
            result_msg = []
            if total_voices > 0 or failed_voices > 0:
                result_msg.append(f"普通语音: {total_voices}个" + (f"(失败{failed_voices}个)" if failed_voices > 0 else ""))
            if skin_total_voices > 0 or skin_failed_voices > 0:
                result_msg.append(f"皮肤语音: {skin_total_voices}个" + (f"(失败{skin_failed_voices}个)" if skin_failed_voices > 0 else ""))
            
            if not result_msg:
                return False, "未能成功下载任何语音"
            
            # 返回合并后的消息
            return True, "，".join(result_msg)
            
        except Exception as e:
            print(f"获取语音时出错: {str(e)}")
            return False, f"获取语音失败: {str(e)}"

    @filter.command("mrfz")
    async def mrfz_handler(self, event: AstrMessageEvent, character: str = None, voice_name: str = None, language: str = None):
        """/mrfz [角色名] [语音名] [jp/cn/fy/skin] 随机播放指定角色的语音。不指定语音名则随机播放。"""
        try:
            # 处理语言参数
            if language:
                # 如果用户明确指定了语言，直接使用
                if language.lower() in ["cn", "1"]:
                    lang = "cn"
                elif language.lower() in ["jp", "0"]:
                    lang = "jp" 
                elif language.lower() in ["fy"]:
                    lang = "fy"
                elif language.lower() in ["skin"]:
                    lang = "skin"
                else:
                    yield event.plain_result("语言参数错误,请使用 jp(日语)、cn(中文)、fy(方言)、skin(皮肤语音)")
                    return
            else:
                # 如果用户没有指定语言，使用优先级配置
                if character in self.SKIN_VOICE_CONFIGS:
                    lang = "skin"
                else:
                    # 根据配置的语言优先级选择语言
                    for rank in self.default_language_rank:
                        try:
                            lang_index = int(rank) - 1  # 优先级从1开始，所以减1
                            if 0 <= lang_index < len(self.language_list):
                                lang = self.language_list[lang_index]
                                # 检查该语言下是否有语音文件
                                if self._get_voice_files(character, lang):
                                    break
                        except (ValueError, IndexError):
                            continue
                    else:
                        # 如果所有优先级都无效或没有找到语音文件，使用默认语言（方言）
                        lang = "fy"

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
                yield event.plain_result(result)
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
            
