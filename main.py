"""
LeetCode 每日一题提醒插件
移植自 nonebot-plugin-leetcode
版本: 1.1.0
"""

import asyncio
import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import AstrBotConfig

from ._version import __version__, __plugin_name__, __author__, __plugin_desc__


# 配置项默认值
DEFAULTS = {
    "admin_users": [],
    "group_inform_hour": 9,
    "group_inform_minute": 0,
    "check_interval_seconds": 30,
    "default_language": "zh",
    "enable_personal_subscribe": True,
    "personal_inform_hour": 9,
    "personal_inform_minute": 30,
    "enable_llm_translation": True,
    "translation_provider_id": "",
    "enable_image_push": False,

}


def _get(config: dict, key: str):
    """从配置中读取值，缺失或为 None 时回退到默认值。"""
    if not config:
        return DEFAULTS[key]
    val = config.get(key)
    return val if val is not None else DEFAULTS[key]


def _parse_bool(value, key: str) -> bool:
    """解析布尔配置，兼容少量字符串写法。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on", "开启", "启用"):
            return True
        if normalized in ("false", "0", "no", "off", "关闭", "禁用"):
            return False
    logger.warning(f"配置项 {key} 的值无效: {value!r}，已回退为默认值 {DEFAULTS[key]!r}")
    return bool(DEFAULTS[key])


def _parse_int_range(value, key: str, min_value: int, max_value: int) -> int:
    """解析整数配置并限制范围，避免时间/间隔乱填导致插件启动失败。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning(f"配置项 {key} 的值无效: {value!r}，已回退为默认值 {DEFAULTS[key]!r}")
        return int(DEFAULTS[key])

    if min_value <= parsed <= max_value:
        return parsed

    logger.warning(
        f"配置项 {key} 超出范围: {parsed}，有效范围为 {min_value}-{max_value}，"
        f"已回退为默认值 {DEFAULTS[key]!r}"
    )
    return int(DEFAULTS[key])


def _parse_language(value) -> str:
    """解析默认语言配置。"""
    language = str(value or "").strip().lower()
    if language in ("zh", "en", "both"):
        return language
    logger.warning(f"default_language 配置无效: {value!r}，已回退为默认值 {DEFAULTS['default_language']!r}")
    return DEFAULTS["default_language"]


class _LeetCodeHTMLToMarkdown(HTMLParser):
    """将 LeetCode 题目 HTML 转换为 Markdown。"""

    def __init__(self):
        super().__init__()
        self._result: list[str] = []
        self._list_depth: int = 0
        self._in_pre: bool = False
        self._code_lang: str = ""
        self._in_code: bool = False
        self._code_buf: list[str] = []
        self._in_li: bool = False
        self._in_p: bool = False
        self._in_strong: bool = False
        self._in_em: bool = False
        self._in_heading: bool = False
        self._heading_level: int = 0

    # ---- handlers ----
    def handle_starttag(self, tag: str, attrs: list):
        t = tag.lower()
        if t == "pre":
            self._in_pre = True
            self._code_buf = []
        elif self._in_pre:
            if t == "br":
                self._code_buf.append("\n")
        elif t == "code" and not self._in_pre:
            self._in_code = True
            self._code_buf = []
        elif t == "p":
            self._in_p = True
            self._maybe_newline()
        elif t in ("ul", "ol"):
            self._list_depth += 1
            self._maybe_newline()
        elif t == "li":
            self._in_li = True
            indent = "  " * (self._list_depth - 1)
            self._result.append(f"\n{indent}- ")
        elif t == "strong" or t == "b":
            self._in_strong = True
            self._result.append("**")
        elif t == "em" or t == "i":
            self._in_em = True
            self._result.append("*")
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = True
            self._heading_level = int(t[1])
            self._maybe_newline()
        elif t == "br":
            self._result.append("\n")
        elif t == "sup":
            self._result.append("^")
        elif t == "sub":
            self._result.append("_")
        elif t == "blockquote":
            self._maybe_newline()
            self._result.append("> ")
        elif t == "hr":
            self._maybe_newline()
            self._result.append("\n---\n")

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t == "pre":
            self._in_pre = False
            code_text = "".join(self._code_buf).strip()
            if code_text:
                # AstrBot 的文本转图片主题可能会把 Markdown 代码块内容渲染为空，
                # 示例区改用普通文本更稳定。
                self._result.append(f"\n{code_text}\n")
            self._code_buf = []
            self._code_lang = ""
        elif self._in_pre:
            return
        elif t == "code" and not self._in_pre:
            self._in_code = False
            self._result.append("`")
            self._result.append("".join(self._code_buf))
            self._result.append("`")
            self._code_buf = []
        elif t == "p":
            self._in_p = False
            self._result.append("\n\n")
        elif t in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
            self._maybe_newline()
        elif t == "li":
            self._in_li = False
        elif t == "strong" or t == "b":
            self._in_strong = False
            self._result.append("**")
        elif t == "em" or t == "i":
            self._in_em = False
            self._result.append("*")
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._in_heading = False
            self._result.append(f"\n{'#' * self._heading_level} ")
        elif t == "blockquote":
            self._result.append("\n\n")

    def handle_data(self, data: str):
        text = data
        if self._in_pre:
            self._code_buf.append(text)
        elif self._in_code:
            self._code_buf.append(text)
        else:
            # 普通文本：保留内部空白
            self._result.append(text)

    def handle_entityref(self, name: str):
        entities = {"quot": '"', "amp": "&", "lt": "<", "gt": ">", "nbsp": " ", "#39": "'"}
        text = entities.get(name, f"&{name};")
        if self._in_pre or self._in_code:
            self._code_buf.append(text)
        else:
            self._result.append(text)

    def handle_charref(self, name: str):
        try:
            if name.startswith("x"):
                ch = chr(int(name[1:], 16))
            else:
                ch = chr(int(name))
        except (ValueError, OverflowError):
            ch = f"&#{name};"
        if self._in_pre or self._in_code:
            self._code_buf.append(ch)
        else:
            self._result.append(ch)

    # ---- helpers ----
    def _maybe_newline(self):
        if self._result and self._result[-1] and self._result[-1][-1] != "\n":
            self._result.append("\n")

    def get_markdown(self) -> str:
        raw = "".join(self._result)
        # 清理多余空行（连续3个以上换行缩减为2个）
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_markdown(html_content: str) -> str:
    """将 LeetCode 题目 HTML 转为 Markdown 格式。"""
    if not html_content:
        return ""
    parser = _LeetCodeHTMLToMarkdown()
    try:
        parser.feed(html_content)
        return parser.get_markdown()
    except Exception:
        # 兜底：如果解析失败，回退到简单清理
        text = re.sub(r"<[^>]+>", "", html_content)
        text = text.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&nbsp;", " ").replace("&#39;", "'")
        return re.sub(r"\n\s*\n", "\n\n", text).strip()


# ============ 配置常量 ============
ADMIN_USERS: list = []


