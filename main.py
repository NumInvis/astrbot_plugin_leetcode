"""
LeetCode 每日一题提醒插件
移植自 nonebot-plugin-leetcode
版本: 1.0.0
"""

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain

from ._version import __author__, __plugin_desc__, __plugin_name__, __version__

try:
    from astrbot.api.web import error_response, json_response, request

    async def read_web_json():
        return await request.json(default=None)

except ImportError:
    # Older AstrBot releases expose plugin Web APIs through Quart.
    from quart import jsonify, request

    def json_response(data):
        return jsonify(data)

    def error_response(message, *, status_code=400):
        return jsonify({"status": "error", "message": message}), status_code

    async def read_web_json():
        return await request.get_json(silent=True)


SETTING_DEFAULTS = {
    "inform_hour": 9,
    "inform_minute": 0,
    "check_interval_seconds": 30,
    "admin_users": [],
}
SETTING_CONFIG_KEYS = {
    "inform_hour": "leetcode_inform_hour",
    "inform_minute": "leetcode_inform_minute",
    "check_interval_seconds": "leetcode_check_interval_seconds",
    "admin_users": "admin_users",
}


def clean_html(html_content: str) -> str:
    """清理HTML标签，提取纯文本"""
    if not html_content:
        return ""
    # 移除HTML标签
    text = re.sub(r"<[^>]+>", "", html_content)
    # 解码HTML实体
    text = (
        text.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    text = text.replace("&nbsp;", " ").replace("&#39;", "'")
    # 移除多余空白
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = text.strip()
    return text


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
        self.group_names: Dict[str, str] = {}

        # 加载配置
        self._load_config()

        # 今日题目缓存
        self.today_question: Optional[Dict] = None
        self.today_date: str = ""

        # HTTP 会话
        self._session = None

        # 文件写入锁
        self._file_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._test_push_lock = asyncio.Lock()
        self._monitor_wakeup = asyncio.Event()
        self._last_inform_date = ""

        # 异步任务
        self._monitor_task: Optional[asyncio.Task] = None
        self.context.register_web_api(
            f"/{__plugin_name__}/settings",
            self.page_get_settings,
            ["GET"],
            "读取 LeetCode 配置和运行状态",
        )
        self.context.register_web_api(
            f"/{__plugin_name__}/settings",
            self.page_save_settings,
            ["POST"],
            "保存并应用 LeetCode 配置",
        )
        self.context.register_web_api(
            f"/{__plugin_name__}/test-push",
            self.page_test_push,
            ["POST"],
            "向指定群测试推送今日题目",
        )

        logger.info("LeetCode 每日一题提醒插件已加载")

    def _load_config(self):
        """加载配置文件"""
        # 默认配置
        default_config = {
            **SETTING_DEFAULTS,
            "group_origins": {},
            "group_names": {},
            "subscribed_groups": [],
        }

        # 从文件加载配置
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded_config = json.load(f)
                    if isinstance(loaded_config, dict):
                        default_config.update(loaded_config)
            except json.JSONDecodeError as e:
                logger.error(f"配置文件JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")

        # 从 AstrBot 配置加载（优先级更高）
        if self.config is not None:
            migrating = not self.config.get("settings_migrated", False)
            for field, config_key in SETTING_CONFIG_KEYS.items():
                value = self.config.get(config_key, default_config[field])
                # New schema defaults must not mask existing data/config.json settings.
                if migrating and value == SETTING_DEFAULTS[field]:
                    value = default_config[field]
                if migrating and field == "admin_users":
                    value = self.config.get("leetcode_admin_users", value)
                default_config[field] = value

        settings = {}
        for field, fallback in SETTING_DEFAULTS.items():
            try:
                settings[field] = self._validate_settings(
                    {field: default_config[field]}
                )[field]
            except ValueError as exc:
                logger.warning(f"LeetCode 配置 {field} 无效，使用默认值: {exc}")
                settings[field] = (
                    fallback.copy() if isinstance(fallback, list) else fallback
                )
        self._apply_settings(settings)
        if self.config is not None and not self.config.get("settings_migrated", False):
            try:
                self._persist_settings(settings)
            except Exception as exc:
                logger.error(f"迁移 LeetCode 配置失败: {exc}")

        # Keep existing group metadata from legacy config.json deployments.
        for field in ("group_origins", "group_names"):
            values = default_config.get(field, {})
            if isinstance(values, dict):
                getattr(self, field).update(
                    {
                        str(key): value.strip()
                        for key, value in values.items()
                        if isinstance(value, str) and value.strip()
                    }
                )

        # 加载订阅配置（动态修改的）
        if os.path.exists(self.subscription_file):
            try:
                with open(self.subscription_file, "r", encoding="utf-8") as f:
                    sub_data = json.load(f)
                    if "subscribed_groups" in sub_data:
                        default_config["subscribed_groups"] = sub_data[
                            "subscribed_groups"
                        ]
                    for field in ("group_origins", "group_names"):
                        values = sub_data.get(field, {})
                        if isinstance(values, dict):
                            getattr(self, field).update(
                                {
                                    str(key): value.strip()
                                    for key, value in values.items()
                                    if isinstance(value, str) and value.strip()
                                }
                            )
            except json.JSONDecodeError as e:
                logger.error(f"订阅配置JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载订阅配置失败: {e}")

        self.subscribed_groups = default_config["subscribed_groups"]

    @staticmethod
    def _validate_settings(payload):
        """Validate both Web API updates and persisted configuration."""
        if not isinstance(payload, dict) or not payload:
            raise ValueError("请提交配置对象")
        if set(payload) - SETTING_DEFAULTS.keys():
            raise ValueError("包含不支持的配置项")
        validated = {}
        for field, value in payload.items():
            if field == "admin_users":
                if not isinstance(value, list) or any(
                    not isinstance(user, str)
                    or not user.strip()
                    or len(user.strip()) > 128
                    for user in value
                ):
                    raise ValueError(
                        "管理员 ID 必须是非空字符串列表，每项不超过 128 个字符"
                    )
                validated[field] = list(dict.fromkeys(user.strip() for user in value))
                continue
            minimum, maximum, label = {
                "inform_hour": (0, 23, "推送小时"),
                "inform_minute": (0, 59, "推送分钟"),
                "check_interval_seconds": (5, 30, "检查间隔（秒）"),
            }[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{label}必须是 {minimum}–{maximum} 之间的整数")
            validated[field] = value
        return validated

    def _apply_settings(self, settings):
        for field, value in settings.items():
            setattr(self, field, value)
        global ADMIN_USERS
        ADMIN_USERS = self.admin_users.copy()

    def _persist_settings(self, settings):
        """Persist before updating runtime state, retaining the previous values on failure."""
        if self.config is not None:
            previous = dict(self.config)
            self.config.update(
                {SETTING_CONFIG_KEYS[key]: value for key, value in settings.items()}
            )
            self.config["settings_migrated"] = True
            try:
                self.config.save_config()
            except Exception:
                self.config.clear()
                self.config.update(previous)
                raise
            return
        # Standalone deployments without AstrBotConfig keep using the original data directory.
        data = {}
        if os.path.exists(self.config_file):
            with open(self.config_file, encoding="utf-8") as source:
                data = json.load(source)
            if not isinstance(data, dict):
                raise ValueError("原配置文件必须是 JSON 对象")
        data.update(settings)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                delete=False,
            ) as target:
                temp_path = target.name
                json.dump(data, target, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.config_file)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def _settings_snapshot(self):
        now = datetime.now().astimezone()
        next_run = now.replace(
            hour=self.inform_hour, minute=self.inform_minute, second=0, microsecond=0
        )
        if next_run + timedelta(
            minutes=1
        ) <= now or self._last_inform_date == now.strftime("%Y-%m-%d"):
            next_run += timedelta(days=1)
        return {
            "settings": {field: getattr(self, field) for field in SETTING_DEFAULTS},
            "runtime": {
                "server_time": now.isoformat(timespec="seconds"),
                "timezone": now.tzname(),
                "next_run": next_run.isoformat(timespec="seconds"),
                "subscribed_groups": list(self.subscribed_groups),
                "test_groups": [
                    {
                        "group_id": group_id,
                        "group_name": self.group_names.get(group_id, ""),
                        "subscribed": group_id in self.subscribed_groups,
                        "available": bool(self.group_origins.get(group_id)),
                    }
                    for group_id in sorted(
                        {str(group) for group in self.subscribed_groups}
                        | {str(group) for group in self.group_origins}
                        | {str(group) for group in self.group_names}
                    )
                ],
                "monitor_running": self._monitor_task is not None
                and not self._monitor_task.done(),
            },
        }

    async def page_get_settings(self):
        """Read configuration through the authenticated Dashboard plugin bridge."""
        return json_response(self._settings_snapshot())

    async def page_save_settings(self):
        """Save validated settings and wake the scheduler without restarting the plugin."""
        try:
            changes = self._validate_settings(await read_web_json())
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        async with self._settings_lock:
            settings = {field: getattr(self, field) for field in SETTING_DEFAULTS}
            settings.update(changes)
            try:
                self._persist_settings(settings)
            except Exception:
                logger.exception("保存 LeetCode 配置失败")
                return error_response(
                    "无法写入配置，请检查文件权限后重试", status_code=500
                )
            self._apply_settings(settings)
            self._monitor_wakeup.set()
        return json_response(self._settings_snapshot())

    async def page_test_push(self):
        """Send today's question to one known group without consuming its scheduled push."""
        payload = await read_web_json()
        if not isinstance(payload, dict) or set(payload) != {"group_id"}:
            return error_response("请提供要测试推送的群号", status_code=400)
        group_id = payload["group_id"]
        if not isinstance(group_id, str):
            return error_response("群号必须是字符串", status_code=400)
        group_id = group_id.strip()
        if (
            not group_id
            or len(group_id) > 128
            or any(char.isspace() or ord(char) < 32 for char in group_id)
        ):
            return error_response(
                "请输入有效群号，长度不超过 128 个字符", status_code=400
            )
        origin = self.group_origins.get(group_id)
        if not isinstance(origin, str) or not origin.strip():
            return error_response(
                "尚未记录该群会话，请先在目标群发送 /lc列表，再刷新状态后重试",
                status_code=400,
            )
        if self._test_push_lock.locked():
            return error_response("已有测试推送正在执行，请稍后重试", status_code=409)

        async with self._test_push_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                question = self.today_question if self.today_date == today else None
                if not question:
                    question = await self._fetch_daily_question()
                    if not question:
                        return error_response(
                            "获取今日题目失败，请稍后重试", status_code=502
                        )
                    self.today_question = question
                    self.today_date = today
                chain = [Comp.Plain("🧪 LeetCode 测试推送\n")]
                chain.extend(self._build_question_message(question))
                sent = await asyncio.wait_for(
                    self.context.send_message(origin, MessageChain(chain)), timeout=30
                )
            except asyncio.TimeoutError:
                return error_response(
                    "发送超时，请先检查目标群是否已收到消息，再决定是否重试",
                    status_code=504,
                )
            except Exception:
                logger.exception(f"LeetCode 测试推送到群 {group_id} 失败")
                return error_response(
                    "测试推送失败，请检查机器人是否在目标群、平台连接和发送权限",
                    status_code=502,
                )
            if not sent:
                return error_response(
                    "未找到该群对应的平台，请检查平台是否已连接并在目标群重新发送 /lc列表",
                    status_code=502,
                )
        logger.info(f"LeetCode 测试推送已提交到群 {group_id}")
        return json_response(
            {
                "group_id": group_id,
                "group_name": self.group_names.get(group_id, ""),
                "question_title": question.get("titleCn")
                or question.get("title", "今日题目"),
                "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取群组ID"""
        group_id = event.get_group_id()
        if group_id:
            return str(group_id)
        return None

    async def _save_group_origin(self, event: AstrMessageEvent, *, refresh_name=False):
        """Record group names and routes when handling a group command, including before subscription."""
        group_id = self._get_group_id(event)
        if not group_id:
            return
        previous = (self.group_origins.get(group_id), self.group_names.get(group_id))
        origin = getattr(event, "unified_msg_origin", None)
        if isinstance(origin, str) and origin:
            self.group_origins[group_id] = origin
        message = getattr(event, "message_obj", None)
        group = getattr(message, "group", None)
        name = getattr(group, "group_name", None) or getattr(
            message, "group_name", None
        )
        if (
            refresh_name or not name and not self.group_names.get(group_id)
        ) and callable(getattr(event, "get_group", None)):
            try:
                details = await asyncio.wait_for(event.get_group(), timeout=5)
                name = getattr(details, "group_name", None) or name
            except Exception as exc:
                logger.debug(f"读取群 {group_id} 名称失败，保留已有记录: {exc}")
        if isinstance(name, str) and name.strip():
            self.group_names[group_id] = name.strip()
        if previous != (
            self.group_origins.get(group_id),
            self.group_names.get(group_id),
        ):
            await self._save_subscription()

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
                    "group_origins": self.group_origins,
                    "group_names": self.group_names,
                }
                with open(self.subscription_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存订阅配置失败: {e}")

    async def initialize(self):
        """插件初始化时执行"""
        self._monitor_task = asyncio.create_task(self._async_monitor())

    async def terminate(self):
        """插件卸载时清理资源"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _async_monitor(self):
        """异步监控任务"""
        logger.info("LeetCode 每日一题监控任务已启动")

        try:
            while True:
                try:
                    now = datetime.now()
                    today_date = now.strftime("%Y-%m-%d")

                    # 检查是否需要通知
                    if (now.hour == self.inform_hour and
                        now.minute == self.inform_minute and
                        today_date != self._last_inform_date):

                        logger.info(f"开始获取 LeetCode 每日一题: {today_date}")
                        question = await self._fetch_daily_question()
                        if question:
                            self.today_question = question
                            self.today_date = today_date
                            await self._send_question_to_subscribers(question)
                            self._last_inform_date = today_date
                            logger.info(f"LeetCode 每日一题已推送: {question.get('title', '未知')}")

                except Exception as e:
                    logger.error(f"LeetCode 监控任务出错: {e}")

                try:
                    await asyncio.wait_for(self._monitor_wakeup.wait(), timeout=self.check_interval_seconds)
                except asyncio.TimeoutError:
                    pass
                self._monitor_wakeup.clear()

        except asyncio.CancelledError:
            logger.info("LeetCode 每日一题监控任务已停止")

    async def _fetch_daily_question(self) -> Optional[Dict]:
        """获取 LeetCode 每日一题 - 使用内置的 urllib"""
        try:
            import ssl
            import urllib.request

            url = "https://leetcode-api-pied.vercel.app/daily"
            logger.info(f"正在向 {url} 发送请求")

            # 创建SSL上下文，忽略证书验证
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # 使用线程池执行同步请求
            loop = asyncio.get_event_loop()

            def fetch():
                req = urllib.request.Request(
                    url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                )
                with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                    return response.read().decode('utf-8')

            response_text = await loop.run_in_executor(None, fetch)
            logger.info(f"响应内容: {response_text[:200]}")

            data = json.loads(response_text)
            question = data.get("question", {})
            link = data.get("link", "")
            title_slug = question.get("titleSlug")

            # 获取标题（优先使用中文标题，如果没有则使用英文）
            title = question.get("title", "")
            title_cn = question.get("translatedTitle")
            if not title_cn:
                title_cn = title

            # 获取题目内容（HTML格式，需要清理）
            content_html = question.get("content", "")

            result = {
                "date": data.get("date"),
                "title": title,
                "titleCn": title_cn,
                "titleSlug": title_slug,
                "frontendQuestionId": question.get("questionFrontendId"),
                "difficulty": question.get("difficulty"),
                "acRate": question.get("acRate", 0) / 100.0 if question.get("acRate") else 0,
                "link": f"https://leetcode.com{link}" if link.startswith("/") else link,
                "topicTags": question.get("topicTags", []),
                "content": content_html
            }

            logger.info(f"成功获取题目: {result}")
            return result
        except Exception as e:
            logger.error(f"获取 LeetCode 每日一题失败: {e}", exc_info=True)

        return None

    async def _fetch_question_by_id(self, question_id: str) -> Optional[Dict]:
        """根据题目号获取 LeetCode 题目详情"""
        try:
            import ssl
            import urllib.request

            # 使用 lcid.cc API 获取题目信息
            url = f"https://lcid.cc/info/{question_id}"
            logger.info(f"正在获取题目 {question_id}: {url}")

            # 创建SSL上下文，忽略证书验证
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # 使用线程池执行同步请求
            loop = asyncio.get_event_loop()

            def fetch():
                req = urllib.request.Request(
                    url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                )
                with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                    return response.read().decode('utf-8')

            response_text = await loop.run_in_executor(None, fetch)
            logger.info(f"题目 {question_id} 响应: {response_text[:200]}")

            data = json.loads(response_text)

            # 获取标题（优先使用中文标题）
            title = data.get("title", "")
            title_cn = data.get("title_cn", "")
            if not title_cn:
                title_cn = title

            # 构建结果
            result = {
                "date": "",
                "title": title,
                "titleCn": title_cn,
                "titleSlug": data.get("slug", ""),
                "frontendQuestionId": str(data.get("id", question_id)),
                "difficulty": data.get("difficulty", ""),
                "acRate": 0,
                "link": f"https://leetcode.com/problems/{data.get('slug', '')}/",
                "topicTags": [],
                "content": data.get("content", "")
            }

            logger.info(f"成功获取题目 {question_id}: {result['title']}")
            return result
        except Exception as e:
            logger.error(f"获取题目 {question_id} 失败: {e}", exc_info=True)

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
        chain.append(Comp.Plain(f"🔗 链接: {link}\n"))

        # 添加完整题目内容
        content = question.get("content", "")
        if content:
            clean_content = clean_html(content)
            chain.append(Comp.Plain("\n📝 题目描述:\n"))
            # 分段发送，避免消息过长，上限3000字符
            max_length = 3000
            if len(clean_content) > max_length:
                chain.append(Comp.Plain(clean_content[:max_length] + "\n\n... (内容已截断，请访问链接查看完整题目)"))
            else:
                chain.append(Comp.Plain(clean_content))

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
        await self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        msg = """🤖 LeetCode 每日一题 - 主菜单

【查询命令】
📋 /lc今日 - 立即获取今日题目（含完整描述）
🔍 /lc题目 [题号] - 查询指定题目（如: /lc题目 1）
🤖 /lc解题 [题号] - 使用AI分析并解答题目（如: /lc解题 1）
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
        await self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        msg = """📖 LeetCode 每日一题 - 详细使用说明

【查询命令】
1️⃣ /lc今日 - 立即获取并显示今日题目（含完整描述）
2️⃣ /lc题目 [题号] - 查询指定题号的题目
   示例: /lc题目 1 (查询两数之和)
   示例: /lc题目  (不传参数则获取今日题目)
3️⃣ /lc解题 [题号] - 使用AI分析题目并提供解题思路、代码和关键点
   示例: /lc解题 1 (AI解答两数之和)
   示例: /lc解题  (不传参数则解答今日题目)
4️⃣ /lc列表 - 查看当前群是否已订阅

【管理命令】
5️⃣ /lc订阅 - 在当前群订阅每日一题推送
6️⃣ /lc退订 - 在当前群取消每日一题推送
7️⃣ /lc全部订阅 - 查看所有群的订阅情况（超级管理员）

【AI解题说明】
/lc解题命令需要AstrBot已配置LLM提供商（如OpenAI、Claude等）
AI会提供：题目理解、解题思路、算法步骤、参考代码、关键点

【配置】
- 默认每日 09:00 推送
- 可在插件详情的「每日一题设置」视图中修改，保存后立即生效

【提示】
- 只有管理员可以使用管理命令
- 每日一题数据来自 LeetCode 中文站"""

        yield event.plain_result(msg)

    @filter.command("lc订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每日一题"""
        await self._save_group_origin(event)
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
        await self._save_group_origin(event)
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
        await self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

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
        await self._save_group_origin(event, refresh_name=True)
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
        await self._save_group_origin(event)
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

    @filter.command("lc题目")
    async def cmd_question(self, event: AstrMessageEvent, question_id: str = ""):
        """根据题目号查询题目，不传参数则获取今日题目"""
        await self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if not question_id:
            # 没有提供题目号，获取今日题目
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
        else:
            # 提供了题目号，查询指定题目
            yield event.plain_result(f"⏳ 正在查询题目 {question_id}...")

            question = await self._fetch_question_by_id(question_id)

            if not question:
                yield event.plain_result(f"❌ 未找到题目 {question_id}，请检查题号是否正确")
                return

            chain = self._build_question_message(question)
            yield event.chain_result(chain)

    @filter.command("lc解题")
    async def cmd_solve(self, event: AstrMessageEvent, question_id: str = ""):
        """使用AI分析并解答题目，不传参数则解答今日题目"""
        await self._save_group_origin(event)
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if not question_id:
            # 没有提供题目号，获取今日题目
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
        else:
            # 提供了题目号，查询指定题目
            yield event.plain_result(f"⏳ 正在查询题目 {question_id}...")

            question = await self._fetch_question_by_id(question_id)

            if not question:
                yield event.plain_result(f"❌ 未找到题目 {question_id}，请检查题号是否正确")
                return

        content = question.get("content", "")
        if not content:
            yield event.plain_result("❌ 暂无题目内容")
            return

        title_cn = question.get("titleCn") or question.get("title", "未知题目")
        qid = question.get("frontendQuestionId", "")
        difficulty = question.get("difficulty", "")
        tags = []
        for tag in question.get("topicTags", []):
            if isinstance(tag, dict):
                tag_name = tag.get("nameTranslated") or tag.get("name", "")
                if tag_name:
                    tags.append(tag_name)

        clean_content = clean_html(content)

        yield event.plain_result("🤖 正在使用AI分析题目，请稍候...")

        try:
            # 构建提示词
            prompt = f"""请作为算法专家，分析并解答以下LeetCode题目：

题目编号: {qid}
题目名称: {title_cn}
难度: {difficulty}
标签: {', '.join(tags)}

题目描述:
{clean_content[:6000]}

请提供：
1. 题目理解：简要说明题目要求
2. 解题思路：分析解题方法，包括时间复杂度和空间复杂度
3. 算法步骤：详细说明解题步骤
4. 参考代码：提供Python实现（包含注释）
5. 关键点：总结解题的关键要点

请用中文回答。"""

            # 调用LLM
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )

            answer = llm_resp.completion_text

            msg = f"🤖 【{qid}. {title_cn}】AI解题分析\n"
            msg += "=" * 40 + "\n\n"
            msg += answer

            yield event.plain_result(msg)

        except Exception as e:
            logger.error(f"AI解题失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ AI解题失败: {e}\n请检查是否已配置LLM提供商")
