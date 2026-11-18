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
            [route[2] for route in reloaded.context.routes], [["GET"], ["POST"]]
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


if __name__ == "__main__":
    unittest.main()