@register(__plugin_name__, __author__, __plugin_desc__, __version__)
class LeetCodePlugin(Star):
    """LeetCode 每日一题提醒插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 数据目录
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_leetcode"))
        os.makedirs(self.data_dir, exist_ok=True)

        # 配置文件路径（仅用于订阅数据等动态配置）
        self.subscription_file = os.path.join(self.data_dir, "subscription.json")
        self.personal_subscription_file = os.path.join(self.data_dir, "personal_subscription.json")

        # 保存群的 unified_msg_origin
        self.group_origins: Dict[str, str] = {}

        # 个人订阅相关数据结构
        self.user_origins: Dict[str, str] = {}  # 保存用户的 unified_msg_origin
        self.subscribed_users: list = []   # 订阅用户ID列表
        self.user_language_prefs: Dict[str, str] = {}  # 用户语言偏好设置
        self.user_push_times: Dict[str, Dict[str, int]] = {}  # 用户自定义推送时间

        # 从 AstrBotConfig 读取配置
        self.admin_users: list = [str(u) for u in _get(config, "admin_users")]
        self.default_language: str = _parse_language(_get(config, "default_language"))
        self.enable_personal_subscribe: bool = _parse_bool(_get(config, "enable_personal_subscribe"), "enable_personal_subscribe")
        self.personal_inform_hour: int = _parse_int_range(_get(config, "personal_inform_hour"), "personal_inform_hour", 0, 23)
        self.personal_inform_minute: int = _parse_int_range(_get(config, "personal_inform_minute"), "personal_inform_minute", 0, 59)
        self.group_inform_hour: int = _parse_int_range(_get(config, "group_inform_hour"), "group_inform_hour", 0, 23)
        self.group_inform_minute: int = _parse_int_range(_get(config, "group_inform_minute"), "group_inform_minute", 0, 59)
        self.check_interval_seconds: int = _parse_int_range(_get(config, "check_interval_seconds"), "check_interval_seconds", 5, 3600)
        self.enable_llm_translation: bool = _parse_bool(_get(config, "enable_llm_translation"), "enable_llm_translation")
        self.translation_provider_id: str = str(_get(config, "translation_provider_id") or "").strip()
        self.enable_image_push: bool = _parse_bool(_get(config, "enable_image_push"), "enable_image_push")


        # 加载动态订阅配置
        self._load_subscription_config()

        # 今日题目缓存
        self.today_question: Optional[Dict] = None
        self.today_date: str = ""

        # HTTP 会话
        self._session = None

        # 全局管理员列表
        global ADMIN_USERS
        ADMIN_USERS = self.admin_users.copy()

        # 文件写入锁
        self._file_lock = asyncio.Lock()

        # 异步任务
        self._monitor_task: Optional[asyncio.Task] = None

        # CronJob 管理（user_id -> job_id 映射）
        self.user_cron_jobs: Dict[str, str] = {}

        logger.info(f"LeetCode 每日一题提醒插件已加载")

    def _load_subscription_config(self):
        """加载动态订阅配置（群组订阅、个人订阅等运行时数据）"""
        # 初始化默认值
        self.subscribed_groups: list = []
        self.inform_hour: int = self.group_inform_hour
        self.inform_minute: int = self.group_inform_minute
        self.group_origins: Dict[str, str] = {}

        # 加载订阅配置
        if os.path.exists(self.subscription_file):
            try:
                with open(self.subscription_file, 'r', encoding='utf-8') as f:
                    sub_data = json.load(f)
                    if "subscribed_groups" in sub_data:
                        self.subscribed_groups = sub_data["subscribed_groups"]
                    if "group_origins" in sub_data:
                        self.group_origins = sub_data["group_origins"]
            except json.JSONDecodeError as e:
                logger.error(f"订阅配置JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载订阅配置失败: {e}")

        # 加载个人订阅配置
        self._load_personal_subscription()

        # 调试日志：打印配置
        logger.info(f"[配置加载] enable_llm_translation: {self.enable_llm_translation}")
        logger.info(f"[配置加载] translation_provider_id: '{self.translation_provider_id}'")
        logger.info(f"[配置加载] enable_image_push: {self.enable_image_push}")

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

    def _load_personal_subscription(self):
        """加载个人订阅配置"""
        if os.path.exists(self.personal_subscription_file):
            try:
                with open(self.personal_subscription_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.subscribed_users = data.get("subscribed_users", [])
                    self.user_origins = data.get("user_origins", {})
                    self.user_language_prefs = data.get("user_language_prefs", {})
                    self.user_push_times = data.get("user_push_times", {})
            except json.JSONDecodeError as e:
                logger.error(f"个人订阅配置JSON格式错误: {e}")
            except Exception as e:
                logger.error(f"加载个人订阅配置失败: {e}")

    async def _save_personal_subscription(self):
        """保存个人订阅配置"""
        async with self._file_lock:
            try:
                data = {
                    "subscribed_users": self.subscribed_users,
                    "user_origins": self.user_origins,
                    "user_language_prefs": self.user_language_prefs,
                    "user_push_times": self.user_push_times,
                }
                with open(self.personal_subscription_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存个人订阅配置失败: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户ID"""
        return str(event.get_sender_id())

    def _save_user_origin(self, event: AstrMessageEvent):
        """保存用户的统一会话标识（使用 session 对象确保 platform_id 正确）"""
        user_id = self._get_user_id(event)
        if hasattr(event, 'session') and event.session:
            # event.session 是 MessageSession 对象，str(event.session) 会输出
            # "正确的platform_id:MessageType:session_id"
            self.user_origins[user_id] = str(event.session)
        elif hasattr(event, 'unified_msg_origin'):
            self.user_origins[user_id] = event.unified_msg_origin

    def _get_user_push_time(self, user_id: str) -> tuple:
        """获取用户的推送时间，优先使用自定义时间，否则用配置默认值。返回 (hour, minute)"""
        if user_id in self.user_push_times:
            pt = self.user_push_times[user_id]
            return pt.get("hour", self.personal_inform_hour), pt.get("minute", self.personal_inform_minute)
        return self.personal_inform_hour, self.personal_inform_minute

    def _format_push_time(self, user_id: str) -> str:
        """格式化用户的推送时间显示"""
        h, m = self._get_user_push_time(user_id)
        is_custom = user_id in self.user_push_times
        custom_tag = "（自定义）" if is_custom else "（默认）"
        return f"{h:02d}:{m:02d} {custom_tag}"

    async def _register_cron_for_user(self, user_id: str, umo: str) -> bool:
        """为指定用户注册 CronJob（Basic 模式）"""
        if not self.enable_personal_subscribe:
            return False

        # 如果已存在，先取消旧的
        if user_id in self.user_cron_jobs:
            await self._unregister_cron_for_user(user_id)

        hour, minute = self._get_user_push_time(user_id)
        cron_expression = f"{minute} {hour} * * *"

        async def handler(**kwargs):
            """定时回调：获取题目并发送给订阅者（与测试推送走同一条路径）"""
            try:
                user_id = kwargs.get("user_id")
                question = await self._fetch_daily_question()
                if question:
                    text = self._build_question_message(question, self._get_user_language(user_id))
                    sent = await self._send_private_message(user_id, text, use_image=self.enable_image_push)
                    if sent:
                        logger.info(f"[CronJob] LeetCode每日一题已推送到用户 {user_id}")
                    else:
                        logger.error(f"[CronJob] 推送失败 user={user_id}")
            except Exception as e:
                logger.error(f"[CronJob] 推送异常 user={kwargs.get('user_id')}: {e}")

        try:
            job = await self.context.cron_manager.add_basic_job(
                name=f"lc_personal_{user_id}",
                cron_expression=cron_expression,
                handler=handler,
                description=f"LeetCode每日一题个人订阅: {user_id}",
                timezone="Asia/Shanghai",
                payload={"umo": umo, "user_id": user_id},
                enabled=True,
                persistent=False,  # 重启后由插件重新注册
            )
            self.user_cron_jobs[user_id] = job.job_id
            logger.info(f"[CronJob] 已为用户 {user_id} 注册定时任务，执行时间: {cron_expression}")
            return True
        except Exception as e:
            logger.error(f"[CronJob] 注册用户 {user_id} 的定时任务失败: {e}")
            return False

    async def _unregister_cron_for_user(self, user_id: str):
        """取消指定用户的 CronJob"""
        job_id = self.user_cron_jobs.get(user_id)
        if job_id:
            try:
                await self.context.cron_manager.delete_job(job_id)
                del self.user_cron_jobs[user_id]
                logger.info(f"[CronJob] 已取消用户 {user_id} 的定时任务")
            except Exception as e:
                logger.error(f"[CronJob] 取消用户 {user_id} 的定时任务失败: {e}")

    async def _restore_all_personal_cron_jobs(self):
        """插件启动时恢复所有个人订阅的 CronJob"""
        if not self.enable_personal_subscribe:
            return

        logger.info(f"[CronJob] 开始恢复 {len(self.subscribed_users)} 个个人订阅的定时任务...")
        restored_count = 0

        for user_id in self.subscribed_users:
            umo = self.user_origins.get(user_id)
            if umo:
                if await self._register_cron_for_user(user_id, umo):
                    restored_count += 1
                await asyncio.sleep(0.1)  # 避免过快注册
            else:
                logger.warning(f"[CronJob] 用户 {user_id} 缺少 UMO，跳过恢复")

        logger.info(f"[CronJob] 成功恢复 {restored_count} 个个人订阅定时任务")

    def _get_session_for_user(self, user_id: str) -> str:
        """获取用户的会话标识"""
        if user_id in self.user_origins:
            return self.user_origins[user_id]
        return user_id

    def _get_user_language(self, user_id: str) -> str:
        """获取用户的语言偏好，如果没有设置则使用默认语言"""
        return self.user_language_prefs.get(user_id, self.default_language)

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
        self._monitor_task = asyncio.create_task(self._async_monitor())
        # 恢复个人订阅的 CronJob
        await self._restore_all_personal_cron_jobs()

    async def terminate(self):
        """插件卸载时清理资源"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        # 清理所有个人订阅的 CronJob
        for user_id in list(self.user_cron_jobs.keys()):
            await self._unregister_cron_for_user(user_id)

    async def _async_monitor(self):
        """异步监控任务（仅用于群组订阅）"""
        logger.info("LeetCode 每日一题监控任务已启动")
        last_inform_date = ""      # 群组推送日期记录

        try:
            while True:
                try:
                    now = datetime.now()
                    today_date = now.strftime("%Y-%m-%d")

                    target_time = now.replace(
                        hour=self.inform_hour,
                        minute=self.inform_minute,
                        second=0,
                        microsecond=0
                    )

                    # 到达群组推送时间后，当天未推送过就推送，避免启动延迟错过整分钟
                    if now >= target_time and today_date != last_inform_date:

                        logger.info(f"开始获取 LeetCode 每日一题(群组): {today_date}")
                        question = await self._fetch_daily_question()
                        if question:
                            self.today_question = question
                            self.today_date = today_date
                            await self._send_question_to_subscribers(question)
                            last_inform_date = today_date
                            logger.info(f"LeetCode 每日一题已推送到群组: {question.get('title', '未知')}")

                except Exception as e:
                    logger.error(f"LeetCode 监控任务出错: {e}")

                await asyncio.sleep(self.check_interval_seconds)

        except asyncio.CancelledError:
            logger.info("LeetCode 每日一题监控任务已停止")

    async def _fetch_daily_question(self, umo: str = None) -> Optional[Dict]:
        """获取 LeetCode 每日一题，优先使用 LeetCode CN 官方 GraphQL。"""
        import urllib.request
        import urllib.error
        import ssl

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        loop = asyncio.get_event_loop()

        def http_request(url, post_data=None, headers=None):
            request_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            if headers:
                request_headers.update(headers)

            data = None
            if post_data is not None:
                data = json.dumps(post_data).encode('utf-8')
                request_headers.setdefault('Content-Type', 'application/json')

            req = urllib.request.Request(url, data=data, headers=request_headers)
            with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                return response.read().decode('utf-8')

        async def fetch_with_retry(url, max_retries=3, post_data=None, headers=None, log_prefix="[每日一题]"):
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await loop.run_in_executor(
                        None,
                        lambda u=url, p=post_data, h=headers: http_request(u, p, h)
                    )
                except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                    last_error = e
                    logger.warning(f"{log_prefix} 第 {attempt}/{max_retries} 次请求失败: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(1)
            logger.error(f"{log_prefix} 获取失败（已重试 {max_retries} 次）: {last_error}", exc_info=True)
            return None

        def parse_ac_rate(value) -> float:
            if value in (None, ""):
                return 0
            if isinstance(value, str):
                value = value.strip()
                if value.endswith("%"):
                    value = value[:-1]
            try:
                number = float(value)
            except (TypeError, ValueError):
                return 0
            return number / 100.0 if number > 1 else number

        async def fetch_from_leetcode_cn() -> Optional[Dict]:
            url = "https://leetcode.cn/graphql/"
            payload = {
                "operationName": "questionOfToday",
                "variables": {},
                "query": """
                query questionOfToday {
                    todayRecord {
                        date
                        question {
                            questionId
                            questionFrontendId
                            questionTitle
                            questionTitleSlug
                            translatedTitle
                            content
                            translatedContent
                            difficulty
                            topicTags {
                                name
                                slug
                                translatedName
                            }
                            stats
                        }
                    }
                }
                """
            }
            headers = {
                'Referer': 'https://leetcode.cn/problemset/'
            }

            logger.info(f"[每日一题] 开始获取 LeetCode CN 官方数据，URL: {url}, umo: {umo}")
            response_text = await fetch_with_retry(
                url,
                post_data=payload,
                headers=headers,
                log_prefix="[每日一题] LeetCode CN"
            )
            if not response_text:
                return None

            try:
                logger.info(f"[每日一题] LeetCode CN 原始响应: {response_text[:500]}...")
                data = json.loads(response_text)
                records = data.get("data", {}).get("todayRecord") or []
                if not records:
                    logger.warning("[每日一题] LeetCode CN 未返回 todayRecord")
                    return None

                record = records[0]
                question = record.get("question") or {}
                title_slug = question.get("questionTitleSlug") or ""
                title = question.get("questionTitle") or ""
                title_cn = question.get("translatedTitle") or title
                content_html = question.get("content") or ""
                content_cn = question.get("translatedContent") or ""

                stats = {}
                stats_raw = question.get("stats")
                if isinstance(stats_raw, str) and stats_raw:
                    try:
                        stats = json.loads(stats_raw)
                    except json.JSONDecodeError:
                        logger.warning(f"[每日一题] LeetCode CN stats 解析失败: {stats_raw[:100]}")
                elif isinstance(stats_raw, dict):
                    stats = stats_raw

                result = {
                    "date": record.get("date"),
                    "title": title,
                    "titleCn": title_cn,
                    "titleSlug": title_slug,
                    "frontendQuestionId": question.get("questionFrontendId") or question.get("questionId"),
                    "difficulty": question.get("difficulty"),
                    "acRate": parse_ac_rate(stats.get("acRate")),
                    "link": f"https://leetcode.com/problems/{title_slug}/" if title_slug else "",
                    "topicTags": question.get("topicTags", []),
                    "content": content_html,
                    "contentCn": content_cn,
                    "contentCnFailed": not bool(content_cn)
                }

                logger.info(
                    f"[每日一题] LeetCode CN 获取成功 - 标题: {title_cn or title}, "
                    f"content长度: {len(content_html)}, contentCn长度: {len(content_cn)}"
                )
                return result
            except Exception as e:
                logger.error(f"[每日一题] 解析 LeetCode CN 每日一题失败: {e}", exc_info=True)
                return None

        async def fetch_from_legacy_api() -> Optional[Dict]:
            url = "https://leetcode-api-pied.vercel.app/daily"
            logger.info(f"[每日一题] 开始获取旧版备用接口，URL: {url}, umo: {umo}")

            response_text = await fetch_with_retry(url, log_prefix="[每日一题] 备用接口")
            if not response_text:
                return None

            try:
                logger.info(f"[每日一题] 备用接口原始响应: {response_text[:500]}...")

                data = json.loads(response_text)
                question = data.get("question", {})
                link = data.get("link", "")
                title_slug = question.get("titleSlug")

                logger.info(f"[每日一题] 备用接口解析数据 - titleSlug: {title_slug}, link: {link}")

                title = question.get("title", "")
                content_html = question.get("content", "")
                title_cn = ""
                content_cn = ""
                content_cn_failed = False

                if self.enable_llm_translation and title_slug:
                    logger.info(f"[每日一题] 备用接口准备使用大模型翻译，title_slug: {title_slug}, umo: {umo}")
                    try:
                        title_cn, content_cn, translation_success = await self._fetch_chinese_content(
                            title_slug, title, content_html, umo=umo
                        )
                        if not translation_success:
                            content_cn_failed = True
                            logger.warning("[每日一题] 备用接口大模型翻译未完全成功")
                    except Exception as e:
                        logger.warning(f"[每日一题] 备用接口大模型翻译失败: {e}")
                        content_cn_failed = True
                else:
                    logger.info("[每日一题] 备用接口大模型翻译已禁用或无 title_slug")
                    content_cn_failed = True

                if not title_cn:
                    title_cn = title

                result = {
                    "date": data.get("date"),
                    "title": title,
                    "titleCn": title_cn,
                    "titleSlug": title_slug,
                    "frontendQuestionId": question.get("questionFrontendId"),
                    "difficulty": question.get("difficulty"),
                    "acRate": parse_ac_rate(question.get("acRate")),
                    "link": f"https://leetcode.com{link}" if link.startswith("/") else link,
                    "topicTags": question.get("topicTags", []),
                    "content": content_html,
                    "contentCn": content_cn,
                    "contentCnFailed": content_cn_failed
                }

                logger.info(
                    f"[每日一题] 备用接口获取成功 - 标题: {title_cn or title}, "
                    f"content长度: {len(content_html) if content_html else 0}, "
                    f"contentCn长度: {len(content_cn) if content_cn else 0}, 失败: {content_cn_failed}"
                )
                return result
            except Exception as e:
                logger.error(f"[每日一题] 解析备用接口每日一题失败: {e}", exc_info=True)
                return None

        question = await fetch_from_leetcode_cn()
        if question:
            return question

        logger.warning("[每日一题] LeetCode CN 获取失败，尝试旧版备用接口")
        return await fetch_from_legacy_api()

    def _parse_ac_rate_value(self, value) -> float:
        """解析 LeetCode 返回的通过率，统一为 0-1 的小数。"""
        if value in (None, ""):
            return 0
        if isinstance(value, str):
            value = value.strip()
            if value.endswith("%"):
                value = value[:-1]
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        return number / 100.0 if number > 1 else number

    def _build_cn_question_result(self, question: Dict, date: str = "") -> Dict:
        """将 LeetCode CN GraphQL 的 question 对象转换为插件统一题目结构。"""
        title_slug = question.get("questionTitleSlug") or question.get("titleSlug") or ""
        title = question.get("questionTitle") or question.get("title") or ""
        title_cn = question.get("translatedTitle") or question.get("titleCn") or title
        content_html = question.get("content") or ""
        content_cn = question.get("translatedContent") or ""

        stats = {}
        stats_raw = question.get("stats")
        if isinstance(stats_raw, str) and stats_raw:
            try:
                stats = json.loads(stats_raw)
            except json.JSONDecodeError:
                logger.warning(f"[LeetCode CN] stats 解析失败: {stats_raw[:100]}")
        elif isinstance(stats_raw, dict):
            stats = stats_raw

        return {
            "date": date,
            "title": title,
            "titleCn": title_cn,
            "titleSlug": title_slug,
            "frontendQuestionId": question.get("questionFrontendId") or question.get("frontendQuestionId") or question.get("questionId"),
            "difficulty": question.get("difficulty"),
            "acRate": self._parse_ac_rate_value(stats.get("acRate") or question.get("acRate")),
            "link": f"https://leetcode.com/problems/{title_slug}/" if title_slug else "",
            "topicTags": question.get("topicTags", []),
            "content": content_html,
            "contentCn": content_cn,
            "contentCnFailed": not bool(content_cn)
        }

    async def _fetch_random_question(self) -> Optional[Dict]:
        """随机获取一道 LeetCode 题目，不使用每日一题缓存。"""
        import random
        import urllib.request
        import urllib.error
        import ssl

        url = "https://leetcode.cn/graphql/"
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        loop = asyncio.get_event_loop()

        def http_post(payload, referer="https://leetcode.cn/problemset/"):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json",
                    "Referer": referer,
                }
            )
            with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                return response.read().decode("utf-8")

        async def post_with_retry(payload, referer="https://leetcode.cn/problemset/", log_prefix="[随机题目]"):
            last_error = None
            for attempt in range(1, 4):
                try:
                    return await loop.run_in_executor(None, lambda: http_post(payload, referer))
                except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                    last_error = e
                    logger.warning(f"{log_prefix} 第 {attempt}/3 次请求失败: {e}")
                    if attempt < 3:
                        await asyncio.sleep(1)
            logger.error(f"{log_prefix} 请求失败（已重试 3 次）: {last_error}", exc_info=True)
            return None

        list_query = """
        query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
            problemsetQuestionList(categorySlug: $categorySlug, limit: $limit, skip: $skip, filters: $filters) {
                total
                questions {
                    frontendQuestionId
                    title
                    titleCn
                    titleSlug
                    difficulty
                    acRate
                    paidOnly
                    topicTags {
                        name
                        slug
                        nameTranslated
                    }
                }
            }
        }
        """

        detail_query = """
        query questionData($titleSlug: String!) {
            question(titleSlug: $titleSlug) {
                questionId
                questionFrontendId
                questionTitle
                questionTitleSlug
                translatedTitle
                content
                translatedContent
                difficulty
                topicTags {
                    name
                    slug
                    translatedName
                }
                stats
            }
        }
        """

        first_payload = {
            "operationName": "problemsetQuestionList",
            "variables": {"categorySlug": "", "skip": 0, "limit": 1, "filters": {}},
            "query": list_query,
        }
        first_text = await post_with_retry(first_payload, log_prefix="[随机题目] 获取题库总数")
        if not first_text:
            return None

        try:
            first_data = json.loads(first_text)
            total = int(first_data.get("data", {}).get("problemsetQuestionList", {}).get("total") or 0)
        except Exception as e:
            logger.error(f"[随机题目] 解析题库总数失败: {e}", exc_info=True)
            return None

        if total <= 0:
            logger.error("[随机题目] 题库总数为空")
            return None

        for attempt in range(1, 6):
            skip = random.randint(0, total - 1)
            list_payload = {
                "operationName": "problemsetQuestionList",
                "variables": {"categorySlug": "", "skip": skip, "limit": 1, "filters": {}},
                "query": list_query,
            }
            list_text = await post_with_retry(list_payload, log_prefix=f"[随机题目] 抽取题目 {attempt}/5")
            if not list_text:
                continue

            try:
                list_data = json.loads(list_text)
                questions = list_data.get("data", {}).get("problemsetQuestionList", {}).get("questions") or []
                if not questions:
                    logger.warning(f"[随机题目] skip={skip} 未返回题目")
                    continue

                light_question = questions[0]
                if light_question.get("paidOnly"):
                    logger.info(f"[随机题目] 抽到会员题，跳过: {light_question.get('titleSlug')}")
                    continue

                title_slug = light_question.get("titleSlug")
                if not title_slug:
                    logger.warning(f"[随机题目] 未获取到 titleSlug: {light_question}")
                    continue

                detail_payload = {
                    "operationName": "questionData",
                    "variables": {"titleSlug": title_slug},
                    "query": detail_query,
                }
                detail_text = await post_with_retry(
                    detail_payload,
                    referer=f"https://leetcode.cn/problems/{title_slug}/",
                    log_prefix=f"[随机题目] 获取详情 {title_slug}"
                )
                if not detail_text:
                    continue

                detail_data = json.loads(detail_text)
                question = detail_data.get("data", {}).get("question") or {}
                if not question:
                    logger.warning(f"[随机题目] 详情为空: {title_slug}")
                    continue

                result = self._build_cn_question_result(question, date="随机题目")
                logger.info(f"[随机题目] 获取成功: {result.get('frontendQuestionId')}. {result.get('titleCn') or result.get('title')}")
                return result
            except Exception as e:
                logger.warning(f"[随机题目] 第 {attempt}/5 次解析失败: {e}", exc_info=True)

        logger.error("[随机题目] 多次抽取均失败")
        return None

    async def _translate_with_llm(self, title: str, content: str, is_title: bool = False, umo: str = None) -> str:
        """使用大模型API翻译题目内容
        
        Args:
            title: 英文标题
            content: 英文内容（HTML格式）
            is_title: 是否只翻译标题
            umo: 统一消息来源标识，用于获取当前会话的LLM提供商
            
        Returns:
            中文翻译结果
        """
        if not self.enable_llm_translation:
            logger.info("[LLM翻译] 大模型翻译已禁用，跳过翻译")
            return ""
        
        try:
            logger.info(f"[LLM翻译] 开始翻译{'标题' if is_title else '内容'}: {title[:50] if title else 'Unknown'}...")
            
            # 清理HTML内容
            clean_content = html_to_markdown(content) if content else ""
            
            if is_title:
                # 只翻译标题
                prompt = f"""请将以下LeetCode题目标题翻译成简洁的中文，只返回翻译后的标题，不要有任何解释：

