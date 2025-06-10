import os
import json
import asyncio
import random
import re
from pathlib import Path
from typing import List, Optional, Dict, Tuple
import aiohttp
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
import logging
from PIL import Image as PILImage, ImageDraw, ImageFont
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.star import StarTools

@register("astrbot_plugin_mrfz", "bushikq", "明日方舟角色语音插件", "3.3.3")
class MyPlugin(Star):
    # HTTP请求头
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://prts.wiki/"
    }
    # 语音描述列表
    VOICE_DESCRIPTIONS = ["任命助理","交谈1","交谈2","交谈3","晋升后交谈1","晋升后交谈2","信赖提升后交谈1","信赖提升后交谈2","信赖提升后交谈3","闲置","干员报到","观看作战记录","精英化晋升1","精英化晋升2","编入队伍","任命队长","行动出发","行动开始","选中干员1","选中干员2","部署1","部署2","作战中1","作战中2","作战中3","作战中4","完成高难行动","3星结束行动","非3星结束行动","行动失败", "进驻设施","戳一下","信赖触摸","标题","新年祝福","问候","生日","周年庆典"]

    # 已知角色ID映射
    KNOWN_CHARACTER_IDS = {"阿": "char_225_haak","赫默": "char_171_bldsk",}

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件
        Args:
            context (Context): 插件上下文
            config (AstrBotConfig): 插件配置
        """
        try:
            super().__init__(context)
            
            # 初始化logger
            self.logger = logging.getLogger("astrbot_plugin_mrfz")
            # 确保logger有处理器
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
                handler.setFormatter(formatter)
                self.logger.addHandler(handler)
                self.logger.setLevel(logging.INFO)
            
            self.logger.info("明日方舟语音插件初始化中...")
            
            # 初始化目录
            self.data_dir = StarTools.get_data_dir("astrbot_plugin_mrfz")
            self.voices_dir = self.data_dir / "voices"
            self.assets_dir = self.data_dir / "assets"
            self.logger.info(f"资源目录路径: {self.assets_dir}")
            self.voice_index: Dict[str, List[str]] = {}
            self.plugin_dir = os.path.dirname(__file__)
            
            # 确保必要的目录存在
            for directory in [self.data_dir, self.voices_dir, self.assets_dir]:
                directory.mkdir(exist_ok=True)
                if not directory.exists():
                    raise RuntimeError(f"无法创建目录：{directory}")
            
            # 处理配置文件
            self._handle_config_schema()
            
            # 从配置中读取设置
            self.config = config
            self.auto_download = self.config.get("auto_download", True)
            self.auto_download_skin = self.config.get("auto_download_skin", True)
            self.language_list = ["fy", "cn", "jp","us",'kr','it']  # 1:方言, 2:汉语, 3:日语, 4:英语, 5:韩语, 6:意大利语
            self.default_language_rank = self.config.get("default_language_rank", "123456")
            self.enable_log_output = self.config.get("enable_log_output", False)
            self.auto_download_language = self.config.get("auto_download_language", "123")
            self.language_dic = {"中文-普通话":"cn","英语":"us","日语":"jp","韩语":"kr","中文-方言":"fy","意大利语":"it","fy":"1","cn":"2","jp":"3","us":"4","kr":"5","it":"6"}
            # 根据配置设置日志级别
            if not self.enable_log_output:
                self.logger.setLevel(logging.ERROR)  # 只输出错误信息
                self.logger.info("已禁用详细日志输出")
            else:
                self.logger.setLevel(logging.INFO)
                self.logger.info("已启用详细日志输出")
            # 扫描已有文件
            self.scan_voice_files()
            
            # 初始化头像缓存
            self._avatar_cache = {}
            self._all_avatars = {}
            
            # 初始化资源文件
            asyncio.create_task(self.ensure_assets())
            
        except Exception as e:
            print(f"插件初始化失败: {e}")
            raise

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
                "description": "设置语言优先级     1:方言, 2:汉语, 3:日语, 4:英语, 5:韩语",
                "hint": "将对应的语音序号优先级输入，默认为12345",
                "default": "12345"
            },
            "auto_download_language":{
                "type":"string",
                "description": "设置需要自动下载的语言     1:方言, 2:汉语, 3:日语, 4:英语, 5:韩语",
                "hint": "将对应的语音序号优先级输入，默认为123",
                "default": "123"
            },
            "enable_log_output": {
                "description": "是否在终端输出详细日志信息",
                "type": "bool",
                "hint": "true/false",
                "default": False
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
            self.voice_index[character] = []
            # 处理普通语言目录
            for lang_dir in char_dir.iterdir():
                if not lang_dir.is_dir():
                    continue
                # 如果是skin目录，特殊处理
                if lang_dir.name == "skin":
                    # 处理皮肤目录下的所有语言
                    skin_character = f"{character}皮肤"
                    if skin_character not in self.voice_index:
                        self.voice_index[skin_character] = []
                    
                    for skin_lang_dir in lang_dir.iterdir():
                        if not skin_lang_dir.is_dir():
                            continue
                        elif skin_lang_dir.is_dir() and not len(os.listdir(skin_lang_dir)) == 0:
                            self.voice_index[skin_character].append(skin_lang_dir.name)
                else:
                    if not len(os.listdir(lang_dir)) == 0:
                        self.voice_index[character].append(lang_dir.name)
        
        # 保存索引到文件
        self._save_voice_index()
        self.logger.info(f"扫描到{len(self.voice_index)}个角色的语音文件")
    
    def _save_voice_index(self) -> None:
        """保存语音索引到文件"""
        try:
            voice_index_file = self.data_dir / "voice_index.json"
            with open(voice_index_file, "w", encoding="utf-8") as f:
                json.dump(self.voice_index, f, ensure_ascii=False, indent=2)
            self.logger.info(f"语音索引已保存到 {voice_index_file}")
        except Exception as e:
            self.logger.error(f"保存语音索引失败: {str(e)}")

    def _get_character_dir(self, character: str) -> Path:
        """获取角色语音目录"""
        # 如果是皮肤，使用基础角色名
        if character.endswith("皮肤"):
            character = character.replace("皮肤", "")
        return self.voices_dir / character

    def _get_voice_path(self,character: str, voice_name: str, language: str ) -> Optional[Path]:
        """获取语音文件的完整路径
        
        Args:
            character: 角色名称
            voice_name: 语音文件名或语音描述
            language: 语言代码
            
        Returns:
            语音文件路径，如果未找到则返回None
        """
        self.logger.info(f"尝试获取语音路径: 角色={character}, 语音={voice_name}, 语言={language}")
        
        # 检查是否是皮肤角色
        is_skin_character = character.endswith("皮肤")
        
        # 获取基础角色目录
        char_dir = self._get_character_dir(character)
        base_path=char_dir / language if not is_skin_character else char_dir / "skin" / language
        path =base_path / f"{voice_name}.wav"
        if path.exists():
            self.logger.info(f"找到语音文件(其他语言): {path}")
            return path
        self.logger.warning(f"未找到语音文件: {character} - {voice_name} - {language}")
        return None

    async def ensure_assets(self):
        """确保必要的资源文件存在，如不存在则下载
        """
        self.logger.info("====== ensure_assets 开始执行 ======")
        
        # 先扫描语音文件，确保voice_index是最新的
        self.logger.info("扫描语音文件...")
        self.scan_voice_files()
        
        # 获取已经下载的语音列表中的角色
        voice_characters = []
        
        # 1. 从内存中的voice_index获取角色
        if self.voice_index:
            voice_characters = list(self.voice_index.keys())
            self.logger.info(f"从内存中的voice_index找到{len(voice_characters)}个角色")
        # 如果没有找到角色，直接返回
        if not voice_characters:
            self.logger.warning("没有找到需要下载头像的角色，跳过头像下载")
            return True
            
        # 下载所有已有语音干员的头像
        self.logger.info("开始下载角色头像...")
        
        # 获取头像映射
        try:
            avatar_map = await self._fetch_all_avatar_mappings()
            self.logger.info(f"获取到的头像映射数量：{len(avatar_map)}")
            
            # 检查已有语音的干员并下载头像
            for character in voice_characters:
                await self.download_avatar(character.replace("皮肤", ""), avatar_map)
            
            self.logger.info(f"头像下载完成，成功下载{len(avatar_map)}个头像")
        except Exception as e:
            self.logger.error(f"[ensure_assets] 下载头像过程出错: {str(e)}")
        
        self.logger.info("====== ensure_assets 执行完毕 ======")
        return True
    async def download_avatar(self,character, avatar_map):
        avatar_file = self.assets_dir / f"{character}.png"
        self.logger.info(f"检查角色头像：{avatar_file}")
        if not avatar_file.exists():
            if character in avatar_map:
                avatar_url = avatar_map[character]
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(avatar_url, headers=self.DEFAULT_HEADERS) as response:
                            if response.status == 200:
                                with open(avatar_file, "wb") as f:
                                    f.write(await response.read())
                                self.logger.info(f"{character}头像下载成功")
                            else:
                                self.logger.warning(f"{character}头像下载失败，状态码：{response.status}")
                except Exception as e:
                    self.logger.error(f"[ensure_assets] {character}头像下载出错: {avatar_url}")
            else:
                self.logger.warning(f"[ensure_assets] 未找到角色 {character} 的头像URL")
        else:
            self.logger.info(f"{character}头像已存在")
    async def fetch_operator_avatar(self, op_name: str) -> Tuple[str, str]:
            # 直接访问文件页面获取真实URL
            file_page_url = f"https://prts.wiki/w/文件:头像_{op_name}.png"
            self.logger.debug(f"访问文件页面: {file_page_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(file_page_url, headers=self.DEFAULT_HEADERS, timeout=10) as response:
                    if response.status == 200:
                        file_page_html = await response.text()
                        
                        # 使用正则表达式从meta标签中提取图片直接URL
                        image_url_match = re.search(r'<meta property="og:image" content="([^"]+)"', file_page_html)
                        if image_url_match:
                            image_url = image_url_match.group(1)
                            self.logger.debug(f"获取到干员{op_name}头像: {image_url}")
                            return op_name,image_url
            
            # 如果没找到，使用固定的无头像图片
            self.logger.debug(f"未找到干员 {op_name} 的头像，使用默认头像")
            return op_name,f"{self.plugin_dir}/assets/无头像.png"
    async def download_voice(self, character: str, voice_url: str, language: str, description: str) -> Tuple[bool, str]:
        """下载单个语音文件
        Args:
            character (str): 角色名
            voice_url (str): 语音文件URL
            language (str): 语言代码
            description (str): 语音描述
        Returns:
            Tuple[bool, str]: (是否成功, 消息)
        """
        MAX_RETRIES = 3
        retry_count = 0
        
        try:
            # 创建保存目录
            char_dir = self._get_character_dir(character)
            save_dir = char_dir / language if not character.endswith("皮肤") else char_dir / "skin" / language
            save_dir.mkdir(parents=True, exist_ok=True)
            # 生成安全的文件名
            safe_description = re.sub(r'[\\/:*?"<>|]', '_', description)
            filename = f"{safe_description}.wav"
            save_path = save_dir / filename
            
            # 检查文件是否已存在
            if save_path.exists():
                if character not in self.voice_index:
                    self.voice_index[character] = []
                if language not in self.voice_index[character]:
                    self.voice_index[character].append(language)
            if os.path.exists(f"{save_dir}/{filename}"):
                return True, f"文件已存在: {filename}"
            self._save_voice_index()

            while retry_count < MAX_RETRIES:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(voice_url, headers=self.DEFAULT_HEADERS) as response:
                            if response.status != 200:
                                retry_count += 1
                                if retry_count == MAX_RETRIES:
                                    return False, f"下载失败: HTTP {response.status}"
                                continue
                            
                            content_type = response.headers.get('content-type', '')
                            if not any(media_type in content_type.lower() for media_type in ['audio', 'video', 'application/octet-stream']):
                                return False, f"非音频文件: {content_type}"
                            
                            content = await response.read()
                            
                            # 保存文件
                            save_path.write_bytes(content)
                            
                            # 更新语音索引
                            if character not in self.voice_index:
                                self.voice_index[character] = []
                            if language not in self.voice_index[character]:
                                self.voice_index[character].append(language)
                            # 保存更新后的索引
                            self._save_voice_index()
                            return True, filename
                            
                except aiohttp.ClientError as e:
                    retry_count += 1
                    if retry_count == MAX_RETRIES:
                        return False, f"网络错误: {str(e)}"
                    await asyncio.sleep(0.5)  # 重试前等待
                    
        except IOError as e:
            return False, f"文件系统错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"
            
        return False, "下载失败: 超过最大重试次数"
    async def get_character_id(self,character: str) -> Optional[Dict]:
        try:
            encoded_character = character.replace("皮肤", "")
            wiki_url = f"https://prts.wiki/w/{encoded_character}/语音记录"
            print(f"正在访问URL: {wiki_url}")
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://prts.wiki/w/{character}/语音记录") as resp:
                    soup = BeautifulSoup(await resp.text(), 'html.parser')

                    # 关键解析步骤
                    voice_div = soup.find('div', {'data-voice-base': True})
                    if not voice_div:
                        print("未找到语音数据div")
                        return None

                    # 提取并处理data-voice-base
                    voice_data = voice_div['data-voice-base']
                    print(f"原始语音数据: {voice_data}")  # 调试输出

                    # 解析为结构化字典
                    result = {}
                    for lang_path in voice_data.split(','):
                        if ':' not in lang_path:  # 跳过无效条目
                            continue
                        
                        # 安全分割（只分割第一个冒号）
                        lang, path = lang_path.split(':', 1)
                        result[lang.strip()] = path.strip()

                        # 提取角色ID（从第一个路径中获取）
                        if 'char_' in path and '语音key' not in result:
                            result['语音key'] = re.search(r'char_\d+_\w+', path).group(0)

                    print(f"解析结果: {result}")
                    return result

        except Exception as e:
            print(f"解析失败: {e}")
            return None
   
    async def fetch_character_voices(self, character: str) -> Tuple[bool, str]:
        """一键获取角色语音和头像
        
        Args:
            character: 角色名称
            force_download: 是否强制下载，当为True时忽略已有文件检查
        
        Returns:
            (是否成功, 结果消息)
        """
        try:
            # 获取基础角色名（去掉"皮肤"后缀）
            base_character = character.replace("皮肤", "") 
            # 先确保语音索引是最新的
            self.scan_voice_files()
            self.logger.info(f"开始获取角色 {character} 的语音文件")
            
            # 获取角色ID
            char_id_list = await self.get_character_id(character)
            base_url='https://torappu.prts.wiki/assets/audio'
            # 设置下载统计
            total_voices = 0
            failed_voices = 0
            for lang, key in char_id_list.items():
                if lang == "语音key" or not self.language_dic[self.language_dic[lang.split('(')[0].strip()]] in self.auto_download_language:  # 跳过元数据字段和非自动下载语言
                    continue
                if '(' in lang and ')' in lang:
                    if not self.auto_download_skin:
                        self.logger.info(f"跳过{lang}语言，因为自动下载皮肤语音功能未启用")
                        continue
                    lang = lang.split('(')[0].strip()
                    character = f"{base_character}皮肤"
                self.logger.info(f"开始下载{lang}语音...")
                desc_idx = 0  # 当前描述索引
                file_idx = 1  # 当前文件索引
                # 遍历处理所有可能的语音文件
                lang = self.language_dic[lang] #换成字母形式
                while desc_idx < len(self.VOICE_DESCRIPTIONS) and file_idx <= 50:
                    # 获取当前描述
                    description = self.VOICE_DESCRIPTIONS[desc_idx]
                    
                    # 拼接文件名和URL
                    fname = f"cn_{file_idx:03d}.wav"
                    voice_url = f"{base_url}/{key}/{fname}"
                    
                    try:
                        # 检查语音文件是否存在
                        async with aiohttp.ClientSession() as session:
                            async with session.head(voice_url, headers=self.DEFAULT_HEADERS) as response:
                                if response.status == 200:
                                    # 文件存在，尝试下载
                                    self.logger.info(f"正在下载: {lang}语音{file_idx} ({voice_url}) -> {description}.wav")
                                    success, message = await self.download_voice(character, voice_url, lang, description)
                                    if success:
                                        total_voices += 1
                                        desc_idx += 1  # 只有下载成功才移动到下一个描述
                                        self.logger.info(f"成功下载: {lang}语音{file_idx}")
                                    else:
                                        failed_voices += 1
                                        self.logger.warning(f"下载失败 ({lang}语音{file_idx}): {message}")
                                else:
                                    self.logger.info(f"语音{file_idx}不存在，跳过")
                    except Exception as e:
                        self.logger.error(f"下载{lang}语音{file_idx}时出错: {str(e)}")
                        failed_voices += 1
                    
                    file_idx += 1  # 无论成功与否都尝试下一个文件
            
            # 在下载完成后立即扫描更新索引
            if total_voices > 0 :
                self.logger.info(f"下载完成，开始扫描语音文件更新索引...")
                self.scan_voice_files()
                self.logger.info(f"语音索引更新完成")
            
            # 构建返回消息
            result_msg = []
            if total_voices > 0 or failed_voices  or True:
                result_msg.append(f"成功语音: {total_voices}个" + (f"(失败{failed_voices}个)" if failed_voices > 0 else ""))
            
            if not result_msg:
                return False, "未能成功下载任何语音"
            
            # 返回合并后的消息
            return True, "，".join(result_msg)
            
        except Exception as e:
            self.logger.error(f"获取语音时出错: {str(e)}")
            return False, f"获取语音失败: {str(e)}"

    async def choose_language(self, character: str, is_skin_character: bool) -> str:
            """根据优先级自动查找可用的语言
            """
            lang = self.language_list[0]  # 默认使用第一个语言
            for rank in self.default_language_rank:
                try:
                    lang_index = int(rank) - 1  # 优先级从1开始，所以减1
                    if 0 <= lang_index < len(self.language_list):
                        potential_lang = self.language_list[lang_index]
                        # 检查该语言下是否有语音文件
                        if  not is_skin_character and (potential_lang in self.voice_index[character]):
                            lang = potential_lang
                            break
                        elif is_skin_character and potential_lang in self.voice_index[character.replace("皮肤", "")]:
                            lang = potential_lang
                            break
                        else:
                            lang = "nodownload"
                except (ValueError, IndexError):
                    continue
            self.logger.info(f"确定使用语言: {lang}")
            return lang
            
    @filter.command("mrfz")
    async def mrfz_handler(self, event: AstrMessageEvent, character: str = None, voice_name: str = None, language: str = None):
        """/mrfz [角色名] [语音名] [语言] 随机播放指定角色的语音。不指定语音名则随机播放。"""
        try:
            # 确保语音索引是最新的（只在开始时扫描一次）
            self.scan_voice_files()
            self.logger.info(f"开始处理mrfz命令: 角色={character}, 语音={voice_name}, 语言={language}")
            
            # 检查是否是皮肤角色
            is_skin_character = character and character.endswith("皮肤")
            
            # 如果未指定角色，随机选择一个角色
            if not character:
                if not self.voice_index:
                    yield event.plain_result("还没有任何角色的语音文件")
                    return
                character = random.choice(list(self.voice_index.keys()))
                self.logger.info(f"未指定角色，随机选择 {character}")
            
            # 确定需要使用的语言
            if not character in self.voice_index:
                lang="nodownload"
            else: 
                if language:
                    # 如果用户明确指定了语言，直接使用
                    if language.lower() in self.language_list:
                        lang = language.lower()
                    else:
                        yield event.plain_result("语言参数错误,请使用 jp(日语)、cn(中文)、fy(方言)、us(英语)、kr(韩语)、it(意语)")
                        return
                else:
                        # 根据配置选择语言优先级
                        lang = await self.choose_language(character,is_skin_character)
            # 如果上述所有方法都找不到语音文件，则尝试下载
            if lang == "nodownload":
                if not self.auto_download:
                    yield event.plain_result(f"未找到角色 {character} 的{language}语音文件。自动下载已禁用，请使用 /mrfz_fetch 手动获取。")
                    return
                
                self.logger.info(f"未找到角色 {character} 的语音文件，尝试自动下载")
                yield event.plain_result(f"未找到角色 {character} 的语音文件，正在自动获取...")
                
                # 尝试下载
                fetch_success, fetch_msg = await self.fetch_character_voices(character)
                if not fetch_success:
                    yield event.plain_result(f"自动获取失败: {fetch_msg}")
                    return
                self.scan_voice_files()
                lang = await self.choose_language(character,is_skin_character)
            # 播放语音
            if not voice_name:
                voice_name = random.choice(self.VOICE_DESCRIPTIONS)
            else:
                voice_name = voice_name
            yield event.plain_result(f"正在播放 {character} 的语音：{voice_name}")
            voice_path = self._get_voice_path(character, voice_name, lang)
            if not voice_path:
                # 如果是皮肤角色，尝试直接使用skin语言
                if is_skin_character and lang != "skin":
                    voice_path = self._get_voice_path(character, voice_name, "skin")
                
                # 如果仍然找不到，尝试在基础角色的skin目录下查找
                if not voice_path and is_skin_character:
                    base_character = character.replace("皮肤", "")
                    voice_path = self._get_voice_path(base_character, voice_name, "skin")
                
                if not voice_path:
                    yield event.plain_result("语音文件不存在")
                    return
            
            async for msg in self.send_voice_message(event, str(voice_path)):
                yield msg
                
        except Exception as e:
            self.logger.error(f"播放语音时出错: {str(e)}", exc_info=True)
            yield event.plain_result(f"播放语音时出错：{str(e)}")

    @filter.command("mrfz_list")
    async def mrfz_list_handler(self, event: AstrMessageEvent):
        """处理语音列表命令"""
        try:
            if not self.voice_index:
                yield event.plain_result("当前没有缓存的语音文件，请先使用 mrfz_fetch 命令获取语音。")
                return
            
            self.scan_voice_files()
            await self.ensure_assets()

            # 1. 语音类型
            voice_types = self.VOICE_DESCRIPTIONS

            # 2. 已下载干员及语言
            operators = []  # 普通角色
            skin_operators = []  # 皮肤角色
            
            for char in self.voice_index:
                # 如果角色名以"皮肤"结尾，放入皮肤列表
                if char.endswith("皮肤"):
                    skin_character = {
                        "name": char,
                        "languages": [],
                        "avatar_url": ""
                    }
                    
                    # 收集语言信息
                    for lang_code in ["jp", "cn", "fy", "us", "kr", "it"]:
                        if lang_code in self.voice_index[char]:
                            display_name = {"jp": "日语", "cn": "中文", "fy": "方言", 
                                          "us": "英语", "kr": "韩语","it":"意语"}.get(lang_code, lang_code)
                            skin_character["languages"].append({"code": lang_code, "display": display_name})
                    
                    # 获取基础角色名，用于头像
                    base_name = char.replace("皮肤", "")
                    skin_character["avatar_url"] = f"file://{self.data_dir / "assets" / base_name}.png".replace("\\", "/")
                    skin_operators.append(skin_character)
                    continue
                
                langs = []
                has_skin = False
                
                # 收集普通语言
                for lang_code, display_name in [
                    ("jp", "日语"), ("cn", "中文"), ("fy", "方言"),
                    ("us", "英语"), ("kr", "韩语"), ("it", "意语")
                ]:
                    if lang_code in self.voice_index[char]:
                        langs.append({"code": lang_code, "display": display_name})
                
                # 检查是否有皮肤语音
                char_dir = self._get_character_dir(char)
                skin_path = char_dir / "skin"
                if skin_path.exists() and skin_path.is_dir():
                    # 遍历skin目录下的所有语言目录
                    for lang_dir in skin_path.iterdir():
                        if lang_dir.is_dir():
                            # 检查每个语言目录中是否有语音文件
                            if any(f.suffix.lower() == '.wav' for f in lang_dir.iterdir() if f.is_file()):
                                has_skin = True
                                break
                    
                    if has_skin:
                        langs.append({"code": "skin", "display": "皮肤"})
                
                # 获取角色头像
                avatar_url = f"file://{self.data_dir / "assets" / "{base_name}.png"}".replace("\\", "/")
                
                operators.append({
                    "name": char,
                    "languages": langs,
                    "avatar_url": avatar_url,
                    "has_skin": has_skin
                })
            
            # 3. 渲染HTML
            template_file = "mrfz_voice_list_template.html"
            # 创建Jinja2环境
            
            env = Environment(loader=FileSystemLoader(self.plugin_dir))
            
            # 如果模板文件不存在，提示错误
            template_path = os.path.join(self.plugin_dir, template_file)
            if not os.path.exists(template_path):
                self.logger.error(f"模板文件不存在: {template_path}")
                yield event.plain_result("生成列表失败: 模板文件不存在")
                return
            
            template = env.get_template(template_file)
            # 获取资源URL
            logo_url = os.path.join("file://", str(Path(self.plugin_dir) / "assets" / "logo.png")).replace("\\", "/")
            caution_line_url = os.path.join("file://",  str(Path(self.plugin_dir) / "assets" / "caution_line.png")).replace("\\", "/")
            plugin_name = "明日方舟语音插件"
            
            # 渲染HTML
            html = template.render(
                voice_types=voice_types,
                operators=operators,
                skin_operators=skin_operators,
                logo_url=logo_url,
                plugin_name=plugin_name,
                caution_line_url=caution_line_url
            )
            
            # 保存HTML (用于调试)
            html_path = os.path.join(self.plugin_dir, 'list.html')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html)
            
            self.logger.info(f"HTML已保存到: {html_path}")
            
            # 直接使用PIL生成图片
            try:
                self.logger.info("开始使用PIL生成图片...")
                
                # 设置图片基本参数
                img_width = 800
                padding = 30
                line_height = 40
                
                # 计算所需高度 (标题 + 语音类型部分 + 干员列表 + 皮肤列表)
                title_height = 120  # 标题和页眉的空间
                
                # 语音类型部分高度
                voice_types_rows = (len(voice_types) + 2) // 3  # 每行3个，向上取整
                voice_types_height = voice_types_rows * 40 + 60  # 每行40像素高度 + 标题和边距
                
                # 干员列表部分高度 - 每行1个干员
                ops_height = 0
                if operators:
                    ops_height = len(operators) * line_height + 60  # 每个干员40像素高度 + 标题和边距
                
                # 如果有皮肤角色，添加皮肤部分高度
                skin_ops_height = 0
                if skin_operators:
                    skin_ops_height = len(skin_operators) * line_height + 60  # 每个皮肤角色40像素高度 + 标题和边距
                
                # 为底部信息增加额外空间
                footer_height = 80  # 为Powered by文本增加更多空间
                
                # 计算总高度
                total_height = title_height + voice_types_height + ops_height + skin_ops_height + footer_height
                
                # 确保最小高度
                min_height = 800
                if total_height < min_height:
                    total_height = min_height
                
                # 额外增加一些边距，防止内容被截断
                total_height += 50
                
                self.logger.info(f"计算图片高度: 标题={title_height}, 语音类型={voice_types_height}, 干员列表={ops_height}, 皮肤列表={skin_ops_height}, 底部={footer_height}, 总计={total_height}")
                
                # 创建图片
                img = PILImage.new('RGB', (img_width, total_height), color=(30, 33, 41))
                draw = ImageDraw.Draw(img)
                
                # 尝试加载字体，如果失败使用默认字体
                try:
                    title_font = ImageFont.truetype("simhei.ttf", 28)
                    section_font = ImageFont.truetype("simhei.ttf", 22)
                    normal_font = ImageFont.truetype("simhei.ttf", 18)
                except:
                    self.logger.warning("无法加载TrueType字体，使用默认字体")
                    title_font = ImageFont.load_default()
                    section_font = title_font
                    normal_font = title_font
                
                y = padding
                
                # 绘制标题
                title_text = "明日方舟语音列表"
                draw.text((img_width//2, y), title_text, fill=(255, 204, 0), font=title_font, anchor="mt")
                y += 60
                
                # 绘制语音类型部分
                draw.text((padding, y), "语音类型总览", fill=(255, 204, 0), font=section_font)
                y += 40
                
                # 绘制语音类型区域背景
                voice_bg_height = voice_types_rows * 40 + 20
                draw.rectangle([(padding, y), (img_width-padding, y+voice_bg_height)], fill=(40, 42, 46))
                draw.line([(padding, y), (img_width-padding, y)], fill=(255, 204, 0), width=3)
                
                # 绘制语音类型网格
                col_width = (img_width - padding*2) // 3
                for i, vt in enumerate(voice_types):
                    col = i % 3
                    row = i // 3
                    x = padding + col * col_width + col_width // 2
                    text_y = y + 20 + row * 40
                    draw.text((x, text_y), vt, fill=(255, 255, 255), font=normal_font, anchor="mm")
                
                y += voice_bg_height + 40
                
                # 创建默认头像
                default_avatar = PILImage.new('RGBA', (32, 32), (60, 63, 65))
                draw_avatar = ImageDraw.Draw(default_avatar)
                draw_avatar.text((16, 16), "?", fill=(255, 204, 0), font=normal_font, anchor="mm")
                
                # 缓存已下载的头像
                avatar_cache = {}
                
                # 绘制已下载干员部分
                if operators:
                    draw.text((padding, y), "已下载干员及语音语言", fill=(255, 204, 0), font=section_font)
                    y += 40
                    
                    # 绘制干员区域背景
                    ops_bg_height = len(operators) * line_height + 20
                    draw.rectangle([(padding, y), (img_width-padding, y+ops_bg_height)], fill=(40, 42, 46))
                    
                    # 绘制干员列表 - 每行1个干员
                    for i, op in enumerate(operators):
                        text_y = y + line_height//2 + i * line_height
                        
                        # 绘制背景线条增强可读性（隔行变色）
                        if i % 2 == 0:
                            draw.rectangle([(padding+2, text_y-line_height//2+2), 
                                          (img_width-padding-2, text_y+line_height//2-2)], 
                                          fill=(45, 48, 56))
                        
                        # 获取头像
                        avatar_img = default_avatar
                        
                        try:
                            # 尝试从缓存获取头像
                            if op['name'] in avatar_cache:
                                avatar_img = avatar_cache[op['name']]
                            else:
                                # 首先检查本地assets目录中是否有头像文件
                                local_avatar_path = self.assets_dir / f"{op['name']}.png"
                                if local_avatar_path.exists():
                                    try:
                                        avatar_img = PILImage.open(str(local_avatar_path)).resize((32, 32))
                                        # 缓存头像
                                        avatar_cache[op['name']] = avatar_img
                                        self.logger.info(f"使用本地头像: {op['name']}")
                                    except Exception as e:
                                        self.logger.warning(f"加载本地头像失败: {op['name']} - {str(e)}")
                                else:
                                    # 本地没有，从网络获取
                                    # 获取头像URL
                                    avatar_url = await self.fetch_operator_avatar(op['name'])
                                    self.logger.info(f"成功获取头像: {op['name']}")
                        except Exception as e:
                            self.logger.warning(f"获取头像URL失败: {op['name']} - {str(e)}")
                        
                        # 绘制头像
                        avatar_x = padding + 10
                        avatar_y = text_y - 16
                        
                        # 绘制黄色边框
                        draw.rectangle([(avatar_x-1, avatar_y-1), (avatar_x+33, avatar_y+33)], 
                                     outline=(255, 204, 0), width=1)
                        
                        # 粘贴头像
                        try:
                            img.paste(avatar_img, (avatar_x, avatar_y), 
                                     avatar_img if avatar_img.mode == 'RGBA' else None)
                        except Exception as e:
                            self.logger.warning(f"粘贴头像失败: {op['name']} - {str(e)}")
                        
                        # 干员名称
                        name_x = padding + 60
                        draw.text((name_x, text_y), op['name'], fill=(255, 204, 0), font=normal_font, anchor="lm")
                        
                        # 语言标签 - 增加起始位置，避免与名称重叠
                        lang_x = padding + 250  # 从180增加到250
                        
                        # 只显示普通语言标签，不显示skin标签，skin标签将在皮肤部分显示
                        for lang in op['languages']:
                            if lang['code'] == 'skin':
                                continue
                                
                            lang_text = lang['display']
                            lang_width = normal_font.getbbox(lang_text)[2] + 20
                            
                            # 语言标签背景
                            tag_color = (46, 58, 77)  # 默认蓝色
                            text_color = (126, 207, 255)  # 默认蓝色文字
                            
                            if lang['code'] == 'cn':
                                tag_color = (45, 58, 45)
                                text_color = (182, 255, 126)
                            elif lang['code'] == 'fy':
                                tag_color = (58, 45, 45)
                                text_color = (255, 182, 126)
                            draw.rectangle([(lang_x, text_y-15), (lang_x+lang_width, text_y+15)], 
                                         fill=tag_color, outline=text_color, width=1)
                            draw.text((lang_x+lang_width//2, text_y), lang_text, 
                                    fill=text_color, font=normal_font, anchor="mm")
                            
                            lang_x += lang_width + 10
                    
                    y += ops_bg_height + 40  # 更新y坐标，移至下一部分
                    
                # 绘制皮肤角色部分
                if skin_operators:
                    draw.text((padding, y), "皮肤语音", fill=(255, 204, 0), font=section_font)
                    y += 40
                    
                    # 绘制皮肤区域背景
                    skin_ops_bg_height = len(skin_operators) * line_height + 20
                    draw.rectangle([(padding, y), (img_width-padding, y+skin_ops_bg_height)], fill=(40, 42, 46))
                    
                    # 绘制皮肤角色列表
                    for i, op in enumerate(skin_operators):
                        text_y = y + line_height//2 + i * line_height
                        
                        # 绘制背景线条增强可读性（皮肤部分使用金色调）
                        if i % 2 == 0:
                            draw.rectangle([(padding+2, text_y-line_height//2+2), 
                                         (img_width-padding-2, text_y+line_height//2-2)], 
                                         fill=(58, 49, 36))  # 深金色背景
                        
                        # 获取头像
                        avatar_img = default_avatar
                        
                        try:
                            # 尝试使用基础角色名获取头像
                            base_name = op['name'].replace("皮肤", "")
                            if base_name in avatar_cache:
                                avatar_img = avatar_cache[base_name]
                            else:
                                # 首先检查本地assets目录中是否有头像文件
                                local_avatar_path = self.assets_dir / f"{base_name}.png"
                                if local_avatar_path.exists():
                                    try:
                                        avatar_img = PILImage.open(str(local_avatar_path)).resize((32, 32))
                                        # 缓存头像
                                        avatar_cache[base_name] = avatar_img
                                    except Exception as e:
                                        self.logger.warning(f"加载皮肤角色头像失败: {base_name} - {str(e)}")
                        except Exception as e:
                            self.logger.warning(f"获取皮肤角色头像URL失败: {op['name']} - {str(e)}")
                        
                        # 绘制头像
                        avatar_x = padding + 10
                        avatar_y = text_y - 16
                        
                        # 绘制金色边框
                        draw.rectangle([(avatar_x-1, avatar_y-1), (avatar_x+33, avatar_y+33)], 
                                     outline=(255, 204, 0), width=2)  # 皮肤角色使用更粗的边框
                        
                        # 粘贴头像
                        try:
                            img.paste(avatar_img, (avatar_x, avatar_y), 
                                     avatar_img if avatar_img.mode == 'RGBA' else None)
                        except Exception as e:
                            self.logger.warning(f"粘贴皮肤角色头像失败: {op['name']} - {str(e)}")
                        
                        # 干员名称 - 皮肤角色使用金色调
                        name_x = padding + 60
                        draw.text((name_x, text_y), op['name'], fill=(255, 204, 0), font=normal_font, anchor="lm")
                        
                        # 语言标签 - 皮肤部分
                        lang_x = padding + 250
                        
                        for lang in op['languages']:
                            lang_text = lang['display']
                            lang_width = normal_font.getbbox(lang_text)[2] + 20
                            
                            # 皮肤语言使用金色调
                            draw.rectangle([(lang_x, text_y-15), (lang_x+lang_width, text_y+15)], 
                                         fill=(58, 49, 31), outline=(255, 204, 0), width=1)
                            draw.text((lang_x+lang_width//2, text_y), lang_text, 
                                    fill=(255, 204, 0), font=normal_font, anchor="mm")
                            
                            lang_x += lang_width + 10
                    
                    y += skin_ops_bg_height + 20  # 更新y坐标，移至底部部分
                
                # 添加底部装饰线
                draw.line([(padding, y+10), (img_width-padding, y+10)], fill=(255, 204, 0), width=2)
                draw.text((img_width//2, y+25), "明日方舟语音插件", fill=(126, 207, 255), font=normal_font, anchor="mm")
                
                # 添加Powered by文本
                try:
                    small_font = ImageFont.truetype("simhei.ttf", 16)  # 小一号的字体
                except:
                    small_font = normal_font
                # 将Powered by文本放在底部装饰线下方足够距离的位置
                powered_by_y = y + 45
                draw.text((img_width//2, powered_by_y), "Powered by astrbot_plugin_mrfz", 
                         fill=(160, 160, 160), font=small_font, anchor="mm")  # 调亮灰色以增加可见度
                
                # 在Powered by文本下方增加一条细线作为装饰
                thin_line_y = powered_by_y + 20
                draw.line([(img_width//2 - 120, thin_line_y), (img_width//2 + 120, thin_line_y)], 
                         fill=(100, 100, 100), width=1)  # 灰色细线
                
                # 保存图片
                output_path = os.path.join(self.plugin_dir, 'list.png')
                img.save(output_path)
                self.logger.info(f"图片生成完成，保存到: {output_path}")
                
                # 发送图片
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    yield event.image_result(output_path)
                else:
                    self.logger.error(f"图片文件未生成或大小为0: {output_path}")
                    raise FileNotFoundError("图片文件未生成")
                    
            except Exception as e:
                self.logger.error(f"生成图片时出错: {str(e)}")
                
                # 发送文本格式的干员列表作为备选
                text_result = "【明日方舟语音列表】\n\n"
                
                # 语音类型
                text_result += "■ 语音类型：\n"
                text_result += ", ".join(voice_types[:5]) + "...\n\n"
                
                # 干员列表
                text_result += "■ 已下载干员：\n"
                for op in operators:
                    # 获取该干员的语音数量
                    voice_counts = {}
                    for lang in op["languages"]:
                        lang_code = lang["code"]
                        if lang_code != "skin" and op['name'] in self.voice_index and lang_code in self.voice_index[op['name']]:
                            voice_counts[lang_code] = len(self.voice_index[op['name']][lang_code])
                    
                    # 构建语言信息，包含语音数量
                    lang_info = []
                    for lang in op["languages"]:
                        lang_code = lang["code"]
                        if lang_code == "skin":
                            continue  # 皮肤语音会单独显示
                        if lang_code in voice_counts:
                            lang_info.append(f"{lang['display']}({voice_counts[lang_code]})")
                        else:
                            lang_info.append(lang["display"])
                    
                    text_result += f"- {op['name']}（{', '.join(lang_info)}）\n"
                
                # 皮肤列表
                if skin_operators:
                    text_result += "\n■ 皮肤语音：\n"
                    for op in skin_operators:
                        text_result += f"- {op['name']}（{', '.join([lang['display'] for lang in op['languages']])}）\n"
                
                yield event.plain_result(text_result)
                
        except Exception as e:
            self.logger.error(f"生成语音列表图片时出错: {str(e)}")
            yield event.plain_result(f"生成语音列表图片时出错：{str(e)}")

    @filter.command("mrfz_fetch")
    async def mrfz_fetch_handler(self, event: AstrMessageEvent, character: str):
        """获取干员语音文件"""
        try:
            yield event.plain_result(f"开始获取干员 {character} 的语音文件，请稍候...")
            
            # 调用获取语音函数
            success, result = await self.fetch_character_voices(character)
            if success:
                # 成功下载后，重新扫描语音文件
                self.scan_voice_files()
                yield event.plain_result(f"获取成功: {result}")
            else:
                # 简化错误提示
                yield event.plain_result(f"获取失败: {result}")

        except Exception as e:
            self.logger.error(f"获取语音文件时出错: {str(e)}", exc_info=True)
            yield event.plain_result(f"获取语音文件时出错：{str(e)}")

    async def send_voice_message(self, event: AstrMessageEvent, voice_file_path: str):
        """发送语音消息"""
        try:
            chain = [Record.fromFileSystem(voice_file_path)]
            yield event.chain_result(chain)
        except Exception as e:
            yield event.plain_result(f"发送语音消息时出错：{str(e)}")
            
    async def _fetch_all_avatar_mappings(self) -> Dict[str, str]:
        """
        直接通过PRTS文件页面获取干员头像

        Returns:
            Dict[str, str]: 干员名称到头像URL的映射字典
        """
        avatar_mappings = {}
        try:
            # 收集所有已知的干员名称
            operator_names = set()
            if self.voice_index:
                for op_name in self.voice_index.keys():
                    operator_names.add(op_name)
            self.logger.info(f"找到{len(operator_names)}个干员名称")
            
            # 根据干员名称获取头像URL
            self.logger.info(f"开始使用文件页面格式获取干员头像...")
            # 创建所有干员头像获取任务
            tasks = []
            for op_name in operator_names:
                tasks.append(self.fetch_operator_avatar(op_name))
            
            # 并发执行所有任务
            results = await asyncio.gather(*tasks)
            
            # 记录未找到头像的干员数量
            not_found_count = 0
            
            # 处理结果
            for op_name,image_url in results:
                if image_url:
                    avatar_mappings[op_name] = image_url
                    if "头像_无头像.png" in image_url:
                        not_found_count += 1
            
            self.logger.info(f"仍有{not_found_count}个干员未找到头像")
            self.logger.info(f"最终获取到{len(avatar_mappings)}个干员头像链接")
            return avatar_mappings
            
        except Exception as e:
            self.logger.error(f"获取头像映射时出错: {str(e)}")
            return {}
            