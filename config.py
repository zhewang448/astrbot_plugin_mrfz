"""插件配置管理"""
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class PluginConfig:
    """插件运行时配置

    Attributes:
        auto_download: 未找到角色语音时是否尝试自动下载
        allow_public_auto_download: 是否允许普通用户通过 /mrfz 触发自动下载
        auto_download_skin: 下载时是否包含皮肤语音
        default_language_rank: 播放时的语言优先级 (1:方言 2:中文 3:日语 4:英语 5:韩语 6:意语)
        auto_download_language: 执行下载指令时默认下载哪些语言
    """

    auto_download: bool = True
    allow_public_auto_download: bool = True
    auto_download_skin: bool = True
    default_language_rank: str = "123456"
    auto_download_language: str = "123"

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "PluginConfig":
        """从配置字典加载

        Args:
            config: AstrBot 配置字典

        Returns:
            PluginConfig 实例
        """
        return cls(
            auto_download=config.get("auto_download", True),
            allow_public_auto_download=config.get("allow_public_auto_download", True),
            auto_download_skin=config.get("auto_download_skin", True),
            default_language_rank=config.get("default_language_rank", "123456"),
            auto_download_language=config.get("auto_download_language", "123"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式

        Returns:
            配置字典
        """
        return {
            "auto_download": self.auto_download,
            "allow_public_auto_download": self.allow_public_auto_download,
            "auto_download_skin": self.auto_download_skin,
            "default_language_rank": self.default_language_rank,
            "auto_download_language": self.auto_download_language,
        }