英文标题: {title}

中文标题:"""
            else:
                # 翻译完整内容
                prompt = f"""请将以下LeetCode题目内容翻译成中文。要求：
1. 保持专业术语准确（如 array 译为数组，tree 译为树）
2. 保持代码和数学公式不变
3. 保持格式清晰，使用Markdown格式
4. 只返回翻译后的内容，不要添加额外说明

题目: {title}

内容:
{clean_content[:8000]}

中文翻译:"""

            # 调用LLM - 优先级: 1)配置的翻译提供商 2)当前会话提供商 3)全局默认提供商
            logger.info(f"[LLM翻译] 优先级判断 - self.translation_provider_id: '{self.translation_provider_id}', umo: '{umo}'")
            if self.translation_provider_id:
                # 优先使用配置的专用翻译提供商
                provider_id = self.translation_provider_id
                logger.info(f"[LLM翻译] 使用配置的翻译提供商: {provider_id}")
            elif umo:
                # 通过会话获取当前provider
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                logger.info(f"[LLM翻译] 使用当前会话的LLM提供商: {provider_id}")
            else:
                # 兜底：使用全局默认提供商
                prov = self.context.get_using_provider(umo=None)
                if prov:
                    provider_id = prov.meta().id
                    logger.info(f"[LLM翻译] 使用全局默认LLM提供商: {provider_id}")
                else:
                    logger.warning("[LLM翻译] 未找到可用的LLM提供商，跳过LLM翻译")
                    return ""
            
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            
            translated_text = llm_resp.completion_text.strip()
            logger.info(f"[LLM翻译] 翻译完成，结果长度: {len(translated_text)}")
            
            return translated_text
            
        except Exception as e:
            logger.error(f"[LLM翻译] 翻译失败: {e}")
            return ""

    async def _translate_title_with_llm(self, title: str, umo: str = None) -> str:
        """使用大模型翻译标题"""
        return await self._translate_with_llm(title, "", is_title=True, umo=umo)

    async def _fetch_chinese_content(self, title_slug: str, title: str = "", content: str = "", umo: str = None) -> tuple:
        """获取中文题目内容 - 使用大模型API翻译
        
        Args:
            title_slug: 题目slug
            title: 英文标题（用于翻译）
            content: 英文内容（用于翻译）
            umo: 统一消息来源标识，用于获取当前会话的LLM提供商
            
        Returns:
            tuple: (中文标题, 中文内容, 是否翻译成功)
        """
        logger.info(f"[中文内容] 使用大模型翻译，title_slug: {title_slug}, umo: {umo}")
        
        translated_title = ""
        translated_content = ""
        translation_success = False
        
        try:
            # 翻译标题
            if title:
                translated_title = await self._translate_title_with_llm(title, umo=umo)
                if translated_title:
                    logger.info(f"[中文内容] 标题翻译成功: {translated_title[:50]}...")
            
            # 翻译内容
            if content:
                translated_content = await self._translate_with_llm(title, content, is_title=False, umo=umo)
                if translated_content:
                    logger.info(f"[中文内容] 内容翻译成功，长度: {len(translated_content)}")
                    translation_success = True
            
            return translated_title, translated_content, translation_success
            
        except Exception as e:
            logger.warning(f"[中文内容] 大模型翻译失败: {e}")
            return "", "", False
            return ""

    async def _fetch_question_by_id(self, question_id: str, umo: str = None) -> Optional[Dict]:
        """根据题目号获取 LeetCode 题目详情
        
        Args:
            question_id: 题目编号
            umo: 统一消息来源标识，用于获取当前会话的LLM提供商（翻译用）
        """
        import urllib.request
        import urllib.error
        import ssl

        # 创建SSL上下文，忽略证书验证
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # 使用线程池执行同步请求
        loop = asyncio.get_event_loop()

        def http_get(url, post_data=None):
            if post_data:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(post_data).encode('utf-8'),
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Content-Type': 'application/json',
                        'Referer': 'https://leetcode.com/'
                    }
                )
            else:
                req = urllib.request.Request(
                    url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                )
            with urllib.request.urlopen(req, context=ssl_context, timeout=30) as response:
                return response.read().decode('utf-8')

        async def fetch_with_retry(url, max_retries=3, post_data=None, log_prefix="[题目查询]"):
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    response_text = await loop.run_in_executor(None, lambda u=url, p=post_data: http_get(u, p))
                    return response_text
                except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                    last_error = e
                    logger.warning(f"{log_prefix} 第 {attempt}/{max_retries} 次请求失败: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(1)
            logger.error(f"{log_prefix} 获取失败（已重试 {max_retries} 次）: {last_error}", exc_info=True)
            return None

        # 第一步：使用 lcid.cc API 获取题目元数据（标题、slug、难度等）
        meta_url = f"https://lcid.cc/info/{question_id}"
        logger.info(f"正在获取题目 {question_id}: {meta_url}")

        meta_text = await fetch_with_retry(meta_url, log_prefix="[题目查询]")
        if not meta_text:
            return None

        try:
            logger.info(f"题目 {question_id} 响应: {meta_text[:200]}")
            data = json.loads(meta_text)

            title = data.get("title", "")
            slug = data.get("titleSlug", "")

            if not slug:
                logger.error(f"题目 {question_id} 未获取到 titleSlug")
                return None

            logger.info(f"[题目查询] 标题: {title}, slug: {slug}")

            # 第二步：使用 LeetCode GraphQL API 获取完整题干和标签
            content_en = ""
            topic_tags = []

            graphql_url = "https://leetcode.com/graphql"
            graphql_query = {
                "query": """
                query questionData($titleSlug: String!) {
                    question(titleSlug: $titleSlug) {
                        content
                        topicTags {
                            name
                            nameTranslated: translatedName
                            slug
                        }
                    }
                }
                """,
                "variables": {"titleSlug": slug}
            }

            logger.info(f"[题目查询] 通过 GraphQL 获取题干，slug: {slug}")
            gql_text = await fetch_with_retry(graphql_url, post_data=graphql_query, log_prefix="[题目查询] GraphQL")

            if gql_text:
                try:
                    gql_data = json.loads(gql_text)
                    q_data = gql_data.get("data", {}).get("question", {})
                    if q_data:
                        content_en = q_data.get("content", "")
                        topic_tags = q_data.get("topicTags", [])
                        logger.info(f"[题目查询] GraphQL 获取成功，内容长度: {len(content_en) if content_en else 0}, 标签数: {len(topic_tags)}")
                    else:
                        logger.warning(f"[题目查询] GraphQL 返回的 question 为空")
                except Exception as e:
                    logger.warning(f"[题目查询] 解析 GraphQL 响应失败: {e}")
            else:
                logger.warning(f"[题目查询] GraphQL 请求失败，题干将为空")

            # 使用大模型翻译获取中文内容
            title_cn = ""
            content_cn = ""
            content_cn_failed = False

            if self.enable_llm_translation and title:
                logger.info(f"[题目查询] 准备使用大模型翻译，umo: {umo}")
                try:
                    if content_en:
                        title_cn, content_cn, translation_success = await self._fetch_chinese_content(
                            slug, title, content_en, umo=umo
                        )
                        if not translation_success:
                            content_cn_failed = True
                            logger.warning(f"[题目查询] 大模型翻译未完全成功")
                    else:
                        title_cn = await self._translate_title_with_llm(title, umo=umo)
                        if title_cn:
                            logger.info(f"[题目查询] 标题翻译成功: {title_cn}")
                        else:
                            content_cn_failed = True
                            logger.warning(f"[题目查询] 标题翻译失败")
                except Exception as e:
                    logger.warning(f"[题目查询] 大模型翻译失败: {e}")
                    content_cn_failed = True
            else:
                logger.info(f"[题目查询] 大模型翻译已禁用或无标题")
                content_cn_failed = True

            if not title_cn:
                title_cn = title

            # 构建结果
            result = {
                "date": "",
                "title": title,
                "titleCn": title_cn,
                "titleSlug": slug,
                "frontendQuestionId": str(data.get("id", question_id)),
                "difficulty": data.get("difficulty", ""),
                "acRate": data.get("acRate", 0) / 100.0 if data.get("acRate") else 0,
                "link": f"https://leetcode.com/problems/{slug}/",
                "topicTags": topic_tags,
                "content": content_en,
                "contentCn": content_cn,
                "contentCnFailed": content_cn_failed
            }

            logger.info(f"成功获取题目 {question_id}: {title_cn or title}")
            return result
        except Exception as e:
            logger.error(f"获取题目 {question_id} 失败: {e}", exc_info=True)

        return None

    def _build_question_message(self, question: Dict, language: str = "zh") -> str:
        """构建题目消息，支持多语言显示
        
        Args:
            question: 题目数据字典
            language: 语言选项 - "zh"(中文), "en"(英文), "both"(双语)
        """
        logger.info(f"[构建消息] 开始构建，language: {language}, question.keys: {list(question.keys())}")

        result_text = f"📅 {question.get('date', '')}\n"

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

        # 根据语言选择决定链接域名：zh使用leetcode.cn，en和both使用leetcode.com
        if language == "zh" and link and "leetcode.com" in link:
            link = link.replace("leetcode.com", "leetcode.cn")
        
        tags = []
        for tag in question.get("topicTags", []):
            if isinstance(tag, dict):
                tag_name = tag.get("translatedName") or tag.get("nameTranslated") or tag.get("name", "")
                if tag_name:
                    tags.append(tag_name)
            else:
                tags.append(str(tag))

        # 根据语言设置构建标题
        if language == "zh":
            # 仅中文
            result_text += f"{emoji} 【{qid}. {title_cn}】\n"
        elif language == "en":
            # 仅英文
            result_text += f"{emoji} 【{qid}. {title}】\n"
        else:
            # 双语模式
            result_text += f"{emoji} 【{qid}. {title_cn}】\n"
            if title_cn != title:
                result_text += f"English: {title}\n"

        result_text += f"难度: {difficulty_cn_text}\n"
        if ac_rate:
            result_text += f"通过率: {ac_rate * 100:.1f}%\n"
        if tags:
            result_text += f"标签: {', '.join(tags)}\n"
        result_text += f"🔗 链接: {link}\n"

        # 添加完整题目内容
        content = question.get("content", "")
        content_cn = question.get("contentCn", "")  # 中文内容
        content_cn_failed = question.get("contentCnFailed", False)  # 中文内容获取是否失败

        logger.info(f"[构建消息] 内容处理 - content长度: {len(content) if content else 0}, contentCn长度: {len(content_cn) if content_cn else 0}, 失败: {content_cn_failed}, language: {language}")

        if content or content_cn:
            clean_content_en = html_to_markdown(content) if content else ""
            clean_content_cn = html_to_markdown(content_cn) if content_cn else ""
            
            # 根据语言设置显示内容
            if language == "zh":
                logger.info(f"[构建消息] 语言=zh，clean_content_cn长度: {len(clean_content_cn)}, clean_content_en长度: {len(clean_content_en)}")
                if clean_content_cn:
                    # 仅中文
                    logger.info("[构建消息] 使用中文内容")
                    result_text += f"\n📝 题目描述:\n"
                    display_content = clean_content_cn
                    # 分段发送，避免消息过长
                    max_length = 3000
                    if len(display_content) > max_length:
                        result_text += display_content[:max_length] + "\n\n... (内容已截断，请访问链接查看完整题目)"
                    else:
                        result_text += display_content
                elif clean_content_en:
                    # 中文内容获取失败，显示提示和英文内容
                    logger.info("[构建消息] 中文内容为空，使用英文内容")
                    if content_cn_failed:
                        result_text += f"\n⚠️ 中文内容获取失败，已切换为英文显示\n"
                    result_text += f"\n📝 Description:\n"
                    display_content = clean_content_en
                    max_length = 3000
                    if len(display_content) > max_length:
                        result_text += display_content[:max_length] + "\n\n... (内容已截断，请访问链接查看完整题目)"
                    else:
                        result_text += display_content
                    
            elif language == "en" and clean_content_en:
                # 仅英文
                result_text += f"\n📝 题目描述:\n"
                display_content = clean_content_en
                max_length = 3000
                if len(display_content) > max_length:
                    result_text += display_content[:max_length] + "\n\n... (内容已截断，请访问链接查看完整题目)"
                else:
                    result_text += display_content
                    
            elif language == "both":
                # 双语模式 - 同时显示中英文
                # 中文部分
                if clean_content_cn:
                    result_text += f"\n📝 题目描述 (中文):\n"
                    max_length = 1500
                    if len(clean_content_cn) > max_length:
                        result_text += clean_content_cn[:max_length] + "\n\n... (内容已截断)"
                    else:
                        result_text += clean_content_cn
                elif content_cn_failed:
                    # 中文内容获取失败提示
                    result_text += f"\n⚠️ 中文内容获取失败\n"
                
                # 英文部分
                if clean_content_en:
                    result_text += f"\n\n📝 Description (English):\n"
                    max_length = 1500
                    if len(clean_content_en) > max_length:
                        result_text += clean_content_en[:max_length] + "\n\n... (content truncated)"
                    else:
                        result_text += clean_content_en
            else:
                # 默认显示可用内容
                result_text += f"\n📝 题目描述:\n"
                display_content = clean_content_cn or clean_content_en
                max_length = 3000
                if len(display_content) > max_length:
                    result_text += display_content[:max_length] + "\n\n... (内容已截断，请访问链接查看完整题目)"
                else:
                    result_text += display_content

        return result_text




    async def _send_question_to_subscribers(self, question: Dict):
        """发送题目到所有群组订阅者"""
        text = self._build_question_message(question, self.default_language)
        image_chain = None
        if self.enable_image_push:
            image_chain = await self._text_to_image_chain(text)

        for group_id in self.subscribed_groups:
            try:
                message = image_chain if image_chain else text
                await self.context.send_message(self._get_session_for_group(group_id), message)
                logger.info(f"LeetCode 每日一题已发送到群 {group_id}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送题目到群 {group_id} 失败: {e}")

    def _get_platforms(self) -> list:
        """获取可用的平台实例列表，返回 [(platform, platform_name), ...]。
        
        直接遍历 platform_manager.platform_insts，通过 meta().name 匹配，
        不依赖已废弃的 get_platform() 方法。
        """
        results = []
        target_names = {"aiocqhttp", "qq_official"}
        try:
            for platform in self.context.platform_manager.platform_insts:
                name = platform.meta().name
                if name in target_names:
                    results.append((platform, name))
        except Exception:
            pass
        return results

    def _is_qq_number(self, user_id: str) -> bool:
        """判断是否为纯数字QQ号（aiocqhttp格式）"""
        return user_id.isdigit()

    def _is_openid(self, user_id: str) -> bool:
        """判断是否为openid（QQ官方API格式，如 264EBBDDA1378C38708B398008FA66F3）"""
        return len(user_id) == 32 and all(c in '0123456789ABCDEFabcdef' for c in user_id)

    async def _send_private_message(self, user_id: str, text: str, use_image: bool = False) -> bool:
        """发送私信给用户，支持多平台适配。

        Args:
            user_id: 用户ID
            text: 要发送的文本内容
            use_image: 是否使用文转图发送（默认纯文本）

        策略（按优先级）：
        1. aiocqhttp: 使用 bot.send_private_msg 直接发送
        2. qq_official: 使用 post_c2c_message API 直接发私聊（绕过 send_by_session 的 msg_id 检查）
        3. 通用兜底: 通过 context.send_message(umo) 发送，兼容飞书、Telegram、Discord 等所有平台

        Returns:
            bool: 是否发送成功
        """
        import random

        sent = False
        sent_as_image = False
        is_qq_num = self._is_qq_number(user_id)
        is_openid = self._is_openid(user_id)
        umo = self.user_origins.get(user_id)

        # 如果启用文转图，先尝试渲染图片
        image_chain = None
        image_file = None
        if use_image:
            try:
                chain = await self._text_to_image_chain(text)
                from astrbot.core.message.components import Plain
                first = chain.chain[0]
                if not isinstance(first, Plain):
                    image_chain = chain
                    image_file = getattr(chain, "_leetcode_image_path", None)
                    if not image_file:
                        image_file = getattr(first, "file", None) or getattr(first, "path", None)
                    if isinstance(image_file, str) and image_file.startswith("file:"):
                        image_file = image_file[5:]
            except Exception as e:
                logger.warning(f"[私信发送] 文转图失败，回退纯文本: {e}")

        logger.info(
            f"[私信发送] 开始发送 user={user_id}, "
            f"类型={'QQ号' if is_qq_num else 'openid' if is_openid else '其他平台'}, "
            f"文转图={'是' if use_image else '否'}, 图片={'已生成' if image_chain else '未生成'}"
        )

        # 开启图片推送时优先使用 AstrBot 框架发送 MessageChain，和 /lc今日 的发送方式保持一致。
        if use_image and image_chain and umo:
            try:
                sent = await self.context.send_message(umo, image_chain)
                if sent:
                    logger.info(f"[私信发送] 框架图片发送成功 user={user_id}")
                    setattr(self, "_last_private_send_as_image", True)
                    return True
                logger.warning(f"[私信发送] 框架图片发送未成功，尝试底层API兜底 user={user_id}")
            except Exception as e:
                logger.warning(f"[私信发送] 框架图片发送失败，尝试底层API兜底 user={user_id}: {e}")

        # 策略1 & 2: QQ 系平台使用底层 API（绕过框架高层 API 的限制）
        if is_qq_num or is_openid:
            platforms = self._get_platforms()
            for platform, platform_name in platforms:
                try:
                    if platform_name == "aiocqhttp" and is_qq_num:
                        bot = getattr(platform, 'bot', None)
                        if not bot:
                            logger.debug(f"[私信发送] aiocqhttp 平台缺少 bot 实例")
                            continue
                        if image_file:
                            msg_list = [{"type": "image", "data": {"file": image_file}}]
                        else:
                            msg_list = [{"type": "text", "data": {"text": text}}]
                        await bot.send_private_msg(user_id=int(user_id), message=msg_list)
                        sent = True
                        sent_as_image = bool(image_file)
                        logger.info(f"[私信发送] aiocqhttp发送成功 user_id={user_id}")
                        break

                    elif platform_name == "qq_official" and is_openid:
                        client = getattr(platform, 'client', None)
                        if not client:
                            logger.debug(f"[私信发送] qq_official 平台缺少 client 实例")
                            continue
                        from botpy.http import Route
                        route = Route("POST", "/v2/users/{openid}/messages", openid=user_id)
                        if image_file:
                            # QQ官方API图片消息需要先上传图片获取file_info
                            payload = {
                                "content": json.dumps({"file_info": image_file}),
                                "msg_type": 7,
                                "media": {"file_info": image_file},
                                "msg_seq": random.randint(1, 10000),
                            }
                        else:
                            from botpy.types.message import MarkdownPayload
                            payload = {
                                "markdown": MarkdownPayload(content=text) if text else None,
                                "msg_type": 2,
                                "msg_seq": random.randint(1, 10000),
                            }
                        result = await client.api._http.request(route, json=payload)
                        if result is None:
                            logger.warning(f"[私信发送] qq_official API 返回 None user={user_id}")
                            continue
                        # markdown 消息发送失败时降级为纯文本
                        if isinstance(result, dict) and "不允许发送原生 markdown" in str(result):
                            logger.info(f"[私信发送] qq_official markdown不允许，降级为纯文本 user={user_id}")
                            fallback_payload = {
                                "content": text,
                                "msg_type": 0,
                                "msg_seq": random.randint(1, 10000),
                            }
                            fallback_route = Route("POST", "/v2/users/{openid}/messages", openid=user_id)
                            result = await client.api._http.request(fallback_route, json=fallback_payload)
                            if result is None:
                                continue
                        sent = True
                        sent_as_image = bool(image_file)
                        logger.info(f"[私信发送] qq_official发送成功 user_openid={user_id}")
                        break
                except Exception as e:
                    logger.debug(f"[私信发送] {platform_name} 发送失败 user={user_id}: {e}")
                    continue

        # 策略3: 通用兜底 — 通过框架 context.send_message 发送（适用于飞书、Telegram、Discord 等）
        if not sent:
            if umo:
                try:
                    from astrbot.core.message.message_event_result import MessageChain
                    from astrbot.core.message.components import Plain, Image
                    if use_image and image_file:
                        chain = MessageChain(chain=[Image.fromFileSystem(image_file)])
                    else:
                        chain = MessageChain(chain=[Plain(text)])
                    sent = await self.context.send_message(umo, chain)
                    sent_as_image = bool(use_image and image_file)
                    if sent:
                        logger.info(f"[私信发送] 通用兜底发送成功 user={user_id}")
                    else:
                        logger.warning(f"[私信发送] 通用兜底未找到匹配平台 user={user_id}")
                except Exception as e:
                    logger.error(f"[私信发送] 通用兜底发送失败 user={user_id}: {e}")
                    if use_image:
                        try:
                            text_chain = MessageChain(chain=[Plain(text)])
                            sent = await self.context.send_message(umo, text_chain)
                            sent_as_image = False
                            if sent:
                                logger.info(f"[私信发送] 图片失败后已降级纯文本发送成功 user={user_id}")
                            else:
                                logger.warning(f"[私信发送] 图片失败后降级纯文本仍未成功 user={user_id}")
                        except Exception as text_error:
                            logger.error(f"[私信发送] 图片失败后降级纯文本发送失败 user={user_id}: {text_error}")
            else:
                logger.error(f"[私信发送] 所有策略均失败 user={user_id}（未找到该用户的平台会话记录）")

        setattr(self, "_last_private_send_as_image", bool(sent and sent_as_image))
        return sent

    async def _text_to_image_chain(self, text: str):
        """将文本渲染为图片，失败时回退纯文本"""
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.message.components import Plain, Image
        from astrbot.core import html_renderer
        try:
            image_path = await html_renderer.render_t2i(text, return_url=False)
            if image_path:
                logger.info(f"[文转图] 渲染成功: {image_path}")
                chain = MessageChain(chain=[Image.fromFileSystem(image_path)])
                setattr(chain, "_leetcode_image_path", image_path)
                return chain
        except Exception as e:
            logger.warning(f"[文转图] 渲染失败，回退纯文本: {e}")
        return MessageChain(chain=[Plain(text)])







    async def _send_question_to_personal_subscribers(self, question: Dict):
        """发送题目到所有个人订阅者"""
        for user_id in self.subscribed_users:
            try:
                # 获取用户的语言偏好
                user_lang = self._get_user_language(user_id)
                text = self._build_question_message(question, user_lang)
                
                sent = await self._send_private_message(user_id, text, use_image=self.enable_image_push)
                if sent:
                    logger.info(f"LeetCode 每日一题已发送到用户 {user_id}")
                else:
                    logger.error(f"[个人订阅] 所有发送策略均失败 user={user_id}")
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"发送题目到用户 {user_id} 失败: {e}")

    async def _send_plain_text(self, group_id: str, text: str):
        """发送纯文本消息"""
        try:
            await self.context.send_message(self._get_session_for_group(group_id), text)
        except Exception as e:
            logger.error(f"发送消息到群 {group_id} 失败: {e}")

    # ========== 命令处理 ==========

    @filter.command("lc菜单")
    async def cmd_menu(self, event: AstrMessageEvent):
        """显示主菜单"""
        group_id = self._get_group_id(event)
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        msg = """🤖 LeetCode 每日一题 - 命令菜单

