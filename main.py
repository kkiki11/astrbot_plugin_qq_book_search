"""QQ 群找书独立插件。

仅在指定 QQ 群处理 /找书 和序号选择；通过已授权的个人账号向书籍服务
查询，接收 TXT 后回传 QQ。它不执行 QQ→TG 实时转发、历史扫描、去重、
定时任务或私信登录。

面向 QQ 的所有输出文案均不暴露底层服务名称，便于在群内直接展示。
"""

from __future__ import annotations

import asyncio
import filecmp
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from telethon import TelegramClient
from telethon.errors import FloodWaitError

PLUGIN_NAME = "astrbot_plugin_qq_book_search"
FORWARDER_PLUGIN_NAME = "astrbot_plugin_qq_to_telegram_forwarder"
CONNECT_TIMEOUT_SECONDS = 25
DEFAULT_WAIT_SECONDS = 45
MAX_WAIT_SECONDS = 180
SEARCH_TTL_SECONDS = 15 * 60
MAX_BOOK_FILE_MB = 512
DEFAULT_DAILY_LIMIT = 50
MAX_DAILY_LIMIT = 1_000
RECALL_DELAY_SECONDS = 2
TEMP_FILE_KEEP_SECONDS = 180

# 书籍服务明确返回空结果时的特征文本；命中即立即结束等待。
EMPTY_RESULT_MARKERS = (
    "没有检索到结果", "没有搜索到", "未检索到", "未找到", "没有找到",
    "无搜索结果", "无相关结果", "换个关键词", "尝试其他关键词",
    "更换关键词", "试试其他", "没有结果", "暂无结果",
)
# 服务正在处理的中间态，需要继续等待而不是判定失败。
PENDING_MARKERS = ("搜索中", "正在搜索", "正在查找", "请稍候", "处理中", "查询中", "加载中")

# 高置信度成人分级标记或隐语；额外的宽泛单字词按标签边界匹配。
ADULT_DIRECT_MARKERS = ("🔞", "成人", "情色", "色情", "18禁", "r18", "nsfw", "h文", "h小说")
ADULT_ABBREVIATIONS = ("rbq", "np", "sm", "bdsm")
ADULT_STRONG_TERMS = ("调教", "总受", "强制爱", "强制", "轮流", "性奴", "媚药", "肉文", "骨科")
# 内置清单会始终与管理面板的附加关键词合并，兼容用户已保存的旧版配置。
DEFAULT_ADULT_FILTER_KEYWORDS = (
    "18+", "18禁", "r18", "成人", "情色", "色情", "nsfw", "h文", "h小说",
    "rbq", "np", "sm", "bdsm", "调教", "总受", "强制爱", "强制", "性奴", "肉文",
    "巨乳", "阴道", "抽插", "乳", "呻吟", "小", "舌头", "加料", "大鸡",
    "高潮", "潮", "淫", "粗", "穴", "刘备", "校花", "龟头", "肉棒",
    "口交", "娇躯", "母狗", "乳头",
)

# 允许回传的电子书格式；留空表示详情中未标注格式。
SUPPORTED_FORMATS = ("", "TXT")


class BookSearchError(RuntimeError):
    """可安全展示给 QQ 用户的查询或下载错误。"""


