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

SUPPORTED_CONFIG_VERSION = "0.2.8"

# 兼容新版统一连接器（SnowLuma 命名空间）和旧版 NapCat 适配器。
# 优先尝试 SnowLuma；若 API 不存在/调用层抛出异常，再尝试 NapCat。
ADAPTER_NAMESPACES = ("adapter.snowluma", "adapter.napcat")
SEND_LIKE_API_SUFFIX = ".account.send_like"
GET_LOGIN_INFO_API_SUFFIX = ".system.get_login_info"

# 强制要求 / 前缀，防止闲聊误触
COMMAND_PATTERN = r"(?<!\S)/(?:点赞|赞|zan)(?:\s+(?P<rest>.+?))?\s*$"

STATE_FILE_NAME = "like_state.json"

SELF_ID_FAILURE_CACHE_SECONDS = 10


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "extension"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
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
        description="未指定次数时的默认点赞次数",
    )

    max_times: int = Field(
        default=20,
        ge=1,
        le=20,
        description="单次命令允许的最大点赞次数",
    )

    daily_limit_per_target: int = Field(
        default=30,
        ge=1,
        le=50,
        description="同一目标每天最多被点赞的次数",
    )

    cooldown_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="同一用户触发命令的冷却秒数",
    )

    allow_at_target: bool = Field(
        default=True,
        description="允许通过 @某人 指定目标",
    )

    allow_reply_target: bool = Field(
        default=True,
        description="允许通过回复消息指定目标",
    )

    allow_raw_qq: bool = Field(
        default=True,
        description="允许直接输入 QQ 号指定目标",
    )

    enable_llm_tool: bool = Field(
        default=True,
        description="启用 AI 口语化触发（关闭后 AI 将无法主动触发，仅允许命令触发；配置更新后生效）",
    )


class AdminSectionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    admin_only: bool = Field(
        default=True,
        description="是否仅允许管理员使用（默认开启，需在下方配置管理员QQ，否则所有人都无法使用）",
    )

    admins: str = Field(
        default="",
        description="管理员 QQ 号，逗号分隔（开启 admin_only 后必填）",
        json_schema_extra={"placeholder": "123456789, qq:987654321"},
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


class QQLikePlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = QQLikeConfig

    def __init__(self) -> None:
        super().__init__()

        self._state_path: Path | None = None

        self._state: dict[str, Any] = {
            "date": "",
            "targets": {},
            "cooldown": {},
        }

        self._lock = asyncio.Lock()

        self._self_id: str = ""
        self._adapter_namespace: str = ""

        self._self_id_failed_until: float = 0.0

        self._pending_target_counts: dict[str, int] = {}

        self._pending_cooldowns: dict[str, float] = {}

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self._state_path = data_dir / STATE_FILE_NAME
        self._state = self._read_state()

        if self.config.admin.admin_only and not self.config.admin.admins.strip():
            self.ctx.logger.warning(
                "⚠️ 安全警告：admin_only 已开启，但 admins 列表为空！"
                "请在 WebUI 插件配置中填入至少一个管理员 QQ 号，否则任何人都无法使用点赞功能。"
            )

        self.ctx.logger.info(
            "QQ 名片点赞插件已加载 enabled=%s, llm_tool=%s, admin_only=%s",
            self.config.plugin.enabled,
            self.config.like.enable_llm_tool,
            self.config.admin.admin_only,
        )

    async def on_unload(self) -> None:
        async with self._lock:
            self._write_state()

        self.ctx.logger.info("QQ 名片点赞插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info(
                "配置已热更新 version=%s",
                version,
            )

            if not self.config.like.enable_llm_tool:
                self.ctx.logger.info(
                    "LLM 点赞工具已按配置禁用；命令触发仍可正常使用。"
                )

    # ------------------------------------------------------------------ #
    # AI Tool
    # ------------------------------------------------------------------ #

    @Tool(
        "like_qq_profile",
        brief_description="给 QQ 用户名片点赞",
        detailed_description=(
            "当用户在聊天中请求给某人点赞时，你必须调用此工具。\n"
            "触发场景：用户说'给我点个赞'、'帮我点赞'、"
            "'给某某点赞'、'给 123456789 点赞'、'帮我给群友点个赞'。\n"
            "参数说明：\n"
            "- target_qq：string，可选。要点赞的 QQ 号。不填则默认给当前发言者点赞。\n"
            "- times：integer，可选。点赞次数，默认 10，最大 20。\n"
            "重要：如果用户明确提出点赞次数，例如'点20个赞'、"
            "'给我来15个赞'，必须准确提取这个数字并填入 times。\n"
            "如果用户说'点满'或'全部点亮'，则填入最大值。\n"
            "调用后请用自然口语回复用户。"
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
                description="点赞次数，如果用户明确要求了次数，必须准确传递",
                required=False,
                default=10,
            ),
        ],
        core_tool=True,  # 恢复 core_tool，确保 AI 能够直接看到该工具
    )
    async def tool_like_qq_profile(
        self,
        target_qq: str = "",
        times: int = 10,
        **kwargs: Any,
    ) -> dict[str, Any]:

        if (
            not self.config.plugin.enabled
            or not self.config.like.enable_llm_tool
        ):
            return {"content": "点赞功能当前未启用。"}

        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        target = self._normalize_user_id(str(target_qq or ""))
        if target and not self.config.like.allow_raw_qq:
            return {"content": "当前配置中禁止直接指定任意 QQ 号点赞。"}

        user_text = str(
            message.get("processed_plain_text")
            or kwargs.get("text")
            or ""
        ).strip()

        extracted_times = self._extract_times_from_text(user_text)

        if extracted_times is not None:
            times = extracted_times

        sender_id = self._sender_id_from_message(message)

        if not sender_id:
            sender_id = self._normalize_user_id(
                str(
                    kwargs.get("user_id")
                    or kwargs.get("sender_id")
                    or ""
                )
            )

        if not target:
            target = sender_id

        if not target:
            return {
                "content": "没有识别出要点赞的目标，请让用户说明要赞谁。"
            }

        if not self._is_valid_qq(target):
            return {
                "content": (
                    f"识别到的目标 '{target}' 不是有效的 QQ 号，"
                    "请让用户提供正确的 5-12 位纯数字 QQ 号。"
                )
            }

        safe_times = self._coerce_times(times)

        ok, msg = await self._perform_like(
            target,
            safe_times,
            sender_id,
        )

        return {"content": msg}

    # ------------------------------------------------------------------ #
    # 命令触发
    # ------------------------------------------------------------------ #

    @Command(
        "qq_like",
        description="给 QQ 名片点赞",
        pattern=COMMAND_PATTERN,
    )
    async def cmd_qq_like(
        self,
        **kwargs: Any,
    ) -> tuple[bool, str | None, bool]:  # 第三位改为 bool，语义更准确

        if not self.config.plugin.enabled:
            return False, None, True

        stream_id = str(kwargs.get("stream_id") or "")

        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        sender_id = self._normalize_user_id(
            str(kwargs.get("user_id") or "")
        )

        matched_groups = kwargs.get("matched_groups") or {}
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        rest = str(
            matched_groups.get("rest") or ""
        ).strip()

        raw_qq, times = self._parse_args(rest)

        if (
            not raw_qq
            and rest.isdigit()
            and 3 <= len(rest) <= 4
        ):
            await self._reply(
                stream_id,
                "这个 QQ 号太短啦，QQ 号需要是 5-12 位纯数字。",
            )
            return False, None, True

        self_id = await self._ensure_self_id()

        target_id = ""

        if raw_qq and self.config.like.allow_raw_qq:
            target_id = raw_qq

        if not target_id:
            target_id, reason = self._resolve_target_from_message(
                message,
                self_id,
            )

            if not target_id:
                return False, None, True

        if times is None:
            times = self.config.like.default_times

        ok, msg = await self._perform_like(
            target_id,
            int(times),
            sender_id,
        )

        # 解决次数虚假和重复回复的问题
        if ok:
            display = self._display_name(target_id, message)
            final_msg = msg.replace(target_id, display)
            await self._reply(stream_id, final_msg)
            return True, None, True
        else:
            await self._reply(stream_id, msg)
            return False, None, True

    # ------------------------------------------------------------------ #
    # 核心点赞逻辑
    # ------------------------------------------------------------------ #

    async def _perform_like(
        self,
        target_id: str,
        times: int,
        sender_id: str,
    ) -> tuple[bool, str]:

        target_id = self._normalize_user_id(target_id)

        if not self._is_valid_qq(target_id):
            return False, "没找到有效的 QQ 点赞目标。"

        if self.config.admin.admin_only and not self._is_admin(sender_id):
            return False, "权限不足：仅管理员可用。"

        self_id = await self._ensure_self_id()

        if self_id and target_id == self_id:
            return False, "不能给自己点赞哦。"

        if times <= 0:
            times = self.config.like.default_times

        times = min(
            max(1, times),
            self.config.like.max_times,
        )

        reservation = await self._reserve_like(
            target_id=target_id,
            sender_id=sender_id,
            requested_times=times,
        )

        if isinstance(reservation, str):
            return False, reservation

        reserved_times = reservation["times"]
        reserved_cooldown = reservation["cooldown"]

        try:
            response, api_name = await self._call_adapter_api(
                SEND_LIKE_API_SUFFIX,
                params={
                    "user_id": int(target_id),
                    "times": reserved_times,
                },
            )

        except Exception as exc:
            await self._release_reservation(
                target_id,
                sender_id,
                reserved_times,
            )

            self.ctx.logger.exception("NapCat/SnowLuma 点赞 API 调用失败")
            return False, f"点赞失败：未能调用 NapCat/SnowLuma 点赞接口（{exc}）"

        if isinstance(response, dict) and response.get("success") is False:
            detail = str(
                response.get("error")
                or response.get("message")
                or "未知错误"
            )

            await self._release_reservation(
                target_id,
                sender_id,
                reserved_times,
            )

            self.ctx.logger.warning(
                "%s 失败: %s",
                api_name,
                detail,
            )

            return False, f"点赞失败：{detail}"

        if not self._is_api_success(response):
            detail = ""

            if isinstance(response, dict):
                detail = str(
                    response.get("wording")
                    or response.get("message")
                    or response.get("error")
                    or response.get("retcode")
                    or ""
                )

            await self._release_reservation(
                target_id,
                sender_id,
                reserved_times,
            )

            self.ctx.logger.warning(
                "%s 协议端返回异常: %s",
                api_name,
                response,
            )

            return False, f"点赞失败：{detail or '协议端返回异常'}"

        await self._commit_reservation(
            target_id=target_id,
            sender_id=sender_id,
            times=reserved_times,
            cooldown=reserved_cooldown,
        )

        return (
            True,
            f"已给 {target_id} 点了 {reserved_times} 次赞",
        )

    # ------------------------------------------------------------------ #
    # 额度预占
    # ------------------------------------------------------------------ #

    async def _reserve_like(
        self,
        target_id: str,
        sender_id: str,
        requested_times: int,
    ) -> dict[str, Any] | str:

        now = time.time()

        async with self._lock:
            self._roll_state_date()

            cooldown = self.config.like.cooldown_seconds

            if cooldown > 0 and sender_id:
                last_ts = float(
                    self._state["cooldown"].get(
                        sender_id,
                        0.0,
                    )
                    or 0.0
                )

                pending_ts = self._pending_cooldowns.get(
                    sender_id,
                    0.0,
                )

                effective_last_ts = max(
                    last_ts,
                    pending_ts,
                )

                elapsed = now - effective_last_ts

                if elapsed < cooldown:
                    wait = int(cooldown - elapsed) + 1

                    return f"别急，{wait} 秒后再试吧。"

            limit = self.config.like.daily_limit_per_target

            used = int(
                self._state["targets"].get(
                    target_id,
                    0,
                )
                or 0
            )

            pending = int(
                self._pending_target_counts.get(
                    target_id,
                    0,
                )
                or 0
            )

            remaining = limit - used - pending

            if remaining <= 0:
                return (
                    f"今天已经给 TA 点过 {limit} 次赞啦，"
                    "明天再来吧。"
                )

            actual_times = min(
                requested_times,
                remaining,
            )

            if actual_times <= 0:
                return (
                    f"今天已经给 TA 点过 {limit} 次赞啦，"
                    "明天再来吧。"
                )

            self._pending_target_counts[target_id] = (
                pending + actual_times
            )

            if cooldown > 0 and sender_id:
                self._pending_cooldowns[sender_id] = now

            return {
                "times": actual_times,
                "cooldown": now if cooldown > 0 and sender_id else 0.0,
            }

    async def _release_reservation(
        self,
        target_id: str,
        sender_id: str,
        times: int,
    ) -> None:

        async with self._lock:
            current = int(
                self._pending_target_counts.get(
                    target_id,
                    0,
                )
                or 0
            )

            remaining = current - times

            if remaining > 0:
                self._pending_target_counts[target_id] = remaining
            else:
                self._pending_target_counts.pop(
                    target_id,
                    None,
                )

            if sender_id:
                self._pending_cooldowns.pop(
                    sender_id,
                    None,
                )

    async def _commit_reservation(
        self,
        target_id: str,
        sender_id: str,
        times: int,
        cooldown: float,
    ) -> None:

        async with self._lock:
            self._roll_state_date()

            current_pending = int(
                self._pending_target_counts.get(
                    target_id,
                    0,
                )
                or 0
            )

            remaining_pending = current_pending - times

            if remaining_pending > 0:
                self._pending_target_counts[target_id] = (
                    remaining_pending
                )
            else:
                self._pending_target_counts.pop(
                    target_id,
                    None,
                )

            current_used = int(
                self._state["targets"].get(
                    target_id,
                    0,
                )
                or 0
            )

            self._state["targets"][target_id] = (
                current_used + times
            )

            if sender_id and cooldown > 0:
                self._state["cooldown"][sender_id] = cooldown

                self._pending_cooldowns.pop(
                    sender_id,
                    None,
                )

            self._write_state()

    # ------------------------------------------------------------------ #
    # 参数解析
    # ------------------------------------------------------------------ #

    def _parse_args(
        self,
        rest: str,
    ) -> tuple[str, int | None]:

        target = ""
        times: int | None = None

        for token in re.split(
            r"[\s,，、]+",
            rest.strip(),
        ):
            if not token:
                continue

            if not token.isdigit():
                continue

            if 5 <= len(token) <= 12:
                target = target or token
                continue

            if 1 <= len(token) <= 2:
                value = int(token)

                if 1 <= value <= self.config.like.max_times:
                    times = (
                        times
                        if times is not None
                        else value
                    )

        return target, times

    def _coerce_times(self, value: Any) -> int:
        """将 LLM 传入的点赞次数安全转换为整数。"""
        default = self.config.like.default_times

        if value is None or value == "":
            return default

        try:
            return int(value)
        except (TypeError, ValueError):
            self.ctx.logger.warning(
                "LLM 点赞次数参数无效，已回退默认值: %r",
                value,
            )
            return default

    def _extract_times_from_text(
        self,
        text: str,
    ) -> int | None:

        if not text:
            return None

        patterns = (
            r"(\d+)\s*(?:个|次)?\s*(?:赞|点赞)",
            r"(?:赞|点赞)\s*(\d+)\s*(?:个|次)?",
        )

        for pattern in patterns:
            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )

            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    return None

        if "点满" in text or "全部点亮" in text:
            return self.config.like.max_times

        return None

    # ------------------------------------------------------------------ #
    # 目标识别
    # ------------------------------------------------------------------ #

    def _resolve_target_from_message(
        self,
        message: dict[str, Any],
        self_id: str,
    ) -> tuple[str, str]:

        segments = message.get("raw_message") or []

        if not isinstance(segments, list):
            return "", "消息段解析失败。"

        if self.config.like.allow_at_target:
            for seg in segments:
                if (
                    not isinstance(seg, dict)
                    or seg.get("type") != "at"
                ):
                    continue

                data = seg.get("data")

                if isinstance(data, dict):
                    uid = (
                        data.get("target_user_id")
                        or data.get("qq")
                    )
                else:
                    uid = data

                uid = self._normalize_user_id(
                    str(uid or "")
                )

                if (
                    uid
                    and uid != "all"
                    and uid != self_id
                    and self._is_valid_qq(uid)
                ):
                    return uid, ""

        if self.config.like.allow_reply_target:
            for seg in segments:
                if (
                    not isinstance(seg, dict)
                    or seg.get("type") != "reply"
                ):
                    continue

                data = seg.get("data")

                if not isinstance(data, dict):
                    continue

                uid = self._normalize_user_id(
                    str(
                        data.get(
                            "target_message_sender_id"
                        )
                        or ""
                    )
                )

                if (
                    uid
                    and uid != self_id
                    and self._is_valid_qq(uid)
                ):
                    return uid, ""

        return "", ""

    # ------------------------------------------------------------------ #
    # 消息信息
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sender_id_from_message(
        message: dict[str, Any],
    ) -> str:

        info = message.get("message_info") or {}

        if isinstance(info, dict):
            user = info.get("user_info") or {}

            if isinstance(user, dict):
                uid = user.get("user_id")

                if uid:
                    return QQLikePlugin._normalize_user_id(
                        str(uid)
                    )

        uid = message.get("user_id")

        if uid:
            return QQLikePlugin._normalize_user_id(
                str(uid)
            )

        return ""

    @staticmethod
    def _display_name(
        target_id: str,
        message: dict[str, Any],
    ) -> str:

        segments = message.get("raw_message") or []

        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict):
                    continue

                if seg.get("type") not in (
                    "at",
                    "reply",
                ):
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

                name = (
                    data.get("target_user_nickname")
                    or data.get(
                        "target_message_sender_nickname"
                    )
                )

                if name:
                    return str(name)

        return target_id

    # ------------------------------------------------------------------ #
    # 状态管理
    # ------------------------------------------------------------------ #

    def _roll_state_date(self) -> None:
        today = time.strftime("%Y-%m-%d")

        if self._state.get("date") != today:
            self._state = {
                "date": today,
                "targets": {},
                "cooldown": {},
            }

            self._pending_target_counts.clear()
            self._pending_cooldowns.clear()

    def _read_state(self) -> dict[str, Any]:

        today = time.strftime("%Y-%m-%d")

        blank: dict[str, Any] = {
            "date": today,
            "targets": {},
            "cooldown": {},
        }

        path = self._state_path

        if path is None or not path.exists():
            return blank

        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except Exception as exc:
            self.ctx.logger.warning(
                "点赞状态读取失败，已重置: %s",
                exc,
            )
            return blank

        if not isinstance(data, dict):
            return blank

        # 跨天清理逻辑：如果发现数据是昨天的，直接返回空白数据
        if data.get("date") != today:
            return blank

        targets = data.get("targets")
        cooldown = data.get("cooldown")

        if not isinstance(targets, dict):
            targets = {}

        if not isinstance(cooldown, dict):
            cooldown = {}

        return {
            "date": today,
            "targets": targets,
            "cooldown": cooldown,
        }

    def _write_state(self) -> None:

        path = self._state_path

        if path is None:
            return

        temp_path = path.with_suffix(
            path.suffix + ".tmp"
        )

        try:
            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            content = json.dumps(
                self._state,
                ensure_ascii=False,
                indent=2,
            )

            temp_path.write_text(
                content,
                encoding="utf-8",
            )

            temp_path.replace(path)

        except Exception as exc:
            self.ctx.logger.warning(
                "点赞状态写入失败: %s",
                exc,
            )

            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 机器人自身 QQ
    # ------------------------------------------------------------------ #

    async def _call_adapter_api(
        self,
        api_suffix: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        """调用 SnowLuma 或 NapCat API。

        只有在 API 调用层抛异常（例如对应命名空间不存在）时才尝试另一个
        命名空间；若接口已返回业务失败结果，则不重复调用，避免重复点赞。
        """
        namespaces = list(ADAPTER_NAMESPACES)
        if self._adapter_namespace in namespaces:
            namespaces.remove(self._adapter_namespace)
            namespaces.insert(0, self._adapter_namespace)

        last_error: Exception | None = None
        for namespace in namespaces:
            api_name = f"{namespace}{api_suffix}"
            try:
                if params is None:
                    response = await self.ctx.api.call(api_name)
                else:
                    response = await self.ctx.api.call(api_name, params=params)
                self._adapter_namespace = namespace
                return response, api_name
            except Exception as exc:
                last_error = exc
                self.ctx.logger.debug("适配器 API %s 调用失败，尝试兼容接口：%s", api_name, exc)

        if last_error is not None:
            raise last_error
        raise RuntimeError("未找到可用的 NapCat/SnowLuma API")

    async def _ensure_self_id(self) -> str:
        if self._self_id:
            return self._self_id

        now = time.time()
        if now < self._self_id_failed_until:
            return ""

        errors: list[str] = []
        for namespace in ADAPTER_NAMESPACES:
            api_name = f"{namespace}{GET_LOGIN_INFO_API_SUFFIX}"
            try:
                info = await self.ctx.api.call(api_name)
            except Exception as exc:
                errors.append(f"{api_name}: {exc}")
                continue

            if not isinstance(info, dict):
                continue
            if info.get("success") is False:
                errors.append(f"{api_name}: {info.get('error') or info.get('message') or '接口返回失败'}")
                continue

            payload: Any = info.get("data") or info.get("result") or info
            if not isinstance(payload, dict):
                continue
            uid = payload.get("user_id") or payload.get("qq") or payload.get("uin")
            if uid:
                self._self_id = self._normalize_user_id(str(uid))
                self._adapter_namespace = namespace
                self._self_id_failed_until = 0.0
                return self._self_id

        self._self_id_failed_until = now + SELF_ID_FAILURE_CACHE_SECONDS
        if errors:
            self.ctx.logger.debug("获取机器人账号失败：%s", "；".join(errors))
        return ""

    # ------------------------------------------------------------------ #
    # 工具函数
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_api_success(resp: Any) -> bool:
        """兼容统一连接器的 success 包装和 OneBot status/retcode 返回。

        未知或空响应不再默认当作成功，避免点赞 API 没有返回有效结果时
        插件仍然报告“点赞成功”并消耗每日额度。
        """
        if not isinstance(resp, dict):
            return False

        if resp.get("success") is False:
            return False
        if resp.get("success") is True:
            return True

        status = resp.get("status")
        retcode = resp.get("retcode")

        if status is not None:
            return status == "ok"
        if retcode is not None:
            return retcode == 0

        # 部分封装直接返回 data/result；只在存在明确响应载荷时接受。
        return "data" in resp or "result" in resp

    @staticmethod
    def _normalize_user_id(raw: str) -> str:

        value = (raw or "").strip()

        if not value:
            return ""

        if ":" in value:
            value = value.rsplit(":", 1)[-1]

        return value.strip()

    @staticmethod
    def _is_valid_qq(value: str) -> bool:

        return bool(
            re.fullmatch(
                r"\d{5,12}",
                value or "",
            )
        )

    def _is_admin(
        self,
        user_id: str,
    ) -> bool:

        if not user_id:
            return False

        raw = self.config.admin.admins or ""

        tokens = [
            token
            for token in re.split(
                r"[\s,，;；]+",
                raw,
            )
            if token
        ]

        allowed = {
            self._normalize_user_id(token)
            for token in tokens
        }

        return user_id in allowed

    async def _reply(
        self,
        stream_id: str,
        text: str,
    ) -> None:

        if not stream_id:
            self.ctx.logger.info(
                "无 stream_id，跳过回复：%s",
                text,
            )
            return

        try:
            await self.ctx.send.text(
                text,
                stream_id,
            )

        except Exception as exc:
            self.ctx.logger.warning(
                "发送回复失败: %s",
                exc,
            )


def create_plugin() -> QQLikePlugin:
    return QQLikePlugin()