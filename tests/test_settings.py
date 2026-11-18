"""Exercise configuration persistence and scheduling without a running AstrBot host."""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code


class Request:
    payload = None

    async def json(self, default=None):
        return self.payload


class Config(dict):
    fail_save = False

    def save_config(self):
        if self.fail_save:
            raise OSError("disk full")
        self.saved = dict(self)


class Context:
    def __init__(self):
        self.routes = []
        self.send_message = AsyncMock(return_value=True)

    def register_web_api(self, *args):
        self.routes.append(args)


class Star:
    def __init__(self, context):
        pass


class StarTools:
    data_dir = None

    @classmethod
    def get_data_dir(cls, name):
        return cls.data_dir


request = Request()


def load_plugin():
    """Stub only the framework boundary; import and exercise the actual plugin code."""
    modules = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.api.web",
        "astrbot.core",
        "astrbot.core.config",
        "astrbot.core.config.astrbot_config",
        "astrbot.core.message",
        "astrbot.core.message.message_event_result",
        "astrbot.core.message.components",
    ):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api"].logger = logging.getLogger("leetcode-tests")
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *args, **kwargs: lambda method: method,
    )
    star = modules["astrbot.api.star"]
    star.Context, star.Star, star.StarTools = Context, Star, StarTools
    star.register = lambda *args: lambda cls: cls
    web = modules["astrbot.api.web"]
    web.request = request
    web.json_response = Response
    web.error_response = lambda message, status_code=400: Response(
        {"status": "error", "message": message},
        status_code,
    )
    modules["astrbot.core.config.astrbot_config"].AstrBotConfig = Config
    modules["astrbot.core.message.message_event_result"].MessageChain = list
    modules["astrbot.core.message.components"].Plain = lambda text: (
        types.SimpleNamespace(text=text)
    )
    package = types.ModuleType("leetcode_test_plugin")
    package.__path__ = [str(ROOT)]
    modules[package.__name__] = package
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "leetcode_test_plugin.main", ROOT / "main.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        StarTools.data_dir = self.temp.name
        self.defaults = {
            key: item["default"]
            for key, item in json.loads(
                (ROOT / "_conf_schema.json").read_text()
            ).items()
        }
        request.payload = None

    def plugin(self, values=None, *, standalone=False):
        config = None if standalone else Config({**self.defaults, **(values or {})})
        return plugin_module.LeetCodePlugin(Context(), config)

    def write_legacy(self, data):
        (Path(self.temp.name) / "config.json").write_text(json.dumps(data))

    async def test_page_save_applies_permissions_and_survives_reload(self):
        plugin = self.plugin()
        request.payload = {
            "inform_hour": 20,
            "inform_minute": 30,
            "check_interval_seconds": 5,
            "admin_users": [" 123 ", "123", "456"],
        }
        response = await plugin.page_save_settings()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body["settings"]["admin_users"], ["123", "456"])
        self.assertTrue(
            plugin._is_admin(
                types.SimpleNamespace(
                    is_admin=lambda: False, get_sender_id=lambda: "123"
                )
            )
        )
        self.assertTrue(plugin._monitor_wakeup.is_set())
        reloaded = self.plugin(plugin.config.saved)
        self.assertEqual(reloaded.inform_hour, 20)
        self.assertEqual(reloaded.inform_minute, 30)
        self.assertEqual(reloaded.check_interval_seconds, 5)
        self.assertEqual(reloaded.admin_users, ["123", "456"])
        self.assertEqual(
            [route[2] for route in reloaded.context.routes],
            [["GET"], ["POST"], ["POST"]],
        )

    async def test_migration_preserves_legacy_time_and_explicit_later_reset(self):
        self.write_legacy(
            {"inform_hour": 20, "inform_minute": 45, "admin_users": ["123"]}
        )
        plugin = self.plugin()
        self.assertEqual((plugin.inform_hour, plugin.inform_minute), (20, 45))
        self.assertTrue(plugin.config["settings_migrated"])
        request.payload = {"inform_hour": 9, "inform_minute": 0, "admin_users": []}
        await plugin.page_save_settings()
        reloaded = self.plugin(plugin.config.saved)
        self.assertEqual((reloaded.inform_hour, reloaded.inform_minute), (9, 0))
        self.assertEqual(reloaded.admin_users, [])

    async def test_existing_nondefault_host_settings_win_migration(self):
        self.write_legacy({"inform_hour": 20, "admin_users": ["old"]})
        plugin = self.plugin({"leetcode_inform_hour": 18, "admin_users": ["current"]})
        self.assertEqual(plugin.inform_hour, 18)
        self.assertEqual(plugin.admin_users, ["current"])

    async def test_invalid_updates_never_change_runtime_or_persisted_settings(self):
        plugin = self.plugin()
        saved = dict(plugin.config)
        for payload in (
            None,
            [],
            {},
            {"inform_hour": True},
            {"inform_hour": 24},
            {"inform_minute": -1},
            {"inform_minute": "30"},
            {"check_interval_seconds": 3600},
            {"check_interval_seconds": 4},
            {"admin_users": [123]},
            {"admin_users": [""]},
            {"admin_users": ["a" * 129]},
            {"subscribed_groups": ["1"]},
        ):
            with self.subTest(payload=payload):
                request.payload = payload
                response = await plugin.page_save_settings()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(dict(plugin.config), saved)
                self.assertEqual(plugin.inform_hour, 9)
                self.assertFalse(plugin._monitor_wakeup.is_set())

    async def test_failed_save_rolls_back_host_config_and_permissions(self):
        plugin = self.plugin({"admin_users": ["original"]})
        original = dict(plugin.config)
        plugin.config.fail_save = True
        request.payload = {"inform_hour": 20, "admin_users": ["new"]}
        with self.assertLogs("leetcode-tests", level="ERROR"):
            response = await plugin.page_save_settings()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(dict(plugin.config), original)
        self.assertEqual(plugin.inform_hour, 9)
        self.assertEqual(plugin.admin_users, ["original"])
        self.assertEqual(plugin_module.ADMIN_USERS, ["original"])
        self.assertFalse(plugin._monitor_wakeup.is_set())

    async def test_standalone_atomic_save_preserves_other_data(self):
        self.write_legacy({"subscribed_groups": ["group"], "inform_hour": 10})
        plugin = self.plugin(standalone=True)
        request.payload = {"inform_hour": 21, "inform_minute": 15}
        response = await plugin.page_save_settings()
        self.assertEqual(response.status_code, 200)
        persisted = json.loads((Path(self.temp.name) / "config.json").read_text())
        self.assertEqual(persisted["subscribed_groups"], ["group"])
        reloaded = self.plugin(standalone=True)
        self.assertEqual((reloaded.inform_hour, reloaded.inform_minute), (21, 15))
        self.assertEqual(
            list(Path(self.temp.name).iterdir()), [Path(self.temp.name) / "config.json"]
        )

    async def test_legacy_unused_hourly_check_interval_becomes_safe_default(self):
        self.write_legacy({"check_interval_seconds": 3600})
        with self.assertLogs("leetcode-tests", level="WARNING"):
            plugin = self.plugin()
        self.assertEqual(plugin.check_interval_seconds, 30)

    async def test_scheduler_uses_interval_and_does_not_repeat_after_settings_save(
        self,
    ):
        plugin = self.plugin({"leetcode_check_interval_seconds": 5})
        plugin._fetch_daily_question = AsyncMock(return_value={"title": "test"})
        plugin._send_question_to_subscribers = AsyncMock()
        intervals = []

        async def finish_cycle(awaitable, timeout):
            awaitable.close()
            intervals.append(timeout)
            raise asyncio.CancelledError

        with (
            patch.object(plugin_module, "datetime") as clock,
            patch.object(
                plugin_module.asyncio,
                "wait_for",
                side_effect=finish_cycle,
            ),
        ):
            clock.now.return_value = datetime(2026, 11, 18, 9, 0, 10)
            await plugin._async_monitor()
            request.payload = {"check_interval_seconds": 10}
            await plugin.page_save_settings()
            await plugin._async_monitor()
        plugin._fetch_daily_question.assert_awaited_once()
        plugin._send_question_to_subscribers.assert_awaited_once()
        self.assertEqual(intervals, [5, 10])

    def prepare_test_push(self):
        plugin = self.plugin()
        plugin.group_origins = {
            "123": "qq:GroupMessage:123",
            "456": "qq:GroupMessage:456",
        }
        plugin.group_names = {"123": "算法训练群", "456": "每日刷题群"}
        plugin.subscribed_groups = ["456"]
        plugin.today_question = {"title": "Two Sum", "titleCn": "两数之和"}
        plugin.today_date = datetime.now().strftime("%Y-%m-%d")
        plugin._fetch_daily_question = AsyncMock(return_value=plugin.today_question)
        request.payload = {"group_id": "123"}
        return plugin

    async def test_test_push_targets_only_requested_group_without_consuming_schedule(
        self,
    ):
        plugin = self.prepare_test_push()
        request.payload = {"group_id": " 123 "}
        plugin._last_inform_date = "2026-11-17"
        plugin._send_question_to_subscribers = AsyncMock()
        saved_config = dict(plugin.config)
        response = await plugin.page_test_push()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body["group_id"], "123")
        self.assertEqual(response.body["group_name"], "算法训练群")
        self.assertEqual(response.body["question_title"], "两数之和")
        plugin.context.send_message.assert_awaited_once()
        origin, chain = plugin.context.send_message.await_args.args
        self.assertEqual(origin, "qq:GroupMessage:123")
        self.assertIn("测试推送", chain[0].text)
        self.assertIn("两数之和", "".join(component.text for component in chain))
        plugin._fetch_daily_question.assert_not_awaited()
        plugin._send_question_to_subscribers.assert_not_awaited()
        self.assertEqual(plugin.subscribed_groups, ["456"])
        self.assertEqual(plugin._last_inform_date, "2026-11-17")
        self.assertEqual(dict(plugin.config), saved_config)
        self.assertFalse(plugin._monitor_wakeup.is_set())

    async def test_test_push_fetches_fresh_question_when_cache_is_from_previous_day(
        self,
    ):
        plugin = self.prepare_test_push()
        plugin.today_date = "2000-01-01"
        fresh = {"title": "New Question"}
        plugin._fetch_daily_question.return_value = fresh
        response = await plugin.page_test_push()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body["question_title"], "New Question")
        plugin._fetch_daily_question.assert_awaited_once()
        self.assertEqual(plugin.today_question, fresh)
        self.assertEqual(plugin.today_date, datetime.now().strftime("%Y-%m-%d"))

    async def test_test_push_rejects_invalid_or_unknown_target_before_sending(self):
        plugin = self.prepare_test_push()
        for payload in (
            None,
            [],
            {},
            {"group_id": 123},
            {"group_id": ""},
            {"group_id": "123 456"},
            {"group_id": "x" * 129},
            {"group_id": "123\x00"},
            {"group_id": "unknown"},
            {"group_id": "123", "origin": "qq:FriendMessage:123"},
        ):
            with self.subTest(payload=payload):
                request.payload = payload
                response = await plugin.page_test_push()
                self.assertEqual(response.status_code, 400)
        plugin._fetch_daily_question.assert_not_awaited()
        plugin.context.send_message.assert_not_awaited()

    async def test_test_push_requires_saved_origin_even_for_subscribed_group(self):
        plugin = self.prepare_test_push()
        plugin.group_origins.pop("456")
        request.payload = {"group_id": "456"}
        response = await plugin.page_test_push()
        self.assertEqual(response.status_code, 400)
        self.assertIn("/lc列表", response.body["message"])
        plugin.context.send_message.assert_not_awaited()

    async def test_test_push_does_not_send_when_question_fetch_fails(self):
        plugin = self.prepare_test_push()
        plugin.today_question = None
        plugin._fetch_daily_question.return_value = None
        response = await plugin.page_test_push()
        self.assertEqual(response.status_code, 502)
        plugin.context.send_message.assert_not_awaited()
        self.assertFalse(plugin._test_push_lock.locked())

    async def test_test_push_reports_missing_platform_send_error_and_timeout(self):
        for outcome, status in (
            (False, 502),
            (RuntimeError("send denied"), 502),
            (asyncio.TimeoutError(), 504),
        ):
            with self.subTest(outcome=outcome):
                plugin = self.prepare_test_push()
                if isinstance(outcome, Exception):
                    plugin.context.send_message.side_effect = outcome
                else:
                    plugin.context.send_message.return_value = outcome
                if isinstance(outcome, RuntimeError):
                    with self.assertLogs("leetcode-tests", level="ERROR"):
                        response = await plugin.page_test_push()
                else:
                    response = await plugin.page_test_push()
                self.assertEqual(response.status_code, status)
                self.assertFalse(plugin._test_push_lock.locked())
                self.assertEqual(plugin._last_inform_date, "")

    async def test_test_push_rejects_concurrent_requests_without_duplicate_send(self):
        plugin = self.prepare_test_push()
        plugin.today_question = None
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch_question():
            started.set()
            await release.wait()
            return {"title": "Concurrent question"}

        plugin._fetch_daily_question.side_effect = fetch_question
        first = asyncio.create_task(plugin.page_test_push())
        await started.wait()
        try:
            response = await plugin.page_test_push()
            self.assertEqual(response.status_code, 409)
        finally:
            release.set()
            first_response = await first
        self.assertEqual(first_response.status_code, 200)
        plugin.context.send_message.assert_awaited_once()

    async def test_runtime_lists_known_unsubscribed_groups_for_test_push(self):
        plugin = self.prepare_test_push()
        plugin.group_origins.pop("456")
        response = await plugin.page_get_settings()
        self.assertEqual(
            response.body["runtime"]["test_groups"],
            [
                {
                    "group_id": "123",
                    "group_name": "算法训练群",
                    "subscribed": False,
                    "available": True,
                },
                {
                    "group_id": "456",
                    "group_name": "每日刷题群",
                    "subscribed": True,
                    "available": False,
                },
            ],
        )

    def group_event(self, name=None, queried_name=None):
        return types.SimpleNamespace(
            get_group_id=lambda: "123",
            unified_msg_origin="qq:GroupMessage:123",
            message_obj=types.SimpleNamespace(
                group=types.SimpleNamespace(group_id="123", group_name=name),
            ),
            get_group=AsyncMock(
                return_value=types.SimpleNamespace(group_name=queried_name)
            ),
            is_admin=lambda: True,
            plain_result=lambda text: text,
        )

    async def test_group_names_and_origins_are_saved_before_subscription_and_survive_reload(
        self,
    ):
        plugin = self.plugin()
        event = self.group_event(name=" 算法训练群 ")
        await plugin._save_group_origin(event)
        event.get_group.assert_not_awaited()
        self.assertEqual(plugin.subscribed_groups, [])
        persisted = json.loads(Path(plugin.subscription_file).read_text())
        self.assertEqual(persisted["group_names"], {"123": "算法训练群"})
        reloaded = self.plugin()
        self.assertEqual(reloaded.group_names["123"], "算法训练群")
        self.assertEqual(reloaded.group_origins["123"], "qq:GroupMessage:123")
        self.assertEqual(reloaded.subscribed_groups, [])

    async def test_missing_group_name_is_enriched_through_platform_event(self):
        plugin = self.plugin()
        event = self.group_event(queried_name="平台返回的群名")
        await plugin._save_group_origin(event)
        event.get_group.assert_awaited_once()
        self.assertEqual(self.plugin().group_names["123"], "平台返回的群名")

    async def test_group_lookup_failure_keeps_route_and_existing_group_name(self):
        plugin = self.plugin()
        event = self.group_event(name="已存储群名")
        await plugin._save_group_origin(event)
        event.message_obj.group.group_name = None
        event.get_group.side_effect = RuntimeError("platform unavailable")
        await plugin._save_group_origin(event, refresh_name=True)
        reloaded = self.plugin()
        self.assertEqual(reloaded.group_names["123"], "已存储群名")
        self.assertEqual(reloaded.group_origins["123"], "qq:GroupMessage:123")

    async def test_group_list_command_updates_stored_name_without_subscribing(self):
        plugin = self.plugin()
        plugin.group_names["123"] = "旧群名"
        event = self.group_event(queried_name="新群名")
        replies = [reply async for reply in plugin.cmd_list(event)]
        self.assertIn("未订阅", replies[0])
        event.get_group.assert_awaited_once()
        self.assertEqual(self.plugin().group_names["123"], "新群名")
        self.assertEqual(plugin.subscribed_groups, [])

    async def test_existing_group_names_load_from_legacy_and_subscription_files(self):
        self.write_legacy(
            {
                "group_origins": {"123": "qq:GroupMessage:123"},
                "group_names": {"123": "旧配置群名"},
            }
        )
        plugin = self.plugin()
        self.assertEqual(plugin.group_names["123"], "旧配置群名")
        Path(plugin.subscription_file).write_text(
            json.dumps(
                {
                    "subscribed_groups": ["123"],
                    "group_names": {"123": "最近保存的群名"},
                }
            )
        )
        reloaded = self.plugin()
        self.assertEqual(reloaded.group_names["123"], "最近保存的群名")
        self.assertEqual(reloaded.group_origins["123"], "qq:GroupMessage:123")

    async def test_platform_without_group_names_still_records_selectable_group(self):
        plugin = self.plugin()
        event = self.group_event()
        await plugin._save_group_origin(event)
        target = self.plugin()._settings_snapshot()["runtime"]["test_groups"][0]
        self.assertEqual(target["group_name"], "")
        self.assertEqual(target["group_id"], "123")
        self.assertTrue(target["available"])


if __name__ == "__main__":
    unittest.main()
