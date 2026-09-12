"""MaiBot QQ 名片点赞插件。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import (
    CONFIG_RELOAD_SCOPE_SELF,
    ToolParameterInfo,
    ToolParamType,
)

SUPPORTED_CONFIG_VERSION = "0.2.0"

SEND_LIKE_API = "adapter.napcat.account.send_like"
GET_LOGIN_INFO_API = "adapter.napcat.system.get_login_info"

COMMAND_PATTERN = r"(?<!\S)/?(?:点赞|赞|zan)(?:\s+(?P<rest>.+?))?\s*$"

STATE_FILE_NAME = "like_state.json"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "extension"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class LikeSectionConfig(PluginConfigBase):
    __ui_label__ = "点赞设置"
    __ui_icon__ = "thumb_up"
    __ui_order__ = 1

    default_times: int = Field(default=10, ge=1, le=10, description="未指定次数时的默认点赞次数")
    max_times: int = Field(default=10, ge=1, le=10, description="单次命令允许的最大点赞次数")
    daily_limit_per_target: int = Field(default=10, ge=1, le=50, description="同一目标每天最多被点赞的次数")
    cooldown_seconds: int = Field(default=30, ge=0, le=3600, description="同一用户触发命令的冷却秒数")
    allow_at_target: bool = Field(default=True, description="允许通过 @某人 指定目标")
    allow_reply_target: bool = Field(default=True, description="允许通过回复消息指定目标")
    allow_raw_qq: bool = Field(default=True, description="允许直接输入 QQ 号指定目标")
    enable_llm_tool: bool = Field(default=True, description="启用 AI 口语化触发")


class AdminSectionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    admin_only: bool = Field(default=False, description="是否仅允许管理员使用")
    admins: str = Field(
        default="",
        description="管理员 QQ 号，逗号分隔",
        json_schema_extra={"placeholder": "123456789, qq:987654321"},
    )


class QQLikeConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    like: LikeSectionConfig = Field(default_factory=LikeSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)


class QQLikePlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = QQLikeConfig

    def __init__(self) -> None:
        super().__init__()
        self._state_path: Path | None = None
        self._state: dict[str, Any] = {"date": "", "targets": {}, "cooldown": {}}
        self._lock = asyncio.Lock()
        self._self_id: str = ""

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / STATE_FILE_NAME
        self._state = self._read_state()
        self.ctx.logger.info(
            "QQ 名片点赞插件已加载 enabled=%s, llm_tool=%s",
            self.config.plugin.enabled,
            self.config.like.enable_llm_tool,
        )

    async def on_unload(self) -> None:
        self._write_state()
        self.ctx.logger.info("QQ 名片点赞插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("配置已热更新 version=%s", version)

    # ------------------------------------------------------------------ #
    # 口语化触发：让 AI 自主调用的工具
    # ------------------------------------------------------------------ #
    @Tool(
        "like_qq_profile",
        brief_description="给 QQ 用户名片点赞",
        detailed_description=(
            "当用户在聊天中请求给某人点赞时调用此工具。\n"
            "触发场景举例：'给我点个赞'、'帮我点赞'、'给某某点赞'、'给 123456789 点赞'、'帮我给群友点个赞'。\n"
            "参数说明：\n"
            "- target_qq：string，可选。要点赞的 QQ 号。不填则默认给当前发言者点赞。\n"
            "- times：integer，可选。点赞次数，默认 10。\n"
            "调用后请用自然口语回复用户，例如'好嘞，已经给你点满啦~'。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target_qq",
                param_type=ToolParamType.STRING,
                description="要点赞的 QQ 号，不填则给当前发言者点赞",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="times",
                param_type=ToolParamType.INTEGER,
                description="点赞次数，默认 10",
                required=False,
                default=10,
            ),
        ],
        core_tool=True,
    )
    async def tool_like_qq_profile(
        self, target_qq: str = "", times: int = 10, **kwargs: Any
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled or not self.config.like.enable_llm_tool:
            return {"content": "点赞功能当前未启用。"}

        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        sender_id = self._sender_id_from_message(message)
        target = self._normalize_user_id(str(target_qq or ""))
        if not target:
            target = sender_id
        if not target:
            return {"content": "没有识别出要点赞的目标，请让用户说明要赞谁。"}

        # ★ 修改点：校验必须是纯数字，且长度在 5-12 位之间（QQ 号规范）
        if not re.fullmatch(r"\d{5,12}", target):
            return {
                "content": (
                    f"识别到的目标 '{target}' 不是有效的 QQ 号，"
                    "请让用户提供正确的 5-12 位纯数字 QQ 号。"
                )
            }

        ok, msg = await self._perform_like(target, int(times or 10), sender_id)
        return {"content": msg}

    # ------------------------------------------------------------------ #
    # 命令触发（/赞 等）
    # ------------------------------------------------------------------ #
    @Command("qq_like", description="给 QQ 名片点赞", pattern=COMMAND_PATTERN)
    async def cmd_qq_like(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not self.config.plugin.enabled:
            return False, "插件未启用", True

        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        sender_id = self._normalize_user_id(str(kwargs.get("user_id") or ""))
        rest = str((kwargs.get("matched_groups") or {}).get("rest") or "").strip()

        raw_qq, times = self._parse_args(rest)
        self_id = await self._ensure_self_id()

        target_id = ""
        if raw_qq and self.config.like.allow_raw_qq:
            target_id = raw_qq
        if not target_id:
            target_id, reason = self._resolve_target_from_message(message, self_id)
            if not target_id:
                await self._reply(
                    stream_id,
                    reason or "没找到点赞目标，用法：/赞 @某人、回复消息发 /赞，或 /赞 QQ号",
                )
                return False, "无有效目标", True

        if times is None:
            times = self.config.like.default_times

        ok, msg = await self._perform_like(target_id, int(times), sender_id)

        if ok:
            display = self._display_name(target_id, message)
            await self._reply(stream_id, f"已给 {display} 点了 {times} 次赞 ✓")
        else:
            await self._reply(stream_id, msg)

        return ok, msg, True

    # ------------------------------------------------------------------ #
    # 核心逻辑：点赞（含额度 / 冷却 / 权限校验）
    # ------------------------------------------------------------------ #
    async def _perform_like(self, target_id: str, times: int, sender_id: str) -> tuple[bool, str]:
        if not target_id:
            return False, "没找到点赞目标。"

        if self.config.admin.admin_only and not self._is_admin(sender_id):
            return False, "权限不足：仅管理员可用。"

        self_id = await self._ensure_self_id()
        if self_id and target_id == self_id:
            return False, "不能给自己点赞哦。"

        if times is None or times <= 0:
            times = self.config.like.default_times
        times = max(1, min(times, self.config.like.max_times))

        now = time.time()
        async with self._lock:
            self._roll_state_date()
            error_msg = self._check_and_reserve(sender_id, target_id, now)
            if error_msg:
                return False, error_msg
            limit = self.config.like.daily_limit_per_target
            used = int(self._state["targets"].get(target_id, 0) or 0)
            times = max(1, min(times, limit - used))
            self._write_state()

        try:
            resp = await self.ctx.api.call(
                SEND_LIKE_API,
                params={"user_id": int(target_id), "times": times},
            )
        except Exception as exc:
            self.ctx.logger.exception("调用 %s 失败", SEND_LIKE_API)
            return False, f"点赞失败：{exc}"

        if isinstance(resp, dict) and resp.get("success") is False:
            detail = str(resp.get("error") or "未知错误")
            self.ctx.logger.warning("send_like 失败: %s", detail)
            return False, f"点赞失败：{detail}"

        if not self._is_napcat_ok(resp):
            detail = ""
            if isinstance(resp, dict):
                detail = str(
                    resp.get("wording") or resp.get("message") or resp.get("error")
                    or resp.get("retcode") or ""
                )
            self.ctx.logger.warning("send_like 协议端返回异常: %s", resp)
            return False, f"点赞失败：{detail or '协议端返回异常'}"

        async with self._lock:
            self._roll_state_date()
            self._state["targets"][target_id] = (
                int(self._state["targets"].get(target_id, 0) or 0) + times
            )
            self._write_state()

        return True, f"已给 {target_id} 点了 {times} 次赞"

    # ------------------------------------------------------------------ #
    # 参数解析
    # ------------------------------------------------------------------ #
    def _parse_args(self, rest: str) -> tuple[str, int | None]:
        target = ""
        times: int | None = None
        for token in re.split(r"[\s,，、]+", rest.strip()):
            if not token.isdigit():
                continue
            if 5 <= len(token) <= 12:
                target = target or token
            elif 1 <= len(token) <= 2 and 1 <= int(token) <= 10:
                times = times if times is not None else int(token)
        return target, times

    def _resolve_target_from_message(self, message: dict[str, Any], self_id: str) -> tuple[str, str]:
        segments = message.get("raw_message") or []
        if not isinstance(segments, list):
            return "", "消息段解析失败。"

        if self.config.like.allow_at_target:
            for seg in segments:
                if not isinstance(seg, dict) or seg.get("type") != "at":
                    continue
                data = seg.get("data")
                if isinstance(data, dict):
                    uid = data.get("target_user_id") or data.get("qq")
                else:
                    uid = data
                uid = self._normalize_user_id(str(uid or ""))
                if uid and uid != "all" and uid != self_id:
                    return uid, ""

        if self.config.like.allow_reply_target:
            for seg in segments:
                if not isinstance(seg, dict) or seg.get("type") != "reply":
                    continue
                data = seg.get("data")
                if not isinstance(data, dict):
                    continue
                uid = self._normalize_user_id(str(data.get("target_message_sender_id") or ""))
                if uid and uid != self_id:
                    return uid, ""

        return "", ""

    @staticmethod
    def _sender_id_from_message(message: dict[str, Any]) -> str:
        """从消息 dict 里拿发言者 QQ 号。"""
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            user = info.get("user_info") or {}
            if isinstance(user, dict):
                uid = user.get("user_id")
                if uid:
                    return QQLikePlugin._normalize_user_id(str(uid))
        uid = message.get("user_id")
        if uid:
            return QQLikePlugin._normalize_user_id(str(uid))
        return ""

    @staticmethod
    def _display_name(target_id: str, message: dict[str, Any]) -> str:
        segments = message.get("raw_message") or []
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") not in ("at", "reply"):
                    continue
                data = seg.get("data")
                if not isinstance(data, dict):
                    continue
                uid = (
                    data.get("target_user_id")
                    or data.get("target_message_sender_id")
                    or data.get("qq")
                )
                if str(uid or "") != target_id:
                    continue
                name = data.get("target_user_nickname") or data.get("target_message_sender_nickname")
                if name:
                    return str(name)
        return target_id

    def _check_and_reserve(self, sender_id: str, target_id: str, now: float) -> str:
        cooldown = self.config.like.cooldown_seconds
        if cooldown > 0 and sender_id:
            last_ts = float(self._state["cooldown"].get(sender_id, 0.0) or 0.0)
            if now - last_ts < cooldown:
                wait = int(cooldown - (now - last_ts)) + 1
                return f"别急，{wait} 秒后再试吧。"

        limit = self.config.like.daily_limit_per_target
        used = int(self._state["targets"].get(target_id, 0) or 0)
        if used >= limit:
            return f"今天已经给 TA 点过 {limit} 次赞啦，明天再来吧。"

        if sender_id:
            self._state["cooldown"][sender_id] = now
        return ""

    def _roll_state_date(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._state.get("date") != today:
            self._state = {"date": today, "targets": {}, "cooldown": {}}

    def _read_state(self) -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        blank: dict[str, Any] = {"date": today, "targets": {}, "cooldown": {}}
        path = self._state_path
        if path is None or not path.exists():
            return blank
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("点赞状态读取失败，已重置: %s", exc)
            return blank
        if not isinstance(data, dict) or data.get("date") != today:
            return blank
        data.setdefault("targets", {})
        data.setdefault("cooldown", {})
        return data

    def _write_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.ctx.logger.warning("点赞状态写入失败: %s", exc)

    async def _ensure_self_id(self) -> str:
        if self._self_id:
            return self._self_id
        try:
            info = await self.ctx.api.call(GET_LOGIN_INFO_API)
        except Exception as exc:
            self.ctx.logger.warning("获取机器人账号失败: %s", exc)
            return ""
        if isinstance(info, dict):
            if info.get("success") is False:
                self.ctx.logger.warning("获取机器人账号失败: %s", info.get("error"))
                return ""
            uid = info.get("user_id") or info.get("qq") or info.get("uin")
            if uid:
                self._self_id = str(uid)
        return self._self_id

    @staticmethod
    def _is_napcat_ok(resp: Any) -> bool:
        if not isinstance(resp, dict):
            return True
        status = resp.get("status")
        retcode = resp.get("retcode")
        if status is None and retcode is None:
            return True
        return status == "ok" or retcode == 0

    @staticmethod
    def _normalize_user_id(raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            return ""
        if ":" in value:
            value = value.rsplit(":", 1)[-1]
        return value.strip()

    def _is_admin(self, user_id: str) -> bool:
        if not user_id:
            return False
        raw = self.config.admin.admins or ""
        tokens = [t for t in re.split(r"[\s,，;；]+", raw) if t]
        allowed = {self._normalize_user_id(t) for t in tokens}
        return user_id in allowed

    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id:
            self.ctx.logger.info("无 stream_id，跳过回复：%s", text)
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("发送回复失败: %s", exc)


def create_plugin() -> QQLikePlugin:
    return QQLikePlugin()