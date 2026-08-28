import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


class _GroupDecorator:
    def __call__(self, function):
        return self

    def command(self, _name):
        return lambda function: function


class _Filter:
    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    class EventMessageType:
        GROUP_MESSAGE = "group"

    @staticmethod
    def command(_name, **_kwargs):
        return lambda function: function

    @staticmethod
    def platform_adapter_type(_value):
        return lambda function: function

    @staticmethod
    def event_message_type(_value):
        return lambda function: function


def _load_plugin():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
    telethon = types.ModuleType("telethon")
    telethon_errors = types.ModuleType("telethon.errors")

    class DummyStar:
        def __init__(self, context):
            self.context = context

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class File:
        def __init__(self, file="", name=""):
            self.file = file
            self.name = name

    class MessageChain:
        def __init__(self, chain):
            self.chain = chain

    class TelegramClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FloodWaitError(Exception):
        seconds = 1

    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, exception=lambda *args, **kwargs: None)
    event.AstrMessageEvent = object
    event.MessageChain = MessageChain
    event.filter = _Filter()
    components.File = File
    components.Plain = Plain
    star.Context = object
    star.Star = DummyStar
    path_module.get_astrbot_data_path = lambda: tempfile.gettempdir()
    telethon.TelegramClient = TelegramClient
    telethon_errors.FloodWaitError = FloodWaitError

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_module,
        "telethon": telethon,
        "telethon.errors": telethon_errors,
    })
    spec = importlib.util.spec_from_file_location("qq_book_search_main", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class BookSearchLogicTests(unittest.TestCase):
    def test_parse_sobook_style_numbered_results(self):
        message = types.SimpleNamespace(message="🔎 搜标题作者> Results 1-3 of 3\n01. 我在永夜打造庇护所\n · TXT · 914KB\n02. 我在永夜打造庇护所-249\n · TXT · 4MB\n03. 另一本书")
        results = plugin.QQBookSearch._parse_results(message)
        self.assertEqual([item["number"] for item in results], [1, 2, 3])
        self.assertEqual(results[0]["title"], "我在永夜打造庇护所")
        self.assertEqual(results[1]["title"], "我在永夜打造庇护所-249")

    def test_parse_metadata_and_filter_adult_results(self):
        message = types.SimpleNamespace(message="01. 正常小说\n · TXT · 5MB · 136.6万字 · 4/8\n02. 成人内容示例\n · TXT · 2MB · 20万字")
        results = plugin.QQBookSearch._parse_results(message)
        self.assertEqual(results[0]["meta"], "TXT · 5MB · 136.6万字")
        instance = plugin.QQBookSearch(object(), {"adult_filter_enabled": True})
        self.assertFalse(instance._is_adult_result(results[0]))
        self.assertTrue(instance._is_adult_result(results[1]))

    def test_user_supplied_keywords_are_in_default_blocklist(self):
        instance = plugin.QQBookSearch(object(), {"adult_filter_enabled": True})
        expected = ("巨乳", "阴道", "抽插", "呻吟", "舌头", "加料", "高潮", "龟头", "肉棒", "口交", "娇躯", "母狗", "乳头")
        for keyword in expected:
            self.assertTrue(instance._is_adult_result({"search_text": f"普通书名 {keyword}"}), keyword)

    def test_builtin_keywords_merge_with_legacy_saved_config(self):
        instance = plugin.QQBookSearch(object(), {"adult_filter_enabled": True, "adult_filter_keywords": ["自定义过滤词"]})
        self.assertIn("巨乳", instance._adult_filter_keywords)
        self.assertIn("自定义过滤词", instance._adult_filter_keywords)
        self.assertTrue(instance._is_adult_result({"search_text": "普通书名 巨乳"}))

    def test_single_character_keywords_require_tag_boundary(self):
        instance = plugin.QQBookSearch(object(), {"adult_filter_enabled": True})
        self.assertFalse(instance._is_adult_result({"search_text": "普通小说 TXT 1MB"}))
        self.assertTrue(instance._is_adult_result({"search_text": "书名 #淫 #加料"}))

    def test_adult_icon_is_hard_filtered_even_when_keyword_filter_disabled(self):
        instance = plugin.QQBookSearch(object(), {"adult_filter_enabled": False})
        self.assertTrue(instance._is_adult_result({"search_text": "普通书名 🔞 TXT 1MB"}))
        self.assertFalse(instance._is_adult_result({"search_text": "普通书名 TXT 1MB"}))

    def test_latest_sort_button_is_clickable(self):
        class Button:
            text = "最新"
            data = b"latest"

        class Message:
            buttons = [[Button()]]

            def __init__(self):
                self.clicked_data = None

            async def click(self, **kwargs):
                self.clicked_data = kwargs.get("data")

        async def click():
            message = Message()
            instance = plugin.QQBookSearch(object(), {})
            result = await instance._click_button_containing(message, "最新")
            return result, message.clicked_data

        result, data = asyncio.run(click())
        self.assertTrue(result)
        self.assertEqual(data, b"latest")

    def test_source_groups_are_isolated_to_book_field(self):
        instance = plugin.QQBookSearch(object(), {"book_source_group_ids": ["428568485", "bad"]})
        self.assertEqual(instance._source_group_ids, {"428568485"})

    def test_api_id_and_wait_seconds_are_normalized(self):
        instance = plugin.QQBookSearch(object(), {"api_id": "123456", "book_search_timeout_seconds": "999"})
        self.assertEqual(instance._api_id, 123456)
        self.assertEqual(instance._wait_seconds, 180)

    def test_book_command_stops_event_when_group_not_enabled(self):
        class Event:
            message_str = "/找书 我在永夜"

            def __init__(self):
                self.stopped = False

            def get_group_id(self):
                return "428568485"

            def stop_event(self):
                self.stopped = True

            def plain_result(self, text):
                return text

        instance = plugin.QQBookSearch(object(), {"book_source_group_ids": []})
        event = Event()

        async def run_command():
            return [item async for item in instance.book_search_command(event, "我在永夜")]

        result = asyncio.run(run_command())
        self.assertTrue(event.stopped)
        self.assertIn("未配置找书功能", result[0])

    def test_book_choice_prefers_bottom_result_button_over_page_button(self):
        class Button:
            def __init__(self, text, data):
                self.text = text
                self.data = data

        class Message:
            def __init__(self):
                self.buttons = [
                    [Button("1", b"page-one"), Button("2", b"page-two")],
                    [Button("1", b"book-one"), Button("2", b"book-two")],
                ]
                self.clicked_data = None

            async def click(self, **kwargs):
                self.clicked_data = kwargs.get("data")

        async def click():
            message = Message()
            instance = plugin.QQBookSearch(object(), {})
            result = await instance._click_book_choice(message, 2, "测试书名")
            return result, message.clicked_data

        result, data = asyncio.run(click())
        self.assertTrue(result)
        self.assertEqual(data, b"book-two")

    def test_click_download_uses_detail_callback_data(self):
        class Button:
            def __init__(self, text, data):
                self.text = text
                self.data = data

        class Message:
            buttons = [[Button("收藏", b"save"), Button("⬇️下载", b"download")]]

            def __init__(self):
                self.clicked_data = None

            async def click(self, **kwargs):
                self.clicked_data = kwargs.get("data")

        async def click():
            message = Message()
            instance = plugin.QQBookSearch(object(), {})
            result = await instance._click_download(message)
            return result, message.clicked_data

        result, data = asyncio.run(click())
        self.assertTrue(result)
        self.assertEqual(data, b"download")

    def test_safe_filename_removes_directory_prefix(self):
        self.assertEqual(plugin.QQBookSearch._safe_filename("../../book.txt", "fallback.txt"), "book.txt")
        self.assertEqual(plugin.QQBookSearch._safe_filename("", "fallback.txt"), "fallback.txt")


class BookSearchV2Tests(unittest.TestCase):
    """覆盖 0.2.0 新增的空结果识别、TXT 限定、配额、撤回与文案脱敏。"""

    def setUp(self):
        self.instance = plugin.QQBookSearch(object(), {})
        self.instance._quota_path.unlink(missing_ok=True)

    def test_empty_result_is_detected_from_service_reply(self):
        text = "末日思中国\n没有检索到结果，请尝试其他关键词或筛选条件\n内容分级:全部"
        self.assertTrue(plugin.QQBookSearch._is_empty_result(text))

    def test_pending_text_is_not_treated_as_empty(self):
        self.assertFalse(plugin.QQBookSearch._is_empty_result("搜索中，请稍候"))
        self.assertTrue(plugin.QQBookSearch._is_pending("搜索中，请稍候"))

    def test_empty_result_text_has_no_service_name(self):
        text = plugin.QQBookSearch._empty_result_text()
        self.assertNotIn("Telegram", text)
        self.assertNotIn("telegram", text)

    def test_txt_only_filters_other_formats(self):
        message = types.SimpleNamespace(message="01. 正常小说\n · TXT · 5MB\n02. 电子书\n · EPUB · 5MB\n03. 未标注\n · 5MB")
        results = plugin.QQBookSearch._parse_results(message)
        self.assertEqual([item["format"] for item in results], ["TXT", "EPUB", ""])
        kept = [item for item in results if item["format"] in plugin.SUPPORTED_FORMATS]
        self.assertEqual([item["number"] for item in kept], [1, 3])

    def test_display_numbers_are_sequential_after_filtering(self):
        results = [{"number": 1, "title": "A"}, {"number": 4, "title": "B"}, {"number": 7, "title": "C"}]
        for display_number, item in enumerate(results, start=1):
            item["display_number"] = display_number
        self.assertEqual([item["display_number"] for item in results], [1, 2, 3])
        self.assertEqual([item["number"] for item in results], [1, 4, 7])

    def test_daily_quota_consumes_and_reports_remaining(self):
        self.assertEqual(self.instance._daily_limit, 50)
        remaining = self.instance._quota_remaining()
        self.instance._consume_quota()
        self.assertEqual(self.instance._quota_remaining(), remaining - 1)

    def test_friendly_error_never_leaks_service_name(self):
        cases = [
            asyncio.TimeoutError(),
            plugin.FloodWaitError(),
            RuntimeError("api_id/api_hash combination is invalid"),
            RuntimeError("Connection reset by peer"),
        ]
        for error in cases:
            text = plugin.QQBookSearch._friendly_error(error)
            self.assertNotIn("Telegram", text)
            self.assertNotIn("telegram", text)

    def test_recall_tracks_only_positive_ids(self):
        recall = plugin._Recall(None, "428568485", "123", 0, -1, "bad")
        self.assertEqual(recall._ids, [123])

    def test_recall_collects_message_id_from_send_result(self):
        class Api:
            calls: list = []

            @staticmethod
            async def call_action(action, **kwargs):
                Api.calls.append((action, kwargs))
                return {"message_id": 456}

        class Bot:
            api = Api()

        recall = plugin._Recall(Bot(), "428568485")

        async def run():
            await recall.send("测试消息")
            return list(recall._ids)

        self.assertEqual(asyncio.run(run()), [456])
        self.assertEqual(Api.calls[0][0], "send_group_msg")

    def test_recall_issues_delete_for_each_tracked_id(self):
        class Api:
            deleted: list[int] = []

            @staticmethod
            async def call_action(action, **kwargs):
                if action == "delete_msg":
                    Api.deleted.append(kwargs.get("message_id"))

        class Bot:
            api = Api()

        recall = plugin._Recall(Bot(), "428568485", 111, 222)

        async def run():
            await recall.recall(delay=0)

        asyncio.run(run())
        self.assertEqual(Api.deleted, [111, 222])
        self.assertEqual(recall._ids, [])


if __name__ == "__main__":
    unittest.main()