class _Recall:
    """记录并撤回一次找书交互中产生的 QQ 文字消息。

    撤回依赖协议端的 message_id，因此这里绕过框架的高层发送接口，
    直接调用 send_group_msg 以保留返回值。
    """

    def __init__(self, bot: Any, group_id: str, *message_ids: Any):
        self._bot = bot
        self._group_id = group_id
        self._ids: list[int] = []
        self.track(*message_ids)

    def track(self, *message_ids: Any) -> None:
        for value in message_ids:
            try:
                mid = int(value)
            except (TypeError, ValueError):
                continue
            if mid > 0 and mid not in self._ids:
                self._ids.append(mid)

    async def _call_action(self, action: str, **kwargs: Any) -> Any:
        api = getattr(self._bot, "api", None)
        target = api if api is not None else self._bot
        return await target.call_action(action, **kwargs)

    async def send(self, text: str) -> None:
        """发送文字并记录 message_id；失败时静默降级，不打断主流程。"""
        if not text or self._bot is None or not str(self._group_id).isdigit():
            return
        try:
            result = await self._call_action(
                "send_group_msg",
                group_id=int(self._group_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QQ 群找书] 发送提示消息失败：%s", _Recall._short(exc))
            return
        self.track(result.get("message_id") if isinstance(result, dict) else result)

    async def recall(self, delay: float = RECALL_DELAY_SECONDS) -> None:
        """撤回全部已记录消息；单条失败只记录调试日志。"""
        if not self._ids:
            return
        if delay > 0:
            await asyncio.sleep(delay)
        for message_id in list(self._ids):
            if self._bot is None:
                break
            try:
                await self._call_action("delete_msg", message_id=message_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[QQ 群找书] 撤回消息 %s 失败：%s", message_id, _Recall._short(exc))
        self._ids.clear()

    @staticmethod
    def _short(error: BaseException | str, limit: int = 120) -> str:
        text = " ".join(str(error).split()) or error.__class__.__name__
        return text[:limit] + ("…" if len(text) > limit else "")


class QQBookSearch(Star):
    """在 QQ 群调用书籍服务搜书并回传 TXT。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._telegram_client: TelegramClient | None = None
        self._book_entity: Any | None = None
        self._book_entity_ref = ""
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cleanup_tasks: list[asyncio.Task[None]] = []
        self._client_lock = asyncio.Lock()
        # 书籍服务是对话式的，并发请求会互相污染结果，因此串行化交互。
        self._bot_lock = asyncio.Lock()
        self._onebot_client: Any | None = None
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir = self._data_dir / "book_downloads"
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._quota_path = self._data_dir / "download_quota.json"
        self._sync_uploaded_session()
        self._sync_forwarder_session()
        logger.info("[QQ 群找书] 独立插件已加载，只处理 /找书 与序号选择。")

    async def terminate(self):
        """停止未完成请求、清理任务并释放连接。"""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._sessions.clear()
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        if self._telegram_client and self._telegram_client.is_connected():
            try:
                await asyncio.wait_for(self._telegram_client.disconnect(), timeout=8)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[QQ 群找书] 关闭连接失败：%s", self._short_error(exc))
        self._telegram_client = None
        self._book_entity = None
        self._book_entity_ref = ""

    # ------------------------------------------------------------------
    # QQ 命令与事件入口
    # ------------------------------------------------------------------
    @filter.command("找书状态", priority=100)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def book_status_command(self, event: AstrMessageEvent):
        """检查当前 QQ 群是否能够使用独立找书功能。"""
        self._stop_event(event)
        group_id = self._event_group_id(event)
        local_session = (self._data_dir / "user_session.session").is_file()
        source_session_dir = Path(get_astrbot_data_path()) / "plugin_data" / FORWARDER_PLUGIN_NAME
        shared_session = any((source_session_dir / name).is_file() for name in ("user_session.session", "telegram_user.session"))
        lines = [
            "QQ 群找书状态：",
            f"当前群号：{group_id or '未识别'}；白名单匹配：{'是' if group_id in self._source_group_ids else '否'}",
            f"服务地址：已配置为 {self._book_bot}",
            f"账号凭据：{'已配置' if self._api_id and self._api_hash else '未配置'}",
            f"本插件授权文件：{'存在' if local_session else '不存在'}；共享授权文件：{'存在' if shared_session else '不存在'}",
            f"今日剩余下载额度：{self._quota_remaining()} 本",
            "若白名单不匹配，请将当前群号加入 book_source_group_ids 后保存并重载插件。",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("找书", priority=100)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def book_search_command(self, event: AstrMessageEvent, query: str = ""):
        """在已配置 QQ 群搜索书名，并阻止该命令流入默认聊天模型。"""
        group_id = self._event_group_id(event)
        self._stop_event(event)
        if group_id not in self._source_group_ids:
            yield event.plain_result("当前 QQ 群未配置找书功能；请在插件配置的 book_source_group_ids 中加入本群号。")
            return
        raw = self._clean_text(getattr(event, "message_str", ""))
        if raw.startswith("/找书"):
            query = raw[len("/找书"):].strip()
        query = self._clean_text(query)
        if not query:
            yield event.plain_result("用法：/找书 书名；收到列表后发送序号，例如 2。")
            return
        session_key = f"{group_id}:{self._sender_id(event)}"
        self._cancel_task(session_key)
        self._sessions.pop(session_key, None)
        bot = await self._bind_event_bot(event)
        recall = _Recall(bot, group_id, self._event_message_id(event))
        await recall.send(f"正在搜索《{self._truncate(query, 80)}》，请稍候……")
        self._tasks[session_key] = asyncio.create_task(
            self._search_and_reply(event.unified_msg_origin, session_key, query, recall),
            name=f"qq-book-search-{session_key}",
        )

    @filter.command("选书", priority=100)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def book_select_command(self, event: AstrMessageEvent, number: int = 0):
        """可选的显式序号选择命令：/选书 2。"""
        group_id = self._event_group_id(event)
        self._stop_event(event)
        session_key = f"{group_id}:{self._sender_id(event)}"
        if group_id not in self._source_group_ids:
            yield event.plain_result("当前 QQ 群未配置找书功能。")
            return
        session = self._sessions.get(session_key)
        if not number or not session:
            yield event.plain_result("没有可用的书籍列表；请先发送 /找书 书名。")
            return
        self._cancel_task(session_key)
        recall = session.get("recall") or _Recall(await self._bind_event_bot(event), group_id)
        recall.track(self._event_message_id(event))
        await recall.send(f"正在下载第 {number} 项，请稍候……")
        self._tasks[session_key] = asyncio.create_task(
            self._download_and_reply(event.unified_msg_origin, session_key, int(number), recall),
            name=f"qq-book-download-{session_key}",
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """接收绑定到同一用户的序号选择。

        由于 /找书 与 /选书 的优先级更高且会终止事件传播，这里只需处理
        纯数字序号，无需重复解析命令本身。
        """
        try:
            group_id = self._event_group_id(event)
            if group_id not in self._source_group_ids:
                return
            text = self._clean_text(getattr(event, "message_str", ""))
            session_key = f"{group_id}:{self._sender_id(event)}"
            selected = re.fullmatch(r"(\d{1,3})", text)
            if not selected or session_key not in self._sessions:
                return
            self._stop_event(event)
            number = int(selected.group(1))
            self._cancel_task(session_key)
            session = self._sessions[session_key]
            recall = session.get("recall") or _Recall(await self._bind_event_bot(event), group_id)
            session["recall"] = recall
            recall.track(self._event_message_id(event))
            await recall.send(f"正在下载第 {number} 项，请稍候……")
            self._tasks[session_key] = asyncio.create_task(
                self._download_and_reply(event.unified_msg_origin, session_key, number, recall),
                name=f"qq-book-download-{session_key}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[QQ 群找书] 处理 QQ 消息失败：%s", self._short_error(exc))

    # ------------------------------------------------------------------
    # 搜索与下载主流程
    # ------------------------------------------------------------------
    async def _search_and_reply(self, origin: str, session_key: str, query: str, recall: _Recall) -> None:
        try:
            async with self._bot_lock:
                client, entity = await self._ensure_client_and_bot()
                before = await self._latest_message_id(client, entity)
                await client.send_message(entity, query)
                outcome = await self._wait_for_list(client, entity, before)
                if not outcome.get("empty"):
                    list_message = outcome["message"]
                    if self._prefer_latest_sort:
                        list_message = await self._sort_results_by_latest(client, entity, list_message)
            if outcome.get("empty"):
                await recall.send(self._empty_result_text())
                return
            all_results = self._parse_results(list_message)
            if self._txt_only:
                all_results = [item for item in all_results if item.get("format", "") in SUPPORTED_FORMATS]
            if not all_results:
                await recall.send(self._empty_result_text())
                return
            results = [item for item in all_results if not self._is_adult_result(item)]
            if not results:
                await recall.send(self._empty_result_text())
                return
            # 过滤会造成原始编号跳号，这里重排为连续序号，内部保留原编号。
            for display_number, item in enumerate(results, start=1):
                item["display_number"] = display_number
            self._sessions[session_key] = {
                "message_id": int(getattr(list_message, "id", 0) or 0),
                "results": results,
                "created_at": time.time(),
                "recall": recall,
            }
            lines = ["以下结果已按最新排序，请在本 QQ 群发送序号："]
            for item in results:
                meta = item.get("meta", "")
                lines.append(f"{item['display_number']:02d}. {item['title']}" + (f"\n    {meta}" if meta else ""))
            await recall.send("\n".join(lines))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._sessions.pop(session_key, None)
            await recall.send(f"找书失败：{self._friendly_error(exc)}")
        finally:
            self._tasks.pop(session_key, None)

    async def _download_and_reply(self, origin: str, session_key: str, number: int, recall: _Recall) -> None:
        temporary: Path | None = None
        try:
            session = self._sessions.get(session_key)
            if not session or time.time() - float(session.get("created_at", 0)) > SEARCH_TTL_SECONDS:
                self._sessions.pop(session_key, None)
                raise BookSearchError("搜索列表已过期，请重新使用 /找书 书名。")
            choice = next((item for item in session["results"] if item.get("display_number") == number), None)
            if choice is None:
                raise BookSearchError(f"列表中没有第 {number} 项，请发送正确序号。")
            if self._quota_remaining() <= 0:
                raise BookSearchError(f"今日下载额度已用完（{self._daily_limit} 本），请明天再试。")

            async with self._bot_lock:
                client, entity = await self._ensure_client_and_bot()
                result_message = await client.get_messages(entity, ids=int(session["message_id"]))
                if not result_message:
                    raise BookSearchError("搜索结果已失效，请重新搜索。")
                before = await self._latest_message_id(client, entity)
                if not await self._click_book_choice(result_message, int(choice["number"]), choice["title"]):
                    raise BookSearchError("未能定位该书籍，请重新搜索后再选择。")
                detail_message = await self._wait_for_book_detail(client, entity, before)
                if not await self._click_download(detail_message):
                    raise BookSearchError("该书暂不支持下载，请换一个序号。")
                file_message = await self._wait_for_file(client, entity, int(getattr(detail_message, "id", 0) or 0))
                filename = self._safe_filename(
                    getattr(getattr(file_message, "file", None), "name", ""),
                    f"{choice['title']}.txt",
                )
                temporary = self._download_dir / f"{int(time.time())}_{filename}"
                downloaded = await client.download_media(file_message, file=str(temporary))
                if downloaded:
                    temporary = Path(str(downloaded))

            if not temporary.is_file():
                raise BookSearchError("文件下载失败，请稍后重试。")
            # 去掉防重名时间戳前缀，按真实文件名回传。
            display_name = re.sub(r"^\d+_", "", temporary.name) or filename
            if not display_name.lower().endswith(".txt"):
                raise BookSearchError("该书不是 TXT 格式，已取消发送；请换一个序号。")
            if temporary.stat().st_size > self._max_file_bytes:
                raise BookSearchError(f"文件超过 {self._max_file_bytes // 1024 // 1024} MB 限制。")
            await self.context.send_message(
                origin,
                MessageChain(chain=[Plain(text=f"《{choice['title']}》下载完成："), File(file=str(temporary), name=display_name)]),
            )
            self._consume_quota()
            # 仅在成功回传后清理过程消息，失败时保留提示便于排查。
            if self._auto_recall:
                await recall.recall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await recall.send(f"下载失败：{self._friendly_error(exc)}")
        finally:
            if temporary is not None:
                # 协议端可能异步读取文件，延迟清理避免竞争。
                self._schedule_cleanup(temporary)
            self._tasks.pop(session_key, None)

    def _schedule_cleanup(self, path: Path) -> None:
        """延迟删除临时文件，并跟踪清理任务以便停用时取消。"""

        async def _remove() -> None:
            try:
                await asyncio.sleep(TEMP_FILE_KEEP_SECONDS)
                path.unlink(missing_ok=True)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                logger.warning("[QQ 群找书] 清理临时文件失败：%s", self._short_error(exc))

        task = asyncio.create_task(_remove(), name="qq-book-cleanup")
        self._cleanup_tasks.append(task)
        task.add_done_callback(lambda _task: self._cleanup_tasks.remove(_task) if _task in self._cleanup_tasks else None)

    # ------------------------------------------------------------------
    # 服务会话与按钮交互
    # ------------------------------------------------------------------
    async def _ensure_client_and_bot(self) -> tuple[TelegramClient, Any]:
        if not self._api_id or not self._api_hash:
            raise BookSearchError("插件未配置账号凭据，请联系管理员。")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self._api_hash):
            raise BookSearchError("账号凭据格式无效，请联系管理员。")
        self._sync_uploaded_session()
        self._sync_forwarder_session()
        session_base = self._data_dir / "user_session"
        if not session_base.with_suffix(".session").is_file():
            raise BookSearchError("未找到账号授权文件；请先完成登录，或在插件中上传已授权的文件。")
        async with self._client_lock:
            if self._telegram_client is None:
                self._telegram_client = TelegramClient(
                    str(session_base), self._api_id, self._api_hash,
                    connection_retries=3, retry_delay=2, auto_reconnect=True, receive_updates=False,
                )
            client = self._telegram_client
            try:
                if not client.is_connected():
                    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
                if not await client.is_user_authorized():
                    raise BookSearchError("账号授权已失效，请重新登录。")
                if self._book_entity is None or self._book_entity_ref != self._book_bot:
                    self._book_entity = await asyncio.wait_for(client.get_input_entity(self._book_bot), timeout=CONNECT_TIMEOUT_SECONDS)
                    self._book_entity_ref = self._book_bot
            except BookSearchError:
                raise
            except asyncio.TimeoutError as exc:
                raise BookSearchError("连接服务超时，请稍后再试。") from exc
            except Exception as exc:  # noqa: BLE001
                raise BookSearchError(self._friendly_error(exc)) from exc
            return client, self._book_entity

    async def _latest_message_id(self, client: TelegramClient, entity: Any) -> int:
        messages = await client.get_messages(entity, limit=1)
        return int(getattr(messages[0], "id", 0) or 0) if messages else 0

    async def _wait_for_list(self, client: TelegramClient, entity: Any, before: int) -> dict[str, Any]:
        """等待书籍列表；服务明确回复空结果或超时都返回空结果标记。"""
        deadline = time.monotonic() + self._wait_seconds
        while time.monotonic() < deadline:
            messages = await client.get_messages(entity, limit=15)
            for message in sorted(messages, key=lambda item: int(getattr(item, "id", 0) or 0)):
                if int(getattr(message, "id", 0) or 0) <= before or getattr(message, "out", False):
                    continue
                text = str(getattr(message, "message", "") or "")
                if self._is_empty_result(text):
                    logger.info("[QQ 群找书] 服务返回空结果，结束等待。")
                    return {"message": message, "empty": True}
                if self._is_pending(text):
                    continue
                if self._parse_results(message):
                    return {"message": message, "empty": False}
            await asyncio.sleep(1)
        logger.info("[QQ 群找书] 等待列表超时，按空结果处理。")
        return {"message": None, "empty": True}

    async def _sort_results_by_latest(self, client: TelegramClient, entity: Any, message: Any) -> Any:
        """点击“最新”排序按钮，并读取排序后的同消息编辑或新结果消息。"""
        old_id = int(getattr(message, "id", 0) or 0)
        old_text = str(getattr(message, "message", "") or "")
        if not await self._click_button_containing(message, "最新"):
            logger.warning("[QQ 群找书] 搜索结果未找到“最新”排序按钮，将使用服务默认排序。")
            return message
        deadline = time.monotonic() + min(12, self._wait_seconds)
        latest_seen = message
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            refreshed = await client.get_messages(entity, ids=old_id)
            if refreshed and self._parse_results(refreshed):
                latest_seen = refreshed
                refreshed_text = str(getattr(refreshed, "message", "") or "")
                if refreshed_text != old_text or self._is_latest_active(refreshed):
                    return refreshed
            messages = await client.get_messages(entity, limit=15)
            for candidate in sorted(messages, key=lambda item: int(getattr(item, "id", 0) or 0)):
                if int(getattr(candidate, "id", 0) or 0) <= old_id or getattr(candidate, "out", False):
                    continue
                if self._parse_results(candidate):
                    return candidate
        return latest_seen

    @staticmethod
    def _is_latest_active(message: Any) -> bool:
        for row in (getattr(message, "buttons", None) or []):
            for button in row:
                label = str(getattr(button, "text", "") or "")
                if "最新" in label and any(mark in label for mark in ("✓", "✔", "✅", "↓")):
                    return True
        return False

    async def _wait_for_book_detail(self, client: TelegramClient, entity: Any, before: int) -> Any:
        """等待选择书籍后返回的详情卡片；详情卡片含“下载”内联按钮。"""
        deadline = time.monotonic() + self._wait_seconds
        while time.monotonic() < deadline:
            messages = await client.get_messages(entity, limit=15)
            for message in sorted(messages, key=lambda item: int(getattr(item, "id", 0) or 0)):
                if int(getattr(message, "id", 0) or 0) <= before or getattr(message, "out", False):
                    continue
                if self._has_download_button(message):
                    return message
            await asyncio.sleep(1)
        raise BookSearchError("等待书籍详情超时，请稍后重新搜索。")

    async def _wait_for_file(self, client: TelegramClient, entity: Any, after_detail_id: int) -> Any:
        """等待下载回调后的 TXT；允许下载器编辑详情消息并在同一消息 ID 附加文件。"""
        deadline = time.monotonic() + self._wait_seconds
        while time.monotonic() < deadline:
            messages = await client.get_messages(entity, limit=15)
            for message in sorted(messages, key=lambda item: int(getattr(item, "id", 0) or 0)):
                if int(getattr(message, "id", 0) or 0) < after_detail_id or getattr(message, "out", False):
                    continue
                filename = str(getattr(getattr(message, "file", None), "name", "") or "").lower()
                if filename.endswith(".txt"):
                    return message
            await asyncio.sleep(1)
        raise BookSearchError("等待 TXT 文件超时，请稍后重新下载。")

    async def _click_book_choice(self, message: Any, number: int, title: str) -> bool:
        """选择底部书籍编号按钮；同名数字存在翻页和书籍两组时始终选择最后一组。"""
        candidates: list[tuple[int, int, Any]] = []
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row):
                label = self._clean_text(getattr(button, "text", ""))
                normalized = re.sub(r"[✓✔✅☑️\s]", "", label)
                if normalized in {str(number), f"{number:02d}"} or (title and title in label):
                    candidates.append((row_index, column_index, button))
        if not candidates:
            return False
        row_index, column_index, button = candidates[-1]
        data = getattr(button, "data", None)
        if data is not None:
            await message.click(data=data)
        else:
            await message.click(i=row_index, j=column_index)
        return True

    @staticmethod
    def _has_download_button(message: Any) -> bool:
        return any(
            "下载" in str(getattr(button, "text", "") or "")
            for row in (getattr(message, "buttons", None) or [])
            for button in row
        )

    async def _click_download(self, message: Any) -> bool:
        """触发书籍详情卡片中的下载内联回调。"""
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row):
                if "下载" not in self._clean_text(getattr(button, "text", "")):
                    continue
                data = getattr(button, "data", None)
                if data is not None:
                    await message.click(data=data)
                else:
                    await message.click(i=row_index, j=column_index)
                return True
        return False

    async def _click_button_containing(self, message: Any, keyword: str) -> bool:
        """按按钮文字触发一次内联回调。"""
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row):
                if keyword not in self._clean_text(getattr(button, "text", "")):
                    continue
                data = getattr(button, "data", None)
                if data is not None:
                    await message.click(data=data)
                else:
                    await message.click(i=row_index, j=column_index)
                return True
        return False

    # ------------------------------------------------------------------
    # 列表解析与成人内容过滤
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_results(message: Any) -> list[dict[str, Any]]:
        """解析列表的编号、书名、格式、大小和字数。"""
        text = str(getattr(message, "message", "") or "")
        results: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            title_match = re.match(r"^(\d{1,3})[.、)）]\s*(.+?)$", line)
            if title_match:
                if current is not None:
                    QQBookSearch._finalize_result_meta(current)
                    results.append(current)
                current = {"number": int(title_match.group(1)), "title": title_match.group(2).strip(), "details": []}
            elif current is not None and line:
                current["details"].append(line.lstrip("·• "))
        if current is not None:
            QQBookSearch._finalize_result_meta(current)
            results.append(current)
        return [item for item in results if item["title"] and not item["title"].lower().startswith(("txt", "results"))][:50]

    @staticmethod
    def _finalize_result_meta(item: dict[str, Any]) -> None:
        details = " ".join(item.pop("details", []))
        parts: list[str] = []
        format_match = re.search(r"\b(TXT|EPUB|MOBI|AZW3|PDF)\b", details, flags=re.IGNORECASE)
        size_match = re.search(r"\b\d+(?:\.\d+)?\s*(?:KB|MB|GB)\b", details, flags=re.IGNORECASE)
        word_match = re.search(r"\b\d+(?:\.\d+)?\s*(?:万|亿)?字\b", details)
        item["format"] = format_match.group(1).upper() if format_match else ""
        if format_match:
            parts.append(item["format"])
        if size_match:
            parts.append(re.sub(r"\s+", "", size_match.group(0).upper()))
        if word_match:
            parts.append(word_match.group(0))
        item["meta"] = " · ".join(parts)
        item["search_text"] = f"{item['title']} {details}".lower()

    @staticmethod
    def _is_empty_result(text: str) -> bool:
        """判断服务是否明确回复了空结果。"""
        normalized = re.sub(r"\s+", "", text or "")
        return any(marker in normalized for marker in EMPTY_RESULT_MARKERS)

    @staticmethod
    def _is_pending(text: str) -> bool:
        """判断服务是否处于处理中的中间态。"""
        return any(marker in (text or "") for marker in PENDING_MARKERS)

    @staticmethod
    def _empty_result_text() -> str:
        return "未找到相关书籍，请换个关键词试试。"

    def _is_adult_result(self, item: dict[str, Any]) -> bool:
        """根据直接标记、变体缩写和组合标签计算成人内容风险。"""
        source_text = str(item.get("search_text", item.get("title", "")))
        # 🔞 是明确内容分级，始终硬过滤，不受关键词开关或阈值影响。
        if "🔞" in source_text:
            return True
        if not self._adult_filter_enabled:
            return False
        score, _reasons = self._adult_risk(source_text)
        return score >= self._adult_filter_threshold

    def _adult_risk(self, value: Any) -> tuple[int, tuple[str, ...]]:
        """返回可解释的风险评分；仅基于本地文本，不上传书名或详情。"""
        raw = unicodedata.normalize("NFKC", str(value or "")).lower()
        spaced = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", raw)
        compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", raw)
        score = 0
        reasons: list[str] = []

        def add(points: int, reason: str) -> None:
            nonlocal score
            if reason not in reasons:
                score += points
                reasons.append(reason)

        # 配置黑名单视为明确人工规则，优先级最高。
        for keyword in self._adult_filter_keywords:
            normalized_keyword = self._normalize_filter_text(keyword)
            if self._keyword_present(raw, compact, normalized_keyword):
                add(6, "自定义关键词")
                break
        for marker in ADULT_DIRECT_MARKERS:
            marker_normalized = self._normalize_filter_text(marker)
            if marker == "🔞" and marker in raw:
                add(10, "成人分级图标")
            elif marker_normalized and marker_normalized in compact:
                add(6, "明确成人标记")
                break
        for term in ADULT_STRONG_TERMS:
            if term in compact:
                add(3, "高风险中文标签")
        for abbreviation in ADULT_ABBREVIATIONS:
            if re.search(rf"(?<![a-z0-9]){re.escape(abbreviation)}(?![a-z0-9])", spaced):
                add(3, "高风险缩写")
        # 处理 r_18、r-18、18 plus 等被符号拆开的常见变体。
        if re.search(r"(?:r\s*18|18\s*(?:x|plus|禁))", spaced):
            add(5, "成人分级变体")
        # 两个中等风险标签同时出现才过滤，降低普通题材的误伤概率。
        medium_terms = ("伪娘", "人妻", "乱伦", "催眠", "雌堕", "ntr", "足交")
        medium_hits = sum(1 for term in medium_terms if term in compact)
        if medium_hits >= 2:
            add(3, "高风险标签组合")
        return score, tuple(reasons)

    @staticmethod
    def _normalize_filter_text(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or "")).lower())

    @staticmethod
    def _keyword_present(raw: str, compact: str, keyword: str) -> bool:
        """多字词可直接匹配；单字仅作为独立标签命中，避免“小说”等普通文本误伤。"""
        if not keyword:
            return False
        if len(keyword) > 1:
            return keyword in compact
        return bool(re.search(rf"(?:^|[\s#_、·|/]){re.escape(keyword)}(?=$|[\s#_、·|/])", raw))

    # ------------------------------------------------------------------
    # 每日下载配额
    # ------------------------------------------------------------------
    def _load_quota(self) -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        state = {"date": today, "count": 0}
        try:
            raw = json.loads(self._quota_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and str(raw.get("date") or "") == today:
                state["count"] = max(0, self._as_int(raw.get("count"), 0))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        return state

    def _quota_remaining(self) -> int:
        return max(0, self._daily_limit - int(self._load_quota()["count"]))

    def _consume_quota(self) -> None:
        state = self._load_quota()
        state["count"] = int(state["count"]) + 1
        temporary = self._quota_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._quota_path)
        except OSError as exc:
            logger.warning("[QQ 群找书] 写入下载配额失败：%s", self._short_error(exc))

    # ------------------------------------------------------------------
    # session 同步与 QQ 协议端绑定
    # ------------------------------------------------------------------
    def _sync_forwarder_session(self) -> None:
        destination = self._data_dir / "user_session.session"
        if destination.is_file():
            return
        original_dir = Path(get_astrbot_data_path()) / "plugin_data" / FORWARDER_PLUGIN_NAME
        for source in (original_dir / "user_session.session", original_dir / "telegram_user.session"):
            if source.is_file():
                try:
                    shutil.copyfile(source, destination)
                    logger.info("[QQ 群找书] 已复用原转发插件的授权文件。")
                except OSError as exc:
                    logger.warning("[QQ 群找书] 复用授权文件失败：%s", self._short_error(exc))
                return

    def _sync_uploaded_session(self) -> None:
        values = self.config.get("telegram_session", [])
        selected = values[0] if isinstance(values, list) and values else values
        raw = str(selected or "").strip()
        if not raw or Path(raw).is_absolute():
            return
        source = (self._data_dir / raw).resolve()
        try:
            source.relative_to(self._data_dir.resolve())
        except ValueError:
            return
        destination = self._data_dir / "user_session.session"
        if source.suffix != ".session" or not source.is_file() or source == destination:
            return
        try:
            if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
                shutil.copyfile(source, destination)
        except OSError as exc:
            logger.warning("[QQ 群找书] 同步上传授权文件失败：%s", self._short_error(exc))

    @staticmethod
    def _supports_action_api(client: Any) -> bool:
        return bool(client and callable(getattr(getattr(client, "api", None), "call_action", None)))

    async def _ensure_onebot_client(self) -> None:
        if self._supports_action_api(self._onebot_client):
            return
        get_platform = getattr(self.context, "get_platform", None)
        if callable(get_platform):
            try:
                platform = get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                get_client = getattr(platform, "get_client", None)
                candidate = get_client() if callable(get_client) else None
                if self._supports_action_api(candidate):
                    self._onebot_client = candidate
                    return
            except Exception:  # 兼容不同 AstrBot 版本的 Context 实现。
                pass
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        platforms = get_insts() if callable(get_insts) else []
        for platform in platforms:
            get_client = getattr(platform, "get_client", None)
            candidate = get_client() if callable(get_client) else None
            if self._supports_action_api(candidate):
                self._onebot_client = candidate
                return

    async def _bind_event_bot(self, event: AstrMessageEvent) -> Any:
        """绑定可用于撤回消息的 QQ 协议端客户端。"""
        candidate = getattr(event, "bot", None)
        if self._supports_action_api(candidate):
            self._onebot_client = candidate
        else:
            await self._ensure_onebot_client()
        return self._onebot_client

    # ------------------------------------------------------------------
    # 配置项与工具方法
    # ------------------------------------------------------------------
    @property
    def _source_group_ids(self) -> set[str]:
        value = self.config.get("book_source_group_ids", [])
        values = value if isinstance(value, list) else str(value or "").replace("，", ",").split(",")
        return {str(item).strip() for item in values if str(item).strip().isdigit()}

    @property
    def _api_id(self) -> int:
        try:
            return int(self.config.get("api_id", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def _api_hash(self) -> str:
        return str(self.config.get("api_hash", "") or "").strip()

    @property
    def _book_bot(self) -> str:
        return str(self.config.get("book_search_bot", "@sobook") or "@sobook").strip()

    @property
    def _wait_seconds(self) -> int:
        try:
            return max(15, min(MAX_WAIT_SECONDS, int(self.config.get("book_search_timeout_seconds", DEFAULT_WAIT_SECONDS))))
        except (TypeError, ValueError):
            return DEFAULT_WAIT_SECONDS

    @property
    def _max_file_bytes(self) -> int:
        try:
            value = int(self.config.get("max_book_file_size_mb", 50) or 50)
        except (TypeError, ValueError):
            value = 50
        return max(1, min(MAX_BOOK_FILE_MB, value)) * 1024 * 1024

    @property
    def _adult_filter_threshold(self) -> int:
        try:
            return max(1, min(12, int(self.config.get("adult_filter_threshold", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def _adult_filter_enabled(self) -> bool:
        return bool(self.config.get("adult_filter_enabled", True))

    @property
    def _adult_filter_keywords(self) -> tuple[str, ...]:
        """合并内置与用户追加关键词，避免旧配置覆盖新版本的安全默认值。"""
        value = self.config.get("adult_filter_keywords", [])
        values = value if isinstance(value, list) else str(value or "").replace("，", ",").split(",")
        words: list[str] = []
        seen: set[str] = set()
        for item in (*DEFAULT_ADULT_FILTER_KEYWORDS, *(str(item).strip() for item in values)):
            normalized = self._normalize_filter_text(item)
            if normalized and normalized not in seen:
                seen.add(normalized)
                words.append(str(item).strip())
        return tuple(words)

    @property
    def _prefer_latest_sort(self) -> bool:
        return bool(self.config.get("prefer_latest_sort", True))

    @property
    def _txt_only(self) -> bool:
        return bool(self.config.get("txt_only", True))

    @property
    def _auto_recall(self) -> bool:
        return bool(self.config.get("auto_recall_messages", True))

    @property
    def _daily_limit(self) -> int:
        try:
            value = int(self.config.get("daily_download_limit", DEFAULT_DAILY_LIMIT) or DEFAULT_DAILY_LIMIT)
        except (TypeError, ValueError):
            value = DEFAULT_DAILY_LIMIT
        return max(1, min(MAX_DAILY_LIMIT, value))

    @staticmethod
    def _event_group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        value = getter() if callable(getter) else getattr(event, "group_id", "")
        if not value:
            value = getattr(getattr(event, "message_obj", None), "group_id", "")
        return str(value or "").strip()

    @staticmethod
    def _event_message_id(event: AstrMessageEvent) -> int:
        message_obj = getattr(event, "message_obj", None)
        value = getattr(message_obj, "message_id", None)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        value = getter() if callable(getter) else getattr(event, "sender_id", "")
        return str(value or "unknown").strip()

    @staticmethod
    def _stop_event(event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    def _cancel_task(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    @staticmethod
    def _safe_filename(value: Any, fallback: str) -> str:
        name = Path(str(value or "").strip()).name.replace("\x00", "")
        return name if name not in {"", ".", ".."} else fallback

    @staticmethod
    def _clean_text(value: Any) -> str:
        return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 1] + "…"

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _short_error(error: BaseException | str, limit: int = 180) -> str:
        value = " ".join(str(error).split()) or error.__class__.__name__
        return value[:limit] + ("…" if len(value) > limit else "")

    @staticmethod
    def _friendly_error(error: BaseException | str) -> str:
        """把底层异常转换为可在 QQ 群展示的文案，不暴露服务名称。"""
        if isinstance(error, BookSearchError):
            return str(error)
        if isinstance(error, FloodWaitError):
            return f"服务繁忙，请在 {error.seconds} 秒后再试。"
        if isinstance(error, asyncio.TimeoutError):
            return "服务响应超时，请稍后再试。"
        text = str(error).lower()
        if "api_id/api_hash combination is invalid" in text:
            return "账号凭据无效，请联系管理员检查插件配置。"
        if "timeout" in text or "timed out" in text:
            return "服务响应超时，请稍后再试。"
        if "connection" in text or "network" in text:
            return "网络连接失败，请稍后再试。"
        return QQBookSearch._short_error(error)


__all__ = ["BookSearchError", "QQBookSearch"]
