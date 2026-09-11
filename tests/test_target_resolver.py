"""群聊指向判断器的最小回归集。在 MaiBot 虚拟环境中可直接运行 pytest。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from typing import Any


class FakeEmbedding:
    def __init__(
        self,
        model_tasks: dict[str, Any] | None = None,
        omit_texts: set[str] | None = None,
        available_result: dict[str, Any] | list[str] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.available_calls = 0
        self.model_tasks = model_tasks or {}
        self.omit_texts = omit_texts or set()
        self.available_result = available_result

    async def embed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        texts = kwargs.get("texts", [])
        returned = [text for text in texts if text not in self.omit_texts]
        return {
            "success": True,
            "results": [{"embedding": [1.0, 0.0], "model_name": "Embed-A"} for _ in returned],
        }

    async def get_available_models(self) -> dict[str, Any] | list[str]:
        self.available_calls += 1
        if self.available_result is not None:
            return self.available_result
        return {"success": True, "models": list(self.model_tasks)}


class FakeConfig:
    def __init__(self, model_tasks: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.model_tasks = model_tasks or {}

    async def get(self, key: str, default: Any = None) -> Any:
        self.calls.append((key, default))
        if key == "model_task_config":
            return {"model_task_config": self.model_tasks}
        return default


def load_plugin(model_tasks: dict[str, Any] | None = None):
    path = Path(__file__).resolve().parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location("group_chat_logic_optimizer_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    plugin = module.create_plugin()
    plugin._plugin_config_instance = module.GroupLogicConfig()
    plugin._host_bot_aliases = ["测试机器人"]
    plugin._host_bot_user_id = "10000"
    embedding = FakeEmbedding(model_tasks)
    config = FakeConfig(model_tasks)
    plugin._ctx = type("FakeContext", (), {"llm": embedding, "config": config})()
    plugin._embedding = embedding
    plugin._host_model_tasks = dict(model_tasks or {})
    plugin._embedding_config_refreshed_at = time.monotonic()
    return plugin


def message(message_id: str, user_id: str, text: str, *, raw_extra: list[dict] | None = None) -> dict[str, Any]:
    raw: list[dict[str, Any]] = [{"type": "text", "data": text}]
    raw.extend(raw_extra or [])
    return {
        "message_id": message_id,
        "session_id": "group:test",
        "platform": "qq",
        "processed_plain_text": text,
        "raw_message": raw,
        "is_mentioned": False,
        "is_at": False,
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": f"user-{user_id}"},
            "group_info": {"group_id": "123"},
            "additional_config": {"self_id": "10000"},
        },
    }


def bot_message(message_id: str, text: str, target_user: str = "") -> dict[str, Any]:
    result = message(message_id, "10000", text)
    result["is_mentioned"] = True
    result["is_at"] = True
    return result


def resolve(plugin, current: dict[str, Any]) -> dict[str, Any]:
    metadata = plugin._extract_dialogue_metadata(current)
    return asyncio.run(plugin._resolve_dialogue_target(current["session_id"], current, metadata))


def test_host_model_tasks_are_loaded_through_sdk_capabilities() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    plugin._host_model_tasks = {}
    plugin._host_model_tasks_fallback = False

    asyncio.run(plugin._refresh_host_embedding_config())

    assert plugin._ctx.config.calls == [("model_task_config", None)]
    assert plugin._embedding.available_calls == 0
    assert plugin._host_model_tasks["embedding"]["model_list"] == ["Embed-A"]
    assert plugin._effective_embedding_task() == "embedding"


def test_available_models_capability_is_used_when_config_get_has_no_binding() -> None:
    plugin = load_plugin({"embedding": {}})
    plugin._host_model_tasks = {}
    plugin._host_model_tasks_fallback = False

    asyncio.run(plugin._refresh_host_embedding_config())

    assert plugin._ctx.config.calls == [("model_task_config", None)]
    assert plugin._embedding.available_calls == 1
    assert plugin._host_model_tasks_fallback is True
    assert plugin._effective_embedding_task() == "embedding"
    asyncio.run(plugin._embed_dialogue_texts(["同一句话"]))
    asyncio.run(plugin._embed_dialogue_texts(["同一句话"]))
    assert len(plugin._embedding.calls) == 1
    assert len(plugin._embedding_cache) == 1
    assert plugin._embedding_cache_model == "Embed-A"


def test_available_models_capability_supports_sdk_list_result() -> None:
    plugin = load_plugin()
    embedding = FakeEmbedding(available_result=["embedding"])
    plugin._embedding = embedding
    plugin._ctx.llm = embedding
    plugin._host_model_tasks = {}
    plugin._host_model_tasks_fallback = False

    asyncio.run(plugin._refresh_host_embedding_config())

    assert embedding.available_calls == 1
    assert plugin._host_model_tasks_fallback is True
    assert plugin._effective_embedding_task() == "embedding"


def test_plugin_does_not_cross_host_or_plugin_file_boundaries() -> None:
    plugin = load_plugin()
    root = Path(__file__).resolve().parents[1]
    text = (root / "plugin.py").read_text(encoding="utf-8")
    metadata = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))

    for banned in (
        "_read_host_model_tasks_file",
        "_find_adapter_config_path",
        "_sync_adapter_access_config",
        "manage_adapter",
        "model_config.toml",
        "parents[2]",
        "os.replace",
        "shutil.copy",
    ):
        assert banned not in text
    assert not hasattr(plugin.config.access, "manage_adapter")
    assert not hasattr(plugin.config.access, "adapter_plugin_id")
    assert metadata["dependencies"] == []


def test_different_user_can_follow_up_bot() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._append_dialogue_event(session, message("u1", "1", "测试机器人，我是谁"))
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "2", "那你是谁？"))
    assert result["target"] == "bot"
    assert result["method"] == "rule"
    assert plugin._embedding.calls == []


def test_direct_imperative_answer_to_bot_question_is_forced() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._append_dialogue_event(session, message("u1", "1", "测试机器人来整个活儿"))
    plugin._append_dialogue_event(
        session,
        bot_message("b1", "整活没有，倒是想问问你硬盘降价了准备囤几块", "1"),
        is_bot=True,
        target_user="1",
    )
    result = resolve(plugin, message("u2", "1", "你出钱给我买"))
    assert result["target"] == "bot"
    assert result["confidence"] >= plugin.config.target_resolver.min_confidence


def test_neutral_acknowledgement_releases_exhausted_followup_budget() -> None:
    plugin = load_plugin()
    plugin.config.access.group_list = ["123"]
    session = "group:test"
    plugin._last_success_reply[session] = {
        "updated_at": time.monotonic() - 5,
        "message_id": "b1",
        "text": "瞎猜啥，我是测试机器人，底模部署那些我不认账",
        "target_user": "1",
        "followup_count": plugin.config.followup.max_followup_turns,
        "resolver_last_at": time.monotonic() - 5,
    }

    acknowledgement = asyncio.run(
        plugin.central_access_control(message=message("u1", "1", "好好好，言之有理"))
    )
    assert acknowledgement == {"action": "continue", "modified_kwargs": {}}
    assert plugin._last_success_reply[session]["followup_count"] == 0

    direct_question = asyncio.run(
        plugin.central_access_control(message=message("u2", "1", "那你告诉我中午我吃什么"))
    )
    assert direct_question["action"] == "continue"
    assert direct_question["modified_kwargs"]["message"]["is_mentioned"] is True


def test_late_food_preference_and_short_imperative_still_target_bot() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    session = "group:test"
    plugin._append_dialogue_event(
        session,
        bot_message("b1", "吃面吧，热乎还快，吃完还能回来继续debug", "1"),
        is_bot=True,
        target_user="1",
    )
    # 7 分钟仍属于本轮实际群聊节奏；旧版 180 秒会直接判定无近期机器人轮次。
    plugin._session_dialogue[session][-1]["timestamp"] = time.monotonic() - 420
    first = message("u1", "1", "不要，我要吃烤鸭")
    first_result = resolve(plugin, first)
    assert first_result["target"] == "bot"
    plugin._append_dialogue_event(session, first, metadata=plugin._extract_dialogue_metadata(first))

    second_result = resolve(plugin, message("u2", "1", "你请我吃"))
    assert second_result["target"] == "bot"


def test_question_to_previous_user_is_not_bot() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._append_dialogue_event(session, message("u1", "1", "你会做饭吗"))
    plugin._append_dialogue_event(session, bot_message("b1", "锅在哪，米也没有", "1"), is_bot=True, target_user="1")
    plugin._append_dialogue_event(session, message("u2", "1", "我今天去爬山了"))
    result = resolve(plugin, message("u3", "2", "你爬的哪座山？"))
    assert result["target"] == "user"
    assert result["target_user"] == "1"


def test_lexical_followup_after_bot() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._append_dialogue_event(session, message("u1", "1", "我饿了"))
    plugin._append_dialogue_event(session, bot_message("b1", "锅在哪，米也没有", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "1", "来点儿赛博做饭，不用锅"))
    assert result["target"] == "bot"


def test_unrelated_message_after_bot_stays_group() -> None:
    plugin = load_plugin()
    # 该用例验证纯状态分数；语义距离由真实 Embedding 回归另行验证。
    plugin.config.target_resolver.use_embedding = False
    session = "group:test"
    plugin._append_dialogue_event(session, message("u1", "1", "你是谁"))
    plugin._append_dialogue_event(session, bot_message("b1", "我是测试机器人", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "1", "今天天气不错"))
    assert result["target"] == "group"


def test_auto_uses_host_embedding_when_plugin_does_not_override() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "2", "那你是谁？"))
    assert result["method"] == "rule+embedding"
    assert plugin._embedding.calls[0]["task_name"] == "embedding"


def test_auto_plugin_override_can_use_model_name() -> None:
    plugin = load_plugin(
        {
            "embedding": {"model_list": ["Embed-A"]},
            "custom_vector": {"model_list": ["Embed-B"]},
        }
    )
    plugin.config.target_resolver.task_name = "Embed-B"
    assert plugin._effective_embedding_task() == "custom_vector"
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "2", "那你是谁？"))
    assert plugin._embedding.calls[0]["task_name"] == "custom_vector"
    assert result["method"] == "rule+embedding"


def test_host_source_ignores_plugin_override() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    plugin.config.target_resolver.embedding_source = "host"
    plugin.config.target_resolver.task_name = "Embed-B"
    assert plugin._effective_embedding_task() == "embedding"


def test_disabled_source_never_calls_embedding() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    plugin.config.target_resolver.embedding_source = "disabled"
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "2", "那你是谁？"))
    assert result["target"] == "bot"
    assert result["method"] == "rule"
    assert plugin._embedding.calls == []


def test_embedding_preserves_original_text_keys_with_extra_whitespace() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    original = "那  你是 谁？"
    vectors = asyncio.run(plugin._embed_dialogue_texts([original, "上一句"]))
    assert original in vectors
    assert plugin._embedding.calls[0]["texts"] == ["那 你是 谁？", "上一句"]


def test_embedding_cache_is_cleared_when_model_binding_changes() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    plugin._host_model_binding_fingerprint = "old"
    plugin._embedding_cache["stale"] = (time.monotonic(), [1.0, 0.0])
    asyncio.run(plugin._refresh_host_embedding_config({"embedding": {"model_list": ["Embed-B"]}}))
    assert plugin._embedding_cache == {}
    assert plugin._effective_embedding_task() == "embedding"


def test_empty_bot_emoji_turn_does_not_block_previous_text_turn() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    emoji_turn = message("b2", "10000", "")
    emoji_turn["is_emoji"] = True
    plugin._append_dialogue_event(session, emoji_turn, is_bot=True, target_user="1")
    result = resolve(plugin, message("u2", "2", "那你是谁？"))
    assert result["target"] == "bot"


def test_planner_history_counts_messages_not_context_items() -> None:
    plugin = load_plugin()
    text = "\n".join(f'<message msg_id="m{i}" user="u">内容{i}</message>' for i in range(40))
    item = {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": text}]}
    result = plugin._prune_planner_history([item])
    result_text = plugin._extract_text_parts(result[0])
    assert result_text.count("<message ") <= plugin.config.context.max_history_messages


def test_explicit_at_gets_forced_reply_fallback() -> None:
    plugin = load_plugin()
    plugin._context_cache["group:test"] = {
        "u1": {"order": 0, "text": "hello", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True}
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "看看"}]}
    forced, reason = plugin._forced_reply_fallback("group:test", source, [source])
    assert forced is not None
    assert reason == ""


def test_failed_send_restores_target_for_retry_and_after_send_has_no_kwargs_modification() -> None:
    plugin = load_plugin()
    state_key = ("group:test", "u1")
    plugin._target_state[state_key] = {
        "status": "pending",
        "updated_at": time.monotonic(),
        "quote_allowed": False,
        "target_user": "1",
        "target_name": "用户一",
        "target_updated_at": time.monotonic(),
    }
    outbound = message("b1", "10000", "先试一下")
    before = asyncio.run(
        plugin.before_send(message=dict(outbound), reply_message_id="u1", sent=False)
    )
    assert before["modified_kwargs"]["set_reply"] is False
    assert plugin._target_state[state_key]["status"] == "sending"

    blocked = asyncio.run(
        plugin.before_send(message=dict(outbound), reply_message_id="u1", sent=False)
    )
    assert blocked["action"] == "abort"
    assert blocked["custom_result"]["reason"] == "duplicate_target"

    failure = asyncio.run(
        plugin.after_send(message=dict(outbound), reply_message_id="u1", sent=False)
    )
    assert failure == {"action": "continue"}
    assert plugin._target_state[state_key]["status"] == "pending"


def test_successful_send_marks_sent_and_blocks_older_target() -> None:
    plugin = load_plugin()
    old_time = time.monotonic() - 10
    new_time = time.monotonic() - 1
    plugin._recent_inbound_users[("group:test", "old")] = {"user_id": "1", "user_name": "一", "updated_at": old_time}
    plugin._recent_inbound_users[("group:test", "new")] = {"user_id": "2", "user_name": "二", "updated_at": new_time}
    plugin._target_state[("group:test", "new")] = {
        "status": "sending",
        "updated_at": new_time,
        "target_user": "2",
        "target_updated_at": new_time,
    }
    outbound = message("b1", "10000", "好了")
    outbound["message_info"]["user_info"]["user_id"] = "10000"
    result = asyncio.run(plugin.after_send(message=outbound, reply_message_id="new", sent=True))
    assert result == {"action": "continue"}
    assert plugin._target_state[("group:test", "new")]["status"] == "sent"
    assert plugin._last_success_reply["group:test"]["target_updated_at"] == new_time

    plugin._context_cache["group:test"] = {
        "old": {"order": 0, "text": "旧消息", "user": "1", "is_self": False, "is_at": False, "is_mentioned": False}
    }
    source = {"item_type": "FunctionCallItem", "meta": {}, "tool_call": {"func_name": "reply", "args": {"msg_id": "old"}}}
    normalized, reason = plugin._normalize_reply_item("group:test", source, {"msg_id": "old"})
    assert normalized is None
    assert "旧目标" in reason


def test_incomplete_alternative_vector_does_not_use_embedding_boost() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "我先不聊这个", "0"), is_bot=True, target_user="0")
    plugin._append_dialogue_event(session, message("u1", "1", "我今天去爬山了"))
    current = message("u2", "2", "你爬的哪座山？")
    alternative_text = str(
        next(
            record
            for record in reversed(plugin._session_dialogue[session])
            if record.get("kind") == "user"
        ).get("text", "")
    )
    plugin._embedding.omit_texts = {alternative_text}
    result = resolve(plugin, current)
    assert result["target"] == "user"
    assert result["method"] == "rule"


def test_non_finite_embedding_vectors_are_rejected() -> None:
    plugin = load_plugin({"embedding": {"model_list": ["Embed-A"]}})

    class BadEmbedding:
        async def embed(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "success": True,
                "results": [{"embedding": [float("nan"), float("inf")]} for _ in kwargs.get("texts", [])],
            }

    plugin._ctx.llm = BadEmbedding()
    vectors = asyncio.run(plugin._embed_dialogue_texts(["a", "b"]))
    assert vectors == {}
    assert plugin._embedding_error_until > 0.0


def test_planner_history_split_preserves_non_text_parts() -> None:
    plugin = load_plugin()
    text = "\n".join(f'<message msg_id="m{i}" user="u">内容{i}</message>' for i in range(40))
    item = {
        "item_type": "UserMessageItem",
        "parts": [
            {"type": "text", "text": text},
            {"type": "image", "data": "keep-me"},
        ],
    }
    result = plugin._prune_planner_history([item])[0]
    assert any(part.get("type") == "image" for part in result["parts"])


def test_emoji_cooldown_does_not_consume_terminal_reply_opportunity() -> None:
    plugin = load_plugin()
    plugin._last_emoji["group:test"] = time.monotonic()
    plugin._recent_inbound_users[("group:test", "u1")] = {
        "user_id": "1",
        "user_name": "一",
        "updated_at": time.monotonic(),
    }
    plugin._context_cache["group:test"] = {
        "u1": {"order": 0, "text": "在吗", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True}
    }
    emoji_call = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "send_emoji", "args": {}},
    }
    reply_call = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "reply", "args": {"msg_id": "u1"}},
    }
    result = asyncio.run(plugin.planner_after_response(session_id="group:test", output_items=[emoji_call, reply_call]))
    items = result["modified_kwargs"]["output_items"]
    assert all(plugin._function_call(item)[0] != "send_emoji" for item in items)
    assert any(plugin._function_call(item)[0] == "reply" for item in items)


def test_model_cannot_directly_invoke_hidden_finish_tool() -> None:
    plugin = load_plugin()
    finish_call = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "group_logic_finish", "args": {}},
    }
    result = asyncio.run(plugin.planner_after_response(session_id="group:test", output_items=[finish_call]))
    items = result["modified_kwargs"]["output_items"]
    assert all(plugin._function_call(item)[0] != "group_logic_finish" for item in items)


def test_proactive_bot_text_is_rate_limited_by_dialogue_state() -> None:
    plugin = load_plugin()
    plugin.config.access.group_list = ["123"]
    outbound = message("b1", "10000", "我先说一句")
    outbound["message_info"]["user_info"]["user_id"] = "10000"
    asyncio.run(plugin.after_send(message=dict(outbound), sent=True))
    assert plugin._last_success_reply["group:test"]["target_user"] == ""

    current = message("u1", "1", "那你是谁？")
    result = asyncio.run(plugin.central_access_control(message=current))
    # 刚发送完主动文本不满足 min_interval_seconds，不应再次强制 Planner。
    assert result == {"action": "continue", "modified_kwargs": {}}


def test_technical_guess_is_not_excused_by_asking_for_logs() -> None:
    plugin = load_plugin()
    plugin._target_state[("group:test", "u1")] = {
        "status": "pending",
        "updated_at": time.monotonic(),
        "technical": True,
    }
    reason = plugin._need_replyer_retry("group:test", "u1", "应该是风控了，发日志我看看")
    assert reason is not None
    assert "未经验证根因" in reason


def test_output_guard_does_not_override_host_tone() -> None:
    plugin = load_plugin()
    reason = plugin._need_replyer_retry("group:test", "u1", "瞎猜啥，我可不认账喵")
    assert reason is None


def test_output_policy_delegates_style_to_host_configuration() -> None:
    plugin = load_plugin()
    policy = plugin._replyer_policy("group:test", "u1")
    assert "完全遵循宿主 MaiBot 配置" in policy
    for forbidden in ("温柔", "俏皮", "可爱", "软乎乎"):
        assert forbidden not in policy


def test_at_other_user_is_deterministic() -> None:
    plugin = load_plugin()
    current = message(
        "u1",
        "2",
        "你觉得呢",
        raw_extra=[{"type": "at", "data": {"target_user_id": "3"}}],
    )
    metadata = plugin._extract_dialogue_metadata(current)
    assert metadata["explicit_other"] is True
    result = resolve(plugin, current)
    assert result["target"] == "user"
    assert result["target_user"] == "3"


def test_receive_hook_marks_resolved_bot_target() -> None:
    plugin = load_plugin()
    plugin.config.access.group_list = ["123"]
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "你名片写着橙汁", "1"), is_bot=True, target_user="1")
    result = asyncio.run(plugin.central_access_control(message=message("u2", "2", "那你是谁？")))
    assert result["action"] == "continue"
    assert result["modified_kwargs"]["message"]["is_mentioned"] is True


def test_receive_hook_keeps_other_user_target_unmarked() -> None:
    plugin = load_plugin()
    plugin.config.access.group_list = ["123"]
    session = "group:test"
    plugin._append_dialogue_event(session, bot_message("b1", "我先忙啦", "1"), is_bot=True, target_user="1")
    plugin._append_dialogue_event(session, message("u1", "1", "我今天去爬山了"))
    result = asyncio.run(plugin.central_access_control(message=message("u2", "2", "你爬的哪座山？")))
    assert result == {"action": "continue", "modified_kwargs": {}}


def test_receive_hook_keeps_group_whitelist() -> None:
    plugin = load_plugin()
    plugin.config.access.group_list = ["999"]
    result = asyncio.run(plugin.central_access_control(message=message("u1", "1", "你好")))
    assert result["action"] == "abort"
    assert result["custom_result"]["target_id"] == "123"


def test_forced_reply_fallback_ignores_historical_alias_digging() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "old": {"order": 0, "text": "测试机器人你是什么MBTI？", "user": "1", "is_self": False, "is_at": False, "is_mentioned": True},
        "latest": {"order": 1, "text": "你买的啥", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "无需回复。"}]}
    forced, reason = plugin._forced_reply_fallback(session, source, [source])
    assert forced is None
    assert reason == ""


def test_forced_reply_fallback_only_targets_latest_message() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "old": {"order": 0, "text": "无关旧消息", "user": "1", "is_self": False, "is_at": False, "is_mentioned": False},
        "latest": {"order": 1, "text": "测试机器人，现在看一下", "user": "2", "is_self": False, "is_at": True, "is_mentioned": True},
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "应该旁观。"}]}
    forced, _reason = plugin._forced_reply_fallback(session, source, [source])
    assert forced is not None
    assert forced["tool_call"]["args"]["msg_id"] == "latest"


def test_direct_reply_to_older_target_is_cancelled() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "old": {"order": 0, "text": "测试机器人做点饭", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True},
        "bot": {"order": 1, "text": "锅在哪，米也没有", "user": "测试机器人", "is_self": True},
        "latest": {"order": 2, "text": "你买的啥", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    item = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "reply", "args": {"msg_id": "old"}},
    }
    normalized, reason = plugin._normalize_reply_item(session, item, {"msg_id": "old"})
    assert normalized is None
    assert "安全窗口" in reason


def test_bounded_older_target_after_last_bot_reply_is_allowed() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "bot": {"order": 0, "text": "我先回答完上一条", "user": "测试机器人", "is_self": True},
        "older": {"order": 1, "text": "测试机器人，看一下这个", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True},
        "latest": {"order": 2, "text": "我插一句", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    item = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "reply", "args": {"msg_id": "older"}},
    }
    normalized, _reason = plugin._normalize_reply_item(session, item, {"msg_id": "older"})
    assert normalized is not None
    assert normalized["tool_call"]["args"]["msg_id"] == "older"


def test_unknown_group_reply_target_is_rejected() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "latest": {"order": 0, "text": "当前消息", "user": "1", "is_self": False, "is_at": False, "is_mentioned": False}
    }
    item = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "reply", "args": {"msg_id": "hallucinated"}},
    }
    normalized, reason = plugin._normalize_reply_item(session, item, {"msg_id": "hallucinated"})
    assert normalized is None
    assert "幻觉目标" in reason


def test_bounded_history_fallback_requires_planner_intent() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "bot": {"order": 0, "text": "刚回答过", "user": "测试机器人", "is_self": True},
        "explicit": {"order": 1, "text": "测试机器人，看看这个", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True},
        "latest": {"order": 2, "text": "我插一句", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "无需回复。"}]}
    assert plugin._forced_reply_fallback(session, source, [source])[0] is None


def test_bounded_history_fallback_selects_recent_explicit_target() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "bot": {"order": 0, "text": "刚回答过", "user": "测试机器人", "is_self": True},
        "explicit": {"order": 1, "text": "测试机器人，看看这个", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True},
        "latest": {"order": 2, "text": "我插一句", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "应该回复刚才对测试机器人的请求。"}]}
    forced, _reason = plugin._forced_reply_fallback(session, source, [source])
    assert forced is not None
    assert forced["tool_call"]["args"]["msg_id"] == "explicit"


def test_bounded_history_fallback_stops_at_newer_bot_reply() -> None:
    plugin = load_plugin()
    session = "qq_group:test"
    plugin._context_cache[session] = {
        "bot": {"order": 0, "text": "上一轮", "user": "测试机器人", "is_self": True},
        "explicit": {"order": 1, "text": "测试机器人，看看这个", "user": "1", "is_self": False, "is_at": True, "is_mentioned": True},
        "new_bot": {"order": 2, "text": "已经回答了", "user": "测试机器人", "is_self": True},
        "latest": {"order": 3, "text": "聊别的", "user": "2", "is_self": False, "is_at": False, "is_mentioned": False},
    }
    source = {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "应该回复。"}]}
    forced, _reason = plugin._forced_reply_fallback(session, source, [source])
    # 旧explicit必须被新机器人回复阻断；Planner仍有回复意图时只允许兜底最新消息。
    assert forced is not None
    assert forced["tool_call"]["args"]["msg_id"] == "latest"


def test_negative_reply_intent_is_not_treated_as_must_reply() -> None:
    plugin = load_plugin()
    output = {
        "item_type": "AssistantMessageItem",
        "parts": [{"type": "text", "text": "这条不应该回复。"}],
    }
    assert plugin._analysis_requests_reply([output]) is False


def test_wait_negation_does_not_cancel_reply_intent() -> None:
    plugin = load_plugin()
    output = {
        "item_type": "AssistantMessageItem",
        "parts": [{"type": "text", "text": "必须回复，无需等待。"}],
    }
    assert plugin._analysis_requests_reply([output]) is True


def test_technical_guard_uses_runtime_group_mapping() -> None:
    plugin = load_plugin()
    session = "group:test"
    plugin._session_group_ids[session] = "123"
    plugin._context_cache[session] = {
        "latest": {
            "order": 0,
            "text": "这个模型API报错怎么解决",
            "user": "1",
            "is_self": False,
            "is_at": False,
            "is_mentioned": False,
        }
    }
    item = {
        "item_type": "FunctionCallItem",
        "meta": {},
        "tool_call": {"func_name": "reply", "args": {"msg_id": "latest"}},
    }
    normalized, reason = plugin._normalize_reply_item(session, item, {"msg_id": "latest"})
    assert normalized is None
    assert "技术消息" in reason
