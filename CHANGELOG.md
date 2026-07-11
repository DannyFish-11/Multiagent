# Changelog

本项目遵循分阶段交付。以下为面向"完整、稳定、易用、可交付"的近期迭代。

## 0.7.0 — 流式输出(M26:SSE /chat/stream)

- **`POST /chat/stream`(text/event-stream)**:逐 token 流式对话。事件 `data: {type: meta|
  token|done|error}`——先 `meta`(event_id + 命中记忆),再逐块 `token`,最后 `done`;流内
  出错发 `error` 事件而非静默断流。
- **真流式**在 chat 档(MemoryAgent):`OpenAICompatAdapter.chat_stream`(OpenAI 兼容
  `stream=true` + `stream_options.include_usage`,SSE 解析增量,usage 在流末进 CostLedger;
  流已开始不做端点 failover)。`MemoryAgent.chat_stream` 产出事件、流末同步写记忆;LLM 不支持
  流式则回落整段一次性。tools/swarm/supervisor 档经端点整段一次性给出(仍走同一 SSE 通道)。
- 浏览器聊天 UI 改走 `/chat/stream`,逐 token 增量渲染(仍 textContent 防 XSS)。

## 0.6.0 — 中心调度 Supervisor(M25:协调者委派 worker 汇总)

- **`agent.autonomy=supervisor`**:中央协调者拆解任务、委派 worker、汇总结果(与 M24 swarm
  的去中心化手递手互补——控制权始终在协调者,worker 只返回结果不接管)。落地即**组合、零新
  循环**:每个 worker 是一个带独立人设的 `ToolAgent`(默认不自动写回子任务);"委派"是一个 `Tool`
  (`delegate_to_<worker>`);协调者本身也是 `ToolAgent`,其工具正是这些委派工具。审批闸 /
  循环硬上限 / CostLedger / 注入防御全部自动复用;深度恒为 2(协调者→worker),无无限递归。
- 委派对 `delegate:<worker>` 可配审批分级——**deny 真正拦住 worker 运行**(运行 worker 就是
  审批闸的 execute 回调,与 swarm 转交的治理修复同理)。`build_supervisor` 装配期校验(空/
  重名 worker),`doctor` 同款预检。需 function-calling 模型;缺 worker 安全回落 MemoryAgent。
- `ToolAgent` 加 `persona`(覆盖系统提示,worker 各有人设)+ `write_back`(worker 不写回记忆)
  两个可选参数(向后兼容);`Tool` 新增 `delegate_tool` 工厂。docs/PLUGINS.md 加 supervisor 段
  + swarm/supervisor 选型指引。

## 0.5.0 — 去中心化 Swarm(M24:成员手递手传任务)

- **`agent.autonomy=swarm`**:去中心化多成员 agent——named 成员之间**自主转交**任务,无中央
  调度器(蜂群式)。不引 langgraph:**转交=一个 Tool**(`transfer_to_<成员>`),整段循环
  复用既有治理——每步工具过审批闸(M9.2)、转接链受 `loops.delegation_chain` 硬上限(防
  A↔B 乒乓,触顶 `loop_capped` 不静默)、成本进 CostLedger、跨成员共享同一段对话转录、对
  不可信数据保持注入防御(其他成员/工具产出不当作指令)。
- 配 `swarm.members`(名字 + 人设 prompt + 私有 tools + handoffs);`build_swarm` 装配期
  fail-fast 校验(空/重名/坏 entry/悬空 handoff),`doctor` 预检同款。需 function-calling
  模型;非 fc 模型或缺成员**安全回落** MemoryAgent。`SwarmAgent` 是 MemoryAgent 的 drop-in。
- `Tool` 加 `handoff_to` 字段;`build_toolbox(names=…)` 支持按成员组装私有工具箱(向后兼容)。
- Harness Profile 亦作用于 swarm(每成员人设 + 采样 + 结果截断)。docs/PLUGINS.md 加 swarm 段。

## 0.4.0 — Harness Profiles(M23:让开源模型发挥真实水平)

- **Harness Profile**:按模型打包的"脚手架"(系统提示 + 采样参数 + 工具循环处理),做成
  一等插件(`profile` 类,走同一套注册表:树内 `@register("profile", ...)`,树外 `profile:xxx`
  entry point)。选 `agent.profile`:`auto`(默认,按 chat 模型名自动匹配;**匹配不到回落
  `default` 零侵入**,闭源旗舰不受影响)/ 具体名 / `none`。内置 gemma/glm/deepseek/kimi/qwen。
