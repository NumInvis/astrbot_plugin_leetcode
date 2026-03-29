"""
LeetCode 每日一题提醒插件
AstrBot LeetCode 每日一题插件

版本: 1.1.0
"""

from ._version import __version__, __plugin_name__, __plugin_desc__, __author__
from .main import LeetCodePlugin

__all__ = ["LeetCodePlugin", "__version__", "__plugin_name__", "__plugin_desc__", "__author__"]
