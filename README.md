# 群聊逻辑优化

面向 MaiBot 1.2.x / SDK 2.x 的群聊治理插件。

仓库：<https://github.com/Luoyehe/Maibot-group-chat-logic-optimizer>

问题反馈：<https://github.com/Luoyehe/Maibot-group-chat-logic-optimizer/issues>

## 安装与启用

推荐直接克隆到 MaiBot 的插件目录：

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/Luoyehe/Maibot-group-chat-logic-optimizer.git group_chat_logic_optimizer
```

随后：

1. 重启 MaiBot；Runner 会根据 `plugin.py` 中的 `config_model` 生成本实例的 `config.toml`。
2. 在 WebUI 的“插件管理”中确认“群聊逻辑优化”已出现并启用。
3. 按需修改 `[access]`、`[mentions]`、`[target_resolver]` 等配置后保存，插件会热更新。
4. 更新时进入插件目录执行 `git pull`，然后在 WebUI 重载插件或重启 MaiBot。

源码包不应携带本地 `config.toml`、日志、缓存、数据库或历史备份。插件目录内提供 `.gitignore`，运行时配置由 Runner 生成。

## 覆盖范围

- Planner 低延迟：wait 固定为短等待、重复 wait 抑制、reply/表情包后自动结束本轮。
- 回复安全：同目标去重、旧目标拦截、自我消息拦截、重复文本拦截。
- 技术话题：未指向机器人旁观；直接提问时强制证据边界，不确定就索要日志/截图/官方状态。
- 多模态桥接：Planner 看图后必须把图片摘要、图内文字、视觉结论和不确定性传给 replyer。
- 输出卫生：短回复、反机械开头、反重复，并可触发一次重写；不指定人格、性格或可爱/俏皮等风格。
- 引用人性化：默认每 4 条可见回复最多 1 条 QQ 引用。
- 表情包冷却：同会话默认 600 秒。
- 插件级白名单：群/私聊双白名单在消息入站早期兜底。
- Embedding + 对话状态指向判断：区分当前消息是对机器人、某个群友还是对整个群说；未配置 Embedding 时自动使用纯规则对话状态判断；命中机器人时只送入 Planner，不代 planner 生成回复。

## 不覆盖范围

模型服务部署、vLLM/llama.cpp 参数、NapCat、WebUI、向量池、模型供应商配置仍属于宿主/系统配置。插件的指向判断只调用宿主已配置或被插件显式覆盖的 Embedding 模型任务，不额外调用生成式 LLM。

## 集中访问控制

只需要在插件中维护：

```toml
[access]
enabled = true
group_mode = "whitelist"   # whitelist / blacklist / all
group_list = ["123456789"]
user_mode = "whitelist"    # whitelist / blacklist / all
user_list = ["10000"]
```

含义：

- `whitelist`：仅列表内群/私聊放行；
- `blacklist`：列表为拒绝列表，未列出的全放行；
- `all`：全量放行。

拦截发生在 `chat.receive.before_process`，早于媒体处理、会话注册、记忆写入和 Planner。

## NapCat Adapter 边界说明

本插件从 2.0.3 起不再读取或修改 NapCat Adapter 的 `config.toml`：

- `[access]` 只作为本插件的消息入站治理配置；
- 适配器自身名单、连接与登录仍由适配器或宿主插件管理器维护；
- 如需适配器层前置过滤，请按适配器自己的文档配置；
- 插件不会因为配置同步而写其他插件目录、备份数据或清理历史配置。

这是为了遵守 MaiBot Plugin SDK 的权限边界，避免普通插件越权改写其他插件配置。

## 原版 MaiBot 最小部署结论

在 MaiBot 1.2.x / SDK 2.5+ 且 NapCat Adapter 已能登录收发消息的前提下，不需要修改 MaiBot 核心源码，也不需要安装自定义 prompt。部署步骤：

1. 在 `model_config.toml` 配置并绑定模型任务：
   - `replyer`：最终回复模型；
   - `planner`：支持工具调用且建议支持视觉的模型，`temperature = 0.0`；
   - `vlm`：视觉模型；
   - `embedding`：可选；A_Memorix 或指向判断需要嵌入模型时配置；
   - 按模型自身需要关闭 thinking（例如 `chat_template_kwargs.enable_thinking=false`）。
2. 将本插件目录放入 `plugins/`。
3. 在插件 `config.toml` 配置：
   - `[access]` 群/私聊白名单、黑名单或全量；
   - `[mentions].bot_aliases` 机器人昵称/别名；
   - `[context].max_history_messages` 插件保留的聊天上下文条数，默认30。
4. 重启 MaiBot。

插件会自动注入 Planner/Replyer 规则、裁剪聊天上下文、抑制 wait 空转、处理 reply 去重、引用频率、表情包冷却、输出卫生、技术证据边界和图片信息桥接。访问控制只作用于本插件入站 Hook，不修改 NapCat Adapter 配置。

模型服务本身、vLLM/llama.cpp 参数、NapCat 登录、WebUI/TLS、Embedding 服务和向量池重建仍属于部署配置，不属于聊天逻辑插件能力范围。

## WebUI 配置

插件使用标准 MaiBot Plugin Config Schema。进入 MaiBot WebUI 的“插件管理”，找到“群聊逻辑优化”，即可查看、修改并保存配置。保存后会触发插件自配置热更新，无需重启 MaiBot。

## 配置项概览

- `[plugin]`：插件启停与配置版本。
- `[access]`：群聊/私聊访问模式与名单。
- `[mentions]`：机器人昵称；留空时自动继承宿主昵称。
- `[context]`、`[followup]`：上下文长度和连续对话保护。
- `[target_resolver]`：Embedding 来源、超时、降级与指向判断阈值。
- `[reply_fallback]`：Planner 未发工具时的有界安全兜底。
- `[reply_safety]`：目标去重、重复文本、引用频率和重写长度。
- `[technical]`、`[visual_bridge]`：技术证据边界和图片信息桥接。
- `[style]`、`[emoji]`：输出卫生约束与表情包冷却。

## 命令

本插件没有面向群聊用户的主动命令。它通过 MaiBot Hook 参与消息入站、Planner、Replyer 和发送链路；`group_logic_finish` 是内部隐藏工具，不进入模型工具列表。

## 权限与能力

Manifest 声明的能力：

- `config.get`：读取宿主机器人昵称、账号和模型任务配置。
- `llm.get_available_models`：在 `config.get` 未暴露模型任务绑定时探测宿主可用模型任务。
- `llm.embed`：调用宿主 Embedding 任务辅助判断群聊受话对象。

本插件没有额外 Python 包依赖，不需要网络直连能力、数据库能力、文件写入能力或发送消息能力；发送侧只通过宿主 Hook 处理既有消息，模型与宿主配置只通过 SDK capability 读取。

## 宿主昵称自动同步

`[mentions].bot_aliases` 留空时，插件会自动继承 MaiBot 宿主配置：

```toml
[bot]
nickname = "机器人昵称"
alias_names = ["别名1", "别名2"]
```

插件订阅 `bot` 配置热更新；在 WebUI 修改 MaiBot 昵称/别名后会自动同步，无需重启。

如果 `[mentions].bot_aliases` 非空，则插件始终优先使用这些显式配置，不再继承宿主昵称。

## Embedding + 对话状态指向判断

插件为每个会话维护轻量对话图：

- 发送者、机器人出站轮次、目标用户；
- QQ `@` 与引用消息；
- 时间间隔、机器人上一轮和中间插话；
- 第二人称/疑问等通用对话行为信号；
- 若 Embedding 可用：当前消息与机器人上一轮、最近活跃群友话题的余弦相似度。

判断目标是 `bot / user / group / none`，不是判断“是否同一用户”。同一用户只是弱状态特征，不同用户也可以追问机器人；`@别人`、引用别人、当前“你”更自然指向上一个发言群友时会判给该用户。模糊消息保持原版调度，不强行触发。

命中 `bot` 时插件只把该消息标记为提及，让 reply necessity 放行并进入 Planner；最终是否回复、回复谁，仍由 Planner 决定。这样不会用规则替代 Planner，也不会额外跑一次生成式模型。

Embedding 来源默认为 `auto`：

1. 插件 `[target_resolver].task_name` 非空时，优先使用该模型任务作为覆盖；
2. 插件未覆盖时，沿用 MaiBot 宿主的 `[model_task_config.embedding]`；
3. 两者都未配置时，不发起 Embedding 请求，自动降级为纯规则对话状态判断；
4. Embedding 请求失败或超时后，同样临时回退纯规则判断。

可用来源：

```toml
[target_resolver]
embedding_source = "auto"     # auto / host / plugin / disabled
task_name = ""                # 留空沿用宿主；建议填宿主模型任务名
use_embedding = true          # false 时始终禁用语义向量
```

- `auto`：插件覆盖优先，其次宿主 embedding；
- `host`：只使用宿主 `[model_task_config.embedding]`；
- `plugin`：只使用插件覆盖的 `task_name`；
- `disabled`：始终纯规则判断。

模型任务配置通过 `config.get` 读取；若当前宿主没有通过该能力暴露模型任务绑定，则用 `llm.get_available_models` 探测任务名。Embedding 请求通过 MaiBot 官方 `llm.embed` capability 调用宿主模型任务。默认批量请求、1.5 秒超时、30 秒失败冷却、300 秒配置探测间隔和 768 条 LRU 向量缓存。

纯规则降级仍会使用确定性 `@`/引用信号、机器人上一轮、插话链、第二人称/疑问行为和混合文本重合率；只是不再计算语义余弦相似度。日志中的 `method=rule` 即表示本条判断没有调用 Embedding。

```toml
[target_resolver]
enabled = true
embedding_source = "auto"
task_name = ""
history_messages = 12
window_seconds = 600
min_confidence = 0.62
min_score_margin = 0.08
use_embedding = true
semantic_min_similarity = 0.45
min_semantic_margin = 0.035
embedding_timeout_seconds = 1.5
embedding_retry_seconds = 30
embedding_config_refresh_seconds = 300
```


## 回复风格边界

人设、性格、语气、称呼习惯和可爱/俏皮等回复风格完全由 MaiBot 本体的人格、性格和回复设定决定。插件只保留通用的输出卫生约束：避免过长、机械开头、重复回复、句首称呼、事实编造和技术猜测；不注入任何特定人设或风格。

## 句首称呼

回复不会以对方名字、昵称、群名片或敬称开头。插件会记录目标用户显示名，并在生成后硬性剥离句首称呼；仅在多人讨论必须消歧时才建议在句中使用称呼。

## 连续直接追问

连续追问不再维护特定短语列表，也不要求同一用户。插件通过“机器人是否是上一轮主要回答者、当前第二人称更自然指向谁、话题与哪一方更相关”判断受话对象；仍受时间窗口、最短长度、最短间隔和连续次数保护。

## Planner 未发工具时的有界兜底

当 Planner 已明确判断要回复但没有发出 `reply` 时，插件会在极小范围内兜底：

```toml
[reply_fallback]
enabled = true
scan_messages = 3
max_age_seconds = 300
require_planner_intent_for_history = true
```

规则：

- 只扫描最近一次机器人发言之后的用户消息；
- 最多扫描最近 3 条；
- 超过约 5 分钟的候选不参与兜底；
- 最新消息明确 @/称呼机器人时可以直接兜底；
- 历史候选必须有 Planner 的非否定回复意图；
- 任何已被后续机器人发言阻断的目标都不会再选中；
- 模型直接传来的 `reply.msg_id` 也必须通过同一安全窗口。

`time` 仅从 `<message time="...">` 文本解析为插件内部字符串/秒数，用于防挖坟年龄判断；不转换宿主时间戳、不回传消息对象，不会复现之前的 naive/aware 时间戳污染问题。

## 故障排查

1. **插件未出现或加载失败**
   - 查看 MaiBot 日志中的 Manifest 校验、依赖和生命周期错误。
   - 确认目录名为 `group_chat_logic_optimizer`，入口为 `plugin.py`，Manifest 版本为 2。
2. **WebUI 配置不生效**
   - 保存后观察日志中的自配置热更新记录。
   - 确认 `[plugin].enabled = true`。
3. **消息被拦截**
   - 检查 `[access]` 群/私聊模式和名单。
   - 本插件不会修改 NapCat Adapter；如需适配器层过滤，请查看适配器自身配置。
4. **指向判断一直 `method=rule`**
   - 检查宿主 `[model_task_config.embedding]` 或插件 `[target_resolver].task_name`。
   - Embedding 超时后会临时降级，等待冷却后自动重试。
5. **疑似回复旧消息**
   - 查看 `[reply_fallback]` 是否保持较小窗口。
   - 日志出现“安全窗口”“挖坟回复”表示旧目标已被拦截。
6. **回归测试**

   ```bash
   cd /path/to/MaiBot
   .venv/bin/python -m pytest -q plugins/group_chat_logic_optimizer/tests/test_target_resolver.py
   ```

## 许可证

MIT License。见 [LICENSE](LICENSE)。
