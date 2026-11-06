"""MaiBot QQ 名片点赞插件。"""

from __future__ import annotations

import asyncio
import sqlite3
import hashlib
import math
import unicodedata
import uuid
from datetime import date
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import (
    CONFIG_RELOAD_SCOPE_SELF,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

SUPPORTED_CONFIG_VERSION = "1.0.2"

# 兼容新版统一连接器（SnowLuma 命名空间）和旧版 NapCat 适配器。
# 仅在明确确认 API 不存在时切换命名空间；点赞请求出现未知异常时不自动重试，
# 避免请求已经在 QQ 侧执行但响应丢失时造成重复点赞。
ADAPTER_NAMESPACES = ("adapter.snowluma", "adapter.napcat")
ADAPTER_API_VERSION = "1"
SEND_LIKE_API_SUFFIX = ".account.send_like"
GET_LOGIN_INFO_API_SUFFIX = ".system.get_login_info"

# Host 可在鉴权前 search；入口只做线性、无重叠回溯的筛选。
# 处理函数随后严格校验本次原始消息的开头，允许宿主在引用渲染后仍路由命令。
COMMAND_PATTERN = r"/(?:点赞|赞|zan)(?=\s|$)"

STATE_FILE_NAME = "like_state.json"
SOURCE_TTL_SECONDS = 180
MAX_SOURCE_MESSAGES = 512
SCHEDULE_CHECK_SECONDS = 30
SCHEDULE_REQUEST_TIMEOUT_SECONDS = 30

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "extension"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="启用命令、自然语言和定时点赞，关闭后不再发送新请求。",
        json_schema_extra={"label": "启用插件"},
    )

    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class LikeSectionConfig(PluginConfigBase):
    __ui_label__ = "点赞设置"
    __ui_icon__ = "thumb_up"
    __ui_order__ = 1

    default_times: int = Field(
        default=10,
        ge=1,
        le=20,
        description="未指定次数时使用，范围 1 至 20；超过单次上限时按单次上限发送。",
        json_schema_extra={"label": "默认点赞次数"},
    )

    max_times: int = Field(
        default=20,
        ge=1,
        le=20,
        description="每次请求的上限，范围 1 至 20；命令、自然语言和定时点赞都适用，“点满”使用此值。",
        json_schema_extra={"label": "单次点赞上限"},
    )

    daily_limit_per_target: int = Field(
        default=30,
        ge=1,
        le=50,
        description="每个目标每天的额度，范围 1 至 50；所有用户共用，结果未确认的请求也占额度。",
        json_schema_extra={"label": "每日点赞额度"},
    )

    cooldown_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="同一发送者两次点赞请求的间隔，命令和自然语言共用；范围 0 至 3600 秒，0 为关闭。",
        json_schema_extra={"label": "冷却时间（秒）"},
    )

    allow_at_target: bool = Field(
        default=True,
        description="允许通过真实的 @ 消息指定点赞目标，命令和自然语言都适用。",
        json_schema_extra={"label": "允许 @ 指定目标"},
    )

    allow_reply_target: bool = Field(
        default=True,
        description="允许通过回复对方的消息指定点赞目标，命令和自然语言都适用。",
        json_schema_extra={"label": "允许回复指定目标"},
    )

    allow_raw_qq: bool = Field(
        default=True,
        description="允许直接输入 QQ 号指定目标；自然语言按昵称查找目标也需要开启此项。",
        json_schema_extra={"label": "允许 QQ 号指定目标"},
    )

    enable_llm_tool: bool = Field(
        default=True,
        description="允许通过自然语言请求点赞；关闭后仍可使用命令。保存后生效。",
        json_schema_extra={"label": "启用自然语言点赞"},
    )


class AdminSectionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    admin_only: bool = Field(
        default=True,
        description="仅允许下方管理员使用命令和自然语言点赞；开启后管理员列表不能为空。关闭后仍有冷却和每日额度限制，不影响定时名单。",
        json_schema_extra={"label": "仅管理员可用"},
    )

    admins: str = Field(
        default="",
        description="管理员 QQ 号，多个号码用逗号分隔；开启“仅管理员可用”时必填。",
        json_schema_extra={"label": "管理员 QQ 号", "placeholder": "123456789, 987654321"},
    )


class ScheduleSectionConfig(PluginConfigBase):
    __ui_label__ = "定时点赞"
    __ui_icon__ = "schedule"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description="每天到点给名单中的 QQ 点赞；需要同时启用插件。保存后生效。",
        json_schema_extra={"label": "启用定时点赞"},
    )

    time: str = Field(
        default="09:00",
        description="每天执行的时间，使用 24 小时制 HH:MM，例如 09:00、21:30；按运行 MaiBot 的电脑时区计算。",
        json_schema_extra={"label": "每天执行时间", "placeholder": "09:00"},
    )

    qq_list: str = Field(
        default="",
        description="要点赞的 QQ 号，用逗号、空格或换行分隔；重复号码只执行一次，无效号码跳过。",
        json_schema_extra={"label": "定时点赞 QQ 名单", "placeholder": "123456789, 987654321"},
    )

    times: int = Field(
        default=10,
        ge=1,
        le=20,
        description="名单中每人每天的点赞次数，范围 1 至 20；仍受单次上限和每日额度限制。",
        json_schema_extra={"label": "每人点赞次数"},
    )


class QQLikeConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(
        default_factory=PluginSectionConfig
    )

    like: LikeSectionConfig = Field(
        default_factory=LikeSectionConfig
    )

    admin: AdminSectionConfig = Field(
        default_factory=AdminSectionConfig
    )

    schedule: ScheduleSectionConfig = Field(
        default_factory=ScheduleSectionConfig
    )



class QQLikePlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = QQLikeConfig

    def __init__(self) -> None:
        super().__init__()
        self._state_path: Path | None = None
        self._db_path: Path | None = None
        self._storage_error = ""
        self._closed = True
        self._source_messages: dict[str, tuple[float, dict[str, Any]]] = {}
        self._schedule_task: asyncio.Task[None] | None = None
        self._schedule_wakeup: asyncio.Event | None = None

    async def on_load(self) -> None:
        try:
            directory = Path(self.ctx.paths.data_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self._state_path = directory / STATE_FILE_NAME
            self._db_path = directory / "like_ledger.sqlite3"
            self._initialize_ledger()
            self._storage_error = ""
        except Exception:
            self._storage_error = "点赞账本读取或迁移失败，请检查数据文件与权限；已暂停发送。"
            self.ctx.logger.exception(self._storage_error)
        self._closed = False
        self._warn_config()
        self._sync_schedule_task()
        self.ctx.logger.info("QQ 点赞插件已加载。")

    async def on_unload(self) -> None:
        # 每条请求单独持久化；旧实例卸载不能覆写新实例的账本。
        self._closed = True
        self._source_messages.clear()
        task, self._schedule_task = self._schedule_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._schedule_wakeup = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._warn_config()
            if not self.config.plugin.enabled or not self.config.like.enable_llm_tool:
                self._source_messages.clear()
            self._sync_schedule_task()
            self.ctx.logger.info("QQ 点赞配置已更新 version=%s", version)

    def _warn_config(self) -> None:
        if self.config.admin.admin_only and not any(
            self._normalize_user_id(value) for value in re.split(r"[\s,，;；]+", self.config.admin.admins)
        ):
            self.ctx.logger.warning("仅管理员可用已开启，但未填写有效的管理员 QQ；聊天点赞不可用，请在插件设置中填写。定时名单不受影响。")
        if self.config.like.default_times > self.config.like.max_times:
            self.ctx.logger.warning("默认点赞次数超过单次上限，未指定次数时将按单次上限发送。")
        if self.config.schedule.enabled:
            if not self._schedule_time_valid():
                self.ctx.logger.warning("定时点赞时间无效，请填写 24 小时制 HH:MM，例如 09:00；定时任务暂停。")
            targets, invalid = self._schedule_targets()
            if not targets:
                self.ctx.logger.warning("定时点赞名单中没有有效 QQ，请在插件设置中填写。")
            if invalid:
                self.ctx.logger.warning("定时点赞名单中有 %s 个无效号码，已跳过。", invalid)
            if self.config.schedule.times > self.config.like.max_times:
                self.ctx.logger.warning("定时点赞次数超过单次上限，将按单次上限发送。")

    def _schedule_time_valid(self) -> bool:
        return re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.config.schedule.time.strip()) is not None

    def _schedule_targets(self) -> tuple[tuple[str, ...], int]:
        targets, invalid = {}, 0
        for token in re.split(r"[\s,，;；]+", self.config.schedule.qq_list.strip()):
            if not token:
                continue
            target = self._normalize_user_id(token)
            if target:
                targets[target] = None
            else:
                invalid += 1
        return tuple(targets), invalid

    def _schedule_plan(self) -> tuple[str, tuple[str, ...], int] | None:
        if (self._closed or self._storage_error or not self.config.plugin.enabled or
                not self.config.schedule.enabled or not self._schedule_time_valid()):
            return None
        targets, _ = self._schedule_targets()
        if not targets:
            return None
        return (self.config.schedule.time.strip(), targets,
                min(self.config.schedule.times, self.config.like.max_times))

    def _sync_schedule_task(self) -> None:
        if self._schedule_plan() is not None and (self._schedule_task is None or self._schedule_task.done()):
            self._schedule_wakeup = asyncio.Event()
            self._schedule_task = asyncio.create_task(self._schedule_loop(), name="qq-like-daily")
        if self._schedule_wakeup is not None:
            self._schedule_wakeup.set()

    async def _schedule_loop(self) -> None:
        while not self._closed:
            wakeup = self._schedule_wakeup
            if wakeup is None:
                return
            wakeup.clear()
            try:
                await self._run_schedule_once()
            except Exception:
                self.ctx.logger.exception("定时点赞检查失败，将在下次检查时继续；已提交的请求不会重发。")
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=SCHEDULE_CHECK_SECONDS)
            except TimeoutError:
                pass

    @staticmethod
    def _schedule_key(day: str, target: str) -> str:
        return f"schedule:{day}:{target}"

    def _schedule_recorded(self, key: str) -> bool:
        connection = self._connect()
        try:
            return (connection.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone() is not None or
                    connection.execute("SELECT 1 FROM requests WHERE id=?", (key,)).fetchone() is not None)
        finally:
            connection.close()

    def _skip_schedule(self, key: str) -> None:
        connection = self._connect()
        try:
            connection.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, "skipped"))
            connection.commit()
        finally:
            connection.close()

    async def _run_schedule_once(self) -> None:
        plan = self._schedule_plan()
        if plan is None:
            return
        local = time.localtime()
        day = time.strftime("%Y-%m-%d", local)
        if time.strftime("%H:%M", local) < plan[0]:
            return
        for target in plan[1]:
            if self._schedule_plan() != plan or time.strftime("%Y-%m-%d") != day:
                return
            key = self._schedule_key(day, target)
            if self._schedule_recorded(key):
                continue
            try:
                # 限制单个号码的等待时间，避免连接故障阻塞整份名单。
                await asyncio.wait_for(self._perform_scheduled_like(target, day, plan), timeout=SCHEDULE_REQUEST_TIMEOUT_SECONDS)
            except Exception:
                self.ctx.logger.exception("定时点赞未完成 target=%s；已提交的请求不会重发。", target)

    async def _perform_scheduled_like(self, target: str, day: str, plan: tuple[str, tuple[str, ...], int]) -> None:
        self_id, namespace = await self._get_account()
        # 登录查询期间可能跨日或修改配置，发送前按当前配置复查。
        if (self._schedule_plan() != plan or time.strftime("%Y-%m-%d") != day or
                time.strftime("%H:%M") < plan[0]):
            return
        key = self._schedule_key(day, target)
        if target == self_id:
            self._skip_schedule(key)
            self.ctx.logger.warning("定时点赞跳过机器人自身 target=%s", target)
            return
        reservation = self._reserve_like(target, "", plan[2], request_key=key, schedule_day=day)
        if isinstance(reservation, str):
            self.ctx.logger.info("定时点赞跳过 target=%s: %s", target, reservation)
            return
        ok, reply = await self._submit_reserved_like(namespace, target, reservation)
        if not ok:
            self.ctx.logger.warning("定时点赞 target=%s: %s", target, reply)

    @Tool(
        "like_qq_profile",
        brief_description="用户请求给自己或他人 QQ 名片点赞时调用。必须传 Planner 消息前缀中的 msg_id 作为 source_message_id（这是消息编号，不是 QQ 号）；无需让用户再发命令。身份、目标和次数由原始消息核验，禁止伪造 user_id。省略次数使用配置默认值，点满传 use_maximum=true。",
        parameters=[
            ToolParameterInfo(name="source_message_id", param_type=ToolParamType.STRING,
                              description="Planner 聊天记录中本次请求消息的 msg_id；这是消息编号，不是 QQ 号、目标 QQ 或昵称中的数字。必须使用发起点赞请求那条消息的编号", required=True),
            ToolParameterInfo(name="target_qq", param_type=ToolParamType.STRING,
                              description="目标 QQ，可省略；填写时必须与原始请求指向一致，给我点赞默认使用原始发言者", required=False, default=""),
            ToolParameterInfo(name="times", param_type=ToolParamType.INTEGER,
                              description="用户指定的次数；0 或省略使用配置默认值，禁止从 QQ 号猜测次数", required=False, default=0),
            ToolParameterInfo(name="use_maximum", param_type=ToolParamType.BOOLEAN,
                              description="用户说点满时设为 true，使用配置中的单次上限", required=False, default=False),
        ],
        core_tool=True,
        platforms=["qq"],
    )
    async def tool_like_qq_profile(self, target_qq: str = "", times: int | None = None, use_maximum: bool = False, source_message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        # 模型只选择待处理请求，不提供身份；身份和操作范围来自接收 Hook 的快照。
        if not self.config.plugin.enabled or not self.config.like.enable_llm_tool:
            return self._tool_error("点赞工具当前未启用。")
        if self._closed:
            return self._tool_error("插件已卸载，未发送点赞。")
        try:
            request_key, message = await self._resolve_source_request(source_message_id, kwargs)
            sender = self._sender_id_from_message(message)
            if self.config.admin.admin_only and not self._is_admin(sender):
                raise ValueError("权限不足：原始请求的发送者不是管理员。")
            if type(use_maximum) is not bool:
                raise ValueError("use_maximum 必须是布尔值。")
            text = self._source_text(message)
            self._validate_oral_request(text)
            count, maximum = self._oral_times(text)
            if use_maximum and not maximum:
                raise ValueError("原始请求没有要求点满，未发送点赞。")
            if times is not None and not (type(times) is int and times == 0):
                if self._coerce_times(times) != count:
                    raise ValueError("工具次数与原始请求不一致，未发送点赞。")
            account = self._normalize_user_id(message.get("account_id") or "")
            self_id, _ = await self._get_account(account)
            target = self._oral_target(message, text, self_id)
            if target_qq and self._normalize_user_id(target_qq) != target:
                raise ValueError("工具目标与原始请求不一致，未发送点赞。")
            ok, reply = await self._perform_like(target, count, sender, expected_account=account,
                                                request_key=request_key, source_message=message)
            return {"success": ok, "content": reply, "error": "" if ok else reply}
        except ValueError as exc:
            return self._tool_error(str(exc))
        except Exception:
            self.ctx.logger.exception("口语化点赞请求核验失败")
            return self._tool_error("暂时无法核验原始请求或适配器账号，未发送点赞。")

    @HookHandler("chat.receive.after_process", name="remember_like_source", mode=HookMode.BLOCKING, order=HookOrder.LATE,
                 description="保存宿主接收的真实 QQ 请求消息，供口语化点赞核验身份")
    async def remember_like_source(self, message: Any = None, **kwargs: Any) -> dict[str, str]:
        # 此入口由宿主接收链调用，不接受工具参数补造消息。只缓存小型文本/目标字段。
        result = {"action": "continue"}
        if self._closed or not self.config.plugin.enabled or not self.config.like.enable_llm_tool:
            return result
        if not isinstance(message, dict) or message.get("platform") != "qq":
            return result
        sender = self._sender_id_from_message(message)
        msg_id, session = message.get("message_id"), message.get("session_id")
        if not sender or not isinstance(msg_id, str) or not 1 <= len(msg_id) <= 256 or not isinstance(session, str) or not session:
            return result
        try:
            timestamp = float(message.get("timestamp"))
            if not math.isfinite(timestamp) or not -5 <= time.time() - timestamp <= SOURCE_TTL_SECONDS:
                return result
        except (TypeError, ValueError, OverflowError):
            return result
        segments = message.get("raw_message")
        if not isinstance(segments, list) or len(segments) > 64:
            return result
        clean, length = [], 0
        for segment in segments:
            if not isinstance(segment, dict):
                return result
            kind, data = segment.get("type"), segment.get("data")
            if kind == "text" and isinstance(data, str):
                length += len(data)
                if length > 512:
                    return result
                clean.append({"type": kind, "data": data})
            elif kind in {"at", "reply"}:
                if isinstance(data, (str, int)) and kind == "at":
                    data = {"qq": data}
                if not isinstance(data, dict):
                    return result
                selected = {key: value for key, value in data.items() if key in {
                    "target_user_id", "qq", "target_message_sender_id", "target_user_nickname",
                    "target_user_cardname", "target_message_sender_nickname", "target_message_sender_cardname"
                } and isinstance(value, (str, int)) and len(str(value)) <= 128}
                clean.append({"type": kind, "data": selected})
        if message.get("account_id") and not self._normalize_user_id(message["account_id"]):
            return result
        snapshot = {key: message.get(key) for key in ("message_id", "platform", "session_id", "account_id", "scope", "timestamp")}
        user = message["message_info"]["user_info"]
        snapshot["message_info"] = {"user_info": {key: value for key, value in user.items()
            if key in {"user_id", "user_nickname", "user_cardname"} and isinstance(value, (str, int)) and len(str(value)) <= 128}}
        snapshot["raw_message"] = clean
        request_key = "oral:" + hashlib.sha256(json.dumps(["qq", snapshot.get("account_id"),
            snapshot.get("scope"), session, msg_id, sender], ensure_ascii=False).encode()).hexdigest()
        now = time.monotonic()
        self._source_messages = {key: value for key, value in self._source_messages.items() if now - value[0] <= SOURCE_TTL_SECONDS}
        created = now - max(0, time.time() - timestamp)
        self._source_messages.setdefault(request_key, (created, snapshot))
        while len(self._source_messages) > MAX_SOURCE_MESSAGES:
            self._source_messages.pop(next(iter(self._source_messages)))
        return result

    def _find_source_request(self, msg_id: Any, stream_id: str = "") -> tuple[str, dict[str, Any]]:
        if not isinstance(msg_id, str) or not msg_id.strip():
            raise ValueError("缺少原始请求消息编号，请使用聊天记录中发出点赞请求的消息 ID。")
        now = time.monotonic()
        candidates = [(key, message) for key, (created, message) in self._source_messages.items()
                      if now - created <= SOURCE_TTL_SECONDS and message["message_id"] == msg_id.strip()
                      and (not stream_id or message["session_id"] == stream_id)]
        if len(candidates) != 1:
            raise ValueError("没有唯一匹配的近期原始请求，或请求已过期；请勿猜测发言者。")
        return candidates[0]

    async def _resolve_source_request(self, msg_id: Any, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """从本地快照或 MaiBot 消息能力解析 Planner 消息编号。"""
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        chat_id = str(kwargs.get("chat_id") or "").strip()
        if chat_id and chat_id != stream_id:
            raise ValueError("当前聊天流信息不一致，未发送点赞。")
        if kwargs.get("platform") and str(kwargs["platform"]).strip().lower() != "qq":
            raise ValueError("本插件仅支持 QQ 平台。")
        if not isinstance(msg_id, str) or not msg_id.strip():
            raise ValueError("缺少原始请求消息编号，请使用聊天记录中发出点赞请求的消息 ID。")
        try:
            return self._find_source_request(msg_id, stream_id)
        except ValueError as cached_error:
            normalized_id = str(msg_id or "").strip()
            if not normalized_id or not stream_id:
                raise cached_error
            message_capability = getattr(getattr(self.ctx, "message", None), "get_by_id", None)
            if not callable(message_capability):
                raise cached_error
            try:
                resolved = await message_capability(
                    normalized_id,
                    chat_id=stream_id,
                    stream_id=stream_id,
                    include_binary_data=False,
                )
            except Exception:
                self.ctx.logger.exception("按 Planner 消息编号查询原始消息失败")
                raise cached_error
            if not isinstance(resolved, dict):
                raise cached_error
            resolved_id = str(resolved.get("message_id") or "").strip()
            resolved_session = str(resolved.get("session_id") or "").strip()
            if resolved_id != normalized_id or resolved_session != stream_id:
                raise cached_error
            await self.remember_like_source(message=resolved)
            return self._find_source_request(normalized_id, stream_id)

    @staticmethod
    def _source_text(message: dict[str, Any]) -> str:
        return " ".join(segment["data"] for segment in message["raw_message"] if segment["type"] == "text").strip()

    @staticmethod
    def _validate_oral_request(text: str) -> None:
        if text.startswith(("/", ">")) or chr(96) in text or re.search(r"(?<!能)不能", text) or any(word in text for word in ("不要", "不用", "别给", "别点", "不必", "取消", "撤销", "示例", "举例", "用法", "测试", "引用", "如果", "假如", "“", "”", '"', "'", "「", "」", "『", "』", "不是", "不想", "别帮", "不给", "不点赞", "不点", "没让", "未让")):
            raise ValueError("原始消息不是可直接执行的点赞请求，未发送点赞。")
        if not re.search(r"点赞|点[^\n]{0,20}赞|(?:来|送)[^\n]{0,12}赞|赞我|给[^\n]{0,30}赞|点满", text):
            raise ValueError("原始消息没有明确的点赞请求，未发送点赞。")
        if not re.search(r"给|帮|请|求|麻烦|来|送|赞我|点满|点个赞|点一个赞", text):
            raise ValueError("原始消息没有明确的执行意图，未发送点赞。")

    def _oral_times(self, text: str) -> tuple[int, bool]:
        values = []
        number = r"([+-]?[0-9]+(?:[.][0-9]+)?|[零〇一二两三四五六七八九十百]+|[０-９]+|[²³¹]+)"
        patterns = (r"(?:点|来|送)\s*([^\s,，。！？!?]{1,16}?)\s*(?:个|次)\s*(?:点赞|赞)",
                    number + r"\s*(?:个|次)\s*(?:点赞|赞)",
                    r"(?:点赞|赞)\s*([^\s,，。！？!?]{1,16}?)\s*(?:个|次)(?:[啦吧啊!！?？。，\s]|$)",
                    r"(?:点|来|送)\s*" + number + r"\s*赞",
                    r"(?:点赞|赞)\s*" + number + r"(?:[啦吧啊!！?？。，\s]|$)")
        spans = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                if not any(match.start() < end and start < match.end() for start, end in spans):
                    token = match.group(1)
                    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
                    digits.update({"两": 2, "〇": 0})
                    if token in digits:
                        count = digits[token]
                    elif re.fullmatch(r"[一二两]?十[一二三四五六七八九]?", token):
                        left, right = token.split("十")
                        count = digits.get(left, 1) * 10 + digits.get(right, 0)
                    else:
                        count = token
                    values.append(self._coerce_times(count))
                    spans.append(match.span())
        if len(values) > 1:
            raise ValueError("原始请求包含多份次数，请明确一个次数。")
        maximum = any(word in text for word in ("点满", "赞满", "全部点亮"))
        if maximum and values and values[0] != self.config.like.max_times:
            raise ValueError("点满与指定次数冲突，未发送点赞。")
        return (self.config.like.max_times if maximum else values[0] if values else self._coerce_times(None)), maximum

    def _oral_target(self, message: dict[str, Any], text: str, self_id: str) -> str:
        ats, replies = [], []
        for segment in message["raw_message"]:
            data = segment.get("data")
            if segment["type"] == "at":
                uid = self._normalize_user_id(data.get("target_user_id") or data.get("qq"))
                if uid == self_id:
                    continue
                if not uid or not self.config.like.allow_at_target:
                    raise ValueError("原始请求的 @目标无效或不被配置允许。")
                ats.append(uid)
            elif segment["type"] == "reply":
                replies.append(self._normalize_user_id(data.get("target_message_sender_id")))
        numbers = re.findall(r"(?:给|帮|为|赞)\s*(?:QQ[:：]?\s*)?([0-9０-９]{5,32})(?=\s|点|赞|来|送|$)", text, re.IGNORECASE)
        if numbers and not self.config.like.allow_raw_qq:
            raise ValueError("当前配置不允许原始请求直接指定 QQ 号。")
        normalized = [self._normalize_user_id(number) for number in numbers]
        if numbers and not all(normalized):
            raise ValueError("原始请求指定的 QQ 号无效。")
        targets = set(ats + normalized)
        if len(targets) > 1:
            raise ValueError("原始请求包含多个点赞目标，未发送点赞。")
        if targets:
            return targets.pop()
        if re.search(r"给我|赞我|帮我(?!\s*给)|我(?:也)?(?:要|想要)", text):
            return self._sender_id_from_message(message)
        names: dict[str, set[str]] = {}
        for created, candidate in self._source_messages.values():
            if (time.monotonic() - created > SOURCE_TTL_SECONDS or
                any(candidate.get(key) != message.get(key) for key in ("session_id", "account_id", "scope"))):
                continue
            user = candidate["message_info"]["user_info"]
            uid = self._normalize_user_id(user.get("user_id"))
            for key in ("user_nickname", "user_cardname"):
                name = user.get(key)
                if isinstance(name, str) and 2 <= len(name) <= 64 and uid != self_id:
                    names.setdefault(name, set()).add(uid)
        matched = {uid for name, ids in names.items() if re.search(r"(?:给|帮|为)\s*" + re.escape(name) + r"\s*(?:点|赞|来|送)", text) for uid in ids}
        if len(matched) > 1:
            raise ValueError("昵称对应多个用户，请 @ 明确目标。")
        if matched:
            if not self.config.like.allow_raw_qq:
                raise ValueError("昵称指定目标需启用直接指定 QQ，或使用真实 @。")
            return matched.pop()
        pronoun = re.search(r"(?:给|为)(?:他|她|TA|ta|对方)(?:点|赞)", text)
        if pronoun and not replies:
            raise ValueError("无法确认他或她指向谁，请 @ 或回复目标消息。")
        if re.search(r"(?:给|为)\s*[^\s，。！？,!?]+", text) and not re.search(r"给(?:我|个赞|一个赞)", text) and not pronoun:
            raise ValueError("无法唯一核验原始请求指向的用户，请 @ 明确目标。")
        if replies:
            if not self.config.like.allow_reply_target or not all(replies) or len(set(replies)) != 1:
                raise ValueError("原始请求的回复目标无效或不被配置允许。")
            return replies[0]
        return self._sender_id_from_message(message)

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {"success": False, "content": message, "error": message}

    @Command("qq_like", description="给 QQ 名片点赞", pattern=COMMAND_PATTERN, platforms=["qq"])
    async def cmd_qq_like(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        if not self.config.plugin.enabled or self._closed:
            return False, None, True
        stream = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return False, None, True
        try:
            parsed = self._parse_message(message)
            if parsed is None:
                return False, None, True
            platform = str(message.get("platform") or kwargs.get("platform") or "").lower()
            if platform != "qq" or (kwargs.get("platform") and str(kwargs["platform"]).lower() != "qq"):
                raise ValueError("本插件仅支持 QQ 平台。")
            sender = self._sender_id_from_message(message)
            supplied = self._normalize_user_id(kwargs.get("user_id", ""))
            if not sender or (kwargs.get("user_id") and supplied != sender):
                raise ValueError("无法核验本次命令的发送者。")
            target, count = parsed
            account = message.get("account_id") or ""
            if account and not self._normalize_user_id(account):
                raise ValueError("无法核验消息所属的机器人账号。")
            ok, reply = await self._perform_like(target, count, sender, expected_account=account, source_message=message)
        except ValueError as exc:
            ok, reply = False, str(exc)
        await self._reply(stream, reply)
        return ok, None, True

    def _parse_message(self, message: dict[str, Any]) -> tuple[str, int] | None:
        segments = message.get("raw_message")
        if not isinstance(segments, list) or len(segments) > 64:
            raise ValueError("消息段无效或过长。")
        texts, ats, replies = [], [], []
        size = 0
        unsupported = False
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("消息段无效。")
            kind, data = segment.get("type"), segment.get("data")
            if kind == "text":
                if not isinstance(data, str):
                    raise ValueError("消息文本无效。")
                size += len(data)
                if size > 512:
                    raise ValueError("点赞命令过长，最多允许 512 个字符。")
                texts.append(data)
            elif kind == "at":
                uid = (data.get("target_user_id") or data.get("qq")) if isinstance(data, dict) else data
                ats.append(self._normalize_user_id(uid))
                texts.append(" @ ")
            elif kind == "reply":
                replies.append(self._normalize_user_id(data.get("target_message_sender_id")) if isinstance(data, dict) else "")
            else:
                unsupported = True
        raw = "".join(texts).strip()
        # 仅匹配本次原始文本开头；不读取引用内容、昵称和 processed_plain_text。
        match = re.match(r"^/(?:点赞|赞|zan)(?=\s|$)", raw)
        if not match:
            return None
        if unsupported:
            raise ValueError("点赞命令不能混入图片、转发等消息段。")
        rest = raw[match.end():].strip()
        tokens = re.split(r"[\s,，、]+", rest) if rest else []
        if tokens.count("@") != len(ats):
            raise ValueError("请使用真实的 @消息段指定目标。")
        raw_targets, counts = [], []
        for token in tokens:
            if token == "@":
                continue
            normalized = self._normalize_user_id(token)
            if normalized:
                raw_targets.append(normalized)
            else:
                counts.append(self._coerce_times(token))
        if len(counts) > 1:
            raise ValueError("请只指定一次点赞次数。")
        if raw_targets and not self.config.like.allow_raw_qq:
            raise ValueError("当前配置禁止直接输入 QQ 号。")
        if ats and (not self.config.like.allow_at_target or not all(ats)):
            raise ValueError("@目标无效或当前配置禁止 @ 指定目标。")
        targets = set(raw_targets + ats)
        if len(targets) > 1:
            raise ValueError("一次命令只能指定一个点赞目标。")
        if not targets:
            if not replies or not self.config.like.allow_reply_target or not all(replies) or len(set(replies)) != 1:
                raise ValueError("请通过 @、回复消息或 QQ 号明确指定一个目标。")
            targets = set(replies)
        return targets.pop(), counts[0] if counts else self._coerce_times(None)

    def _coerce_times(self, value: Any) -> int:
        if value is None:
            return min(self.config.like.default_times, self.config.like.max_times)
        if type(value) is int:
            count = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}", value):
            count = int(value)
        else:
            raise ValueError("点赞次数必须是整数，不能含小数、负号或其他文字。")
        if not 1 <= count <= self.config.like.max_times:
            raise ValueError(f"点赞次数必须在 1 到 {self.config.like.max_times} 之间。")
        return count

    async def _perform_like(self, target_id: str, times: int, sender_id: str, *, expected_account: str = "", request_key: str = "", source_message: dict[str, Any] | None = None) -> tuple[bool, str]:
        if self._closed or not self.config.plugin.enabled:
            return False, "点赞功能当前未启用。"
        if self._storage_error:
            return False, self._storage_error
        target, sender = self._normalize_user_id(target_id), self._normalize_user_id(sender_id)
        if not target or not sender:
            return False, "目标或发送者 QQ 无效。"
        if self.config.admin.admin_only and not self._is_admin(sender):
            return False, "权限不足：仅管理员可用。"
        try:
            times = self._coerce_times(times)
        except ValueError as exc:
            return False, str(exc)
        try:
            # 每次重新查询，无永久 self_id 缓存。登录信息与发送使用同一命名空间。
            self_id, namespace = await self._get_account(self._normalize_user_id(expected_account))
        except Exception:
            self.ctx.logger.exception("获取机器人 QQ 失败")
            return False, "无法确认机器人当前账号，已停止点赞，请检查适配器连接。"
        if target == self_id:
            return False, "不能给机器人自己点赞。"
        if self._closed or not self.config.plugin.enabled:
            return False, "插件已停用或正在卸载，未发送点赞。"
        if self.config.admin.admin_only and not self._is_admin(sender):
            return False, "权限配置已更新，当前用户无权执行点赞。"
        if request_key and not self.config.like.enable_llm_tool:
            return False, "自然语言点赞已关闭，未发送请求。"
        if source_message is not None:
            try:
                if request_key:
                    _, current = self._find_source_request(source_message["message_id"], source_message["session_id"])
                    text = self._source_text(current)
                    current_target = self._oral_target(current, text, self_id)
                    current_times, _ = self._oral_times(text)
                else:
                    parsed = self._parse_message(source_message)
                    if parsed is None:
                        raise ValueError("原始消息不是点赞命令，未发送请求。")
                    current_target, current_times = parsed
                if (current_target, current_times) != (target, times):
                    raise ValueError("点赞配置已更新，请按当前配置重新发送请求。")
            except ValueError as exc:
                return False, str(exc)
        try:
            reservation = self._reserve_like(target, sender, times, request_key=request_key)
        except Exception:
            self.ctx.logger.exception("预占点赞额度失败")
            return False, "点赞账本无法写入，未发送请求，请检查文件和磁盘。"
        if isinstance(reservation, str):
            return False, reservation
        return await self._submit_reserved_like(namespace, target, reservation)

    async def _submit_reserved_like(self, namespace: str, target: str, reservation: tuple[str, int]) -> tuple[bool, str]:
        request_id, count = reservation
        try:
            response = await self._api(namespace, SEND_LIKE_API_SUFFIX, {"user_id": int(target), "times": count})
        except BaseException as exc:
            # 包括取消：已落盘 reserved 记录仍占额度，重启后也不会重发。
            self._finish_request(request_id, "unknown")
            if not isinstance(exc, Exception):
                raise
            self.ctx.logger.warning("点赞请求结果未确认 request=%s: %s", request_id, exc)
            return False, "点赞结果未确认，已保留本次额度以防重复；请勿重复触发。"
        if not self._is_api_success(response):
            self._finish_request(request_id, "unknown")
            self.ctx.logger.warning("点赞未确认 request=%s target=%s response=%r", request_id, target, response)
            return False, "适配器未确认点赞成功，已保留本次额度以防重复；请检查适配器日志。"
        stored = self._finish_request(request_id, "confirmed")
        if not stored:
            return False, "适配器返回成功，但账本确认写入失败；原预占额度仍保留，请勿重复触发。"
        self.ctx.logger.info("点赞请求已确认 request=%s target=%s times=%s", request_id, target, count)
        return True, f"已向 QQ 提交给 {target} 点赞 {count} 次的请求（实际到账以 QQ 为准）。"

    def _connect(self) -> sqlite3.Connection:
        if self._db_path is None:
            raise RuntimeError("账本尚未初始化")
        connection = sqlite3.connect(self._db_path, timeout=2, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize_ledger(self) -> None:
        connection = self._connect()
        try:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("账本完整性检查失败")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {"metadata", "requests", "legacy_cooldowns"}:
                raise ValueError("账本表结构缺失或不受支持，不能按空额度重建")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL,
                times INTEGER NOT NULL CHECK(typeof(times)='integer' AND times>0),
                created REAL NOT NULL CHECK(created>=0),
                state TEXT NOT NULL CHECK(state IN ('reserved','confirmed','unknown'))
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS request_target ON requests(target)")
            connection.execute("CREATE INDEX IF NOT EXISTS request_sender ON requests(sender)")
            connection.execute("CREATE TABLE IF NOT EXISTS legacy_cooldowns (sender TEXT PRIMARY KEY, created REAL NOT NULL)")
            version = connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            if version is None:
                if connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]:
                    raise ValueError("账本缺少版本信息")
                self._migrate_legacy(connection)
                connection.execute("INSERT INTO metadata VALUES ('schema', '1')")
            elif version != ("1",):
                raise ValueError("不支持的账本版本")
            for _, day, target, sender, count, created, state in connection.execute("SELECT * FROM requests"):
                parsed_day = date.fromisoformat(day)
                if (parsed_day.isoformat() != day or self._normalize_user_id(target) != target or not target or
                    (sender and self._normalize_user_id(sender) != sender) or
                    type(count) is not int or count < 1 or not math.isfinite(created) or created < 0 or
                    state not in {"reserved", "confirmed", "unknown"}):
                    raise ValueError("账本内容无效")
            for sender, created in connection.execute("SELECT sender,created FROM legacy_cooldowns"):
                if (not sender or self._normalize_user_id(sender) != sender or
                    type(created) not in (int, float) or not math.isfinite(created) or created < 0):
                    raise ValueError("账本冷却记录无效")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate_legacy(self, connection: sqlite3.Connection) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("date"), str):
            raise ValueError("旧状态格式无效")
        day = data["date"]
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("旧状态日期无效")
        if not isinstance(data.get("targets"), dict) or not isinstance(data.get("cooldown"), dict):
            raise ValueError("旧状态格式无效")
        targets, cooldowns = {}, {}
        for key, count in data["targets"].items():
            target = self._normalize_user_id(key)
            if not target or type(count) is not int or count < 0:
                raise ValueError("旧额度记录无效")
            targets[target] = targets.get(target, 0) + count
        for key, timestamp in data["cooldown"].items():
            sender = self._normalize_user_id(key)
            if not sender or type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("旧冷却记录无效")
            cooldowns[sender] = max(cooldowns.get(sender, 0), timestamp)
        # 别名额度累加、冷却取最晚值；旧 JSON 原样保留，迁移只发生一次。
        for target, count in targets.items():
            if count:
                connection.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                                   (uuid.uuid4().hex, day, target, "", count, 0, "confirmed"))
        connection.executemany("INSERT INTO legacy_cooldowns VALUES (?,?)", cooldowns.items())

    def _reserve_like(self, target: str, sender: str, requested: int, *, request_key: str = "", schedule_day: str = "") -> tuple[str, int] | str:
        connection = self._connect()
        try:
            now, today = time.time(), time.strftime("%Y-%m-%d")
            if schedule_day:
                if schedule_day != today or sender or request_key != self._schedule_key(today, target):
                    return "定时任务日期已变更或参数无效，未发送请求。"
                if connection.execute("SELECT 1 FROM metadata WHERE key=?", (request_key,)).fetchone():
                    return "该号码今天的定时任务已处理，不会重复执行。"
            if request_key and connection.execute("SELECT 1 FROM requests WHERE id=?", (request_key,)).fetchone():
                return "这条原始消息已处理或结果待确认，不会重复发送点赞。"
            # 未确认请求跨日继续占额，防止昨天的迟到请求与今天额度叠加。
            used = connection.execute("SELECT COALESCE(SUM(times),0) FROM requests WHERE target=? AND (day>=? OR state!='confirmed')", (target, today)).fetchone()[0]
            last = connection.execute("SELECT COALESCE(MAX(created),0) FROM requests WHERE sender=?", (sender,)).fetchone()[0]
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_cooldowns'").fetchone():
                row = connection.execute("SELECT created FROM legacy_cooldowns WHERE sender=?", (sender,)).fetchone()
                if row:
                    last = max(last, row[0])
            cooldown = self.config.like.cooldown_seconds
            if not schedule_day and cooldown and now - last < cooldown:
                return f"别急，{math.ceil(cooldown - (now - last))} 秒后再试吧。"
            remaining = self.config.like.daily_limit_per_target - used
            if remaining <= 0:
                if schedule_day:
                    connection.execute("INSERT INTO metadata VALUES (?, ?)", (request_key, "skipped"))
                    connection.commit()
                return "该目标今日额度已用完或被未确认请求占用，请勿重复触发。"
            count, request_id = min(requested, remaining, self.config.like.max_times), request_key or uuid.uuid4().hex
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?)", (request_id, today, target, sender, count, now, "reserved"))
            if schedule_day:
                connection.execute("INSERT INTO metadata VALUES (?, ?)", (request_key, "submitted"))
            connection.commit()
            return request_id, count
        finally:
            # 失败或提前返回时回滚；绝不能先发请求再尝试落盘。
            connection.close()

    def _finish_request(self, request_id: str, state: str) -> bool:
        try:
            connection = self._connect()
            try:
                # 迟到的成功计入完成当天，不能释放当天已被旧请求占用的额度。
                changed = connection.execute("UPDATE requests SET state=?, day=MAX(day,?) WHERE id=? AND state='reserved'",
                                             (state, time.strftime("%Y-%m-%d"), request_id)).rowcount
                if changed != 1:
                    raise ValueError("请求预占记录不存在或已结算")
                connection.commit()
            finally:
                connection.close()
            return True
        except Exception:
            self.ctx.logger.exception("点赞账本结算失败 request=%s", request_id)
            return False

    async def _api(self, namespace: str, suffix: str, params: dict[str, Any] | None = None) -> Any:
        options: dict[str, Any] = {}
        if namespace == "adapter.snowluma":
            options["version"] = ADAPTER_API_VERSION
        if params is not None:
            options["params"] = params
        return await self.ctx.api.call(namespace + suffix, **options)

    async def _get_account(self, expected_account: str = "") -> tuple[str, str]:
        for namespace in ADAPTER_NAMESPACES:
            version = ADAPTER_API_VERSION if namespace == "adapter.snowluma" else ""
            # 先查询无副作用的 API 元信息，缺少登录或点赞接口时直接检查下一种适配器。
            available = True
            for suffix in (GET_LOGIN_INFO_API_SUFFIX, SEND_LIKE_API_SUFFIX):
                metadata = await self.ctx.api.get(namespace + suffix, version=version)
                if metadata is None:
                    available = False
                    break
                if not isinstance(metadata, dict) or not metadata or metadata.get("success") is False:
                    raise RuntimeError("无法发现适配器接口")
            if not available:
                continue
            try:
                result = await self._api(namespace, GET_LOGIN_INFO_API_SUFFIX)
            except Exception as exc:
                if self._is_missing_api_error(exc):
                    continue
                raise
            if self._is_missing_api_result(result):
                continue
            layers = self._api_response_layers(result)
            payload = layers[-1]
            uid = self._normalize_user_id(payload.get("user_id") or payload.get("qq") or payload.get("uin")) if isinstance(payload, dict) else ""
            if not uid:
                raise RuntimeError("登录信息中缺少有效 QQ")
            if expected_account and uid != expected_account:
                continue
            return uid, namespace
        raise RuntimeError("未找到 NapCat/SnowLuma 登录接口")

    @staticmethod
    def _is_missing_api_result(result: Any) -> bool:
        # 仅识别 Host 路由层的明确错误；业务错误、未知响应和超时不能触发重发。
        if not isinstance(result, dict) or result.get("success") is not False:
            return False
        error = result.get("error")
        return isinstance(error, str) and error.startswith("未找到 API: adapter.")

    @staticmethod
    def _is_missing_api_error(exc: Exception) -> bool:
        text = str(exc)
        return text.startswith("未找到 API: adapter.") or text.lower().startswith("api not found: adapter.")

    @staticmethod
    def _api_response_layers(response: Any) -> list[dict[str, Any]]:
        layers = []
        for _ in range(8):
            if not isinstance(response, dict):
                raise ValueError("适配器返回格式无效。")
            if "success" in response and response["success"] is not True:
                raise ValueError("适配器返回失败。")
            if "retcode" in response and (type(response["retcode"]) is not int or response["retcode"] != 0):
                raise ValueError("适配器返回非成功状态。")
            if "status" in response and response["status"] != "ok":
                raise ValueError("适配器返回非成功状态。")
            if response.get("error"):
                raise ValueError("适配器返回错误信息。")
            layers.append(response)
            if "result" in response:
                response = response["result"]
            elif isinstance(response.get("data"), dict):
                response = response["data"]
            else:
                return layers
        raise ValueError("适配器返回嵌套过深。")

    @classmethod
    def _is_api_success(cls, response: Any) -> bool:
        try:
            layers = cls._api_response_layers(response)
        except ValueError:
            return False
        # result 的外层 success 只表示转发成功，不能据此判断 QQ 侧的处理结果。
        return any("result" not in layer and (
            layer.get("success") is True or layer.get("status") == "ok" or
            (type(layer.get("retcode")) is int and layer["retcode"] == 0)
        ) for layer in layers)

    @staticmethod
    def _normalize_user_id(raw: Any) -> str:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            return ""
        value = str(raw).strip()
        if value.lower().startswith("qq:"):
            value = value[3:].strip()
        if not value or len(value) > 32:
            return ""
        try:
            digits = "".join(str(unicodedata.decimal(char)) for char in value)
        except (ValueError, TypeError):
            return ""
        digits = digits.lstrip("0")
        return digits if 5 <= len(digits) <= 12 else ""

    @staticmethod
    def _sender_id_from_message(message: dict[str, Any]) -> str:
        info = message.get("message_info")
        user = info.get("user_info") if isinstance(info, dict) else None
        return QQLikePlugin._normalize_user_id(user.get("user_id")) if isinstance(user, dict) else ""

    def _is_admin(self, user_id: str) -> bool:
        return user_id in {self._normalize_user_id(token) for token in re.split(r"[\s,，;；]+", self.config.admin.admins or "") if token}

    async def _reply(self, stream_id: str, text: str) -> None:
        if stream_id:
            try:
                await self.ctx.send.text(text, stream_id)
            except Exception:
                self.ctx.logger.exception("发送点赞回复失败")


def create_plugin() -> QQLikePlugin:
    return QQLikePlugin()
