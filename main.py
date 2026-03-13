"""
LeetCode 每日一题提醒插件
移植自 nonebot-plugin-leetcode
版本: 1.0.0
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Set

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
import astrbot.core.message.components as Comp

from ._version import __version__, __plugin_name__, __author__, __plugin_desc__


# ============ 配置常量 ============
ADMIN_USERS: List[str] = []


@register(__plugin_name__, __author__, __plugin_desc__, __version__)
class LeetCodePlugin(Star):
    """LeetCode 每日一题提醒插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self.context = context

        # 数据目录
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_leetcode"))
        os.makedirs(self.data_dir, exist_ok=True)

        # 配置文件路径
        self.config_file = os.path.join(self.data_dir, "config.json")
        self.subscription_file = os.path.join(self.data_dir, "subscription.json")

        # 保存群的 unified_msg_origin
        self.group_origins: Dict[str, str] = {}

        # 加载配置
        self._load_config()

        # 今日题目缓存
        self.today_question: Optional[Dict] = None
        self.today_date: str = ""

        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None

        # 全局管理员列表
        global ADMIN_USERS
        ADMIN_USERS = self.admin_users.copy()

        # 文件写入锁
        self._file_lock = asyncio.Lock()

        # 异步任务
        self._monitor_task: Optional[asyncio.Task] = None

        logger.info(f"LeetCode 每日一题提醒插件已加载")

    def _load_config(self):
        """加载配置文件"""
        # 默认配置
        default_config = {
            "check_interval_seconds": 3600,
            "inform_hour": 9,
            "inform_minute": 0,
            "admin_users": [],
            "group_origins": {},
            "subscribed_groups": []
        }

        # 从文件加载配置
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    default_config.update(loaded_config)
            except json.JSONDecodeError as e:
                logger.error(f"配置文件JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")

        # 从 AstrBot 配置加载（优先级更高）
        if self.config:
            default_config["check_interval_seconds"] = self.config.get(
                "leetcode_check_interval_seconds", default_config["check_interval_seconds"]
            )
            default_config["inform_hour"] = self.config.get(
                "leetcode_inform_hour", default_config["inform_hour"]
            )
            default_config["inform_minute"] = self.config.get(
                "leetcode_inform_minute", default_config["inform_minute"]
            )

            # 加载管理员列表
            admin_from_config = self.config.get("leetcode_admin_users", [])
            if admin_from_config:
                default_config["admin_users"] = [str(u) for u in admin_from_config]

        # 加载订阅配置（动态修改的）
        if os.path.exists(self.subscription_file):
            try:
                with open(self.subscription_file, 'r', encoding='utf-8') as f:
                    sub_data = json.load(f)
                    if "subscribed_groups" in sub_data:
                        default_config["subscribed_groups"] = sub_data["subscribed_groups"]
                    if "group_origins" in sub_data:
                        self.group_origins = sub_data["group_origins"]
            except json.JSONDecodeError as e:
                logger.error(f"订阅配置JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载订阅配置失败: {e}")

        self.check_interval_seconds = default_config["check_interval_seconds"]
        self.inform_hour = default_config["inform_hour"]
        self.inform_minute = default_config["inform_minute"]
        self.admin_users = default_config["admin_users"]
        self.subscribed_groups = default_config["subscribed_groups"]

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取群组ID"""
        group_id = event.get_group_id()
        if group_id:
            return str(group_id)
        return None

    def _save_group_origin(self, event: AstrMessageEvent):
        """保存群的统一会话标识"""
        group_id = self._get_group_id(event)
        if group_id and hasattr(event, 'unified_msg_origin'):
            self.group_origins[group_id] = event.unified_msg_origin

    def _get_session_for_group(self, group_id: str) -> str:
        """获取群的会话标识"""
        if group_id in self.group_origins:
            return self.group_origins[group_id]
        return group_id

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员"""
        if event.is_admin():
            return True
        sender_id = str(event.get_sender_id())
        return sender_id in ADMIN_USERS

    async def _save_subscription(self):
        """保存订阅配置"""
        async with self._file_lock:
            try:
                data = {
                    "subscribed_groups": self.subscribed_groups,
                    "group_origins": self.group_origins
                }
                with open(self.subscription_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存订阅配置失败: {e}")

    async def initialize(self):
        """插件初始化时执行"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        self._session = aiohttp.ClientSession(headers=headers)
        self._monitor_task = asyncio.create_task(self._async_monitor())

    async def terminate(self):
        """插件卸载时清理资源"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    async def _async_monitor(self):
        """异步监控任务"""
        logger.info("LeetCode 每日一题监控任务已启动")
        last_inform_date = ""

        try:
            while True:
                try:
                    now = datetime.now()
                    today_date = now.strftime("%Y-%m-%d")

                    # 检查是否需要通知
                    if (now.hour == self.inform_hour and
                        now.minute == self.inform_minute and
                        today_date != last_inform_date):

                        logger.info(f"开始获取 LeetCode 每日一题: {today_date}")
                        question = await self._fetch_daily_question()
                        if question:
                            self.today_question = question
                            self.today_date = today_date
                            await self._send_question_to_subscribers(question)
                            last_inform_date = today_date
                            logger.info(f"LeetCode 每日一题已推送: {question.get('title', '未知')}")

                except Exception as e:
                    logger.error(f"LeetCode 监控任务出错: {e}")

                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("LeetCode 每日一题监控任务已停止")

    async def _fetch_daily_question(self) -> Optional[Dict]:
        """获取 LeetCode 每日一题 - 使用可靠的第三方 API"""
        if not self._session:
            logger.error("HTTP 会话未初始化")
            return None

        try:
            url = "https://leetcode-api-pied.vercel.app/daily"
            logger.info(f"正在向 {url} 发送请求")
            
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                logger.info(f"响应状态码: {response.status}")
                
                if response.status == 200:
                    data = await response.json()
                    question = data.get("question", {})
                    link = data.get("link", "")
                    title_slug = question.get("titleSlug")
                    
                    # 获取标题（优先使用中文标题，如果没有则使用英文）
                    title = question.get("title", "")
                    title_cn = question.get("translatedTitle")
                    if not title_cn:
                        title_cn = title
                    
                    result = {
                        "date": data.get("date"),
                        "title": title,
                        "titleCn": title_cn,
                        "titleSlug": title_slug,
                        "frontendQuestionId": question.get("questionFrontendId"),
                        "difficulty": question.get("difficulty"),
                        "acRate": question.get("acRate", 0) / 100.0 if question.get("acRate") else 0,
                        "link": f"https://leetcode.com{link}" if link.startswith("/") else link,
                        "topicTags": question.get("topicTags", [])
                    }
                    
                    logger.info(f"成功获取题目: {result}")
                    return result
                else:
                    logger.error(f"请求失败，状态码: {response.status}")
                    response_text = await response.text()
                    logger.error(f"响应内容: {response_text}")
        except Exception as e:
            logger.error(f"获取 LeetCode 每日一题失败: {e}", exc_info=True)

        return None

    def _build_question_message(self, question: Dict) -> List:
        """构建题目消息"""
        chain = []

        difficulty_emoji = {
            "Easy": "🟢",
            "Medium": "🟡",
            "Hard": "🔴"
        }

        difficulty_cn = {
            "Easy": "简单",
            "Medium": "中等",
            "Hard": "困难"
        }

        emoji = difficulty_emoji.get(question.get("difficulty", ""), "⚪")
        title_cn = question.get("titleCn") or question.get("title", "未知题目")
        title = question.get("title", "未知题目")
        qid = question.get("frontendQuestionId", "")
        difficulty = question.get("difficulty", "")
        difficulty_cn_text = difficulty_cn.get(difficulty, difficulty)
        ac_rate = question.get("acRate", 0)
        link = question.get("link", "")
        
        tags = []
        for tag in question.get("topicTags", []):
            if isinstance(tag, dict):
                tag_name = tag.get("nameTranslated") or tag.get("name", "")
                if tag_name:
                    tags.append(tag_name)
            else:
                tags.append(str(tag))

        chain.append(Comp.Plain(f"📅 {question.get('date', '')}\n"))
        chain.append(Comp.Plain(f"{emoji} 【{qid}. {title_cn}】\n"))
        if title_cn != title:
            chain.append(Comp.Plain(f"英文标题: {title}\n"))
        chain.append(Comp.Plain(f"难度: {difficulty_cn_text}\n"))
        if ac_rate:
            chain.append(Comp.Plain(f"通过率: {ac_rate * 100:.1f}%\n"))
        if tags:
            chain.append(Comp.Plain(f"标签: {', '.join(tags)}\n"))
        chain.append(Comp.Plain(f"🔗 链接: {link}"))

        return chain

    async def _send_question_to_subscribers(self, question: Dict):
        """发送题目到所有订阅者"""
        chain = self._build_question_message(question)

        for group_id in self.subscribed_groups:
            try:
                await self.context.send_message(
                    self._get_session_for_group(group_id),
                    MessageChain(chain)
                )
                logger.info(f"LeetCode 每日一题已发送到群 {group_id}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送题目到群 {group_id} 失败: {e}")

    async def _send_plain_text(self, group_id: str, text: str):
        """发送纯文本消息"""
        try:
            chain = [Comp.Plain(text)]
            await self.context.send_message(self._get_session_for_group(group_id), MessageChain(chain))
        except Exception as e:
            logger.error(f"发送消息到群 {group_id} 失败: {e}")

    # ========== 命令处理 ==========

    @filter.command("lc菜单")
    async def cmd_menu(self, event: AstrMessageEvent):
        """显示主菜单"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        msg = """🤖 LeetCode 每日一题 - 主菜单

【查询命令】
📋 /lc今日 - 立即获取今日题目
📋 /lc列表 - 查看当前群订阅状态

【管理命令】
➕ /lc订阅 - 在当前群订阅每日一题
➖ /lc退订 - 在当前群取消订阅
📋 /lc全部订阅 - 查看所有群的订阅
📖 /lc帮助 - 查看详细帮助"""

        yield event.plain_result(msg)

    @filter.command("lc帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示详细帮助"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        msg = """📖 LeetCode 每日一题 - 详细使用说明

【查询命令】
1️⃣ /lc今日 - 立即获取并显示今日题目
2️⃣ /lc列表 - 查看当前群是否已订阅

【管理命令】
3️⃣ /lc订阅 - 在当前群订阅每日一题推送
4️⃣ /lc退订 - 在当前群取消每日一题推送
5️⃣ /lc全部订阅 - 查看所有群的订阅情况（超级管理员）

【配置】
- 默认每日 09:00 推送
- 可在插件配置中修改推送时间

【提示】
- 只有管理员可以使用管理命令
- 每日一题数据来自 LeetCode 中文站"""

        yield event.plain_result(msg)

    @filter.command("lc订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每日一题"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id in self.subscribed_groups:
            yield event.plain_result("❌ 本群已经订阅了 LeetCode 每日一题")
            return

        self.subscribed_groups.append(group_id)
        await self._save_subscription()

        yield event.plain_result(f"✅ 本群已成功订阅 LeetCode 每日一题\n每日 {self.inform_hour:02d}:{self.inform_minute:02d} 推送")

    @filter.command("lc退订")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消订阅"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id not in self.subscribed_groups:
            yield event.plain_result("❌ 本群没有订阅 LeetCode 每日一题")
            return

        self.subscribed_groups.remove(group_id)
        await self._save_subscription()

        yield event.plain_result("✅ 本群已取消订阅 LeetCode 每日一题")

    @filter.command("lc今日")
    async def cmd_today(self, event: AstrMessageEvent):
        """获取今日题目"""
        self._save_group_origin(event)

        today_date = datetime.now().strftime("%Y-%m-%d")

        # 检查缓存
        if self.today_question and self.today_date == today_date:
            question = self.today_question
        else:
            yield event.plain_result("⏳ 正在获取今日题目...")
            question = await self._fetch_daily_question()
            if question:
                self.today_question = question
                self.today_date = today_date

        if not question:
            yield event.plain_result("❌ 获取今日题目失败，请稍后再试")
            return

        chain = self._build_question_message(question)
        yield event.chain_result(chain)

    @filter.command("lc列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """查看当前群订阅状态"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令只能在群聊中使用")
            return

        if group_id in self.subscribed_groups:
            yield event.plain_result(f"✅ 本群已订阅 LeetCode 每日一题\n每日 {self.inform_hour:02d}:{self.inform_minute:02d} 推送")
        else:
            yield event.plain_result("❌ 本群未订阅 LeetCode 每日一题")

    @filter.command("lc全部订阅")
    async def cmd_all_subscriptions(self, event: AstrMessageEvent):
        """查看所有群的订阅"""
        self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if not self.subscribed_groups:
            yield event.plain_result("📋 暂无群订阅 LeetCode 每日一题")
            return

        lines = ["📋 已订阅 LeetCode 每日一题的群:"]
        lines.append("=" * 30)
        for i, group_id in enumerate(self.subscribed_groups, 1):
            lines.append(f"{i}. {group_id}")

        yield event.plain_result("\n".join(lines))