【查询命令】
📋 /lc今日 - 立即获取今日题目（含完整描述）
🎲 /lc随机 - 随机获取一道题目（不使用今日缓存）
🔍 /lc题目 [题号] - 查询指定题目（如: /lc题目 1）
🤖 /lc解题 [题号] - 使用AI分析并解答题目（如: /lc解题 1）
📖 /lc帮助 - 查看详细帮助

【群聊订阅】（群聊管理员）
➕ /lc订阅 - 在当前群订阅每日一题
➖ /lc退订 - 在当前群取消订阅
📋 /lc列表 - 查看当前群订阅状态
📋 /lc全部订阅 - 查看所有群订阅

【个人订阅】（私聊）
➕ /lc订阅我 - 订阅每日一题私信推送
➖ /lc退订我 - 取消个人订阅
📋 /lc我的状态 - 查看个人订阅状态
⏰ /lc时间 [HH:MM] - 设置推送时间（如: /lc时间 8:00）
🌐 /lc语言 [zh/en/both] - 设置题目显示语言
🧪 /lc测试推送 - 立即测试个人推送

【管理员命令】
📋 /lc全部个人订阅 - 查看所有个人订阅
🧪 /lc测试订阅 [用户ID] [--add] - 测试个人订阅推送

⚠️ 注意：中文题目内容依赖大模型API实时翻译，请确保已配置LLM提供商"""

        yield event.plain_result(msg)

    @filter.command("lc帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示详细帮助"""
        group_id = self._get_group_id(event)

        # 群聊需要管理员权限，私聊无需权限
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        # 保存会话标识
        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        msg = """📖 LeetCode 每日一题 - 完整使用说明

【查询命令】
0. /lc菜单 - 显示简版命令菜单
1. /lc今日 - 立即获取并显示今日题目
2. /lc随机 - 随机获取一道题目，不使用今日题目缓存
3. /lc题目 [题号] - 查询指定题号的题目
   示例: /lc题目 1 (查询两数之和)
4. /lc解题 [题号] - 使用AI分析题目并提供解题思路
   示例: /lc解题 1 (AI解答两数之和)

【群聊订阅命令】（群聊管理员）
5. /lc订阅 - 在当前群订阅每日一题推送
6. /lc退订 - 在当前群取消每日一题推送
7. /lc列表 - 查看当前群是否已订阅
8. /lc全部订阅 - 查看所有群订阅情况

【个人订阅命令】（私聊）
9. /lc订阅我 - 订阅每日一题私信推送
   每天会自动收到题目推送
10. /lc退订我 - 取消个人订阅
11. /lc我的状态 - 查看订阅状态和语言设置
12. /lc时间 [HH:MM] - 设置推送时间
   示例: /lc时间 8:00
   示例: /lc时间 默认 (恢复默认时间)

【语言设置】
13. /lc语言 [zh/en/both] - 设置题目显示语言
   - zh   - 仅中文
   - en   - 仅英文
   - both - 双语显示
   示例: /lc语言 zh

【管理员命令】
14. /lc全部个人订阅 - 查看所有个人订阅用户
15. /lc测试订阅 [用户ID] [--add] - 测试给指定用户推送

【测试命令】
16. /lc测试推送 - 私聊中立即给自己发送一次每日一题，用于确认推送通道

【AI解题说明】
/lc解题命令需要AstrBot已配置LLM提供商
AI会提供：题目理解、解题思路、算法步骤、参考代码、关键点

【配置说明】
- 群推送时间: group_inform_hour / group_inform_minute
- 个人默认推送时间: personal_inform_hour / personal_inform_minute
- 用户可通过 /lc时间 单独覆盖个人推送时间

【重要提示】
中文题目内容依赖大模型API实时翻译
- 请确保AstrBot已配置LLM提供商
- 翻译功能可在配置中开启/关闭
- 如翻译失败将显示英文内容
- 建议选择适合翻译的LLM模型（如GPT-4、Claude等）"""

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
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)

        # 群聊需要管理员权限，私聊无需权限
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        # 保存会话标识
        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        today_date = datetime.now().strftime("%Y-%m-%d")

        # 获取 umo 用于 LLM 翻译
        umo = event.unified_msg_origin if hasattr(event, 'unified_msg_origin') else None

        # 检查缓存
        if self.today_question and self.today_date == today_date:
            question = self.today_question
        else:
            yield event.plain_result("⏳ 正在获取今日题目...")
            question = await self._fetch_daily_question(umo=umo)
            if question:
                self.today_question = question
                self.today_date = today_date

        if not question:
            yield event.plain_result("❌ 获取今日题目失败，请稍后再试")
            return

        # 根据用户或群组设置选择语言
        if group_id:
            language = self.default_language
        else:
            language = self._get_user_language(user_id)

        text = self._build_question_message(question, language)
        from astrbot.core.message.message_event_result import MessageEventResult
        if self.enable_image_push:
            chain = await self._text_to_image_chain(text)
            yield MessageEventResult(chain=chain.chain)
        else:
            yield event.plain_result(text)

    @filter.command("lc随机")
    async def cmd_random_question(self, event: AstrMessageEvent):
        """随机获取一道题目，不使用今日题目缓存"""
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)

        # 群聊需要管理员权限，私聊无需权限
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        yield event.plain_result("⏳ 正在随机获取一道题目...")
        question = await self._fetch_random_question()
        if not question:
            yield event.plain_result("❌ 随机题目获取失败，请稍后再试")
            return

        language = self.default_language if group_id else self._get_user_language(user_id)
        text = self._build_question_message(question, language)

        from astrbot.core.message.message_event_result import MessageEventResult
        if self.enable_image_push:
            chain = await self._text_to_image_chain(text)
            yield MessageEventResult(chain=chain.chain)
        else:
            yield event.plain_result(text)

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

    # ========== 多语言配置命令 ==========

    @filter.command("lc语言")
    async def cmd_set_language(self, event: AstrMessageEvent, language: str = ""):
        """设置题目显示语言: /lc语言 [zh/en/both]"""
        self._save_user_origin(event)
        user_id = self._get_user_id(event)

        if not language:
            # 显示当前语言设置
            current_lang = self._get_user_language(user_id)
            lang_desc = {"zh": "中文", "en": "英文", "both": "双语"}
            yield event.plain_result(
                f"🌐 您当前的语言设置: {lang_desc.get(current_lang, current_lang)} ({current_lang})\n\n"
                f"可用选项:\n"
                f"• zh - 仅中文\n"
                f"• en - 仅英文\n"
                f"• both - 双语显示\n\n"
                f"用法: /lc语言 [zh/en/both]"
            )
            return

        language = language.lower().strip()
        if language not in ["zh", "en", "both"]:
            yield event.plain_result(
                "❌ 无效的语言选项\n\n"
                "可用选项:\n"
                "• zh - 仅中文\n"
                "• en - 仅英文\n"
                "• both - 双语显示"
            )
            return

        # 保存用户语言偏好
        self.user_language_prefs[user_id] = language
        await self._save_personal_subscription()

        lang_desc = {"zh": "中文", "en": "英文", "both": "双语"}
        yield event.plain_result(
            f"✅ 语言设置已更新为: {lang_desc.get(language, language)}\n"
            f"后续收到的题目将以所选语言显示。"
        )

    # ========== 个人推送时间配置 ==========

    @filter.command("lc时间")
    async def cmd_push_time(self, event: AstrMessageEvent, time_str: str = ""):
        """查看或设置个人推送时间

        用法:
          /lc时间          - 查看当前推送时间
          /lc时间 8:00     - 设置推送时间为 08:00
          /lc时间 22:30    - 设置推送时间为 22:30
          /lc时间 默认     - 恢复使用配置文件默认时间
        """
        if self._get_group_id(event):
            yield event.plain_result("❌ 此命令只能在私聊中使用\n请直接私信我发送 /lc时间")
            return

        self._save_user_origin(event)
        user_id = self._get_user_id(event)

        # 无参数 → 查看当前推送时间
        if not time_str.strip():
            yield event.plain_result(
                f"⏰ 当前推送时间: {self._format_push_time(user_id)}\n\n"
                f"默认时间: {self.personal_inform_hour:02d}:{self.personal_inform_minute:02d}\n\n"
                f"设置方法: /lc时间 [HH:MM]\n"
                f"示例: /lc时间 8:00\n"
                f"恢复默认: /lc时间 默认"
            )
            return

        time_str = time_str.strip()

        # "默认" → 恢复配置默认值
        if time_str in ["默认", "default"]:
            if user_id in self.user_push_times:
                del self.user_push_times[user_id]
                await self._save_personal_subscription()
            if user_id in self.subscribed_users:
                umo = self.user_origins.get(user_id)
                if umo:
                    if not await self._register_cron_for_user(user_id, umo):
                        yield event.plain_result("⚠️ 已恢复默认时间，但定时任务注册失败，请查看 AstrBot 日志")
                        return
            yield event.plain_result(
                f"✅ 已恢复为默认推送时间: {self.personal_inform_hour:02d}:{self.personal_inform_minute:02d}"
            )
            return

        # 解析 HH:MM 格式
        match = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
        if not match:
            yield event.plain_result(
                "❌ 时间格式无效\n\n"
                "正确格式: HH:MM（24小时制）\n"
                "示例: /lc时间 8:00  或  /lc时间 22:30\n"
                "恢复默认: /lc时间 默认"
            )
            return

        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            yield event.plain_result("❌ 时间范围无效，小时: 0-23，分钟: 0-59")
            return

        # 保存并重新注册 CronJob
        self.user_push_times[user_id] = {"hour": hour, "minute": minute}
        await self._save_personal_subscription()

        if user_id in self.subscribed_users:
            umo = self.user_origins.get(user_id)
            if umo:
                if not await self._register_cron_for_user(user_id, umo):
                    yield event.plain_result("⚠️ 已保存时间，但定时任务注册失败，请查看 AstrBot 日志")
                    return
            else:
                yield event.plain_result("⚠️ 已保存时间，但缺少私聊会话记录，请重新发送 /lc订阅我 后再试")
                return

        yield event.plain_result(
            f"✅ 推送时间已设置为: {hour:02d}:{minute:02d}\n"
            f"下次推送将按此时间执行。\n"
            f"注意：这是每天 {hour:02d}:{minute:02d} 推送，不是 {hour} 小时 {minute} 分钟后推送。"
        )

    # ========== 个人订阅管理命令 ==========

    @filter.command("lc订阅我")
    async def cmd_subscribe_me(self, event: AstrMessageEvent):
        """个人订阅每日一题"""
        if not self.enable_personal_subscribe:
            yield event.plain_result("❌ 个人订阅功能已禁用")
            return

        self._save_user_origin(event)
        user_id = self._get_user_id(event)
        umo = self.user_origins.get(user_id) or str(event.session)

        # 检查是否在群聊中使用
        if self._get_group_id(event):
            yield event.plain_result("❌ 此命令只能在私聊中使用\n请直接私信我发送 /lc订阅我")
            return

        if user_id in self.subscribed_users:
            yield event.plain_result(
                "❌ 您已经订阅了 LeetCode 每日一题\n\n"
                f"推送时间: {self._format_push_time(user_id)}"
            )
            return

        self.subscribed_users.append(user_id)
        await self._save_personal_subscription()

        # 注册 CronJob
        if not await self._register_cron_for_user(user_id, umo):
            yield event.plain_result("⚠️ 订阅已保存，但定时任务注册失败，请查看 AstrBot 日志")
            return

        yield event.plain_result(
            f"✅ 订阅成功！\n\n"
            f"您已成功订阅 LeetCode 每日一题\n"
            f"推送时间: {self._format_push_time(user_id)}\n\n"
            f"其他命令:\n"
            f"• /lc时间 - 设置推送时间\n"
            f"• /lc语言 - 设置题目显示语言\n"
            f"• /lc退订我 - 取消订阅\n"
            f"• /lc今日 - 立即获取今日题目\n"
            f"• /lc随机 - 随机获取一道题目"
        )

    @filter.command("lc退订我")
    async def cmd_unsubscribe_me(self, event: AstrMessageEvent):
        """取消个人订阅"""
        self._save_user_origin(event)
        user_id = self._get_user_id(event)

        # 检查是否在群聊中使用
        if self._get_group_id(event):
            yield event.plain_result("❌ 此命令只能在私聊中使用\n请直接私信我发送 /lc退订我")
            return

        if user_id not in self.subscribed_users:
            yield event.plain_result("❌ 您没有订阅 LeetCode 每日一题")
            return

        self.subscribed_users.remove(user_id)
        # 清理用户自定义推送时间
        if user_id in self.user_push_times:
            del self.user_push_times[user_id]
        await self._save_personal_subscription()

        # 取消 CronJob
        await self._unregister_cron_for_user(user_id)

        yield event.plain_result("✅ 已取消订阅 LeetCode 每日一题\n期待您再次使用！")

    @filter.command("lc我的状态")
    async def cmd_my_status(self, event: AstrMessageEvent):
        """查看个人订阅状态"""
        self._save_user_origin(event)
        user_id = self._get_user_id(event)

        # 检查是否在群聊中使用
        if self._get_group_id(event):
            yield event.plain_result("❌ 此命令只能在私聊中使用\n请直接私信我发送 /lc我的状态")
            return

        # 获取用户语言偏好
        current_lang = self._get_user_language(user_id)
        lang_desc = {"zh": "中文", "en": "英文", "both": "双语"}

        lines = ["📊 您的个人订阅状态"]
        lines.append("=" * 30)

        # 订阅状态
        if user_id in self.subscribed_users:
            lines.append("✅ 订阅状态: 已订阅")
            lines.append(f"📅 推送时间: {self._format_push_time(user_id)}")
        else:
            lines.append("❌ 订阅状态: 未订阅")
            lines.append("💡 使用 /lc订阅我 可以订阅每日推送")

        lines.append(f"🌐 语言偏好: {lang_desc.get(current_lang, current_lang)}")
        lines.append("")
        lines.append("可用命令:")
        lines.append("• /lc订阅我 - 订阅推送")
        lines.append("• /lc退订我 - 取消订阅")
        lines.append("• /lc时间 [HH:MM] - 设置推送时间")
        lines.append("• /lc语言 [zh/en/both] - 设置语言")
        lines.append("• /lc今日 - 获取今日题目")
        lines.append("• /lc随机 - 随机获取一道题目")

        yield event.plain_result("\n".join(lines))

    @filter.command("lc测试推送")
    async def cmd_test_my_push(self, event: AstrMessageEvent):
        """私聊中测试给自己推送每日一题"""
        if self._get_group_id(event):
            yield event.plain_result("❌ 此命令只能在私聊中使用\n请直接私信我发送 /lc测试推送")
            return

        self._save_user_origin(event)
        user_id = self._get_user_id(event)
        language = self._get_user_language(user_id)

        today_date = datetime.now().strftime("%Y-%m-%d")
        if self.today_question and self.today_date == today_date:
            question = self.today_question
        else:
            yield event.plain_result("⏳ 正在获取今日题目并测试推送...")
            question = await self._fetch_daily_question(umo=getattr(event, "unified_msg_origin", None))
            if question:
                self.today_question = question
                self.today_date = today_date

        if not question:
            yield event.plain_result("❌ 获取今日题目失败，无法测试推送")
            return

        text = self._build_question_message(question, language)
        sent = await self._send_private_message(user_id, text, use_image=self.enable_image_push)
        if sent:
            mode = "图片" if self.enable_image_push else "文本"
            actual = "图片" if getattr(self, "_last_private_send_as_image", False) else "文本"
            yield event.plain_result(f"✅ 测试推送成功，配置模式: {mode}，实际发送: {actual}")
        else:
            yield event.plain_result("❌ 测试推送失败，请查看 AstrBot 日志中的 [私信发送] 记录")

    @filter.command("lc全部个人订阅")
    async def cmd_all_personal_subscriptions(self, event: AstrMessageEvent):
        """查看所有个人订阅（管理员命令）"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        if not self.subscribed_users:
            yield event.plain_result("📋 暂无个人订阅 LeetCode 每日一题")
            return

        lines = ["📋 已订阅 LeetCode 每日一题的用户:"]
        lines.append("=" * 30)
        for i, user_id in enumerate(self.subscribed_users, 1):
            lang = self._get_user_language(user_id)
            lines.append(f"{i}. {user_id} (语言: {lang})")

        lines.append("")
        lines.append(f"总计: {len(self.subscribed_users)} 人")

        yield event.plain_result("\n".join(lines))

    @filter.command("lc测试订阅")
    async def cmd_test_personal_subscription(self, event: AstrMessageEvent, target_user: str = ""):
        """测试个人订阅推送（管理员命令）
        
        用法：/lc测试订阅 [用户ID] [--add]
        示例：/lc测试订阅 123456789
              /lc测试订阅 123456789 --add（测试并添加到订阅列表）
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        # 解析参数
        add_to_subscription = "--add" in target_user
        target_user = target_user.replace("--add", "").strip()

        if not target_user:
            yield event.plain_result("❌ 请指定用户ID\n\n用法：/lc测试订阅 [用户ID] [--add]\n示例：/lc测试订阅 123456789")
            return

        # 获取今日题目
        today_date = datetime.now().strftime("%Y-%m-%d")
        question = None

        # 检查缓存
        if self.today_question and self.today_date == today_date:
            question = self.today_question
        else:
            yield event.plain_result(f"⏳ 正在获取今日题目并发送给用户 {target_user}...")
            question = await self._fetch_daily_question()
            if question:
                self.today_question = question
                self.today_date = today_date

        if not question:
            yield event.plain_result("❌ 获取今日题目失败，无法测试发送")
            return

        # 获取用户语言偏好（如果用户已订阅）
        user_lang = self._get_user_language(target_user)
        text = self._build_question_message(question, user_lang)

        # 直接发送消息给用户
        yield event.plain_result(f"⏳ 正在发送每日一题给用户 {target_user}...")
        
        # 如果用户未订阅，临时添加到订阅列表以测试
        temp_added = False
        if target_user not in self.subscribed_users:
            if add_to_subscription:
                self.subscribed_users.append(target_user)
                await self._save_personal_subscription()
                temp_added = True
                logger.info(f"[测试订阅] 已将用户 {target_user} 添加到订阅列表")
            else:
                logger.info(f"[测试订阅] 用户 {target_user} 不在订阅列表中，但仍尝试发送")

        # 直接发送（与实际推送走同一条路径）
        sent = await self._send_private_message(target_user, text, use_image=self.enable_image_push)

        if sent:
            result_msg = f"✅ 测试推送成功！\n"
            result_msg += f"📋 已将每日一题发送给用户 {target_user}"
            if temp_added:
                result_msg += "\n✅ 该用户已添加到个人订阅列表"
            elif target_user in self.subscribed_users:
                result_msg += f"\n📋 该用户当前语言设置: {user_lang}"
            else:
                result_msg += "\n💡 该用户未订阅，使用默认语言发送"
            yield event.plain_result(result_msg)
        else:
            error_msg = f"❌ 测试推送失败，请检查平台配置和用户ID格式"
            if temp_added:
                self.subscribed_users.remove(target_user)
                await self._save_personal_subscription()
                error_msg += "\n⚠️ 已回滚订阅状态"
            yield event.plain_result(error_msg)

    @filter.command("lc题目")
    async def cmd_question(self, event: AstrMessageEvent, question_id: str = ""):
        """根据题目号查询题目，不传参数则获取今日题目"""
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)

        # 群聊需要管理员权限，私聊无需权限
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        # 保存会话标识
        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        # 获取语言设置
        language = self.default_language if group_id else self._get_user_language(user_id)

        # 获取 umo 用于 LLM 翻译
        umo = event.unified_msg_origin if hasattr(event, 'unified_msg_origin') else None
        
        if not question_id:
            # 没有提供题目号，获取今日题目
            today_date = datetime.now().strftime("%Y-%m-%d")

            # 检查缓存
            if self.today_question and self.today_date == today_date:
                question = self.today_question
            else:
                yield event.plain_result("⏳ 正在获取今日题目...")
                question = await self._fetch_daily_question(umo=umo)
                if question:
                    self.today_question = question
                    self.today_date = today_date

            if not question:
                yield event.plain_result("❌ 获取今日题目失败，请稍后再试")
                return

            text = self._build_question_message(question, language)
            yield event.plain_result(text)
        else:
            # 提供了题目号，查询指定题目
            yield event.plain_result(f"⏳ 正在查询题目 {question_id}...")

            question = await self._fetch_question_by_id(question_id, umo=umo)

            if not question:
                yield event.plain_result(f"❌ 未找到题目 {question_id}，请检查题号是否正确")
                return

            text = self._build_question_message(question, language)
            yield event.plain_result(text)

    @filter.command("lc解题")
    async def cmd_solve(self, event: AstrMessageEvent, question_id: str = ""):
        """使用AI分析并解答题目，不传参数则解答今日题目"""
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)

        # 群聊需要管理员权限，私聊无需权限
        if group_id and not self._is_admin(event):
            yield event.plain_result("⚠️ 只有管理员可以使用此命令")
            return

        # 保存会话标识
        if group_id:
            self._save_group_origin(event)
        else:
            self._save_user_origin(event)

        # 获取语言设置
        language = self.default_language if group_id else self._get_user_language(user_id)

        # 获取 umo 用于 LLM 翻译
        umo = event.unified_msg_origin if hasattr(event, 'unified_msg_origin') else None

        if not question_id:
            # 没有提供题目号，获取今日题目
            today_date = datetime.now().strftime("%Y-%m-%d")

            # 检查缓存
            if self.today_question and self.today_date == today_date:
                question = self.today_question
            else:
                yield event.plain_result("⏳ 正在获取今日题目...")
                question = await self._fetch_daily_question(umo=umo)
                if question:
                    self.today_question = question
                    self.today_date = today_date

            if not question:
                yield event.plain_result("❌ 获取今日题目失败，请稍后再试")
                return
        else:
            # 提供了题目号，查询指定题目
            yield event.plain_result(f"⏳ 正在查询题目 {question_id}...")

            question = await self._fetch_question_by_id(question_id, umo=umo)

            if not question:
                yield event.plain_result(f"❌ 未找到题目 {question_id}，请检查题号是否正确")
                return

        content = question.get("content", "")
        content_cn = question.get("contentCn", "")
        title_cn = question.get("titleCn") or question.get("title", "未知题目")
        qid = question.get("frontendQuestionId", "")
        difficulty = question.get("difficulty", "")
        tags = []
        for tag in question.get("topicTags", []):
            if isinstance(tag, dict):
                tag_name = tag.get("nameTranslated") or tag.get("name", "")
                if tag_name:
                    tags.append(tag_name)

        # 如果有题干，先输出题目信息
        if content or content_cn:
            question_msg = self._build_question_message(question, language)
            yield event.plain_result(question_msg)

        if not content:
            yield event.plain_result("⚠️ 题目描述获取为空，AI 将根据题目信息进行分析...")

        clean_content = html_to_markdown(content)

        yield event.plain_result("🤖 正在使用AI分析题目，请稍候...")

        try:
            # 根据语言设置调整回答语言
            lang_instruction = {
                "zh": "请用中文回答。",
                "en": "Please answer in English.",
                "both": "请用中文回答，并在关键术语后附上英文对照。"
            }.get(language, "请用中文回答。")

            # 构建提示词
            if clean_content:
                desc_section = f"题目描述:\n{clean_content[:6000]}"
            else:
                desc_section = f"（题目描述为空，请根据题目编号 {qid} \"{title_cn}\" 自行分析该LeetCode题目并提供解答）"

            prompt = f"""请作为算法专家，分析并解答以下LeetCode题目：

题目编号: {qid}
题目名称: {title_cn}
难度: {difficulty}
标签: {', '.join(tags)}

{desc_section}

请提供：
1. 题目理解：简要说明题目要求
2. 解题思路：分析解题方法，包括时间复杂度和空间复杂度
3. 算法步骤：详细说明解题步骤
4. 参考代码：提供Python实现（包含注释）
5. 关键点：总结解题的关键要点

{lang_instruction}"""

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