- 作用点(纯加法,不改协议签名):`system_prompt` 指引对 MemoryAgent 与 ToolAgent 都叠加;
  采样参数经 `**kw` 透传给 `chat_tools`;`tool_result_max_chars` 在工具结果回灌前截断
  (文章"只回传有用结果"的轻量落地——省 token、防长结果塞爆上下文);`max_tools_per_turn`/
  `max_steps` 可覆盖工具循环上限。
- `doctor` 增 Harness profile 顾问项(auto 显示实际选中项;未注册名报错列出可用);
  `make plugins` / `/plugins` 列出 profile 类。写 profile 见 docs/PLUGINS.md。

## 0.3.0 — 自主工具循环(M22)

- `agent.autonomy=tools`(**默认开**):会用工具的 agent 循环——LLM 自己决定调用工具(内置
  `recall`/`remember`/`web_search`/`web_fetch` + 第三方 `tool` 插件),检索→决策→经审批闸执行→
  回灌→迭代,受循环硬上限约束(`loop_capped` 不静默)。非 function-calling 模型(echo/local)
  自动回落记忆问答;想纯问答设 `autonomy=chat`。默认工具集仅安全的 recall/remember。
- 治理复用:每次工具调用经 `ApprovalQueue.gate`(auto/confirm/deny + 审计);安全工具
  (recall/remember)自动直放,危险工具按 config 分级。
- LLM 适配器加 `chat_tools`(OpenAI-compat / LiteLLM 的 function-calling);`ToolAgent` 是
  `MemoryAgent` 的 drop-in(services 按 autonomy 装配)。`doctor` 校验 tools 模式的模型能力。

## 0.2.0 — 易用性 / 可观测性 / 模块化(M20–M21)

### 易用性(M20 A + turnkey)
- 无 key demo 档(`llm.mode=echo` + fake 嵌入 + 内存向量库):`make demo` 零 key/GPU/docker 看记忆闭环。
- 首次运行向导 `memory-agent setup`:交互选 LLM/嵌入/预算 → 写 `.env`(密钥只落 `.env`)。
- 终端对话 `memory-agent chat`;一键引导 `make quickstart`(检查 docker → 向导 → compose up → 等健康检查)。
- `.env` 现被 `load_config` 自动读取(优先级:进程环境 > `.env` > `config.yaml`)。
- README 三条上手路径 + `docs/QUICKSTART.md`;CI 绿灯(install + lint + test + demo)。

### 可观测性(M20 B,可选)
- Langfuse via OpenTelemetry,业务零侵入(`adapters/observability.py`);`observability.enabled` 默认关 = 零依赖零开销。
- 独立 `docker-compose.observability.yaml`;span 用 GenAI 语义约定(model/token/耗时/检索命中)。

### 模块化 / 插件系统(M21)
- 统一插件注册表 `core/plugins.py`:`@register` + entry_points(`memory_agent.plugins`)自动发现;六类扩展点(llm/embedder/memory/cloud_provider/task_source/tool)。
- 工厂全部改走注册表(向后兼容);选择器字段 `Literal→str`,第三方插件名可直接配。
- 现成 drop-in(可选 extra):**LiteLLM**(100+ LLM)、**Ray**(大规模并发)、**Inspect-AI**(跑 benchmark)。
- `make plugins` 列出插件;`docs/PLUGINS.md` 写插件指南。

### 产品化(M21 续)
- 统一 CLI `memory-agent {doctor,config,plugins,run,chat,setup,demo}`(console entry point)。
- **`memory-agent doctor` 启动前体检**:配置/依赖/插件名/目录一次性校验,给 ✅/⚠️/❌ + 修复提示,有 ❌ 退出非零。
- 诊断路由 `/config`(密钥脱敏)、`/plugins`;`docs/DEPLOY.md` 生产部署清单。

### 修复
- 双层熔断占比闸门死代码、conductor 大实验饿死队列、commons 举报绕过采用感知门槛、m15 治理成本恒 0(第四轮排查,+4 回归)。

## 0.1.0 — PHASE 1–5
四层记忆 agent · 身份/A2A/行动层/记忆资产化/代谢 · API/Docker 化 · 并发/上网/邮件/支付 · 群体资产库/治理实验 · 云端实验工厂/世代演化。
