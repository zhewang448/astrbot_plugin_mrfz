import os
import json
import asyncio
import random
import re
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Union
import aiohttp
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
import logging
from PIL import Image
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.star import StarTools

# 常量定义
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Referer": "https://prts.wiki/"
}

# 确保模板目录存在
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')
if not os.path.exists(TEMPLATE_DIR):
    os.makedirs(TEMPLATE_DIR)

MRFZ_VOICE_LIST_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>明日方舟 语音类型与已下载干员</title>
    <style>
        body {
            background: linear-gradient(135deg, #232526 0%, #414345 100%);
            margin: 0;
            padding: 0;
            font-family: 'HarmonyOS Sans', '微软雅黑', Arial, sans-serif;
            min-height: 100vh;
        }
        .arknights-logo {
            width: 180px;
            margin: 32px auto 0 auto;
            display: block;
        }
        .plugin-badge {
            text-align: center;
            margin-top: 8px;
            margin-bottom: 18px;
            font-size: 1.12em;
            color: #7ecfff;
            letter-spacing: 2px;
            font-family: 'Roboto Mono', monospace;
            font-weight: bold;
            text-shadow: 0 1px 6px #000a;
            user-select: none;
        }
        .main-container {
            max-width: 1000px;
            margin: 24px auto 0 auto;
            padding-bottom: 32px;
        }
        .section-title {
            font-size: 1.4em;
            color: #ffcc00;
            font-weight: bold;
            letter-spacing: 2px;
            margin: 32px 0 16px 0;
            text-shadow: 1px 2px 8px #000, 0 0 2px #ffcc00;
        }
        .voice-type-grid {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 8px 0;
            background: rgba(40, 42, 46, 0.96);
            border-radius: 16px;
            box-shadow: 0 4px 16px 0 rgba(31, 38, 135, 0.18);
            padding: 18px 0 18px 0;
            margin-bottom: 32px;
            border-top: 3px solid #ffcc00;
        }
        .voice-type-item {
            color: #fff;
            font-size: 1em;
            text-align: center;
            padding: 8px 0;
            font-family: 'Roboto Mono', monospace;
            letter-spacing: 1px;
            transition: background 0.2s;
            border-bottom: 1px solid #4442;
        }
        .voice-type-item:hover {
            background: rgba(255, 204, 0, 0.08);
            color: #ffcc00;
        }
        .downloaded-list-block {
            background: rgba(40, 42, 46, 0.96);
            border-radius: 16px;
            box-shadow: 0 4px 16px 0 rgba(31, 38, 135, 0.18);
            padding: 24px 32px 18px 32px;
        }
        .downloaded-list {
            margin: 0;
            padding: 0;
            list-style: none;
        }
        .downloaded-list li {
            margin-bottom: 14px;
            font-size: 1.13em;
            color: #fff;
            display: flex;
            align-items: center;
        }
        .op-avatar {
            width: 32px;
            height: 32px;
            border-radius: 8px;
            margin-right: 10px;
            border: 2px solid #ffcc00;
            background: #222;
            object-fit: cover;
        }
        .op-name {
            font-weight: bold;
            color: #ffcc00;
            margin-right: 12px;
            font-size: 1.1em;
            letter-spacing: 1px;
        }
        .lang-tag {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 8px;
            font-size: 0.98em;
            font-family: 'Roboto Mono', monospace;
            margin-right: 6px;
            margin-left: 2px;
        }
        .lang-jp {
            background: #2e3a4d;
            color: #7ecfff;
            border: 1px solid #7ecfff;
        }
        .lang-cn {
            background: #2d3a2d;
            color: #b6ff7e;
            border: 1px solid #b6ff7e;
        }
        .lang-fy {
            background: #3a2d2d;
            color: #ffb67e;
            border: 1px solid #ffb67e;
        }
        .lang-skin {
            background: #3a2d3a;
            color: #ff7ecf;
            border: 1px solid #ff7ecf;
        }
        .caution-line {
            width: 100%;
            height: 24px;
            background: url('assets/caution_line.png') repeat-x center;
            margin: 40px 0 0 0;
            border-radius: 0 0 12px 12px;
            box-shadow: 0 2px 8px #0008;
            position: relative;
        }
    </style>
</head>
<body>
    <img class="arknights-logo" src="{{ logo_url }}" alt="明日方舟LOGO">
    <div class="plugin-badge">{{ plugin_name }}</div>
    <div class="main-container">
        <div class="section-title">语音类型总览</div>
        <div class="voice-type-grid">
            {% for vt in voice_types %}
            <div class="voice-type-item">{{ vt }}</div>
            {% endfor %}
        </div>
        <div class="section-title">已下载干员及语音语言</div>
        <div class="downloaded-list-block">
            <ul class="downloaded-list">
                {% for op in operators %}
                <li>
                    <img class="op-avatar" src="{{ op.avatar_url }}" alt="{{ op.name }}头像">
                    <span class="op-name">{{ op.name }}</span>
                    {% for lang in op.languages %}
                        <span class="lang-tag lang-{{ lang.code }}">{{ lang.display }}</span>
                    {% endfor %}
                </li>
                {% endfor %}
            </ul>
        </div>
    </div>
    <div class="caution-line"></div>
</body>
</html> 
'''

@register("astrbot_plugin_mrfz", "bushikq", "明日方舟角色语音插件", "2.0.0")
class MyPlugin(Star):
    # HTTP请求头
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://prts.wiki/"
    }

    # 语音描述列表
    VOICE_DESCRIPTIONS = [
        {"type": "任命助理", "code": "appointed"},
        {"type": "交谈1", "code": "talk1"},
        {"type": "交谈2", "code": "talk2"},
        {"type": "交谈3", "code": "talk3"},
        {"type": "晋升后交谈1", "code": "e1talk1"},
        {"type": "晋升后交谈2", "code": "e1talk2"},
        {"type": "信赖提升后交谈1", "code": "e2talk1"},
        {"type": "信赖提升后交谈2", "code": "e2talk2"},
        {"type": "信赖提升后交谈3", "code": "e2talk3"},
        {"type": "闲置", "code": "idle"},
        {"type": "干员报到", "code": "report"},
        {"type": "观看作战记录", "code": "watch_battle"},
        {"type": "精英化晋升1", "code": "e1"},
        {"type": "精英化晋升2", "code": "e2"},
        {"type": "编入队伍", "code": "join_team"},
        {"type": "任命队长", "code": "team_leader"},
        {"type": "行动出发", "code": "mission_start"},
        {"type": "行动开始", "code": "battle_start"},
        {"type": "选中干员1", "code": "select1"},
        {"type": "选中干员2", "code": "select2"},
        {"type": "部署1", "code": "deploy1"},
        {"type": "部署2", "code": "deploy2"},
        {"type": "作战中1", "code": "battle1"},
        {"type": "作战中2", "code": "battle2"},
        {"type": "作战中3", "code": "battle3"},
        {"type": "作战中4", "code": "battle4"},
        {"type": "完成高难行动", "code": "hard_win"},
        {"type": "3星结束行动", "code": "3starwin"},
        {"type": "非3星结束行动", "code": "fail"},
        {"type": "行动失败", "code": "mission_fail"},
        {"type": "进驻设施", "code": "enter"},
        {"type": "戳一下", "code": "poke"},
        {"type": "信赖触摸", "code": "trust"},
        {"type": "标题", "code": "title"},
        {"type": "新年祝福", "code": "newyear"},
        {"type": "问候", "code": "greet"},
        {"type": "生日", "code": "birthday"},
        {"type": "周年庆典", "code": "anniversary"}
    ]

    # 已知角色ID映射
    KNOWN_CHARACTER_IDS = {
        "阿": "char_225_haak",
        "赫默": "char_171_bldsk",
    }

    # 皮肤语音配置
    SKIN_VOICE_CONFIGS = {
        "维什戴尔皮肤": {
            "base_url": "https://torappu.prts.wiki/assets/audio/voice",
            "patterns": ["cn_{num:03d}.wav"]
        }
    }

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
            self.voice_index: Dict[str, Dict[str, List[str]]] = {}
            
            # 确保必要的目录存在
            for directory in [self.data_dir, self.voices_dir, self.assets_dir]:
                directory.mkdir(exist_ok=True)
                if not directory.exists():
                    raise RuntimeError(f"无法创建目录：{directory}")
            
            # 确保模板目录存在
            if not os.path.exists(TEMPLATE_DIR):
                os.makedirs(TEMPLATE_DIR)
            
            # 处理配置文件
            self._handle_config_schema()
            
            # 从配置中读取设置
            self.config = config
            self.auto_download = self.config.get("auto_download", True)
            self.auto_download_skin = self.config.get("auto_download_skin", True)
            self.language_list = ["fy", "cn", "jp"]  # 1:方言, 2:汉语, 3:日语
            self.default_language_rank = self.config.get("default_language_rank", "123")
            
            # 读取日志输出设置
            self.enable_log_output = self.config.get("enable_log_output", True)
            # 根据配置设置日志级别
            if not self.enable_log_output:
                self.logger.setLevel(logging.ERROR)  # 只输出错误信息
                self.logger.info("已禁用详细日志输出")
            
            # 扫描已有文件
            self.scan_voice_files()
            
            # 初始化头像缓存
            self._avatar_cache = {}
            self._all_avatars = {}
            
            # 初始化资源文件
            asyncio.create_task(self._async_init())
            
        except Exception as e:
            print(f"插件初始化失败: {e}")
            raise
            
    async def _async_init(self):
        """异步初始化函数"""
        try:
            # 扫描语音文件确保索引是最新的
            self.logger.info("初始化阶段扫描语音文件目录...")
            if self.voices_dir.exists():
                self.scan_voice_files()
                self.logger.info(f"语音文件扫描完成，发现{len(self.voice_index)}个角色")
            
            # 确保资源目录存在并下载必要资源
            await self.ensure_assets()
            
            # 预加载干员头像URL
            await self._preload_avatar_urls()
        except Exception as e:
            self.logger.error(f"异步初始化时出错: {str(e)}")
    
    async def _preload_avatar_urls(self):
        """预加载干员头像URL"""
        try:
            self.logger.info("开始预加载干员头像URL...")
            self._all_avatars = await self._fetch_all_avatar_mappings()
            self.logger.info(f"成功加载 {len(self._all_avatars)} 个干员头像URL")
        except Exception as e:
            self.logger.error(f"预加载干员头像URL时出错: {str(e)}")

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
            },
            "enable_log_output": {
                "description": "是否在终端输出详细日志信息",
                "type": "bool",
                "hint": "true/false",
                "default": True
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

        # 创建一个从语音描述到代码的映射
        desc_to_code = {desc['type']: desc['code'] for desc in self.VOICE_DESCRIPTIONS}

        for char_dir in self.voices_dir.iterdir():
            if not char_dir.is_dir():
                continue

            character = char_dir.name
            self.voice_index[character] = {}
            
            # 先收集该角色所有语言目录下的语音文件
            all_languages_files = {}
            
            # 处理普通语言目录
            for lang_dir in char_dir.iterdir():
                if not lang_dir.is_dir():
                    continue
                    
                # 如果是skin目录，特殊处理
                if lang_dir.name == "skin":
                    # 处理皮肤目录下的所有语言
                    skin_character = f"{character}皮肤"
                    if skin_character not in self.voice_index:
                        self.voice_index[skin_character] = {}
                    
                    for skin_lang_dir in lang_dir.iterdir():
                        if not skin_lang_dir.is_dir():
                            continue
                            
                        skin_language = skin_lang_dir.name
                        skin_voice_files = []
                        
                        # 收集该语言下的所有皮肤语音文件
                        for f in skin_lang_dir.iterdir():
                            if f.is_file() and f.suffix.lower() == '.wav':
                                # 从文件名中提取语音描述（去掉.wav扩展名）
                                description = f.name.replace('.wav', '')
                                skin_voice_files.append((f.name, description))
                        
                        # 添加到皮肤角色的索引中
                        if skin_language not in self.voice_index[skin_character]:
                            self.voice_index[skin_character][skin_language] = []
                            
                        # 为每个皮肤语音文件分配代码
                        for file_name, description in skin_voice_files:
                            code = desc_to_code.get(description, description.lower().replace(' ', '_'))
                            self.voice_index[skin_character][skin_language].append({
                                'code': code,
                                'file': file_name,
                                'description': description
                            })
                            
                        # 同时在基础角色下添加skin语言类型
                        if "skin" not in self.voice_index[character]:
                            self.voice_index[character]["skin"] = []
                            
                        # 将皮肤语音也添加到基础角色的skin语言下
                        for file_name, description in skin_voice_files:
                            code = desc_to_code.get(description, description.lower().replace(' ', '_'))
                            
                            # 检查是否已存在该文件
                            exists = False
                            for item in self.voice_index[character]["skin"]:
                                if isinstance(item, dict) and item.get('file') == file_name:
                                    exists = True
                                    break
                                    
                            if not exists:
                                self.voice_index[character]["skin"].append({
                                    'code': code,
                                    'file': file_name,
                                    'description': description,
                                    'language': skin_language  # 额外记录实际语言
                                })
                    
                    continue  # 跳过，不作为普通语言处理
                
                language = lang_dir.name
                voice_files = []
                
                # 收集该语言下的所有语音文件
                for f in lang_dir.iterdir():
                    if f.is_file() and f.suffix.lower() == '.wav':
                        # 从文件名中提取语音描述（去掉.wav扩展名）
                        description = f.name.replace('.wav', '')
                        voice_files.append((f.name, description))
                
                all_languages_files[language] = voice_files
                self.voice_index[character][language] = []
            
            # 为每个语音文件分配唯一的代码，保持一致性
            for language, files in all_languages_files.items():
                for file_name, description in files:
                    # 将语音描述转换为代码
                    code = desc_to_code.get(description, description.lower().replace(' ', '_'))
                    
                    # 添加到索引中（包含代码和文件名）
                    self.voice_index[character][language].append({
                        'code': code,
                        'file': file_name,
                        'description': description
                    })
        
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
            base_character = character.replace("皮肤", "")
            return self.voices_dir / base_character
        return self.voices_dir / character

    def _get_voice_files(self, character: str, language: str = "jp") -> List[Union[str, Dict]]:
        """获取指定角色和语言的语音文件列表
        
        返回语音文件信息列表，每项可能是字符串（兼容旧版本）或字典（包含code、file和description）
        """
        self.scan_voice_files()
        if character not in self.voice_index:
            return []
        if language not in self.voice_index[character]:
            return []
            
        # 检查索引项的类型，支持新旧两种格式
        if self.voice_index[character][language] and isinstance(self.voice_index[character][language][0], dict):
            # 新格式：返回完整的字典列表
            return sorted(self.voice_index[character][language], key=lambda x: x.get('code', ''))
        else:
            # 旧格式：返回文件名列表
            return sorted(self.voice_index[character][language])

    def _get_voice_path(self, character: str, voice_name_or_code: str, language: str = "jp") -> Optional[Path]:
        """获取语音文件的完整路径
        
        Args:
            character: 角色名称
            voice_name_or_code: 语音文件名或语音代码
            language: 语言代码
            
        Returns:
            语音文件路径，如果未找到则返回None
        """
        self.logger.info(f"尝试获取语音路径: 角色={character}, 语音={voice_name_or_code}, 语言={language}")
        
        # 检查是否是皮肤角色
        is_skin_character = character.endswith("皮肤")
        
        # 获取基础角色目录
        char_dir = self._get_character_dir(character)
        
        # 确定搜索路径和是否在皮肤目录下搜索
        if is_skin_character or language == "skin":
            # 对于皮肤角色或language=skin的情况，使用skin子目录
            base_path = char_dir / "skin"
            self.logger.info(f"使用皮肤语音路径: {base_path}")
            
            # 如果皮肤目录不存在，记录并返回None
            if not base_path.exists() or not base_path.is_dir():
                self.logger.warning(f"皮肤目录不存在: {base_path}")
                return None
                
            # 在皮肤目录下按优先级依次尝试不同语言
            for try_lang in ["cn", "jp", "fy"]:
                lang_dir = base_path / try_lang
                
                # 检查特定语言的目录是否存在
                if lang_dir.exists() and lang_dir.is_dir():
                    # 尝试直接匹配完整文件名
                    if voice_name_or_code.endswith('.wav'):
                        skin_path = lang_dir / voice_name_or_code
                    else:
                        skin_path = lang_dir / f"{voice_name_or_code}.wav"
                        
                    if skin_path.exists():
                        self.logger.info(f"找到皮肤语音(优先级顺序): {skin_path}")
                        return skin_path
            
            # 如果没有找到指定的语音，扫描所有皮肤语言目录查找任何可用的语音
            for lang_dir in base_path.iterdir():
                if lang_dir.is_dir():
                    for voice_file in lang_dir.iterdir():
                        if voice_file.is_file() and voice_file.suffix.lower() == '.wav':
                            # 直接匹配文件名(不含扩展名)或完整文件名
                            if voice_file.stem == voice_name_or_code or voice_file.name == voice_name_or_code:
                                self.logger.info(f"找到皮肤语音(通用搜索): {voice_file}")
                                return voice_file
                            
                            # 检查索引中的代码匹配
                            if character in self.voice_index:
                                lang_name = lang_dir.name
                                if lang_name in self.voice_index[character]:
                                    for voice_info in self.voice_index[character][lang_name]:
                                        if isinstance(voice_info, dict) and voice_info.get('code') == voice_name_or_code:
                                            self.logger.info(f"找到皮肤语音(索引代码匹配): {voice_file}")
                                            return voice_file
            
            # 如果找不到匹配的语音，随机选择一个
            if voice_name_or_code == "random":
                all_voices = []
                for lang_dir in base_path.iterdir():
                    if lang_dir.is_dir():
                        for voice_file in lang_dir.iterdir():
                            if voice_file.is_file() and voice_file.suffix.lower() == '.wav':
                                all_voices.append(voice_file)
                
                if all_voices:
                    random_voice = random.choice(all_voices)
                    self.logger.info(f"随机选择皮肤语音: {random_voice}")
                    return random_voice
            
            # 如果没有找到任何皮肤语音，返回None
            self.logger.warning(f"未找到任何皮肤语音: {character} - {voice_name_or_code}")
            return None
        
        # 处理普通语音
        base_path = char_dir
        
        # 首先尝试直接使用给定的名称（可能是完整文件名）
        if voice_name_or_code.endswith('.wav'):
            # 直接使用文件名
            path = base_path / language / voice_name_or_code
            if path.exists():
                self.logger.info(f"找到语音文件(直接匹配): {path}")
                return path
        
        # 检查是否有该角色的索引
        if character in self.voice_index and language in self.voice_index[character]:
            # 检查索引结构
            if self.voice_index[character][language] and isinstance(self.voice_index[character][language][0], dict):
                # 新索引结构：查找匹配的code或文件名
                for voice_info in self.voice_index[character][language]:
                    if voice_info['code'] == voice_name_or_code or voice_info['file'] == voice_name_or_code or voice_info['file'] == f"{voice_name_or_code}.wav":
                        path = base_path / language / voice_info['file']
                        if path.exists():
                            self.logger.info(f"找到语音文件(索引匹配): {path}")
                            return path
            else:
                # 旧索引结构：直接尝试文件名
                if voice_name_or_code in self.voice_index[character][language]:
                    path = base_path / language / voice_name_or_code
                    if path.exists():
                        self.logger.info(f"找到语音文件(旧索引匹配): {path}")
                        return path
                
                # 尝试添加.wav后缀
                wav_name = f"{voice_name_or_code}.wav"
                if wav_name in self.voice_index[character][language]:
                    path = base_path / language / wav_name
                    if path.exists():
                        self.logger.info(f"找到语音文件(添加wav后缀): {path}")
                        return path
        
        # 如果指定语言没有找到，查找所有语言目录
        for lang_dir in base_path.iterdir():
            if not lang_dir.is_dir():
                continue
                
            # 尝试直接匹配文件名
            path = lang_dir / voice_name_or_code
            if not path.suffix:
                path = lang_dir / f"{voice_name_or_code}.wav"
                
            if path.exists():
                self.logger.info(f"找到语音文件(其他语言): {path}")
                return path
                
            # 尝试从该语言的索引中查找
            other_lang = lang_dir.name
            if character in self.voice_index and other_lang in self.voice_index[character]:
                if self.voice_index[character][other_lang] and isinstance(self.voice_index[character][other_lang][0], dict):
                    # 新索引结构
                    for voice_info in self.voice_index[character][other_lang]:
                        if voice_info['code'] == voice_name_or_code:
                            path = lang_dir / voice_info['file']
                            if path.exists():
                                self.logger.info(f"找到语音文件(其他语言索引): {path}")
                                return path
        
        self.logger.warning(f"未找到语音文件: {character} - {voice_name_or_code} - {language}")
        return None

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
                
                # 获取语音代码
                desc_to_code = {desc['type']: desc['code'] for desc in self.VOICE_DESCRIPTIONS}
                code = desc_to_code.get(description, description.lower().replace(' ', '_'))
                
                # 检查是否已在索引中
                exists = False
                for item in self.voice_index[character][language]:
                    if isinstance(item, dict) and item.get('file') == filename:
                        exists = True
                        break
                    elif isinstance(item, str) and item == filename:
                        # 将旧格式转换为新格式
                        self.voice_index[character][language].remove(item)
                        exists = False
                        break
                
                if not exists:
                    # 使用字典格式添加
                    self.voice_index[character][language].append({
                        'code': code,
                        'file': filename,
                        'description': description
                    })
                    # 保存更新后的索引
                    self._save_voice_index()
                
                return True, f"文件已存在: {filename}"
            
            # 下载文件（带重试）
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://prts.wiki/"
            }
            
            while retry_count < MAX_RETRIES:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(voice_url, headers=headers) as response:
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
                                self.voice_index[character] = {}
                            if language not in self.voice_index[character]:
                                self.voice_index[character][language] = []
                            if filename not in self.voice_index[character][language]:
                                self.voice_index[character][language].append(filename)
                            
                            # 保存更新后的索引
                            self._save_voice_index()
                            
                            return True, filename
                            
                except aiohttp.ClientError as e:
                    retry_count += 1
                    if retry_count == MAX_RETRIES:
                        return False, f"网络错误: {str(e)}"
                    await asyncio.sleep(1)  # 重试前等待
                    
        except IOError as e:
            return False, f"文件系统错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"
            
        return False, "下载失败: 超过最大重试次数"

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

    async def fetch_character_voices(self, character: str, force_download: bool = False) -> Tuple[bool, str]:
        """获取角色语音
        
        Args:
            character: 角色名称
            force_download: 是否强制下载，当为True时忽略已有文件检查
        
        Returns:
            (是否成功, 结果消息)
        """
        try:
            # 获取基础角色名（去掉"皮肤"后缀）
            base_character = character.replace("皮肤", "")
            is_skin = character.endswith("皮肤")
            
            # 先确保语音索引是最新的
            self.scan_voice_files()
            
            # 预先检查是否已有足够的语音文件，但如果指定强制下载则跳过检查
            if not force_download and character in self.voice_index:
                # 计算已有的语音文件数量
                total_existing = 0
                langs_with_files = []
                
                for lang, files in self.voice_index[character].items():
                    if files and len(files) > 5:  # 如果某个语言有超过5个文件，认为已经下载过了
                        total_existing += len(files)
                        langs_with_files.append(lang)
                
                if total_existing >= 10:  # 如果总共有10个以上的语音文件，认为不需要再下载
                    self.logger.info(f"角色 {character} 已有足够的语音文件({total_existing}个，语言: {langs_with_files})，无需下载")
                    result_msg = []
                    if is_skin:
                        result_msg.append(f"皮肤语音: {total_existing}个(已有)")
                    else:
                        result_msg.append(f"普通语音: {total_existing}个(已有)")
                    return True, "，".join(result_msg)
            
            self.logger.info(f"开始获取角色 {character} 的语音文件")
            
            # 获取角色ID
            char_id = await self.get_character_id(character)
            # 如果是皮肤且没找到合法皮肤ID，直接跳过
            if is_skin and not char_id:
                return False, f"{character} 没有专属皮肤语音"
            if not char_id:
                return False, f"无法获取角色 {character} 的ID"
            self.logger.info(f"获取到角色ID: {char_id}")
            
            # 同时下载干员头像
            assets_dir = self.data_dir / "assets"
            assets_dir.mkdir(exist_ok=True)
            avatar_file = assets_dir / f"{base_character}.png"
            
            if not avatar_file.exists():
                self.logger.info(f"尝试下载干员 {base_character} 的头像")
                # 获取头像URL
                avatar_url = await self.get_character_avatar_url(base_character)
                if avatar_url:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(avatar_url, headers=self.DEFAULT_HEADERS) as response:
                                if response.status == 200:
                                    with open(avatar_file, 'wb') as f:
                                        f.write(await response.read())
                                    self.logger.info(f"已下载头像: {avatar_file}")
                    except Exception as e:
                        self.logger.error(f"下载头像失败: {str(e)}")
                else:
                    self.logger.warning(f"未找到干员 {base_character} 的头像URL")
            
            # 也尝试从prts.wiki获取头像
            try:
                prts_avatar_url = f"https://prts.wiki/images/thumb/头像_{base_character}.png/120px-头像_{base_character}.png"
                prts_avatar_file = assets_dir / f"{base_character}_prts.png"
                
                if not prts_avatar_file.exists():
                    self.logger.info(f"尝试从PRTS Wiki获取干员 {base_character} 的头像")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(prts_avatar_url, headers=self.DEFAULT_HEADERS) as response:
                            if response.status == 200:
                                with open(prts_avatar_file, 'wb') as f:
                                    f.write(await response.read())
                                self.logger.info(f"已从PRTS Wiki下载头像: {prts_avatar_file}")
                                
                                # 如果主头像不存在，使用PRTS头像作为备份
                                if not avatar_file.exists():
                                    with open(avatar_file, 'wb') as f:
                                        with open(prts_avatar_file, 'rb') as src_f:
                                            f.write(src_f.read())
                                    self.logger.info(f"已将PRTS头像复制为主头像: {avatar_file}")
            except Exception as e:
                self.logger.warning(f"从PRTS Wiki获取头像失败: {str(e)}")
            
            # 构建语音配置 - 使用main1.py中的方式
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
            
            # 设置下载统计
            total_voices = 0
            failed_voices = 0
            skin_total_voices = 0
            skin_failed_voices = 0
            
            # 使用语音配置进行下载
            for lang, config in voice_configs.items():
                base_url = config["base_url"]
                found_pattern = None
                
                # 查找可用的模式
                for pattern in config["patterns"]:
                    test_url = f"{base_url}/{pattern.format(num=1)}"
                    try:
                        self.logger.info(f"尝试检查URL: {test_url}")
                        async with aiohttp.ClientSession() as session:
                            async with session.head(test_url, headers=self.DEFAULT_HEADERS) as response:
                                if response.status == 200:
                                    found_pattern = pattern
                                    self.logger.info(f"发现可用URL模式: {lang} - {test_url}")
                                    break
                                else:
                                    self.logger.info(f"URL不可用: {test_url}, 状态码: {response.status}")
                    except Exception as e:
                        self.logger.warning(f"检查URL异常: {test_url}, 错误: {str(e)}")
                        continue
                
                # 如果没有找到可用模式，尝试下一种语言
                if not found_pattern:
                    self.logger.info(f"未找到{lang}语音文件格式，尝试下一种语言")
                    continue
                
                # 开始下载该语言的语音
                self.logger.info(f"开始下载{lang}语音...")
                desc_idx = 0  # 当前描述索引
                file_idx = 1  # 当前文件索引
                
                # 遍历处理所有可能的语音文件
                while desc_idx < len(self.VOICE_DESCRIPTIONS) and file_idx <= 50:
                    # 获取当前描述
                    description = self.VOICE_DESCRIPTIONS[desc_idx]["type"] if isinstance(self.VOICE_DESCRIPTIONS[desc_idx], dict) else self.VOICE_DESCRIPTIONS[desc_idx]
                    
                    # 拼接文件名和URL
                    fname = found_pattern.format(num=file_idx)
                    voice_url = f"{base_url}/{fname}"
                    
                    try:
                        # 检查语音文件是否存在
                        async with aiohttp.ClientSession() as session:
                            async with session.head(voice_url, headers=self.DEFAULT_HEADERS) as response:
                                if response.status == 200:
                                    # 文件存在，尝试下载
                                    self.logger.info(f"正在下载: {lang}语音{file_idx} ({voice_url}) -> {description}.wav")
                                    success, message = await self.download_voice(character, voice_url, lang, description)
                                    if success:
                                        if is_skin:
                                            skin_total_voices += 1
                                        else:
                                            total_voices += 1
                                        desc_idx += 1  # 只有下载成功才移动到下一个描述
                                        self.logger.info(f"成功下载: {lang}语音{file_idx}")
                                    else:
                                        if is_skin:
                                            skin_failed_voices += 1
                                        else:
                                            failed_voices += 1
                                        self.logger.warning(f"下载失败 ({lang}语音{file_idx}): {message}")
                                else:
                                    self.logger.info(f"语音{file_idx}不存在，跳过")
                    except Exception as e:
                        self.logger.error(f"下载{lang}语音{file_idx}时出错: {str(e)}")
                        if is_skin:
                            skin_failed_voices += 1
                        else:
                            failed_voices += 1
                    
                    file_idx += 1  # 无论成功与否都尝试下一个文件
            
            # 如果不是皮肤且启用了自动下载皮肤语音，尝试下载皮肤语音
            if not is_skin and self.auto_download_skin:
                skin_character = f"{base_character}皮肤"
                
                # 检查是否已有皮肤语音
                skin_dir = self._get_character_dir(base_character) / "skin"
                if skin_dir.exists() and any(lang_dir.is_dir() and any(f.suffix.lower() == '.wav' for f in lang_dir.iterdir() if f.is_file()) 
                                          for lang_dir in skin_dir.iterdir() if lang_dir.is_dir()):
                    self.logger.info(f"{skin_character}已有语音文件，跳过下载")
                else:
                    self.logger.info(f"尝试下载{skin_character}的语音...")
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
                        self.logger.warning(f"下载{skin_character}的语音失败: {skin_result}")
            
            # 在下载完成后立即扫描更新索引
            if total_voices > 0 or skin_total_voices > 0:
                self.logger.info(f"下载完成，开始扫描语音文件更新索引...")
                self.scan_voice_files()
                self.logger.info(f"语音索引更新完成")
            
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
            self.logger.error(f"获取语音时出错: {str(e)}")
            return False, f"获取语音失败: {str(e)}"

    @filter.command("mrfz")
    async def mrfz_handler(self, event: AstrMessageEvent, character: str = None, voice_name: str = None, language: str = None):
        """/mrfz [角色名] [语音名] [jp/cn/fy/skin] 随机播放指定角色的语音。不指定语音名则随机播放。"""
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
            elif is_skin_character:
                # 如果是皮肤角色但未指定语言，默认使用skin
                lang = "skin"
                self.logger.info(f"用户请求皮肤角色 {character}，自动设置language=skin")
            elif character in self.SKIN_VOICE_CONFIGS:
                # 如果是已知的皮肤配置
                lang = "skin"
            else:
                # 根据配置选择语言优先级
                lang = self.language_list[0]  # 默认使用第一个语言
                for rank in self.default_language_rank:
                    try:
                        lang_index = int(rank) - 1  # 优先级从1开始，所以减1
                        if 0 <= lang_index < len(self.language_list):
                            potential_lang = self.language_list[lang_index]
                            # 检查该语言下是否有语音文件
                            if character in self.voice_index and potential_lang in self.voice_index[character] and self.voice_index[character][potential_lang]:
                                lang = potential_lang
                                break
                    except (ValueError, IndexError):
                        continue
            
            self.logger.info(f"确定使用语言: {lang}")
            
            # 尝试查找语音文件（多种方式）
            voice_files = []
            found_voice = False
            
            # 方式1：直接从索引中查找
            if character in self.voice_index:
                self.logger.info(f"角色 {character} 在语音索引中")
                
                # 检查指定语言
                if lang in self.voice_index[character] and self.voice_index[character][lang]:
                    self.logger.info(f"在 {lang} 语言下找到了{len(self.voice_index[character][lang])}个语音文件")
                    voice_files = self.voice_index[character][lang]
                    found_voice = True
                else:
                    # 尝试其他语言
                    self.logger.info(f"在 {lang} 语言下未找到语音，尝试其他语言")
                    for alt_lang, files in self.voice_index[character].items():
                        if files:
                            lang = alt_lang
                            voice_files = files
                            found_voice = True
                            self.logger.info(f"在 {alt_lang} 语言下找到了{len(files)}个语音文件")
                            break
            
            # 方式2：如果是皮肤角色，尝试在基础角色的skin目录下查找
            if not found_voice and is_skin_character:
                base_character = character.replace("皮肤", "")
                self.logger.info(f"尝试在基础角色 {base_character} 的skin目录下查找")
                
                if base_character in self.voice_index and "skin" in self.voice_index[base_character]:
                    voice_files = self.voice_index[base_character]["skin"]
                    lang = "skin"
                    found_voice = True
                    self.logger.info(f"在基础角色 {base_character} 的skin语言下找到了{len(voice_files)}个语音文件")
                else:
                    # 检查物理文件
                    char_dir = self._get_character_dir(base_character)
                    skin_dir = char_dir / "skin"
                    if skin_dir.exists() and skin_dir.is_dir():
                        self.logger.info(f"发现 {base_character} 有skin目录，检查内容")
                        has_files = False
                        
                        # 检查skin目录下是否有语音文件
                        for lang_dir in skin_dir.iterdir():
                            if lang_dir.is_dir() and any(f.suffix.lower() == '.wav' for f in lang_dir.iterdir() if f.is_file()):
                                has_files = True
                                break
                        
                        if has_files:
                            # 重新扫描
                            self.logger.info(f"发现皮肤目录有文件，重新扫描")
                            self.scan_voice_files()
                            
                            # 再次检查
                            if base_character in self.voice_index and "skin" in self.voice_index[base_character]:
                                voice_files = self.voice_index[base_character]["skin"]
                                lang = "skin"
                                found_voice = True
                                self.logger.info(f"扫描后在基础角色 {base_character} 的skin语言下找到了{len(voice_files)}个语音文件")
            
            # 方式3：检查物理文件但未索引的情况
            if not found_voice:
                char_dir = self._get_character_dir(character)
                if char_dir.exists():
                    self.logger.info(f"检查角色 {character} 的物理文件夹")
                    has_files = False
                    found_lang_dir = None
                    
                    # 检查各个语言目录
                    for lang_dir in char_dir.iterdir():
                        if lang_dir.is_dir():
                            if lang_dir.name == "skin" and (is_skin_character or lang == "skin"):
                                # 检查皮肤目录下的各个语言
                                for skin_lang_dir in lang_dir.iterdir():
                                    if skin_lang_dir.is_dir() and any(f.suffix.lower() == '.wav' for f in skin_lang_dir.iterdir() if f.is_file()):
                                        has_files = True
                                        found_lang_dir = f"skin/{skin_lang_dir.name}"
                                        break
                            elif any(f.suffix.lower() == '.wav' for f in lang_dir.iterdir() if f.is_file()):
                                has_files = True
                                found_lang_dir = lang_dir.name
                                break
                        
                        if has_files:
                            break
                    
                    if has_files:
                        self.logger.info(f"发现角色 {character} 有未索引的语音文件，在 {found_lang_dir} 目录")
                        # 重新扫描
                        self.scan_voice_files()
                        
                        # 再次检查索引
                        if character in self.voice_index:
                            # 先尝试找我们刚发现的语言目录
                            if found_lang_dir:
                                actual_lang = found_lang_dir.split("/")[-1] if "/" in found_lang_dir else found_lang_dir
                                if actual_lang in self.voice_index[character] and self.voice_index[character][actual_lang]:
                                    voice_files = self.voice_index[character][actual_lang]
                                    lang = actual_lang
                                    found_voice = True
                                    self.logger.info(f"扫描后在 {actual_lang} 语言下找到了{len(voice_files)}个语音文件")
                            
                            # 如果上面没找到，尝试任何语言
                            if not found_voice:
                                for try_lang, files in self.voice_index[character].items():
                                    if files:
                                        lang = try_lang
                                        voice_files = files
                                        found_voice = True
                                        self.logger.info(f"扫描后在 {try_lang} 语言下找到了{len(files)}个语音文件")
                                        break
            
            # 方式4：直接尝试获取一个随机语音（皮肤特殊处理）
            if not found_voice and is_skin_character:
                self.logger.info("尝试直接获取随机皮肤语音文件")
                voice_path = self._get_voice_path(character, "random", "skin")
                if voice_path:
                    self.logger.info(f"成功获取随机皮肤语音文件: {voice_path}")
                    yield event.plain_result(f"正在播放 {character} 的皮肤语音")
                    async for msg in self.send_voice_message(event, str(voice_path)):
                        yield msg
                    return
                else:
                    # 尝试基础角色的皮肤目录
                    base_character = character.replace("皮肤", "")
                    voice_path = self._get_voice_path(base_character, "random", "skin")
                    if voice_path:
                        self.logger.info(f"成功获取基础角色的随机皮肤语音文件: {voice_path}")
                        yield event.plain_result(f"正在播放 {character} 的皮肤语音")
                        async for msg in self.send_voice_message(event, str(voice_path)):
                            yield msg
                        return
            
            # 如果上述所有方法都找不到语音文件，则尝试下载
            if not found_voice:
                if not self.auto_download:
                    yield event.plain_result(f"未找到角色 {character} 的语音文件。自动下载已禁用，请使用 /mrfz_fetch 手动获取。")
                    return
                
                self.logger.info(f"未找到角色 {character} 的语音文件，尝试自动下载")
                yield event.plain_result(f"未找到角色 {character} 的语音文件，正在自动获取...")
                
                # 尝试下载
                fetch_success, fetch_msg = await self.fetch_character_voices(character)
                if not fetch_success:
                    yield event.plain_result(f"自动获取失败: {fetch_msg}")
                    return
                
                # 下载后重新扫描
                self.scan_voice_files()
                
                # 再次检查索引
                if character in self.voice_index:
                    if lang in self.voice_index[character] and self.voice_index[character][lang]:
                        voice_files = self.voice_index[character][lang]
                        found_voice = True
                        self.logger.info(f"下载后在 {lang} 语言下找到了{len(voice_files)}个语音文件")
                    else:
                        # 尝试其他语言
                        for try_lang, files in self.voice_index[character].items():
                            if files:
                                lang = try_lang
                                voice_files = files
                                found_voice = True
                                self.logger.info(f"下载后在 {try_lang} 语言下找到了{len(files)}个语音文件")
                                break
                
                # 如果仍然找不到，皮肤最后一次尝试
                if not found_voice and is_skin_character:
                    voice_path = self._get_voice_path(character, "random", "skin")
                    if voice_path:
                        self.logger.info(f"下载后找到皮肤语音文件: {voice_path}")
                        yield event.plain_result(f"正在播放 {character} 的皮肤语音")
                        async for msg in self.send_voice_message(event, str(voice_path)):
                            yield msg
                        return
                    
                    # 尝试基础角色的皮肤目录
                    base_character = character.replace("皮肤", "")
                    voice_path = self._get_voice_path(base_character, "random", "skin")
                    if voice_path:
                        self.logger.info(f"下载后找到基础角色的皮肤语音文件: {voice_path}")
                        yield event.plain_result(f"正在播放 {character} 的皮肤语音")
                        async for msg in self.send_voice_message(event, str(voice_path)):
                            yield msg
                        return
                
                # 如果仍未找到
                if not found_voice:
                    yield event.plain_result(f"获取后仍未找到角色 {character} 的语音文件")
                    return
                else:
                    yield event.plain_result(f"成功获取角色 {character} 的语音文件")
            
            # 到这里，我们已经找到了语音文件列表，处理语音选择
            if not voice_name:
                # 随机选择一个语音
                selected_voice = random.choice(voice_files)
                
                # 根据索引格式处理
                if isinstance(selected_voice, dict):
                    voice_name = selected_voice['file']
                    voice_description = selected_voice['description']
                else:
                    voice_name = selected_voice
                    voice_description = voice_name.replace('.wav', '')
            else:
                # 用户指定了语音名
                if not voice_name.endswith('.wav'):
                    voice_name = f"{voice_name}.wav"
                
                # 检查指定的语音是否存在
                voice_found = False
                voice_description = voice_name.replace('.wav', '')
                
                # 检查是否在列表中
                for vf in voice_files:
                    if isinstance(vf, dict):
                        if vf['file'] == voice_name or vf['code'] == voice_name.replace('.wav', ''):
                            voice_name = vf['file']
                            voice_description = vf['description']
                            voice_found = True
                            break
                    elif vf == voice_name:
                        voice_found = True
                        break
                
                if not voice_found:
                    yield event.plain_result(f"未找到语音：{voice_name}")
                    return
            
            # 播放语音
            yield event.plain_result(f"正在播放 {character} 的语音：{voice_description}")
            
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

            # 1. 语音类型
            voice_types = [desc['type'] for desc in self.VOICE_DESCRIPTIONS]

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
                                          "us": "美语", "kr": "韩语", "it": "意语"}.get(lang_code, lang_code)
                            skin_character["languages"].append({"code": lang_code, "display": display_name})
                    
                    # 获取基础角色名，用于头像
                    base_name = char.replace("皮肤", "")
                    skin_character["avatar_url"] = await self.get_avatar_url(base_name)
                    skin_operators.append(skin_character)
                    continue
                
                langs = []
                has_skin = False
                
                # 收集普通语言
                for lang_code, display_name in [
                    ("jp", "日语"), ("cn", "中文"), ("fy", "方言"),
                    ("us", "美语"), ("kr", "韩语"), ("it", "意语")
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
                avatar_url = await self.get_avatar_url(char)
                
                operators.append({
                    "name": char,
                    "languages": langs,
                    "avatar_url": avatar_url,
                    "has_skin": has_skin
                })
            
            # 3. 渲染HTML
            template_file = "mrfz_voice_list_template.html"
            # 创建Jinja2环境
            TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')
            if not os.path.exists(TEMPLATE_DIR):
                os.makedirs(TEMPLATE_DIR)
            
            env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
            
            # 确保模板文件存在
            if not os.path.exists(os.path.join(TEMPLATE_DIR, template_file)):
                with open(os.path.join(TEMPLATE_DIR, template_file), 'w', encoding='utf-8') as f:
                    f.write(MRFZ_VOICE_LIST_TEMPLATE)
            
            template = env.get_template(template_file)
            
            # 获取资源URL
            logo_url = os.path.join("file://", str(self.data_dir / "assets" / "logo.png")).replace("\\", "/")
            caution_line_url = os.path.join("file://", str(self.data_dir / "assets" / "caution_line.png")).replace("\\", "/")
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
            
            # 使用插件目录而不是临时目录
            plugin_dir = os.path.dirname(__file__)
            
            # 保存HTML (用于调试)
            html_path = os.path.join(plugin_dir, 'list.html')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html)
            
            self.logger.info(f"HTML已保存到: {html_path}")
            
            # 直接使用PIL生成图片
            try:
                from PIL import Image, ImageDraw, ImageFont
                import textwrap
                import io
                import requests
                
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
                img = Image.new('RGB', (img_width, total_height), color=(30, 33, 41))
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
                default_avatar = Image.new('RGBA', (32, 32), (60, 63, 65))
                draw_avatar = ImageDraw.Draw(default_avatar)
                draw_avatar.text((16, 16), "?", fill=(255, 204, 0), font=normal_font, anchor="mm")
                
                # 缓存已下载的头像
                avatar_cache = {}
                
                # 获取角色ID映射
                name_to_id, _ = await self.get_char_id_map()
                self.logger.info(f"获取到 {len(name_to_id)} 个干员ID映射")
                
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
                                        avatar_img = Image.open(str(local_avatar_path)).resize((32, 32))
                                        # 缓存头像
                                        avatar_cache[op['name']] = avatar_img
                                        self.logger.info(f"使用本地头像: {op['name']}")
                                    except Exception as e:
                                        self.logger.warning(f"加载本地头像失败: {op['name']} - {str(e)}")
                                else:
                                    # 本地没有，从网络获取
                                    # 获取头像URL
                                    avatar_url = await self.get_avatar_url(op['name'])
                                    self.logger.info(f"头像URL: {avatar_url}")
                                    
                                    # 下载头像
                                    try:
                                        response = requests.get(avatar_url, timeout=3)
                                        if response.status_code == 200:
                                            # 成功获取头像
                                            avatar_img = Image.open(io.BytesIO(response.content)).resize((32, 32))
                                            # 缓存头像
                                            avatar_cache[op['name']] = avatar_img
                                            self.logger.info(f"成功获取头像: {op['name']}")
                                    except Exception as e:
                                        self.logger.warning(f"处理头像失败: {op['name']} - {str(e)}")
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
                                        avatar_img = Image.open(str(local_avatar_path)).resize((32, 32))
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
                output_path = os.path.join(plugin_dir, 'list.png')
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

    @filter.command("mrfz_avatar")
    async def mrfz_avatar_handler(self, event: AstrMessageEvent, character: str):
        """获取干员头像"""
        try:
            self.logger.info(f"尝试获取干员 {character} 的头像")
            
            # 获取资源文件夹路径
            assets_dir = self.data_dir / "assets"
            assets_dir.mkdir(exist_ok=True)
            
            # 检查本地是否已有头像
            avatar_file = assets_dir / f"{character}.png"
            
            if avatar_file.exists():
                self.logger.info(f"找到本地头像文件: {avatar_file}")
                # 发送本地头像
                chain = [Image.fromFileSystem(str(avatar_file))]
                yield event.chain_result(chain)
                return
            
            # 获取头像URL
            avatar_map = await self._fetch_all_avatar_mappings()
            
            if character in avatar_map:
                avatar_url = avatar_map[character]
                self.logger.info(f"找到干员 {character} 的头像URL: {avatar_url}")
                
                # 下载头像
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(avatar_url, headers=self.DEFAULT_HEADERS) as response:
                            if response.status == 200:
                                # 保存头像
                                with open(avatar_file, 'wb') as f:
                                    f.write(await response.read())
                                self.logger.info(f"干员 {character} 的头像下载成功")
                                
                                # 发送头像
                                chain = [Image.fromFileSystem(str(avatar_file))]
                                yield event.chain_result(chain)
                                return
                            else:
                                self.logger.warning(f"下载头像失败，状态码: {response.status}")
                except Exception as e:
                    self.logger.error(f"下载头像时出错: {str(e)}")
            
            # 如果上面的方法都失败了，尝试使用标准URL格式
            standard_url = f"https://prts.wiki/images/thumb/头像_{character}.png/113px-头像_{character}.png"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(standard_url, headers=self.DEFAULT_HEADERS) as response:
                        if response.status == 200:
                            # 保存头像
                            with open(avatar_file, 'wb') as f:
                                f.write(await response.read())
                            self.logger.info(f"使用标准URL下载干员 {character} 的头像成功")
                            
                            # 发送头像
                            chain = [Image.fromFileSystem(str(avatar_file))]
                            yield event.chain_result(chain)
                            return
            except Exception as e:
                self.logger.error(f"使用标准URL下载头像时出错: {str(e)}")
            
            # 如果所有尝试都失败，返回错误消息
            yield event.plain_result(f"未找到干员 {character} 的头像。")
            
        except Exception as e:
            yield event.plain_result(f"获取干员头像时出错：{str(e)}")
    
    @filter.command("mrfz_fetch_all_avatars")
    async def mrfz_fetch_all_avatars_handler(self, event: AstrMessageEvent):
        """批量获取所有干员头像"""
        try:
            yield event.plain_result("开始获取所有干员头像，这可能需要一些时间...")
            
            # 获取资源文件夹路径
            assets_dir = self.data_dir / "assets"
            assets_dir.mkdir(exist_ok=True)
            
            # 获取所有干员头像映射
            avatar_map = await self._fetch_all_avatar_mappings()
            
            if not avatar_map:
                yield event.plain_result("获取干员头像映射失败，请稍后再试。")
                return
                
            self.logger.info(f"获取到{len(avatar_map)}个干员头像映射")
            yield event.plain_result(f"获取到{len(avatar_map)}个干员头像映射，开始下载...")
            
            # 统计成功和失败的数量
            success_count = 0
            fail_count = 0
            
            # 批量下载头像
            for character, avatar_url in avatar_map.items():
                avatar_file = assets_dir / f"{character}.png"
                
                # 如果已经存在，跳过
                if avatar_file.exists():
                    success_count += 1
                    continue
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(avatar_url, headers=self.DEFAULT_HEADERS) as response:
                            if response.status == 200:
                                # 保存头像
                                with open(avatar_file, 'wb') as f:
                                    f.write(await response.read())
                                self.logger.info(f"干员 {character} 的头像下载成功")
                                success_count += 1
                            else:
                                self.logger.warning(f"干员 {character} 头像下载失败，状态码: {response.status}")
                                fail_count += 1
                except Exception as e:
                    self.logger.error(f"下载干员 {character} 头像时出错: {str(e)}")
                    fail_count += 1
            
            # 返回结果
            yield event.plain_result(f"头像下载完成！成功: {success_count}，失败: {fail_count}")
            
        except Exception as e:
            yield event.plain_result(f"批量获取干员头像时出错：{str(e)}")

    async def send_voice_message(self, event: AstrMessageEvent, voice_file_path: str):
        """发送语音消息"""
        try:
            chain = [Record.fromFileSystem(voice_file_path)]
            yield event.chain_result(chain)
        except Exception as e:
            yield event.plain_result(f"发送语音消息时出错：{str(e)}")
            
    async def _fetch_all_avatar_mappings(self) -> Dict[str, str]:
        """
        直接通过PRTS文件页面获取干员头像URL

        Returns:
            Dict[str, str]: 干员名称到头像URL的映射字典
        """
        avatar_mappings = {}
        
        try:
            # 收集所有已知的干员名称
            operator_names = set()
            
            # 1. 从已知角色ID映射获取
            for op_name in self.KNOWN_CHARACTER_IDS.keys():
                operator_names.add(op_name)
            
            # 2. 从voice_index获取
            if self.voice_index:
                for op_name in self.voice_index.keys():
                    operator_names.add(op_name)
            
            # 3. 从voices目录获取
            if self.voices_dir.exists():
                try:
                    for d in self.voices_dir.iterdir():
                        if d.is_dir():
                            operator_names.add(d.name)
                except Exception as e:
                    self.logger.error(f"扫描voices目录失败: {str(e)}")
            
            self.logger.info(f"找到{len(operator_names)}个干员名称")
            
            # 根据干员名称获取头像URL
            self.logger.info(f"开始使用文件页面格式获取干员头像...")
            
            async def fetch_operator_avatar(op_name):
                try:
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
                                    return image_url, op_name
                    
                    # 如果没找到，使用固定的无头像图片
                    self.logger.debug(f"未找到干员 {op_name} 的头像，使用默认头像")
                    return "https://prts.wiki/images/c/c3/头像_无头像.png", op_name
                    
                except Exception as e:
                    self.logger.warning(f"获取干员 {op_name} 头像时出错: {str(e)}")
                    return "https://prts.wiki/images/c/c3/头像_无头像.png", op_name
            
            # 创建所有干员头像获取任务
            tasks = []
            for op_name in operator_names:
                tasks.append(fetch_operator_avatar(op_name))
            
            # 并发执行所有任务
            results = await asyncio.gather(*tasks)
            
            # 记录未找到头像的干员数量
            not_found_count = 0
            
            # 处理结果
            for image_url, op_name in results:
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

    async def get_avatar_url(self, char_name: str) -> str:
        """获取角色头像URL，优先使用本地文件"""
        # 如果已缓存，直接返回
        if not hasattr(self, '_avatar_cache'):
            self._avatar_cache = {}
            
        # 移除"皮肤"后缀和空格
        base_name = char_name.replace("皮肤", "").strip()
        
        # 如果已缓存，直接返回
        if base_name in self._avatar_cache:
            return self._avatar_cache[base_name]
        
        # 1. 首先检查本地文件
        assets_dir = self.data_dir / "assets"
        local_avatar_path = assets_dir / f"{base_name}.png"
        if local_avatar_path.exists():
            # 使用file://协议返回本地文件路径
            local_url = f"file://{str(local_avatar_path)}".replace("\\", "/")
            self._avatar_cache[base_name] = local_url
            return local_url
        
        # 2. 直接使用PRTS Wiki头像URL
        try:
            prts_url = f"https://media.prts.wiki/c/c5/头像_{base_name}.png"
            self._avatar_cache[base_name] = prts_url
            return prts_url
        except Exception as e:
            self.logger.warning(f"构建PRTS头像URL失败: {base_name} - {str(e)}")
        
        # 3. 如果所有尝试都失败，返回默认头像
        default_url = "https://media.prts.wiki/c/c3/头像_无头像.png"
        self._avatar_cache[base_name] = default_url
        return default_url

    async def get_char_id_map(self):
        """异步获取角色ID映射"""
        url = "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel/character_table.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if not response.ok:
                        print(f"[get_char_id_map] 获取角色ID映射失败，状态码：{response.status}")
                        return {}, {}
                    data = await response.json()
                    name2id = {}
                    id2name = {}
                    for char_id, info in data.items():
                        name2id[info['name']] = char_id
                        id2name[char_id] = info['name']
                    print(f"[get_char_id_map] 获取到{len(name2id)}个干员ID映射")
                    return name2id, id2name
        except Exception as e:
            print(f"[get_char_id_map] 获取干员ID映射失败: {e}")
            return {}, {}

    async def download_avatar_by_id(self, char_id, char_name, assets_dir):
        """异步下载角色头像"""
        avatar_url = f"https://github.com/yuanyan3060/ArknightsGameResource/raw/main/avatar/{char_id}.png"
        avatar_path = assets_dir / f"{char_id}.png"
        if avatar_path.exists():
            print(f"[download_avatar_by_id] {char_name}({char_id})头像已存在")
            return True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url, timeout=10) as response:
                    if response.ok:
                        content = await response.read()
                        avatar_path.write_bytes(content)
                        print(f"[download_avatar_by_id] {char_name}({char_id})头像下载成功")
                        return True
                    else:
                        print(f"[download_avatar_by_id] {char_name}({char_id})头像下载失败，状态码：{response.status}")
                        return False
        except Exception as e:
            print(f"[download_avatar_by_id] 下载{char_name}({char_id})头像失败: {e}")
            return False

    async def ensure_assets(self):
        """确保必要的资源文件存在，如不存在则下载
        """
        self.logger.info("====== ensure_assets 开始执行 ======")
        
        # 创建资源目录
        assets_dir = self.data_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        self.logger.info(f"资源目录路径: {assets_dir}")
        
        # 先扫描语音文件，确保voice_index是最新的
        self.logger.info("扫描语音文件...")
        self.scan_voice_files()
        
        # 获取已经下载的语音列表中的角色
        voice_characters = []
        
        # 1. 从内存中的voice_index获取角色
        if self.voice_index:
            voice_characters = list(self.voice_index.keys())
            self.logger.info(f"从内存中的voice_index找到{len(voice_characters)}个角色")
        # 2. 如果内存中没有，尝试从文件读取
        else:
            voice_index_file = self.data_dir / "voice_index.json"
            if voice_index_file.exists():
                try:
                    with open(voice_index_file, "r", encoding="utf-8") as f:
                        voice_index = json.load(f)
                        voice_characters = list(voice_index.keys())
                    self.logger.info(f"从voice_index.json文件中读取到{len(voice_characters)}个角色")
                except Exception as e:
                    self.logger.error(f"[ensure_assets] 读取voice_index.json失败: {str(e)}")
        
        # 3. 如果还是为空，尝试直接从voices目录获取
        if not voice_characters and self.voices_dir.exists():
            try:
                voice_characters = [d.name for d in self.voices_dir.iterdir() if d.is_dir()]
                self.logger.info(f"从voices目录直接获取到{len(voice_characters)}个角色")
            except Exception as e:
                self.logger.error(f"[ensure_assets] 扫描voices目录失败: {str(e)}")
                
        # 记录找到的角色
        if voice_characters:
            self.logger.info(f"当前需要下载头像的角色：{voice_characters}")
        else:
            self.logger.warning("未找到任何语音角色，跳过头像下载")
        
        # 下载Logo
        logo_file = assets_dir / "logo.png"
        self.logger.info(f"检查LOGO文件：{logo_file}")
        if not logo_file.exists():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("https://github.com/ArknightsResources/Operators/raw/main/logo/logo.png", headers=self.DEFAULT_HEADERS) as response:
                        if response.status == 200:
                            with open(logo_file, "wb") as f:
                                f.write(await response.read())
                            self.logger.info("LOGO下载成功")
                        else:
                            self.logger.warning(f"LOGO下载失败，状态码：{response.status}")
            except Exception as e:
                self.logger.error(f"[ensure_assets] LOGO下载失败: {str(e)}")
        
        # 下载警戒线
        caution_line_file = assets_dir / "caution_line.png"
        self.logger.info(f"检查警戒线文件：{caution_line_file}")
        if not caution_line_file.exists():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("https://github.com/ArknightsResources/Operators/raw/main/ui/caution_line.png", headers=self.DEFAULT_HEADERS) as response:
                        if response.status == 200:
                            with open(caution_line_file, "wb") as f:
                                f.write(await response.read())
                            self.logger.info("警戒线下载成功")
                        else:
                            self.logger.warning(f"警戒线下载失败，状态码：{response.status}")
            except Exception as e:
                self.logger.error(f"[ensure_assets] 警戒线下载失败: {str(e)}")
        
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
            success_count = 0
            for character in voice_characters:
                avatar_file = assets_dir / f"{character}.png"
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
                                        success_count += 1
                                    else:
                                        self.logger.warning(f"{character}头像下载失败，状态码：{response.status}")
                        except Exception as e:
                            self.logger.error(f"[ensure_assets] {character}头像下载出错: {avatar_url}")
                    else:
                        self.logger.warning(f"[ensure_assets] 未找到角色 {character} 的头像URL")
                else:
                    self.logger.info(f"{character}头像已存在")
                    success_count += 1
            
            self.logger.info(f"头像下载完成，成功：{success_count}/{len(voice_characters)}")
        except Exception as e:
            self.logger.error(f"[ensure_assets] 下载头像过程出错: {str(e)}")
        
        self.logger.info("====== ensure_assets 执行完毕 ======")
        return True

    async def get_character_avatar_url(self, character: str) -> str:
        """
        获取角色头像URL
        
        Args:
            character (str): 角色名称
            
        Returns:
            str: 头像URL，如果未找到则返回默认头像
        """
        try:
            # 移除可能的"皮肤"后缀
            base_character = character.replace("皮肤", "").strip()
            
            # 使用统一的PRTS Wiki头像URL格式
            return f"https://media.prts.wiki/c/c5/头像_{base_character}.png"
            
        except Exception as e:
            self.logger.error(f"获取角色头像时发生错误: {str(e)}")
            return "https://media.prts.wiki/c/c3/头像_无头像.png"

    def _generate_voice_list_html(self) -> str:
        """生成语音列表的HTML页面"""
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>明日方舟语音列表</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                .character-section {
                    margin-bottom: 30px;
                    padding: 15px;
                    border: 1px solid #ddd;
                    border-radius: 5px;
                }
                .character-header {
                    display: flex;
                    align-items: center;
                    margin-bottom: 15px;
                }
                .character-avatar {
                    width: 64px;
                    height: 64px;
                    border-radius: 50%;
                    margin-right: 15px;
                    object-fit: cover;
                    border: 2px solid #ddd;
                }
                .character-name {
                    font-size: 24px;
                    color: #333;
                }
                .voice-list {
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                    gap: 10px;
                }
                .voice-item {
                    padding: 8px;
                    background: #f5f5f5;
                    border-radius: 4px;
                }
                .language-tag {
                    display: inline-block;
                    padding: 2px 6px;
                    background: #e0e0e0;
                    border-radius: 3px;
                    margin-right: 5px;
                    font-size: 12px;
                }
            </style>
        </head>
        <body>
            <h1>明日方舟语音列表</h1>
        """
        
        # 按角色名称排序
        characters = sorted(self.voice_index.keys())
        
        for character in characters:
            # 移除可能的"皮肤"后缀并生成头像URL
            base_character = character.replace("皮肤", "").strip()
            avatar_url = f"https://media.prts.wiki/c/c5/头像_{base_character}.png"
            
            html_content += f"""
            <div class="character-section">
                <div class="character-header">
                    <img class="character-avatar" src="{avatar_url}" 
                         onerror="this.src='https://media.prts.wiki/c/c3/头像_无头像.png'" 
                         alt="{character}的头像">
                    <h2 class="character-name">{character}</h2>
                </div>
                <div class="voice-list">
            """
            
            # 获取该角色所有语音文件
            for language in self.voice_index[character]:
                for voice_file in sorted(self.voice_index[character][language]):
                    # 移除文件扩展名并美化显示
                    voice_name = voice_file.replace(".wav", '')
                    html_content += f"""
                    <div class="voice-item">
                        <span class="language-tag">{language}</span>
                        {voice_name}
                    </div>
                    """
            
            html_content += """
                </div>
            </div>
            """
        
        html_content += """
        </body>
        </html>
        """
        
        return html_content
            