"""群聊逻辑优化插件。

通过 MaiBot 2.x Hook 机制，把本轮部署中验证过的群聊调度、回复安全、
输出卫生、多模态信息桥接和防刷屏规则做成可迁移插件。

设计原则：
1. 不直接 import src.*，不修改宿主进程对象；
2. 不读取或写入宿主文件，也不修改其他插件配置；所有跨边界数据均走 SDK capability；
3. Planner 输出在执行前统一校正，wait/reply/send_emoji 的危险调用不会进入宿主；
4. reply 成功后追加一个隐藏的 finish 工具，让宿主原生 stop_after_execution 结束本轮；
5. 发送前再做目标去重、文本去重、表情包冷却和引用频率兜底。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

_MESSAGE_RE = re.compile(r"<message\b(?P<attrs>[^>]*)>\s*(?P<body>.*?)(?:</message>|(?=<message\b)|$)", re.S | re.I)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*[\"']?([^\"'\s>]+)[\"']?", re.I)
_TECH_KEYWORDS = (
    "api", "codex", "openai", "anthropic", "claude", "model", "endpoint",
    "error", "capacity", "rate limit", "timeout", "http", "dns",
    "模型", "报错", "错误", "容量", "限流", "风控", "网络", "代理", "中转",
    "客户端", "服务端", "架构", "配置", "部署", "数据库", "请求", "超时", "日志", "官方源",
)
_SPECULATIVE_PATTERNS = (
    "估计是", "应该是", "肯定是", "大概率是", "难怪是", "八成是", "疑似",
    "可能是", "怀疑是", "感觉是", "我猜",
)
_EVIDENCE_RELAX_PATTERNS = ("不确定", "无法确定", "看不出", "待确认", "没有证据", "证据不足")
_BANNED_OPENERS = ("嗯", "好", "知道了", "确实", "对啊", "哈哈", "那", "这")


class PluginSwitchConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "shield-check"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用群聊逻辑优化")
    config_version: str = Field(default="2.0.4", description="配置版本")


class LatencyConfig(PluginConfigBase):
    __ui_label__ = "低延迟调度"
    __ui_icon__ = "zap"
    __ui_order__ = 1
    wait_seconds: int = Field(default=5, ge=1, le=30, description="Planner wait 的硬上限，默认5秒")
    suppress_repeated_wait: bool = Field(default=True, description="短时间内重复 wait 时直接结束，避免空闲循环")
    repeated_wait_window_seconds: int = Field(default=20, ge=1, le=3600, description="重复 wait 抑制窗口")
    stop_after_reply: bool = Field(default=True, description="reply/send_emoji 后追加隐藏 finish 工具并结束本轮")
    planner_analysis_char_limit: int = Field(default=180, ge=60, le=1000, description="Planner 分析提示的字符上限")
    disable_typing_simulation: bool = Field(default=True, description="发送前关闭打字等待，优先群聊响应速度")


class ContextConfig(PluginConfigBase):
    """由插件接管的 Planner 聊天上下文长度配置。"""

    __ui_label__ = "上下文"
    __ui_icon__ = "list"
    __ui_order__ = 2
    max_history_messages: int = Field(default=30, ge=5, le=200, description="保留最近多少条用户聊天消息")

class ReplySafetyConfig(PluginConfigBase):
    __ui_label__ = "回复安全"
    __ui_icon__ = "message-square-check"
    __ui_order__ = 3
    target_dedupe_seconds: int = Field(default=180, ge=1, le=86400, description="同一目标消息只允许一次可见回复")
    duplicate_text_cooldown_seconds: int = Field(default=120, ge=0, le=86400, description="同会话重复文本抑制窗口，0关闭")
    quote_every_n: int = Field(default=4, ge=0, le=100, description="每 N 条可见回复最多允许一次QQ引用；0表示全部禁止引用")
    max_reply_chars_for_retry: int = Field(default=70, ge=20, le=500, description="超过该长度时尝试让 replyer 重写一次")


class ReplyFallbackConfig(PluginConfigBase):
    """限定 Planner 未发工具时的安全兜底范围。"""

    __ui_label__ = "回复兜底"
    __ui_icon__ = "life-buoy"
    __ui_order__ = 4
    enabled: bool = Field(default=True, description="启用明确指向消息的强制reply兜底")
    scan_messages: int = Field(default=3, ge=1, le=8, description="只扫描最近一次机器人发言后的最近N条用户消息，绝不扫描全历史")
    max_age_seconds: int = Field(default=300, ge=10, le=3600, description="兜底候选相对当前时钟的最大年龄；跨零点按最近发生时间估算")
    require_planner_intent_for_history: bool = Field(
        default=True,
        description="历史候选不是最新消息时，必须存在Planner明确回复意图才兜底；最新明确@仍可硬兜底",
    )


class StyleConfig(PluginConfigBase):
    __ui_label__ = "输出卫生"
    __ui_icon__ = "message-circle"
    __ui_order__ = 5
    natural_style: bool = Field(
        default=True,
        description="注入通用输出卫生规则；不指定人格、性格、语气或特定回复风格",
    )
    avoid_recent_reply_count: int = Field(default=5, ge=0, le=20, description="注入近期回复以避免重复句式")


class MentionConfig(PluginConfigBase):
    """插件接管的文本提及/昵称识别配置。"""

    __ui_label__ = "昵称与提及"
    __ui_icon__ = "at-sign"
    __ui_order__ = 6
    enabled: bool = Field(default=True, description="启用插件文本提及识别；不影响平台原生@识别")
    bot_aliases: list[str] = Field(
        default_factory=list,
        description="机器人昵称/别名列表。为空时自动继承 MaiBot 的 bot.nickname 和 bot.alias_names；非空时优先使用插件显式配置。",
    )

    def effective_aliases(self) -> list[str]:
        return [str(value or "").strip() for value in self.bot_aliases if str(value or "").strip()]


class FollowupConfig(PluginConfigBase):
    """连续对话保护参数。"""

    __ui_label__ = "连续对话"
    __ui_icon__ = "messages-square"
    __ui_order__ = 7
    enabled: bool = Field(default=True, description="启用指向判断后的连续对话触发保护")
    window_seconds: int = Field(default=600, ge=5, le=900, description="上一轮成功回复后的连续对话识别窗口，默认10分钟")
    min_text_overlap: float = Field(default=0.05, ge=0.0, le=1.0, description="无Embedding时的混合文本重合率辅助信号")
    min_current_chars: int = Field(default=6, ge=1, le=50, description="非引用续聊至少需要的当前消息字符数，避免“知道”等短词误触发")
    min_interval_seconds: int = Field(default=2, ge=0, le=60, description="两次连续对话触发之间的最小间隔，防止刷屏强制触发")
    max_followup_turns: int = Field(default=3, ge=1, le=20, description="同一轮对话最多连续触发多少次；到上限后需重新点名或等待窗口过期")
    quote_replies: bool = Field(default=True, description="用户引用机器人最近一条回复时视为明确指向机器人；仍受次数/间隔保护")


class TargetResolverConfig(PluginConfigBase):
    """用对话状态与 Embedding 判断当前消息的主要受话对象。"""

    __ui_label__ = "指向判断"
    __ui_icon__ = "git-branch"
    __ui_order__ = 8
    enabled: bool = Field(default=True, description="启用受话对象解析器；命中机器人时仅强制进入 Planner，最终是否回复仍由 Planner 决定")
    embedding_source: str = Field(
        default="auto",
        description="Embedding来源：auto=插件覆盖优先、其次宿主embedding；host=只用宿主embedding；plugin=只用插件覆盖；disabled=始终纯规则",
    )
    task_name: str = Field(
        default="",
        description="插件覆盖的Embedding模型任务名或模型名；留空表示沿用宿主 [model_task_config.embedding]",
    )
    history_messages: int = Field(default=12, ge=4, le=50, description="每个会话保留的近期对话状态条数")
    window_seconds: int = Field(default=600, ge=10, le=900, description="对话状态窗口，默认10分钟；窗口外不猜测受话对象")
    min_confidence: float = Field(default=0.62, ge=0.30, le=1.0, description="判定为指向机器人的最低综合置信度")
    min_score_margin: float = Field(default=0.08, ge=0.0, le=0.50, description="机器人与其他受话对象的最低分差")
    use_embedding: bool = Field(default=True, description="使用宿主 Embedding 能力辅助区分机器人话题和其他用户话题")
    semantic_min_similarity: float = Field(default=0.45, ge=-1.0, le=1.0, description="参与加分的最低余弦相似度，避免无关文本也获得语义加分")
    min_semantic_margin: float = Field(default=0.035, ge=0.0, le=0.50, description="Embedding 相似度需要超过该差距才参与加分")
    embedding_boost: float = Field(default=0.18, ge=0.0, le=0.50, description="Embedding 语义一致性可提供的最大加分")
    embedding_timeout_seconds: float = Field(default=1.5, ge=0.2, le=5.0, description="单次批量Embedding超时；超时后回退对话状态")
    embedding_retry_seconds: int = Field(default=30, ge=1, le=900, description="Embedding失败后的冷却重试间隔")
    embedding_config_refresh_seconds: int = Field(default=300, ge=10, le=3600, description="宿主Embedding配置探测间隔")
    cache_size: int = Field(default=768, ge=32, le=8192, description="Embedding向量LRU缓存条数")


class TechnicalGuardConfig(PluginConfigBase):
    __ui_label__ = "技术话题"
    __ui_icon__ = "search-code"
    __ui_order__ = 9
    enabled: bool = Field(default=True, description="启用技术话题指向性与不确定性保护")


class VisualBridgeConfig(PluginConfigBase):
    __ui_label__ = "多模态桥接"
    __ui_icon__ = "image"
    __ui_order__ = 10
    enabled: bool = Field(default=True, description="让 Planner 把图片摘要透传给非多模态 replyer")
    require_summary_markers: bool = Field(default=True, description="图片目标缺少摘要时注入禁止猜测边界")


class EmojiConfig(PluginConfigBase):
    __ui_label__ = "表情包"
    __ui_icon__ = "smile"
    __ui_order__ = 11
    cooldown_seconds: int = Field(default=600, ge=0, le=86400, description="同会话表情包冷却")


class AccessControlConfig(PluginConfigBase):
    """插件集中管理群聊与私聊访问模式。"""

    __ui_label__ = "访问控制"
    __ui_icon__ = "lock"
    __ui_order__ = 12
    enabled: bool = Field(default=True, description="启用插件集中访问控制；消息会在进入媒体处理、记忆和 Planner 前被判定")
    group_mode: str = Field(
        default="whitelist",
        description="群聊模式：whitelist=仅 group_list 放行；blacklist=group_list 拒绝；all=全量放行",
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="群号列表。whitelist 时是允许列表；blacklist 时是拒绝列表；all 时忽略",
    )
    user_mode: str = Field(
        default="whitelist",
        description="私聊模式：whitelist=仅 user_list 放行；blacklist=user_list 拒绝；all=全量放行",
    )
    user_list: list[str] = Field(
        default_factory=list,
        description="私聊QQ列表。whitelist 时是允许列表；blacklist 时是拒绝列表；all 时忽略",
    )
    @staticmethod
    def _normalize_mode(value: str, fallback: str = "whitelist") -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"whitelist", "blacklist", "all"} else fallback


class GroupLogicConfig(PluginConfigBase):
    plugin: PluginSwitchConfig = Field(default_factory=PluginSwitchConfig)
    latency: LatencyConfig = Field(default_factory=LatencyConfig)
    reply_safety: ReplySafetyConfig = Field(default_factory=ReplySafetyConfig)
    reply_fallback: ReplyFallbackConfig = Field(default_factory=ReplyFallbackConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    mentions: MentionConfig = Field(default_factory=MentionConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    followup: FollowupConfig = Field(default_factory=FollowupConfig)
    target_resolver: TargetResolverConfig = Field(default_factory=TargetResolverConfig)
    technical: TechnicalGuardConfig = Field(default_factory=TechnicalGuardConfig)
    visual_bridge: VisualBridgeConfig = Field(default_factory=VisualBridgeConfig)
    emoji: EmojiConfig = Field(default_factory=EmojiConfig)
    access: AccessControlConfig = Field(default_factory=AccessControlConfig)


GroupLogicConfig.model_rebuild()




class GroupChatLogicPlugin(MaiBotPlugin):
    """以 Hook 方式集中治理 Maisaka 群聊行为。"""

    config_model = GroupLogicConfig
    # bot：同步宿主昵称；model：同步宿主 Embedding 任务配置。
    config_reload_subscriptions = ("bot", "model")

    def __init__(self) -> None:
        super().__init__()
        self._context_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._last_wait: Dict[str, float] = {}
        self._target_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._recent_texts: Dict[str, Deque[Tuple[float, str]]] = {}
        self._reply_retry_seen: set[str] = set()
        self._last_emoji: Dict[str, float] = {}
        self._last_success_reply: Dict[str, Dict[str, Any]] = {}
        self._recent_inbound_users: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._session_dialogue: Dict[str, Deque[Dict[str, Any]]] = {}
        self._session_group_ids: OrderedDict[str, str] = OrderedDict()
        self._embedding_cache: OrderedDict[str, Tuple[float, List[float]]] = OrderedDict()
        self._embedding_cache_model = ""
        self._embedding_error_until = 0.0
        self._embedding_warned = False
        self._host_model_tasks: Dict[str, Dict[str, Any]] = {}
        self._host_model_tasks_fallback = False
        self._host_model_binding_fingerprint = ""
        self._embedding_config_refreshed_at = 0.0
        self._embedding_source_warned = False
        self._next_state_cleanup_at = 0.0
        self._reply_total: Dict[str, int] = {}
        self._explicit_bot_target_ids: set[Tuple[str, str]] = set()
        self._whitelist_logged: set[Tuple[str, str]] = set()
        self._host_bot_aliases: List[str] = []
        self._host_bot_user_id = ""
        self._host_alias_refreshed_at: float = 0.0
        self._host_alias_warned = False

    async def on_load(self) -> None:
        self._safe_log("info", "群聊逻辑优化已加载：指向判断（自动Embedding/纯规则降级）/Hook调度/回复安全/输出卫生/视觉桥接/防刷屏启用")
        await self._refresh_host_bot_aliases()
        await self._refresh_host_embedding_config()

    async def on_unload(self) -> None:
        self._safe_log("info", "群聊逻辑优化已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热更新：bot 同步昵称；model 同步 Embedding。self 重置插件内缓存。"""

        del version
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope == "bot":
            self._update_host_bot_aliases_from_config(config_data)
            return
        if normalized_scope == "model":
            # 热更新 payload 可能只包含变更片段；这里重新读取完整宿主配置，避免任务表被部分覆盖。
            await self._refresh_host_embedding_config()
            return
        if normalized_scope in {"", "self"}:
            self._embedding_error_until = 0.0
            self._embedding_source_warned = False
            self._embedding_cache.clear()
            self._embedding_cache_model = ""

    def _safe_log(self, level: str, message: str) -> None:
        try:
            logger = getattr(self.ctx, "logger", None)
            if logger is None:
                return
            getattr(logger, level)(message)
        except Exception:
            pass

    async def _refresh_host_bot_aliases(self) -> None:
        """通过官方 config.get capability 读取宿主机器人账号、昵称与别名。"""

        values: List[Any] = []
        for key in ("bot.qq_account", "bot.nickname", "bot.alias_names"):
            try:
                result = await self.ctx.config.get(key, None)
                # SDK 会把 config.get 的 {success,value} 规范化成直接值；
                # 这里同时兼容直接值和旧版完整字典，避免误判为读取失败。
                if isinstance(result, dict):
                    values.append(result.get("value") if bool(result.get("success")) else None)
                else:
                    values.append(result)
            except Exception as exc:
                values.append(None)
                if not self._host_alias_warned:
                    self._host_alias_warned = True
                    self._safe_log("warning", f"群聊逻辑优化：读取宿主身份配置失败: {exc}")
        raw_account = values[0] if values else None
        account = str(raw_account or "").strip()
        if account and account != "0":
            self._host_bot_user_id = account
        if any(value is not None for value in values):
            self._update_host_bot_aliases(values[1] if len(values) > 1 else None, values[2] if len(values) > 2 else None)
        self._host_alias_refreshed_at = time.monotonic()

    def _update_host_bot_aliases_from_config(self, config_data: dict[str, object] | None) -> None:
        """从 bot 配置热更新payload中提取昵称。"""

        bot_config = config_data.get("bot") if isinstance(config_data, dict) else None
        if bot_config is None and isinstance(config_data, dict) and (
            "nickname" in config_data or "alias_names" in config_data or "qq_account" in config_data
        ):
            # 兼容宿主只传 bot 节本身、不外包一层 scope key 的热更新载荷。
            bot_config = config_data
        if not isinstance(bot_config, dict):
            return
        account = str(bot_config.get("qq_account", "") or "").strip()
        if account and account != "0":
            self._host_bot_user_id = account
        self._update_host_bot_aliases(
            bot_config.get("nickname"),
            bot_config.get("alias_names"),
        )

    def _update_host_bot_aliases(self, nickname: Any, alias_names: Any) -> None:
        """归一化并缓存宿主昵称。"""

        aliases: List[str] = []
        raw_values: List[Any] = [nickname]
        if isinstance(alias_names, (list, tuple, set)):
            raw_values.extend(alias_names)
        elif alias_names is not None:
            raw_values.append(alias_names)
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized and normalized not in aliases:
                aliases.append(normalized)
        if aliases != self._host_bot_aliases:
            self._safe_log(
                "info",
                "群聊逻辑优化：已同步 MaiBot 宿主昵称: " + "/".join(aliases),
            )
        self._host_bot_aliases = aliases
        self._host_alias_refreshed_at = time.monotonic()
        self._host_alias_warned = False

    @staticmethod
    def _unwrap_config_value(value: Any) -> Any:
        """兼容 SDK 直接值与旧版 `{success,value}` 配置返回。"""

        if isinstance(value, dict) and "success" in value and "value" in value:
            return value.get("value") if bool(value.get("success")) else None
        return value

    @staticmethod
    def _normalize_model_tasks(value: Any) -> Dict[str, Dict[str, Any]]:
        """把宿主 model_task_config 规范化为轻量字典。"""

        if hasattr(value, "model_dump"):
            try:
                value = value.model_dump(mode="json")
            except Exception:
                value = {}
        if not isinstance(value, dict):
            return {}
        source = value.get("model_task_config") if isinstance(value.get("model_task_config"), dict) else value
        if not isinstance(source, dict):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for task_name, task_config in source.items():
            if not isinstance(task_name, str) or not isinstance(task_config, dict):
                continue
            result[task_name] = dict(task_config)
        return result

    async def _refresh_host_embedding_config(self, config_data: dict[str, object] | None = None) -> None:
        """读取宿主模型任务配置，确认 embedding 任务是否可用。"""

        raw_tasks: Any = None
        if isinstance(config_data, dict):
            raw_tasks = config_data.get("model_task_config", config_data)
            self._host_model_tasks_fallback = False
        else:
            try:
                result = await asyncio.wait_for(
                    self.ctx.config.get("model_task_config", None),
                    timeout=max(0.2, float(self.config.target_resolver.embedding_timeout_seconds)),
                )
                raw_tasks = self._unwrap_config_value(result)
                self._host_model_tasks_fallback = False
            except Exception as exc:
                raw_tasks = {}
                self._host_model_tasks_fallback = False
                if not self._embedding_source_warned:
                    self._embedding_source_warned = True
                    self._safe_log("warning", f"群聊逻辑优化：读取宿主模型任务配置失败，将尝试使用能力接口探测: {exc}")

            # config.get 只能提供部分宿主配置视图；当它没有返回模型任务绑定时，
            # 使用官方 llm.get_available_models capability 探测任务名。该接口虽然
            # 不返回模型绑定详情，但足以判断 embedding 任务是否存在。
            known_tasks = self._normalize_model_tasks(raw_tasks)
            has_model_binding = any(
                [str(item or "").strip() for item in task_config.get("model_list", []) if str(item or "").strip()]
                for task_config in known_tasks.values()
            )
            if not has_model_binding:
                try:
                    payload = self._unwrap_config_value(
                        await asyncio.wait_for(
                            self.ctx.llm.get_available_models(),
                            timeout=max(0.2, float(self.config.target_resolver.embedding_timeout_seconds)),
                        )
                    )
                    if isinstance(payload, list):
                        task_names = payload
                    elif isinstance(payload, dict):
                        task_names = payload.get("models", [])
                    else:
                        task_names = []
                    raw_tasks = {str(name or "").strip(): {} for name in task_names or [] if str(name or "").strip()}
                    self._host_model_tasks_fallback = bool(raw_tasks)
                except Exception as exc:
                    raw_tasks = {}
                    self._host_model_tasks_fallback = False
                    if not self._embedding_source_warned:
                        self._embedding_source_warned = True
                        self._safe_log("warning", f"群聊逻辑优化：读取宿主 Embedding 配置失败，将使用纯规则判断: {exc}")

        new_model_tasks = self._normalize_model_tasks(raw_tasks)
        previous_host_models = [
            str(item or "").strip()
            for item in self._host_model_tasks.get("embedding", {}).get("model_list", [])
            if str(item or "").strip()
        ]
        binding_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    task_name: [str(item or "").strip() for item in task_config.get("model_list", [])]
                    for task_name, task_config in new_model_tasks.items()
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if self._host_model_binding_fingerprint and binding_fingerprint != self._host_model_binding_fingerprint:
            # 同名任务更换模型时必须弃用旧向量，避免不同 Embedding 空间混算余弦。
            self._embedding_cache.clear()
        # 配置探测结果变化后，等待下一次 llm.embed 返回的 model_name 重新校准缓存。
        self._embedding_cache.clear()
        self._embedding_cache_model = ""
        self._host_model_tasks = new_model_tasks
        self._host_model_binding_fingerprint = binding_fingerprint
        self._embedding_config_refreshed_at = time.monotonic()
        self._embedding_source_warned = False
        host_models = [
            str(item or "").strip()
            for item in self._host_model_tasks.get("embedding", {}).get("model_list", [])
            if str(item or "").strip()
        ]
        if host_models != previous_host_models:
            if host_models:
                self._safe_log("info", f"群聊逻辑优化：检测到宿主 Embedding 配置: {', '.join(host_models)}")
            else:
                self._safe_log("info", "群聊逻辑优化：未检测到宿主 Embedding 配置，指向判断将按需使用纯规则降级")
        effective_task = self._effective_embedding_task()
        self._safe_log(
            "info",
            "群聊逻辑优化：Embedding来源探测完成 "
            f"source={self._normalize_embedding_source(self.config.target_resolver.embedding_source)} "
            f"host={','.join(host_models) or '<empty>'} "
            f"effective={effective_task or '<rule-only>'}",
        )

    async def _maybe_refresh_host_embedding_config(self) -> None:
        if time.monotonic() - self._embedding_config_refreshed_at < int(
            self.config.target_resolver.embedding_config_refresh_seconds
        ):
            return
        await self._refresh_host_embedding_config()

    @staticmethod
    def _normalize_embedding_source(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"auto", "host", "plugin", "disabled"} else "auto"

    def _resolve_plugin_embedding_task_reference(self) -> str:
        reference = str(self.config.target_resolver.task_name or "").strip()
        if not reference:
            return ""
        if reference in self._host_model_tasks:
            return reference
        # 允许填模型名：在宿主各模型任务中反查所属任务，优先 embedding 命名任务。
        matches = [
            task_name
            for task_name, task_config in self._host_model_tasks.items()
            if reference in [str(item or "").strip() for item in task_config.get("model_list", [])]
        ]
        preferred = [task_name for task_name in matches if "embed" in task_name.lower()]
        if preferred:
            return preferred[0]
        return matches[0] if matches else reference

    def _effective_embedding_task(self) -> str:
        """返回实际使用的宿主模型任务名；空值表示纯规则降级。"""

        cfg = self.config.target_resolver
        source = self._normalize_embedding_source(cfg.embedding_source)
        if not cfg.enabled or not cfg.use_embedding or source == "disabled":
            return ""
        if source == "host":
            host_config = self._host_model_tasks.get("embedding", {})
            host_configured = bool(host_config.get("model_list")) or (
                self._host_model_tasks_fallback and "embedding" in self._host_model_tasks
            )
            return "embedding" if host_configured else ""
        if source == "plugin":
            return self._resolve_plugin_embedding_task_reference()

        # auto：插件显式覆盖优先；未覆盖时沿用宿主 embedding 任务。
        plugin_task = self._resolve_plugin_embedding_task_reference()
        if plugin_task:
            return plugin_task
        host_config = self._host_model_tasks.get("embedding", {})
        host_configured = bool(host_config.get("model_list")) or (
            self._host_model_tasks_fallback and "embedding" in self._host_model_tasks
        )
        return "embedding" if host_configured else ""

    def _enabled(self) -> bool:
        try:
            return bool(self.config.plugin.enabled)
        except Exception:
            return True

    @staticmethod
    def _id_set(values: list[str]) -> set[str]:
        return {str(value or "").strip() for value in values if str(value or "").strip()}

    @staticmethod
    def _followup_text_overlap(left: str, right: str) -> float:
        """计算适合中英文的混合文本重合率。

        只看二元组会漏掉“锅在哪”和“不用锅”这种共享单个关键字的直接续聊；
        只看单字又容易被“的/了/吗”这类功能词干扰。这里取两者较保守的组合：
        - 字符二元组重合率；
        - 去停用词后的单字关键内容重合率，再乘 0.5。
        """

        def normalize(value: str) -> str:
            return re.sub(r"[\W_]+", "", str(value or "").lower())

        left_normalized = normalize(left)
        right_normalized = normalize(right)
        if not left_normalized or not right_normalized:
            return 0.0

        def bigrams(value: str) -> set[str]:
            if len(value) <= 1:
                return {value}
            return {value[i : i + 2] for i in range(len(value) - 1)}

        left_bigrams = bigrams(left_normalized)
        right_bigrams = bigrams(right_normalized)
        bigram_overlap = len(left_bigrams & right_bigrams) / min(len(left_bigrams), len(right_bigrams))

        stop_chars = set("的了是我你他她它吗呢啊呀吧这那也就在有很不很就还和跟去说要看会能被把没嘛么")
        left_chars = {char for char in left_normalized if char not in stop_chars}
        right_chars = {char for char in right_normalized if char not in stop_chars}
        unigram_overlap = (
            len(left_chars & right_chars) / min(len(left_chars), len(right_chars))
            if left_chars and right_chars
            else 0.0
        )
        return max(bigram_overlap, unigram_overlap * 0.5)

    @staticmethod
    def _cosine_similarity(left: List[float], right: List[float]) -> float:
        if not left or not right or len(left) != len(right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm <= 0.0 or right_norm <= 0.0:
            return -1.0
        return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

    @staticmethod
    def _turn_signals(text: str) -> Tuple[bool, bool, bool]:
        """提取通用对话行为信号，不穷举具体追问句。"""
        compact = re.sub(r"\s+", "", str(text or ""))
        leading = compact.lstrip("，。！？,.!?：:；;“”\"'（）()")
        second_person = bool(re.search(r"你|您|你们", compact))
        starts_second = leading.startswith(("你", "您", "你们"))
        question = any(
            marker in compact
            for marker in ("?", "？", "吗", "呢", "什么", "怎么", "为什么", "哪", "谁", "多少", "几", "是不是", "能不能", "有没有")
        )
        # “饿不饿”“去不去”这类 A不A 结构也是通用疑问信号，不枚举具体追问。
        question = question or bool(re.search(r"(.)不\1", compact))
        return second_person, starts_second, question

    def _extract_dialogue_metadata(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """从 Host 消息中提取受话对象解析所需的确定性信号。"""
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info, dict) and isinstance(info.get("user_info"), dict) else {}
        additional = info.get("additional_config") if isinstance(info, dict) and isinstance(info.get("additional_config"), dict) else {}
        at_targets: List[Dict[str, str]] = []
        reply_to = ""
        reply_target_user = ""
        reply_target_name = ""
        reply_text = ""
        for part in message.get("raw_message", []) or []:
            if not isinstance(part, dict):
                continue
            segment_type = str(part.get("type", "") or "").strip().lower()
            data = part.get("data") if isinstance(part.get("data"), dict) else {}
            if segment_type == "at":
                target = str(data.get("target_user_id") or data.get("qq") or data.get("user_id") or data.get("user") or "").strip()
                if target:
                    at_targets.append({
                        "user_id": target,
                        "user_name": str(data.get("target_user_cardname") or data.get("target_user_nickname") or "").strip(),
                    })
            elif segment_type == "reply":
                reply_to = str(data.get("target_message_id") or data.get("message_id") or data.get("id") or "").strip()
                reply_target_user = str(data.get("target_message_sender_id") or data.get("sender_id") or "").strip()
                reply_target_name = str(data.get("target_message_sender_cardname") or data.get("target_message_sender_nickname") or "").strip()
                reply_text = str(data.get("target_message_content") or "").strip()
        if not reply_to:
            reply_to = str(message.get("reply_to") or message.get("reply_message_id") or "").strip()

        bot_id = str(self._host_bot_user_id or additional.get("self_id") or "").strip()
        text = self._message_text(message)
        alias_hit = any(alias and alias in text for alias in self._normalize_aliases())
        platform_bot_mention = bool(message.get("is_mentioned", False)) or bool(message.get("is_at", False))
        at_bot = bool(bot_id and any(item.get("user_id") == bot_id for item in at_targets))
        # QQ 适配器的 is_at 只在 @ 到机器人时为 true；即使身份ID暂时缺失，
        # 也不能把它误判成“@了其他用户”。
        at_other = bool(at_targets and not at_bot and not platform_bot_mention)
        reply_bot = bool(bot_id and reply_target_user and reply_target_user == bot_id)
        # 只有明确知道引用的是其他用户时才做确定性排除；
        # 仅知 msg_id 时留给对话图查目标，避免未知引用被误判。
        reply_other = bool(reply_target_user and not reply_bot)
        explicit_other = at_other or reply_other
        explicit_bot = (platform_bot_mention and not at_other) or at_bot or reply_bot or (alias_hit and not explicit_other)
        return {
            "message_id": str(message.get("message_id", "") or "").strip(),
            "session_id": str(message.get("session_id", "") or "").strip(),
            "user_id": str(user_info.get("user_id", "") or "").strip(),
            "user_name": str(user_info.get("user_cardname") or user_info.get("user_nickname") or "").strip(),
            "text": text,
            "at_targets": at_targets,
            "reply_to": reply_to,
            "reply_target_user": reply_target_user,
            "reply_target_name": reply_target_name,
            "reply_text": reply_text,
            "bot_id": bot_id,
            "alias_hit": alias_hit,
            "explicit_bot": explicit_bot,
            "explicit_other": explicit_other,
            "timestamp": time.monotonic(),
        }

    def _append_dialogue_event(
        self,
        session_id: str,
        message: Dict[str, Any],
        *,
        is_bot: bool = False,
        target_user: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """维护每个会话的轻量对话图，不保存二进制消息内容。"""
        meta = metadata if isinstance(metadata, dict) else self._extract_dialogue_metadata(message)
        message_id = str(meta.get("message_id", "") or "").strip()
        history_limit = max(4, int(self.config.target_resolver.history_messages))
        queue = self._session_dialogue.get(session_id)
        if queue is None:
            queue = deque(maxlen=history_limit)
            self._session_dialogue[session_id] = queue
        elif queue.maxlen != history_limit:
            queue = deque(queue, maxlen=history_limit)
            self._session_dialogue[session_id] = queue
        if message_id and any(str(record.get("message_id", "") or "") == message_id for record in queue):
            return meta
        explicit_target = ""
        if meta.get("explicit_bot"):
            explicit_target = str(meta.get("bot_id", "") or "")
        elif meta.get("at_targets"):
            explicit_target = str((meta.get("at_targets") or [{}])[0].get("user_id", "") or "")
        elif meta.get("reply_target_user"):
            explicit_target = str(meta.get("reply_target_user", "") or "")
        record = dict(meta)
        record.update({
            "kind": "bot" if is_bot else "user",
            "user_id": str((meta.get("bot_id") if is_bot else meta.get("user_id")) or ""),
            "target_user": str(target_user or explicit_target or ""),
            "timestamp": time.monotonic(),
        })
        queue.append(record)
        return record

    def _find_dialogue_record(self, session_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        if not message_id:
            return None
        for record in reversed(self._session_dialogue.get(session_id, deque())):
            if str(record.get("message_id", "") or "") == message_id:
                return record
        return None

    async def _embed_dialogue_texts(self, texts: List[str]) -> Dict[str, List[float]]:
        """通过已解析的宿主 Embedding 任务批量取向量；不可用时返回空。"""
        cfg = self.config.target_resolver
        embedding_task = self._effective_embedding_task()
        if not embedding_task:
            return {}
        input_pairs = [(str(text or ""), re.sub(r"\s+", " ", str(text or "").strip())) for text in texts]
        normalized_values = list(dict.fromkeys(normalized for _original, normalized in input_pairs if normalized))
        if not normalized_values:
            return {}
        now = time.monotonic()
        cache_ttl = max(int(cfg.window_seconds), int(self.config.followup.window_seconds)) + 60.0
        if self._host_model_tasks_fallback:
            # 任务名探测无法提前感知同名任务背后的模型更换，短 TTL 保证变化后快速自愈。
            cache_ttl = min(30.0, cache_ttl)
        vectors: Dict[str, List[float]] = {}
        missing: List[str] = []
        cache_model = str(self._embedding_cache_model or "").strip()
        cache_enabled = bool(cache_model)
        for normalized in normalized_values:
            key = self._embedding_cache_key(embedding_task, cache_model, normalized)
            cached = self._embedding_cache.get(key) if cache_enabled else None
            if cached is not None and now - cached[0] <= cache_ttl:
                self._embedding_cache.move_to_end(key)
                for original, original_normalized in input_pairs:
                    if original_normalized == normalized:
                        vectors[original] = cached[1]
            else:
                self._embedding_cache.pop(key, None)
                missing.append(normalized)
        if not missing:
            return vectors
        if now < self._embedding_error_until:
            return vectors
        try:
            payload = await asyncio.wait_for(
                self.ctx.llm.embed(texts=missing, task_name=embedding_task),
                timeout=float(cfg.embedding_timeout_seconds),
            )
            if not isinstance(payload, dict) or not bool(payload.get("success", False)):
                raise RuntimeError(str(payload.get("error", "embedding capability returned failure")))
            results = payload.get("results")
            if not isinstance(results, list) or len(results) != len(missing):
                raise RuntimeError("embedding batch result count mismatch")
            new_vectors: Dict[str, List[float]] = {}
            result_model = ""
            for text, result in zip(missing, results, strict=True):
                if not isinstance(result, dict):
                    continue
                raw_vector = result.get("embedding")
                candidate_model = str(result.get("model_name", "") or "").strip()
                if candidate_model and not result_model:
                    result_model = candidate_model
                if not isinstance(raw_vector, list) or not raw_vector:
                    continue
                try:
                    vector = [float(value) for value in raw_vector]
                except (TypeError, ValueError):
                    continue
                if len(vector) != len(raw_vector) or not all(math.isfinite(value) for value in vector):
                    continue
                new_vectors[text] = vector
            if result_model and result_model != cache_model:
                self._embedding_cache.clear()
                cache_model = result_model
                self._embedding_cache_model = result_model
                cache_enabled = True
            for text, vector in new_vectors.items():
                key = self._embedding_cache_key(embedding_task, cache_model, text)
                if cache_enabled:
                    self._embedding_cache[key] = (now, vector)
                    self._embedding_cache.move_to_end(key)
            if len(new_vectors) != len(missing):
                raise RuntimeError("embedding result contains empty or invalid vectors")
            for original, normalized in input_pairs:
                vector = new_vectors.get(normalized)
                if vector is not None:
                    vectors[original] = vector
            self._embedding_error_until = 0.0
            self._embedding_warned = False
        except Exception as exc:
            self._embedding_error_until = now + max(1, int(cfg.embedding_retry_seconds))
            if not self._embedding_warned:
                self._embedding_warned = True
                self._safe_log("warning", f"群聊逻辑优化：Embedding任务 {embedding_task} 暂不可用，回退纯规则判断: {exc}")
        while len(self._embedding_cache) > max(32, int(cfg.cache_size)):
            self._embedding_cache.popitem(last=False)
        return vectors

    @staticmethod
    def _embedding_cache_key(task_name: str, model_name: str, text: str) -> str:
        return hashlib.sha256(f"{task_name}\0{model_name}\0{text}".encode("utf-8")).hexdigest()

    def _can_safely_mark_message(self, message: Dict[str, Any]) -> bool:
        """只有QQ文本消息才回传修改，避免平台时间戳和RPC大帧问题。"""
        if str(message.get("platform", "") or "").strip().lower() not in {"qq", ""}:
            return False
        for part in message.get("raw_message", []) or []:
            if isinstance(part, dict) and (
                "binary_data_base64" in part
                or part.get("type") in {"image", "emoji", "voice", "file", "video"}
            ):
                return False
        return True

    async def _resolve_dialogue_target(
        self,
        session_id: str,
        message: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """判断当前消息主要对谁说；不调用生成式LLM。"""
        cfg = self.config.target_resolver
        if not cfg.enabled or not self.config.followup.enabled or not self._can_safely_mark_message(message):
            return {"target": "none", "confidence": 0.0, "reason": "disabled_or_unsafe", "method": "rule"}

        current_text = str(metadata.get("text", "") or "").strip()
        current_user = str(metadata.get("user_id", "") or "")
        now = time.monotonic()
        history = list(self._session_dialogue.get(session_id, deque()))

        if metadata.get("explicit_bot"):
            return {"target": "bot", "confidence": 0.99, "reason": "explicit_mention", "method": "deterministic", "target_user": current_user}
        if metadata.get("explicit_other"):
            target_user = str(metadata.get("reply_target_user") or (metadata.get("at_targets") or [{}])[0].get("user_id", "") or "")
            return {"target": "user", "confidence": 0.98, "reason": "explicit_other_target", "method": "deterministic", "target_user": target_user}

        reply_to = str(metadata.get("reply_to", "") or "")
        if self.config.followup.quote_replies and reply_to:
            quoted = self._find_dialogue_record(session_id, reply_to)
            if quoted is not None:
                if quoted.get("kind") == "bot":
                    return {"target": "bot", "confidence": 0.99, "reason": "quote_bot", "method": "deterministic", "target_user": current_user}
                target_user = str(quoted.get("user_id", "") or "")
                return {"target": "user", "confidence": 0.98, "reason": "quote_user", "method": "deterministic", "target_user": target_user}

        second_person, starts_second, question = self._turn_signals(current_text)
        if (
            not current_text
            # 第二人称短句可能是命令/反问，不一定带问号或疑问词；
            # 例如“你请我吃”。因此短文本只对第二人称直接指向放行。
            or (len(re.sub(r"\s+", "", current_text)) < int(self.config.followup.min_current_chars) and not second_person)
        ):
            return {"target": "none", "confidence": 0.0, "reason": "empty_or_short", "method": "rule"}

        window = max(5, int(cfg.window_seconds))
        last_bot: Optional[Dict[str, Any]] = None
        for record in reversed(history):
            # 表情包等无文本出站轮次仍可被 quote 定位，但不能遮蔽上一条可比较的文本轮次。
            if (
                record.get("kind") == "bot"
                and str(record.get("text", "") or "").strip()
                and now - float(record.get("timestamp", 0.0)) <= window
            ):
                last_bot = record
                break
        if last_bot is None:
            previous_reply = self._last_success_reply.get(session_id)
            if isinstance(previous_reply, dict) and now - float(previous_reply.get("updated_at", 0.0)) <= window:
                last_bot = {
                    "kind": "bot",
                    "message_id": str(previous_reply.get("message_id", "") or ""),
                    "text": str(previous_reply.get("text", "") or ""),
                    "target_user": str(previous_reply.get("target_user", "") or ""),
                    "timestamp": float(previous_reply.get("updated_at", 0.0)),
                }
        if last_bot is None:
            return {"target": "group", "confidence": 0.0, "reason": "no_recent_bot_turn", "method": "rule"}

        bot_text = str(last_bot.get("text", "") or "").strip()
        if not bot_text:
            return {"target": "group", "confidence": 0.0, "reason": "no_bot_text", "method": "rule"}
        *_, bot_question = self._turn_signals(bot_text)
        bot_index = next((index for index, record in enumerate(history) if record is last_bot), -1)
        intervening = [record for record in history[bot_index + 1 :] if bot_index >= 0 and record.get("kind") == "user"]
        last_event = history[-1] if history else None
        last_user = next((record for record in reversed(history) if record.get("kind") == "user" and str(record.get("text", "") or "").strip()), None)
        alternative = last_user if last_user is not None and str(last_user.get("user_id", "") or "") != current_user else None
        if alternative is None and last_user is not None:
            alternative = next(
                (
                    record
                    for record in reversed(history)
                    if record.get("kind") == "user"
                    and str(record.get("user_id", "") or "") != current_user
                    and str(record.get("text", "") or "").strip()
                ),
                None,
            )
        # 机器人刚说话时，“你”默认优先指上一轮说话者（机器人）。
        # 被机器人回答过的提问文本本身不是可延续的并行话题，避免“我是谁/那你是谁”被误判。
        if (
            last_event is not None
            and last_event.get("kind") == "bot"
            and alternative is not None
            and str(alternative.get("user_id", "") or "") == str(last_bot.get("target_user", "") or "")
        ):
            alternative = None

        bot_age = max(0.0, now - float(last_bot.get("timestamp", now)))
        age_ratio = min(1.0, bot_age / window)
        bot_score = 0.20 + 0.10 * (1.0 - age_ratio)
        alt_score = 0.20 if alternative is not None else 0.0
        if last_event is not None and last_event.get("kind") == "bot":
            bot_score += 0.18
        else:
            bot_score -= min(0.18, 0.045 * len(intervening))
            if alternative is not None:
                alt_score += 0.10
        if current_user and current_user == str(last_bot.get("target_user", "") or ""):
            bot_score += 0.06
        else:
            bot_score += 0.03
        if alternative is not None and last_event is not None and last_event.get("kind") == "user":
            alt_score += 0.30
        if second_person and question:
            bot_score += 0.20
            alt_score += 0.24 if alternative is not None else 0.0
        elif question:
            bot_score += 0.06
            alt_score += 0.08 if alternative is not None else 0.0
        if starts_second:
            bot_score += 0.03
            alt_score += 0.03 if alternative is not None else 0.0
        # 机器人上一轮刚向目标用户提问，目标用户随即用第二人称陈述/反问/命令回应，
        # 是对话结构上的直接接话。这里不枚举具体句式，只使用“上一轮提问 + 当前第二人称”。
        if (
            second_person
            and (bot_question or current_user == str(last_bot.get("target_user", "") or ""))
        ):
            bot_score += 0.12

        method = "rule"
        bot_overlap = self._followup_text_overlap(current_text, bot_text)
        if bot_overlap >= float(self.config.followup.min_text_overlap):
            bot_score += min(0.16, 0.08 + (bot_overlap - float(self.config.followup.min_text_overlap)) * 0.4)
        alt_overlap = 0.0
        if alternative is not None:
            alt_overlap = self._followup_text_overlap(current_text, str(alternative.get("text", "") or ""))
            if alt_overlap >= float(self.config.followup.min_text_overlap):
                alt_score += min(0.16, 0.08 + (alt_overlap - float(self.config.followup.min_text_overlap)) * 0.4)

        semantic_bot = -1.0
        semantic_alt = -1.0
        if cfg.use_embedding and self._normalize_embedding_source(cfg.embedding_source) != "disabled":
            await self._maybe_refresh_host_embedding_config()
            embedding_task = self._effective_embedding_task()
        else:
            embedding_task = ""
        if embedding_task:
            alt_text = str(alternative.get("text", "") or "") if alternative is not None else ""
            vectors = await self._embed_dialogue_texts([current_text, bot_text, alt_text])
            current_vector = vectors.get(current_text)
            bot_vector = vectors.get(bot_text)
            alt_vector = vectors.get(alt_text) if alt_text else None
            # 只要存在备选用户但其向量缺失，就不能把 bot 相似度与 -1 比较；
            # 否则一次部分缓存失败会被误当成“机器人语义显著更强”。
            semantic_complete = (
                current_vector is not None
                and bot_vector is not None
                and (alternative is None or not alt_text or alt_vector is not None)
            )
            if semantic_complete:
                semantic_bot = self._cosine_similarity(current_vector, bot_vector)
                if alternative is not None and alt_text and alt_vector is not None:
                    semantic_alt = self._cosine_similarity(current_vector, alt_vector)
                semantic_delta = semantic_bot - semantic_alt
                if semantic_bot >= float(cfg.semantic_min_similarity) and semantic_delta >= float(cfg.min_semantic_margin):
                    bot_score += min(float(cfg.embedding_boost), semantic_delta * 0.8)
                    method = "rule+embedding"
                elif semantic_alt >= float(cfg.semantic_min_similarity) and -semantic_delta >= float(cfg.min_semantic_margin):
                    alt_score += min(float(cfg.embedding_boost), -semantic_delta * 0.8)
                    method = "rule+embedding"
                else:
                    method = "rule+embedding"
            # 语义信息不完整时保持 method=rule，方便日志确认没有使用残缺向量。

        bot_score = max(0.0, min(0.99, bot_score))
        alt_score = max(0.0, min(0.99, alt_score))
        threshold = float(cfg.min_confidence)
        margin = float(cfg.min_score_margin)
        if bot_score >= threshold and bot_score - alt_score >= margin:
            return {
                "target": "bot",
                "confidence": round(bot_score, 4),
                "reason": f"bot_age={int(bot_age)}s intervening={len(intervening)} overlap={bot_overlap:.3f}/{alt_overlap:.3f} cosine={semantic_bot:.3f}/{semantic_alt:.3f}",
                "method": method,
                "target_user": current_user,
            }
        if alternative is not None and alt_score >= threshold and alt_score - bot_score >= margin:
            return {
                "target": "user",
                "confidence": round(alt_score, 4),
                "reason": f"active_user={alternative.get('user_id', '')} bot_age={int(bot_age)}s overlap={alt_overlap:.3f}/{bot_overlap:.3f} cosine={semantic_alt:.3f}/{semantic_bot:.3f}",
                "method": method,
                "target_user": str(alternative.get("user_id", "") or ""),
            }
        return {
            "target": "group",
            "confidence": round(max(bot_score, alt_score), 4),
            "reason": f"below_threshold bot={bot_score:.3f} other={alt_score:.3f} overlap={bot_overlap:.3f}/{alt_overlap:.3f} cosine={semantic_bot:.3f}/{semantic_alt:.3f}",
            "method": method,
            "target_user": "",
        }

    def _should_reset_followup_chain(
        self,
        message: Dict[str, Any],
        metadata: Dict[str, Any],
        last_reply: Any,
    ) -> bool:
        """判断一条自然旁观消息是否已经打断旧连续追问轮。"""

        if not isinstance(last_reply, dict):
            return False
        if str(metadata.get("reply_to", "") or "").strip():
            return False
        current_text = str(metadata.get("text", "") or "")
        second_person, _starts_second, question = self._turn_signals(current_text)
        if second_person or metadata.get("alias_hit"):
            return False

        current_user = str(metadata.get("user_id", "") or "")
        bot_target_user = str(last_reply.get("target_user", "") or "")
        if question and current_user and current_user == bot_target_user:
            # 机器人目标用户继续提问时仍可能是在追问，不能当作自然断链。
            return False

        last_text = str(last_reply.get("text", "") or "")
        overlap = self._followup_text_overlap(current_text, last_text)
        return overlap < float(self.config.followup.min_text_overlap)

    def _access_allowed(self, group_id: str, user_id: str) -> tuple[bool, str, str]:
        """返回是否放行、目标ID与模式。群聊按群号判定，私聊按用户QQ判定。"""

        cfg = self.config.access
        if group_id:
            mode = AccessControlConfig._normalize_mode(cfg.group_mode)
            allowed = mode == "all" or (
                (group_id in self._id_set(cfg.group_list)) if mode == "whitelist" else (group_id not in self._id_set(cfg.group_list))
            )
            return allowed, group_id, f"group:{mode}"
        mode = AccessControlConfig._normalize_mode(cfg.user_mode)
        allowed = mode == "all" or (
            (user_id in self._id_set(cfg.user_list)) if mode == "whitelist" else (user_id not in self._id_set(cfg.user_list))
        )
        return allowed, user_id, f"user:{mode}"

    @staticmethod
    def _extract_text_parts(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        return "\n".join(
            str(part.get("text", "") or "")
            for part in item.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()

    @staticmethod
    def _to_naive_timestamp(value: Any) -> str:
        """把 Context Item timestamp 统一为宿主使用的本地 naive datetime。"""

        parsed: Optional[datetime] = None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                parsed = None
        if parsed is None:
            parsed = datetime.now()
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.isoformat()

    def _normalize_context_item_timestamps(self, items: List[Any]) -> List[Any]:
        """归一化插件可见/输出 Context Item 的 meta.timestamp，避免历史混用 aware/naive。"""

        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta")
            if isinstance(meta, dict) and "timestamp" in meta:
                meta["timestamp"] = self._to_naive_timestamp(meta.get("timestamp"))
        return items

    def _evict_session_state(self) -> None:
        """清理过期目标状态与长期增长的辅助集合。"""

        now = time.monotonic()
        target_ttl = max(
            int(self.config.reply_safety.target_dedupe_seconds),
            int(self.config.latency.repeated_wait_window_seconds),
        )
        for key, state in list(self._target_state.items()):
            try:
                if now - float(state.get("updated_at", 0.0)) > target_ttl:
                    self._target_state.pop(key, None)
            except (TypeError, ValueError):
                self._target_state.pop(key, None)
        for key in list(self._explicit_bot_target_ids):
            session_id, _message_id = key
            if session_id not in self._context_cache and session_id not in self._session_dialogue:
                self._explicit_bot_target_ids.discard(key)
        if len(self._explicit_bot_target_ids) > 2048:
            self._explicit_bot_target_ids.clear()
        text_cooldown = max(1, int(self.config.reply_safety.duplicate_text_cooldown_seconds))
        for session_id, queue in list(self._recent_texts.items()):
            while queue and now - float(queue[0][0]) > text_cooldown:
                queue.popleft()
            if not queue:
                self._recent_texts.pop(session_id, None)
        for key, record in list(self._recent_inbound_users.items()):
            try:
                if now - float(record.get("updated_at", 0.0)) > target_ttl:
                    self._recent_inbound_users.pop(key, None)
            except (TypeError, ValueError):
                self._recent_inbound_users.pop(key, None)
        wait_window = max(1, int(self.config.latency.repeated_wait_window_seconds))
        for session_id, timestamp in list(self._last_wait.items()):
            if now - timestamp > wait_window:
                self._last_wait.pop(session_id, None)
        emoji_cooldown = max(0, int(self.config.emoji.cooldown_seconds))
        for session_id, timestamp in list(self._last_emoji.items()):
            if emoji_cooldown == 0 or now - timestamp > emoji_cooldown:
                self._last_emoji.pop(session_id, None)
        if len(self._last_wait) > 256:
            self._last_wait.clear()
        if len(self._last_emoji) > 256:
            self._last_emoji.clear()
        if len(self._reply_total) > 256:
            self._reply_total.clear()
        followup_ttl = max(
            int(self.config.followup.window_seconds) * 2,
            300,
        )
        for session_id, record in list(self._last_success_reply.items()):
            try:
                if now - float(record.get("updated_at", 0.0)) > followup_ttl:
                    self._last_success_reply.pop(session_id, None)
            except (TypeError, ValueError):
                self._last_success_reply.pop(session_id, None)
        dialogue_ttl = max(
            int(self.config.target_resolver.window_seconds) * 2,
            followup_ttl,
            300,
        )
        for session_id, queue in list(self._session_dialogue.items()):
            while queue and now - float(queue[0].get("timestamp", 0.0)) > dialogue_ttl:
                queue.popleft()
            if not queue:
                self._session_dialogue.pop(session_id, None)
        while len(self._session_dialogue) > 128:
            oldest_session = min(
                self._session_dialogue,
                key=lambda session_id: float(self._session_dialogue[session_id][0].get("timestamp", 0.0)),
                default="",
            )
            if not oldest_session:
                break
            self._session_dialogue.pop(oldest_session, None)
        while len(self._embedding_cache) > max(32, int(self.config.target_resolver.cache_size)):
            self._embedding_cache.popitem(last=False)
        if len(self._context_cache) > 128:
            for session_id in list(self._context_cache)[:-128]:
                self._context_cache.pop(session_id, None)
        while len(self._session_group_ids) > 512:
            self._session_group_ids.popitem(last=False)
        if len(self._reply_retry_seen) > 4096:
            self._reply_retry_seen.clear()
        if len(self._whitelist_logged) > 1024:
            self._whitelist_logged.clear()

    def _parse_context_items(self, session_id: str, items: List[Any]) -> None:
        cache: Dict[str, Dict[str, Any]] = {}
        order = 0
        for item in items:
            if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
                continue
            text = self._extract_text_parts(item)
            if not text or "<message " not in text.lower():
                continue
            for match in _MESSAGE_RE.finditer(text):
                attrs = {
                    key.lower(): value
                    for key, value in _ATTR_RE.findall(match.group("attrs"))
                }
                msg_id = str(attrs.get("msg_id", "") or "").strip()
                body = (match.group("body") or "").strip()
                if not msg_id:
                    continue
                cache[msg_id] = {
                    "order": order,
                    "text": body,
                    "user": str(attrs.get("user", "") or ""),
                    "time": str(attrs.get("time", "") or ""),
                    "is_self": str(attrs.get("is_self_message", "") or "").lower() == "true",
                    "is_at": str(attrs.get("is_at", "") or "").lower() == "true",
                    "is_mentioned": str(attrs.get("is_mentioned", "") or "").lower() == "true",
                }
                order += 1
        self._context_cache[session_id] = cache

    def _prune_planner_history(self, items: List[Any]) -> List[Any]:
        """只保留最新 N 条真实聊天消息，避免原版宿主上下文过大。"""

        maximum = int(self.config.context.max_history_messages)
        total_messages = sum(
            len(_MESSAGE_RE.findall(self._extract_text_parts(item)))
            for item in items
            if isinstance(item, dict) and item.get("item_type") == "UserMessageItem"
        )
        if total_messages <= maximum:
            return items

        remove_count = total_messages - maximum
        result: List[Any] = []
        retained_chat = False
        for item in items:
            if retained_chat:
                result.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("item_type") == "SystemMessageItem":
                result.append(item)
                continue
            if item.get("item_type") != "UserMessageItem":
                continue
            full_text = self._extract_text_parts(item)
            matches = list(_MESSAGE_RE.finditer(full_text))
            if not matches:
                continue
            if remove_count >= len(matches):
                remove_count -= len(matches)
                continue
            if remove_count:
                first_kept = matches[remove_count]
                modified = deepcopy(item)
                modified["parts"] = [
                    {"type": "text", "text": full_text[first_kept.start() :]},
                    *[
                        deepcopy(part)
                        for part in item.get("parts", [])
                        if isinstance(part, dict) and part.get("type") != "text"
                    ],
                ]
                result.append(modified)
                remove_count = 0
            else:
                result.append(item)
            retained_chat = True
        return result

    def _planner_policy(self) -> str:
        cfg = self.config
        aliases = "/".join(self._normalize_aliases())
        tech_rule = ""
        if cfg.technical.enabled:
            tech_rule = (
                f"\n- 群聊技术消息只有当前目标本身含 @机器人、{aliases}，或明确引用机器人消息，才可回复；"
                "否则必须旁观并结束。用户直接询问技术问题时，只基于报错、日志、截图文字、官方状态或可靠通用知识作答；"
                "证据不足时索要关键信息，禁止猜测风控、容量、队列、内部实现或厂商决策。"
            )
        visual_rule = ""
        if cfg.visual_bridge.enabled:
            visual_rule = (
                "\n- 你能看图，最终回复模型看不到原图。图片是回复依据时，reply_reference 必须包含"
                "【图片摘要】【图内文字】【视觉结论】【不确定】；看不清就写不确定，禁止让回复模型猜图。"
            )
        emoji_rule = f"\n- send_emoji 仅在用户明确要求或明确回应机器人时使用；同会话{cfg.emoji.cooldown_seconds}秒内不要连续发表情包。" if cfg.emoji.cooldown_seconds > 0 else ""
        return (
            "# 群聊逻辑优化（最高优先级）\n"
            "- 身份与人设完全遵循宿主 MaiBot 配置；本插件只做调度、安全与表达约束。\n"
            f"- Planner 分析最多3行、不超过{cfg.latency.planner_analysis_char_limit}字，只写目标、关键事实、行动结论。\n"
            f"- wait 只用于极短观察，seconds 必须为{cfg.latency.wait_seconds}；禁止60、120等长等待。没有更多操作时直接输出分析并结束。\n"
            "# 可见回复调度\n"
            "- 私聊、用户被@、提及插件配置的机器人昵称、直接向机器人提问、或最新消息尚未回复且用户明显等待时，必须调用 reply。\n"
            "- 指向判断器只负责把可能对机器人说的消息送进来；不要仅因进入本轮就回复。若@、引用或上下文显示主要受话对象是其他群友，必须旁观并结束。\n"
            "- 只在分析里写“应该回复”而不调用 reply 是失败；分析文本用户不可见。信息不足且用户明确提问时，用 reply 索要关键证据。\n"
            "- 骚扰、攻击、刷屏、无意义内容、安全风险、未指向机器人的技术话题应旁观并结束。\n"
            "# 回复目标边界\n"
            "- reply.msg_id 必须指向尚未回复且不早于上次成功回复目标的新用户消息；禁止回复机器人自己、Planner分析或工具结果。\n"
            "- 每个目标 msg_id 只允许一次成功 reply；reply 成功后本轮立即结束，不得继续 wait、fetch_history、send_emoji 或补充回复。\n"
            "- set_quote 默认false；只在回复旧消息、群内刷屏对象不明或明确回答多条之一时true。连续对话不要每次QQ引用。\n"
            f"{tech_rule}{visual_rule}{emoji_rule}"
            "\n# 工具调用格式\n"
            "- 优先使用原生 tool_calls。若没有原生 tool_calls，必须在正文末尾输出XML兜底："
            "`<tool_call><function=reply><parameter=msg_id>实际消息ID</parameter><parameter=set_quote>false</parameter>"
            "<parameter=reply_reference>关键事实</parameter><parameter=reply_style>正常回复</parameter></function></tool_call>`。"
        )

    def _normalize_aliases(self) -> list[str]:
        if not self.config.mentions.enabled:
            return []
        explicit_aliases = self.config.mentions.effective_aliases()
        if explicit_aliases:
            return explicit_aliases
        # 插件未显式填写昵称时，自动继承 MaiBot 的 bot.nickname 与 bot.alias_names。
        return list(self._host_bot_aliases)

    def _system_item(self, text: str) -> Dict[str, Any]:
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"gcl-{uuid.uuid4().hex}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    def _inject_policy_as_single_system(self, items: List[Any], policy_text: str) -> List[Any]:
        """把插件规则合并进开头唯一 System Item。

        vLLM 的 OpenAI 兼容层要求 system message 位于最前，且当前 Planner 端点
        不接受连续两个 system message；因此不能额外插入第二个 SystemMessageItem。
        """

        normalized = [deepcopy(item) for item in items]
        leading_system: List[int] = []
        for index, item in enumerate(normalized):
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
                leading_system.append(index)
            else:
                break

        if not leading_system:
            return [self._system_item(policy_text), *normalized]

        first = deepcopy(normalized[leading_system[0]])
        texts: List[str] = []
        other_parts: List[Dict[str, Any]] = []
        for index in leading_system:
            item = normalized[index]
            for part in item.get("parts", []) or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text", "") or "").strip()
                    if text:
                        texts.append(text)
                elif isinstance(part, dict):
                    other_parts.append(deepcopy(part))
        if "# 群聊逻辑优化" not in "\n".join(texts):
            texts.append(policy_text)
        merged_text = "\n\n".join(texts)
        first["parts"] = [
            {"type": "text", "text": merged_text},
            *other_parts,
        ]
        return [first, *normalized[leading_system[-1] + 1 :]]

    def _mutate_tool_definitions(self, tools: List[Any]) -> List[Any]:
        result: List[Any] = []
        wait_seconds = int(self.config.latency.wait_seconds)
        for raw in tools:
            if not isinstance(raw, dict):
                continue
            tool = deepcopy(raw)
            function = tool.get("function")
            if not isinstance(function, dict):
                # 兼容少数 provider 直接展开定义的形态。
                function = tool if tool.get("name") else None
            name = str(function.get("name", "") or "") if isinstance(function, dict) else ""
            if name == "group_logic_finish":
                # 隐藏内部工具：注册表可调用，但不暴露给模型。
                continue
            if name == "wait" and isinstance(function, dict):
                function["description"] = f"极短观察后续消息；群聊低延迟模式固定等待{wait_seconds}秒。"
                parameters = function.get("parameters")
                if not isinstance(parameters, dict):
                    parameters = {"type": "object", "properties": {}, "required": []}
                    function["parameters"] = parameters
                properties = parameters.get("properties")
                if not isinstance(properties, dict):
                    properties = {}
                    parameters["properties"] = properties
                seconds = properties.get("seconds")
                if not isinstance(seconds, dict):
                    seconds = {}
                    properties["seconds"] = seconds
                seconds.update({"type": "integer", "minimum": 0, "maximum": wait_seconds, "enum": [wait_seconds]})
            if name == "reply" and isinstance(function, dict):
                parameters = function.get("parameters")
                if isinstance(parameters, dict):
                    properties = parameters.get("properties")
                    if isinstance(properties, dict) and isinstance(properties.get("set_quote"), dict):
                        every = int(self.config.reply_safety.quote_every_n)
                        properties["set_quote"]["description"] = (
                            "QQ引用是消歧工具，不是礼貌标志。普通连续聊天保持false；仅消息落后很多、群内刷屏或对象不明时可true。"
                            + (f"插件会按每{every}条最多1条的比例兜底。" if every > 0 else "插件默认禁止引用。")
                        )
            result.append(tool)
        return result

    @HookHandler(
        "maisaka.planner.before_request",
        name="planner_policy_and_tool_guard",
        description="注入群聊决策规则，收敛 wait/reply/表情包行为",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        if not self._enabled():
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id", "") or "").strip()
        try:
            now = time.monotonic()
            if (
                not self.config.mentions.effective_aliases()
                and not self._host_bot_aliases
                and now - self._host_alias_refreshed_at > 60.0
            ):
                await self._refresh_host_bot_aliases()
        except Exception:
            pass
        self._evict_session_state()
        items = kwargs.get("items")
        if isinstance(items, list) and session_id:
            items = self._prune_planner_history(list(items))
            items = self._normalize_context_item_timestamps(items)
            self._parse_context_items(session_id, items)
            # 必须合并为唯一的开头 System Item；不能插入第二个 system message。
            items = self._inject_policy_as_single_system(list(items), self._planner_policy())
            kwargs["items"] = items
        tools = kwargs.get("tool_definitions")
        if isinstance(tools, list):
            kwargs["tool_definitions"] = self._mutate_tool_definitions(tools)
        return {"action": "continue", "modified_kwargs": kwargs}

    @staticmethod
    def _function_call(item: Any) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            return "", {}, {}
        call = item.get("tool_call")
        if not isinstance(call, dict):
            return "", {}, {}
        name = str(call.get("func_name", "") or "").strip()
        args = call.get("args")
        args = deepcopy(args) if isinstance(args, dict) else {}
        return name, args, call

    @staticmethod
    def _assistant_note(item: Any, text: str) -> Dict[str, Any]:
        meta = deepcopy(item.get("meta", {})) if isinstance(item, dict) else {}
        meta["item_id"] = f"gcl-note-{uuid.uuid4().hex}"
        meta["timestamp"] = datetime.now().isoformat()
        return {
            "item_type": "AssistantMessageItem",
            "meta": meta,
            "parts": [{"type": "text", "text": text}],
        }

    def _finish_item(self, reply_item: Any) -> Dict[str, Any]:
        meta = deepcopy(reply_item.get("meta", {})) if isinstance(reply_item, dict) else {}
        meta["item_id"] = f"gcl-finish-{uuid.uuid4().hex}"
        meta["timestamp"] = datetime.now().isoformat()
        return {
            "item_type": "FunctionCallItem",
            "meta": meta,
            "tool_call": {
                "call_id": f"gcl-finish-{uuid.uuid4().hex}",
                "func_name": "group_logic_finish",
                "args": {},
                "extra_content": {"tool_call_source": "plugin_group_logic"},
            },
        }

    def _is_technical(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword.lower() in lowered for keyword in _TECH_KEYWORDS)

    def _latest_nonself_context(self, session_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """返回本轮上下文中顺序最新的非机器人消息。"""

        latest: Optional[Tuple[str, Dict[str, Any]]] = None
        for msg_id, info in self._context_cache.get(session_id, {}).items():
            if not isinstance(info, dict) or info.get("is_self"):
                continue
            if latest is None or int(info.get("order", -1)) > int(latest[1].get("order", -1)):
                latest = (msg_id, info)
        return latest

    def _latest_self_order(self, session_id: str) -> int:
        """返回上下文中最新机器人消息的顺序；没有则为 -1。"""

        latest_order = -1
        for info in self._context_cache.get(session_id, {}).values():
            if isinstance(info, dict) and info.get("is_self"):
                latest_order = max(latest_order, int(info.get("order", -1)))
        return latest_order

    def _current_turn_nonself_records(self, session_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        """返回最近机器人发言后的非机器人消息，按时间正序。"""

        barrier = self._latest_self_order(session_id)
        records = [
            (msg_id, info)
            for msg_id, info in self._context_cache.get(session_id, {}).items()
            if isinstance(info, dict) and not info.get("is_self") and int(info.get("order", -1)) > barrier
        ]
        records.sort(key=lambda record: int(record[1].get("order", -1)))
        return records

    def _is_explicit_context_target(self, session_id: str, msg_id: str, info: Dict[str, Any]) -> bool:
        text = str(info.get("text", "") or "")
        aliases = self._normalize_aliases()
        return (
            bool(info.get("is_at"))
            or (session_id, msg_id) in self._explicit_bot_target_ids
            or any(alias in text for alias in aliases)
            or "@机器人" in text
        )

    @staticmethod
    def _analysis_requests_reply(output_items: Optional[List[Any]]) -> bool:
        """只承认非否定语境下的明确回复意图。"""

        analysis_text = "\n".join(
            str(part.get("text", "") or "")
            for item in output_items or []
            if isinstance(item, dict) and item.get("item_type") == "AssistantMessageItem"
            for part in item.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        markers = ("必须回复", "应该回复", "需要回复", "必须调用reply", "必须调用 reply", "应该调用reply", "应该调用 reply")
        negations = (
            "无需回复",
            "不需要回复",
            "不必回复",
            "不应该回复",
            "不应回复",
            "不该回复",
            "禁止回复",
            "不调用reply",
            "不调用 reply",
            "无需调用reply",
            "无需调用 reply",
        )
        sentences = re.split(r"[\n。；;！!？?]+", analysis_text)
        return any(
            any(marker in sentence for marker in markers) and not any(negation in sentence for negation in negations)
            for sentence in sentences
        )

    def _is_allowed_recent_reply_target(self, session_id: str, msg_id: str) -> bool:
        """模型或兜底只能选择当前轮内的少量近期目标。"""

        records = self._current_turn_nonself_records(session_id)[-max(1, int(self.config.reply_fallback.scan_messages)) :]
        return any(
            record_id == msg_id and not self._context_record_is_too_old(
                record,
                max(10, int(self.config.reply_fallback.max_age_seconds)),
            )
            for record_id, record in records
        )

    @staticmethod
    def _context_record_is_too_old(info: Dict[str, Any], max_age_seconds: int) -> bool:
        """只用消息文本中的time做插件内部年龄估算，不构造/回传宿主时间戳。"""

        raw_time = str(info.get("time", "") or "").strip()
        match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", raw_time)
        if match is None:
            return False
        hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return False
        now = datetime.now()
        candidate = datetime.combine(now.date(), datetime.min.time()).replace(hour=hour, minute=minute, second=second)
        # 若候选钟点比当前晚超过12小时，按跨零点的最近发生时间处理。
        if (candidate - now).total_seconds() >= 12 * 3600:
            candidate = candidate.fromordinal(candidate.toordinal() - 1)
        return (now - candidate).total_seconds() > max_age_seconds

    def _normalize_reply_item(self, session_id: str, item: Any, args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        msg_id = str(args.get("msg_id", "") or "").strip()
        if not msg_id:
            return None, "群聊逻辑优化：reply 缺少 msg_id，已取消。"
        existing_state = self._target_state.get((session_id, msg_id))
        if (
            isinstance(existing_state, dict)
            and existing_state.get("status") in {"sending", "sent"}
            and time.monotonic() - float(existing_state.get("updated_at", 0.0))
            < int(self.config.reply_safety.target_dedupe_seconds)
        ):
            return None, "群聊逻辑优化：同一目标消息在去重窗口内，已取消重复回复。"

        last_success = self._last_success_reply.get(session_id)
        inbound_state = self._recent_inbound_users.get((session_id, msg_id))
        if (
            isinstance(last_success, dict)
            and isinstance(inbound_state, dict)
            and float(last_success.get("target_updated_at", 0.0) or 0.0) > 0.0
            and float(last_success.get("target_updated_at", 0.0) or 0.0)
            > float(inbound_state.get("updated_at", 0.0) or 0.0)
        ):
            return None, "群聊逻辑优化：目标早于上次成功回复，已取消旧目标回复。"

        info = self._context_cache.get(session_id, {}).get(msg_id)
        if info is None:
            if self._context_cache.get(session_id):
                return None, "群聊逻辑优化：reply目标不在当前上下文中，已取消可能的幻觉目标。"
            # 上下文无法校验时不粗暴丢弃；但仍执行引用频率和 msg_id 去重。
            every = int(self.config.reply_safety.quote_every_n)
            total = int(self._reply_total.get(session_id, 0)) + 1
            self._reply_total[session_id] = total
            quote_allowed = every == 1 or (every > 1 and total % every == 0)
            if every == 0:
                quote_allowed = False
            args["set_quote"] = bool(args.get("set_quote", False)) and quote_allowed
            raw = deepcopy(item)
            raw["tool_call"]["args"] = args
            self._target_state[(session_id, msg_id)] = {
                "status": "pending",
                "updated_at": time.monotonic(),
                "quote_allowed": quote_allowed,
                "technical": False,
                "target_updated_at": float((inbound_state or {}).get("updated_at", 0.0) or 0.0),
            }
            return raw, ""

        if info.get("is_self"):
            return None, "群聊逻辑优化：目标是机器人自己的消息，已取消。"
        order = int(info.get("order", 0))
        if not self._is_allowed_recent_reply_target(session_id, msg_id):
            return None, "群聊逻辑优化：reply目标不在最近机器人发言后的安全窗口内，已取消挖坟回复。"
        if (
            isinstance(last_success, dict)
            and int(last_success.get("target_order", -1) or -1) >= 0
            and order <= int(last_success.get("target_order", -1) or -1)
        ):
            return None, "群聊逻辑优化：reply目标不晚于上次成功回复目标，已取消旧目标回复。"
        text = str(info.get("text", "") or "")
        aliases = self._normalize_aliases()
        if (
            self.config.technical.enabled
            and bool(self._session_group_ids.get(session_id))
            and self._is_technical(text)
            and not (
                bool(info.get("is_at"))
                or bool(info.get("is_mentioned"))
                or any(alias in text for alias in aliases)
            )
        ):
            return None, "群聊逻辑优化：群聊技术消息未字面指向机器人，已旁观。"

        reference = str(args.get("reply_reference", "") or "")
        if self.config.visual_bridge.enabled and ("[图片]" in text or "[表情包]" in text) and self.config.visual_bridge.require_summary_markers:
            if not ("【图片摘要】" in reference and "【图内文字】" in reference):
                args["reply_reference"] = (
                    "【图片信息不足】Planner未提供完整图片摘要；回复者不得猜测图片内容，看不清就向用户确认。\n" + reference
                ).strip()
        if (
            self.config.technical.enabled
            and self._is_technical(text)
            and (bool(info.get("is_at")) or bool(info.get("is_mentioned")) or any(alias in text for alias in aliases))
        ):
            args["reply_reference"] = (
                "【技术回复边界】只回答上下文明确证据；未知原因必须承认不确定并索要日志/截图/官方状态，禁止断言根因。\n" + str(args.get("reply_reference", "") or "")
            ).strip()

        every = int(self.config.reply_safety.quote_every_n)
        total = int(self._reply_total.get(session_id, 0)) + 1
        self._reply_total[session_id] = total
        quote_allowed = every == 1 or (every > 1 and total % every == 0)
        if every == 0:
            quote_allowed = False
        args["set_quote"] = bool(args.get("set_quote", False)) and quote_allowed

        raw = deepcopy(item)
        raw["tool_call"]["args"] = args
        self._target_state[(session_id, msg_id)] = {
            "status": "pending",
            "updated_at": time.monotonic(),
            "order": order,
            "quote_allowed": quote_allowed,
            "technical": self.config.technical.enabled and self._is_technical(text),
            "target_updated_at": float((inbound_state or {}).get("updated_at", 0.0) or 0.0),
            "target_user": str(
                (self._recent_inbound_users.get((session_id, msg_id)) or {}).get("user_id", "")
                or info.get("user", "")
                or ""
            ),
            "target_name": str(
                (self._recent_inbound_users.get((session_id, msg_id)) or {}).get("user_name", "")
                or info.get("user", "")
                or ""
            ),
        }
        return raw, ""

    def _forced_reply_fallback(self, session_id: str, source_item: Any, output_items: Optional[List[Any]] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """有界安全兜底：只扫最近机器人发言后的少量明确指向消息。"""

        if not self.config.reply_fallback.enabled:
            return None, ""
        records = self._current_turn_nonself_records(session_id)[-max(1, int(self.config.reply_fallback.scan_messages)) :]
        if not records:
            return None, ""

        max_age = max(10, int(self.config.reply_fallback.max_age_seconds))
        records = [
            (msg_id, info)
            for msg_id, info in records
            if not self._context_record_is_too_old(info, max_age)
        ]
        if not records:
            return None, ""

        latest_id, latest_info = records[-1]
        planner_wants_reply = self._analysis_requests_reply(output_items)
        explicit_records = [
            (msg_id, info)
            for msg_id, info in records
            if self._is_explicit_context_target(session_id, msg_id, info)
        ]

        if self._is_explicit_context_target(session_id, latest_id, latest_info):
            target_id, target_info = latest_id, latest_info
        elif explicit_records:
            if self.config.reply_fallback.require_planner_intent_for_history and not planner_wants_reply:
                return None, ""
            target_id, target_info = explicit_records[-1]
        elif planner_wants_reply:
            target_id, target_info = latest_id, latest_info
        else:
            return None, ""

        existing_state = self._target_state.get((session_id, target_id))
        if isinstance(existing_state, dict) and existing_state.get("status") in {"sending", "sent"}:
            return None, ""

        text = str(target_info.get("text", "") or "").strip().replace("\n", " ")
        preview = text[:160] + ("..." if len(text) > 160 else "")
        meta = deepcopy(source_item.get("meta", {})) if isinstance(source_item, dict) else {}
        meta["item_id"] = f"gcl-forced-reply-{uuid.uuid4().hex}"
        meta["timestamp"] = datetime.now().isoformat()
        item = {
            "item_type": "FunctionCallItem",
            "meta": meta,
            "tool_call": {
                "call_id": f"gcl-forced-reply-{uuid.uuid4().hex}",
                "func_name": "reply",
                "args": {
                    "msg_id": target_id,
                    "set_quote": False,
                    "reply_reference": (
                        "【强制回复兜底】用户明确@或称呼机器人，但Planner未发出reply工具。"
                        f"用户原话：{preview}。请自然、简短、直接回应用原话，不要编造。"
                    ),
                    "reply_style": "正常回复",
                },
                "extra_content": {"tool_call_source": "plugin_forced_reply_fallback"},
            },
        }
        normalized, _reason = self._normalize_reply_item(
            session_id,
            item,
            deepcopy(item["tool_call"]["args"]),
        )
        return (normalized, "") if normalized is not None else (None, "")

    @HookHandler(
        "maisaka.planner.after_response",
        name="planner_output_guard",
        description="执行前校正 wait/reply/send_emoji 工具调用并注入本轮结束信号",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def planner_after_response(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        if not self._enabled():
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id", "") or "").strip()
        output_items = kwargs.get("output_items")
        if not session_id or not isinstance(output_items, list):
            return {"action": "continue", "modified_kwargs": kwargs}

        new_items: List[Any] = []
        terminal_seen = False
        wait_seen = False
        rejected: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            name, args, _call = self._function_call(item)
            if name == "group_logic_finish":
                # 内部 finish 只能由插件在真实终端动作后注入；
                # 模型自行幻觉出该工具时不能让它提前结束本轮。
                reason = "群聊逻辑优化：模型不能直接调用内部finish工具，已取消。"
                rejected.append(reason)
                new_items.append(self._assistant_note(item, reason))
                continue
            if name == "reply" and not terminal_seen:
                normalized, reason = self._normalize_reply_item(session_id, item, args)
                if normalized is None:
                    rejected.append(reason)
                    new_items.append(self._assistant_note(item, reason))
                else:
                    if wait_seen:
                        new_items = [x for x in new_items if self._function_call(x)[0] != "wait"]
                        wait_seen = False
                    new_items.append(normalized)
                    if self.config.latency.stop_after_reply:
                        new_items.append(self._finish_item(normalized))
                    terminal_seen = True
                continue
            if name == "send_emoji" and not terminal_seen:
                emoji_cooldown = max(0, int(self.config.emoji.cooldown_seconds))
                last_emoji = self._last_emoji.get(session_id)
                if emoji_cooldown > 0 and last_emoji is not None and time.monotonic() - last_emoji < emoji_cooldown:
                    reason = "群聊逻辑优化：表情包冷却中，已取消并保留后续文本reply机会。"
                    rejected.append(reason)
                    new_items.append(self._assistant_note(item, reason))
                    continue
                if wait_seen:
                    new_items = [x for x in new_items if self._function_call(x)[0] != "wait"]
                    wait_seen = False
                terminal_seen = True
                new_items.append(item)
                if self.config.latency.stop_after_reply:
                    new_items.append(self._finish_item(item))
                continue
            if name == "wait":
                now = time.monotonic()
                last = self._last_wait.get(session_id)
                repeated = (
                    self.config.latency.suppress_repeated_wait
                    and last is not None
                    and now - last < int(self.config.latency.repeated_wait_window_seconds)
                )
                if terminal_seen or wait_seen or repeated:
                    reason = "群聊逻辑优化：重复等待已取消，本轮结束。"
                    rejected.append(reason)
                    new_items.append(self._assistant_note(item, reason))
                else:
                    raw = deepcopy(item)
                    raw["tool_call"]["args"] = {"seconds": int(self.config.latency.wait_seconds)}
                    new_items.append(raw)
                    self._last_wait[session_id] = now
                    wait_seen = True
                continue
            if terminal_seen and name:
                reason = f"群聊逻辑优化：{name} 在本轮终端动作之后，已取消。"
                rejected.append(reason)
                new_items.append(self._assistant_note(item, reason))
                continue
            new_items.append(item)

        # 硬兜底：用户明确@/称呼机器人时，即使模型只输出长分析、被 max_tokens 截断，
        # 也必须产生可见 reply，而不是把“应该回复”留在分析里。
        if not terminal_seen:
            source_item = next((item for item in reversed(output_items) if isinstance(item, dict)), None)
            forced_reply, forced_reason = self._forced_reply_fallback(session_id, source_item, output_items)
            if forced_reply is not None:
                # 移除错误选择的 wait，确保 reply 立即执行。
                new_items = [
                    item for item in new_items
                    if self._function_call(item)[0] != "wait"
                ]
                new_items.append(forced_reply)
                if self.config.latency.stop_after_reply:
                    new_items.append(self._finish_item(forced_reply))
                self._safe_log("info", "群聊逻辑优化：明确提及但Planner未调用reply，已强制注入reply兜底")

        if rejected:
            self._safe_log("info", "Planner输出校正: " + "；".join(rejected))
        kwargs["output_items"] = self._normalize_context_item_timestamps(new_items)
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        "group_logic_finish",
        description="群聊逻辑优化内部终端工具；成功reply或send_emoji后结束本轮Planner。",
        parameters=[],
        visibility="deferred",
    )
    async def group_logic_finish(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return {
            "success": True,
            "content": "群聊逻辑优化：终端动作已完成，本轮Planner结束，等待新用户消息。",
            "stop_after_execution": True,
        }

    def _replyer_policy(self, session_id: str, reply_message_id: str) -> str:
        lines: list[str] = []
        if self.config.style.natural_style:
            lines.append(
                "# 输出卫生\n"
                "- 人设、性格、语气、称呼习惯和回复风格完全遵循宿主 MaiBot 配置；本插件不指定任何特定风格。\n"
                "- 群聊回复通常8到45字，最多58字；用当前人格自然表达。\n"
                "- 禁止以对方名字、昵称、群名片、称呼或敬称开头；直接说内容。\n"
                "- 禁止以“嗯/好/知道了/确实/对啊/哈哈/那/这”开头；不要重复近期回复的开头和句式。\n"
                "- 不编造作品、数字、报价、经历或当前状态。"
            )
        if self.config.technical.enabled:
            lines.append(
                "# 技术证据边界\n"
                "- 技术原因、报错根因、容量/风控/调度/架构必须来自上下文明确证据。\n"
                "- 没有证据时只说“不确定/看不出原因”，并索要日志、截图或官方状态；禁止“估计是/应该是/肯定是/大概率是”。"
            )
        count = int(self.config.style.avoid_recent_reply_count)
        if count > 0:
            recent: list[str] = [
                text for _ts, text in list(self._recent_texts.get(session_id, deque()))[-count:]
            ]
            if recent:
                lines.append("# 避免重复\n不要复用这些近期回复的词、句式或开头：" + "；".join(recent[-count:]))
        return "\n\n".join(lines)

    def _find_target_state(self, session_id: str, reply_message_id: str) -> Optional[Dict[str, Any]]:
        return self._target_state.get((session_id, reply_message_id))

    @HookHandler(
        "maisaka.replyer.before_request",
        name="replyer_input_hygiene_guard",
        description="注入输出卫生、反重复、技术不确定性和图片信息边界",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def replyer_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        if not self._enabled():
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id", "") or "").strip()
        reply_message_id = str(kwargs.get("reply_message_id", "") or "").strip()
        policy = self._replyer_policy(session_id, reply_message_id)
        state = self._find_target_state(session_id, reply_message_id)
        if state is not None:
            state["last_replyer_attempt"] = int(kwargs.get("attempt", 1) or 1)
        if policy:
            kwargs["extra_prompt"] = f"{str(kwargs.get('extra_prompt', '') or '').strip()}\n\n{policy}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}

    @staticmethod
    def _target_name_candidates(target_name: Any) -> list[str]:
        """生成用于匹配句首称呼的目标名字变体。"""

        raw = str(target_name or "").strip()
        if not raw:
            return []
        candidates = {raw}
        without_tags = re.sub(r"[【\[】\]]+", " ", raw).strip()
        no_leading_tag = re.sub(r"^[【\[][^】\]]*[】\]]\s*", "", raw).strip()
        values = [raw, without_tags, no_leading_tag, *re.split(r"\s+", without_tags)]
        for value in values:
            value = value.strip()
            if not value:
                continue
            candidates.add(value)
            chinese_prefix = re.match(r"[\u4e00-\u9fff]{1,6}", value)
            if chinese_prefix:
                candidates.add(chinese_prefix.group(0))
        return sorted((x for x in candidates if x), key=len, reverse=True)

    def _strip_leading_addressee(self, response: str, target_name: Any) -> str:
        """去掉模型硬加在句首的目标名字/称呼。"""

        normalized = str(response or "").lstrip()
        if not normalized:
            return normalized
        for name in self._target_name_candidates(target_name):
            if not normalized.startswith(name):
                continue
            rest = normalized[len(name):]
            honorific_match = re.match(
                r"^(?:大人|老师|同学|大佬|学长|学姐|师傅|先生|女士|殿下|陛下)?\s*[，,。.！!？?：:；;~～ ]+",
                rest,
            )
            if honorific_match:
                rest = rest[honorific_match.end():]
            rest = rest.lstrip("，,。.！!？?：:；;~～ ")
            if rest:
                return rest
        return normalized

    def _need_replyer_retry(self, session_id: str, reply_message_id: str, response: str) -> Optional[str]:
        text = re.sub(r"\s+", "", response)
        if not text:
            return "回复为空"
        state = self._find_target_state(session_id, reply_message_id)
        technical = bool(state and state.get("technical"))
        if technical:
            has_speculation = any(pattern in response for pattern in _SPECULATIVE_PATTERNS)
            has_relax = any(pattern in response for pattern in _EVIDENCE_RELAX_PATTERNS)
            if has_speculation and not has_relax:
                return "技术回复包含未经验证根因，请改为不确定并索要日志/截图/官方状态"
        if self.config.style.natural_style:
            if any(response.lstrip().startswith(opener) for opener in _BANNED_OPENERS):
                return "回复开头机械，请按宿主人设换自然表达"
            if len(text) > int(self.config.reply_safety.max_reply_chars_for_retry):
                return f"回复超过{self.config.reply_safety.max_reply_chars_for_retry}字，请缩短成群聊短句"
        cooldown = int(self.config.reply_safety.duplicate_text_cooldown_seconds)
        if cooldown > 0:
            normalized = re.sub(r"\s+", "", response)
            now = time.monotonic()
            for ts, old in self._recent_texts.get(session_id, deque()):
                if now - ts <= cooldown and re.sub(r"\s+", "", old) == normalized:
                    return "与近期回复重复，请换一种自然说法"
        return None

    @HookHandler(
        "maisaka.replyer.after_response",
        name="replyer_output_guard",
        description="对机械、重复、过长或技术猜测回复触发一次重写",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def replyer_after_response(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        if not self._enabled():
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id", "") or "").strip()
        reply_message_id = str(kwargs.get("reply_message_id", "") or "").strip()
        response = str(kwargs.get("response", "") or "").strip()
        state_for_address = self._find_target_state(session_id, reply_message_id)
        stripped_response = self._strip_leading_addressee(
            response,
            (state_for_address or {}).get("target_name") if isinstance(state_for_address, dict) else "",
        )
        if stripped_response != response:
            self._safe_log("info", "群聊逻辑优化：已去掉句首对方名字/称呼")
            response = stripped_response
            kwargs["response"] = response
        attempt = int(kwargs.get("attempt", 1) or 1)
        max_retries = int(kwargs.get("max_retries", 0) or 0)
        retry_key = f"{session_id}:{reply_message_id}:{response}"
        reason = self._need_replyer_retry(session_id, reply_message_id, response)
        if reason and attempt <= max_retries and retry_key not in self._reply_retry_seen:
            self._reply_retry_seen.add(retry_key)
            kwargs["retry"] = True
            kwargs["retry_reason"] = f"群聊逻辑优化：{reason}。"
            self._safe_log("info", kwargs["retry_reason"])
            return {"action": "continue", "modified_kwargs": kwargs}
        # 只允许 after_send 在真实发送成功后记录文本。
        # 不能在 replyer after_response 预记录候选文本，否则 before_send 会把
        # 本条消息自身识别为“重复文本”并中止发送。
        return {"action": "continue", "modified_kwargs": kwargs}

    @staticmethod
    def _message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        text = str(message.get("processed_plain_text", "") or "").strip()
        if text:
            return text
        parts: list[str] = []
        for raw in message.get("raw_message", []) or []:
            if isinstance(raw, dict) and raw.get("type") == "text":
                parts.append(str(raw.get("data", "") or ""))
        return " ".join(parts).strip()

    @HookHandler(
        "send_service.before_send",
        name="outbound_anti_flood_guard",
        description="发送前兜底：目标去重、文本去重、表情包冷却、引用频率",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def before_send(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        message = kwargs.pop("message", None)
        if not self._enabled():
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(message, dict):
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(message.get("session_id", "") or "").strip()
        if not session_id:
            return {"action": "continue", "modified_kwargs": kwargs}
        now = time.monotonic()
        if self.config.latency.disable_typing_simulation:
            kwargs["typing"] = False

        if bool(message.get("is_emoji", False)):
            cooldown = int(self.config.emoji.cooldown_seconds)
            last = self._last_emoji.get(session_id)
            if cooldown > 0 and last is not None and now - last < cooldown:
                remaining = int(cooldown - (now - last))
                self._safe_log("info", f"群聊逻辑优化：表情包冷却中，剩余{remaining}秒，已拦截")
                return {"action": "abort", "custom_result": {"reason": "emoji_cooldown", "remaining_seconds": remaining}}

        target_id = str(kwargs.get("reply_message_id", "") or "").strip()
        if target_id:
            state = self._target_state.get((session_id, target_id))
            if state is not None:
                age = now - float(state.get("updated_at", now))
                if (
                    state.get("status") in {"sending", "sent"}
                    and age < int(self.config.reply_safety.target_dedupe_seconds)
                ):
                    self._safe_log("info", f"群聊逻辑优化：目标 {target_id} 已回复过，拦截重复发送")
                    return {"action": "abort", "custom_result": {"reason": "duplicate_target", "msg_id": target_id}}
                if age >= int(self.config.reply_safety.target_dedupe_seconds):
                    state.update({"status": "pending", "updated_at": now})
                # before_send 不能提前判定 sent；真正成功由 after_send 记录，
                # 失败时 after_send(sent=false) 会把 sending 回滚为 pending。
                state["status"] = "sending"
                state["updated_at"] = now
                if not bool(state.get("quote_allowed", False)):
                    kwargs["set_reply"] = False

        cooldown = int(self.config.reply_safety.duplicate_text_cooldown_seconds)
        text = re.sub(r"\s+", "", self._message_text(message))
        if cooldown > 0 and text:
            for ts, old in self._recent_texts.get(session_id, deque()):
                if now - ts <= cooldown and re.sub(r"\s+", "", old) == text:
                    self._safe_log("info", "群聊逻辑优化：拦截同会话重复文本")
                    return {"action": "abort", "custom_result": {"reason": "duplicate_text", "preview": text[:40]}}

        kwargs.pop("message", None)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "send_service.after_send",
        name="outbound_success_recorder",
        description="记录成功文本/表情包，供反重复与冷却使用",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def after_send(self, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        message = kwargs.pop("message", None)
        sent = bool(kwargs.get("sent", False))
        if not sent and isinstance(message, dict):
            # 即使插件在发送间隙被禁用，也不能让 sending 状态卡住；
            # 失败回滚属于状态一致性清理，不依赖功能开关。
            session_id = str(message.get("session_id", "") or "").strip()
            target_id = str(kwargs.get("reply_message_id", "") or "").strip()
            target_state = self._target_state.get((session_id, target_id)) if session_id and target_id else None
            if isinstance(target_state, dict) and target_state.get("status") == "sending":
                target_state.update({"status": "pending", "updated_at": time.monotonic()})
                self._safe_log("info", f"群聊逻辑优化：目标 {target_id} 发送失败，已恢复为可重试状态")
        if not self._enabled():
            return {"action": "continue"}
        if not sent:
            # after_send 宿主 Hook 不允许修改 kwargs；这里只做副作用。
            return {"action": "continue"}
        if not isinstance(message, dict):
            return {"action": "continue"}
        session_id = str(message.get("session_id", "") or "").strip()
        if not session_id:
            return {"action": "continue"}
        if bool(message.get("is_emoji", False)):
            self._last_emoji[session_id] = time.monotonic()
        text = self._message_text(message)
        if text:
            self._recent_texts.setdefault(session_id, deque(maxlen=20)).append((time.monotonic(), text))

        target_id = str(kwargs.get("reply_message_id", "") or "").strip()
        target_state = self._target_state.get((session_id, target_id)) if target_id else None
        if isinstance(target_state, dict):
            target_state.update({"status": "sent", "updated_at": time.monotonic()})
        bot_target_user = str(target_state.get("target_user", "") or "") if isinstance(target_state, dict) else ""
        # 成功出站消息也是对话图的一部分；后续 quote/@/第二人称状态都以它作为机器人轮次。
        self._append_dialogue_event(
            session_id,
            message,
            is_bot=True,
            target_user=bot_target_user,
        )
        if text:
            # 主动消息没有 reply 目标状态，但仍是机器人文本轮次；
            # 不记录会导致后续指向判断缺少次数/间隔保护。
            previous = self._last_success_reply.get(session_id) if isinstance(self._last_success_reply.get(session_id), dict) else {}
            current_target_user = str(target_state.get("target_user", "") or "") if isinstance(target_state, dict) else ""
            same_target = str(previous.get("target_user", "") or "") == current_target_user
            try:
                followup_count = int(previous.get("followup_count", 0) or 0) if same_target else 0
            except (TypeError, ValueError):
                followup_count = 0
            self._last_success_reply[session_id] = {
                "updated_at": time.monotonic(),
                "message_id": str(message.get("message_id", "") or "").strip(),
                "text": text,
                "target_user": current_target_user,
                "target_message_id": target_id,
                "target_order": int(target_state.get("order", -1)) if isinstance(target_state, dict) else -1,
                "target_updated_at": (
                    float(target_state.get("target_updated_at", 0.0) or 0.0)
                    if isinstance(target_state, dict)
                    else 0.0
                ),
                "followup_count": followup_count,
                "resolver_last_at": time.monotonic(),
            }
        return {"action": "continue"}

    @HookHandler(
        "chat.receive.before_process",
        name="central_access_control",
        description="插件集中管理群聊/私聊白名单、黑名单与全量模式",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def central_access_control(self, **kwargs: Any) -> Dict[str, Any]:
        # 普通消息不回传 message，宿主保留原始对象；只有 QQ 文本连续对话兜底
        # 才回传 is_mentioned 修改。其它平台/二进制消息保持不改写，避免时间戳和 RPC 帧风险。
        raw_message = kwargs.get("message")
        if not self._enabled() or not self.config.access.enabled:
            return {"action": "continue", "modified_kwargs": {}}
        message = raw_message
        if not isinstance(message, dict):
            # 无法识别消息时保持放行，交给后续安全规则；全量/名单模式仍可正常处理常规消息。
            return {"action": "continue", "modified_kwargs": {}}

        try:
            now = time.monotonic()
            if now >= self._next_state_cleanup_at:
                self._evict_session_state()
                self._next_state_cleanup_at = now + 60.0
        except Exception:
            pass

        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = info.get("group_info") if isinstance(info, dict) and isinstance(info.get("group_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info, dict) and isinstance(info.get("user_info"), dict) else {}
        group_id = str(group_info.get("group_id", "") or "").strip() if group_info else ""
        user_id = str(user_info.get("user_id", "") or "").strip() if user_info else ""
        session_id = str(message.get("session_id", "") or "").strip()
        message_id = str(message.get("message_id", "") or "").strip()
        if session_id and group_id:
            if session_id in self._session_group_ids:
                self._session_group_ids.move_to_end(session_id, last=False)
            self._session_group_ids[session_id] = group_id
        if message_id and user_id:
            inbound_key = (session_id, message_id)
            inbound_record = self._recent_inbound_users.get(inbound_key, {})
            inbound_record.update(
                {
                    "user_id": user_id,
                    "user_name": str((user_info.get("user_cardname") or user_info.get("user_nickname") or "").strip()),
                }
            )
            # Hook 重试不能刷新同一消息的到达时间，否则旧目标拦截会误判时序。
            inbound_record.setdefault("updated_at", time.monotonic())
            self._recent_inbound_users[inbound_key] = inbound_record
        allowed, target_id, mode = self._access_allowed(group_id, user_id)
        if allowed:
            metadata = self._extract_dialogue_metadata(message)
            last_reply = self._last_success_reply.get(session_id)
            explicit_bot = bool(metadata.get("explicit_bot", False))
            if explicit_bot:
                if message_id:
                    self._explicit_bot_target_ids.add((session_id, message_id))
                # 显式点名开启新的对话轮，清空旧连续对话计数。
                if isinstance(last_reply, dict):
                    last_reply["followup_count"] = 0
                self._append_dialogue_event(session_id, message, metadata=metadata)
                if self._can_safely_mark_message(message) and not bool(message.get("is_mentioned", False)):
                    message = dict(message)
                    message["is_mentioned"] = True
                    kwargs["message"] = message
                    self._safe_log("info", "群聊逻辑优化：识别到文本昵称/引用机器人，已触发 Planner")
                    return {"action": "continue", "modified_kwargs": kwargs}
                return {"action": "continue", "modified_kwargs": {}}

            if isinstance(last_reply, dict):
                try:
                    last_age = time.monotonic() - float(last_reply.get("updated_at", 0.0))
                except (TypeError, ValueError):
                    last_age = float("inf")
                if last_age > max(
                    int(self.config.followup.window_seconds),
                    int(self.config.target_resolver.window_seconds),
                ):
                    self._last_success_reply.pop(session_id, None)
                    last_reply = None

            resolution = await self._resolve_dialogue_target(session_id, message, metadata)
            self._append_dialogue_event(session_id, message, metadata=metadata)
            if resolution.get("target") != "bot":
                # 明确指向其他用户、或与旧轮无直接延续关系的自然旁观消息，
                # 都要释放 max_followup_turns，避免旧计数卡死后续新一轮直接提问。
                if (
                    resolution.get("target") == "user"
                    or self._should_reset_followup_chain(message, metadata, last_reply)
                ) and isinstance(last_reply, dict):
                    last_reply["followup_count"] = 0
                return {"action": "continue", "modified_kwargs": {}}

            try:
                followup_count = int(last_reply.get("followup_count", 0) or 0) if isinstance(last_reply, dict) else 0
            except (TypeError, ValueError):
                followup_count = 0
            if followup_count >= int(self.config.followup.max_followup_turns):
                return {"action": "continue", "modified_kwargs": {}}
            try:
                last_trigger_at = float(
                    (last_reply or {}).get("resolver_last_at", (last_reply or {}).get("updated_at", 0.0))
                )
            except (TypeError, ValueError):
                last_trigger_at = 0.0
            if time.monotonic() - last_trigger_at < int(self.config.followup.min_interval_seconds):
                return {"action": "continue", "modified_kwargs": {}}
            # 触发时先占用次数；即使后续 Planner/发送失败，也不会无限重复强制触发。
            if isinstance(last_reply, dict):
                try:
                    last_reply["followup_count"] = int(last_reply.get("followup_count", 0) or 0) + 1
                except (TypeError, ValueError):
                    last_reply["followup_count"] = 1
                last_reply["resolver_last_at"] = time.monotonic()
            message = dict(message)
            message["is_mentioned"] = True
            kwargs["message"] = message
            self._safe_log(
                "info",
                "群聊逻辑优化：受话对象解析为机器人"
                f" confidence={resolution.get('confidence', 0)} method={resolution.get('method', '')}"
                f" reason={resolution.get('reason', '')}；已交给 Planner 最终决策",
            )
            return {"action": "continue", "modified_kwargs": kwargs}

        log_key = (session_id, target_id, mode)
        if log_key not in self._whitelist_logged:
            self._whitelist_logged.add(log_key)
            self._safe_log(
                "info",
                f"群聊逻辑优化：集中访问控制拦截 session={session_id or '<empty>'} target={target_id or '<empty>'} mode={mode}",
            )
        return {
            "action": "abort",
            "custom_result": {"reason": "group_logic_access_control", "target_id": target_id, "mode": mode},
        }

def create_plugin() -> GroupChatLogicPlugin:
    return GroupChatLogicPlugin()
