# Changelog

本项目遵循分阶段交付。以下为面向"完整、稳定、易用、可交付"的近期迭代。

## 0.12.1 — 全项目深度审查 + 加固(4 个并行子 agent 二次全库对抗审计)

在 0.12.0 之上再做一轮更深的全项目对抗审计,4 个并行子 agent 各扫一片,共发现并**修复
15 处真实缺陷**(既有代码与新代码兼有),全部补回归测试锁定(317 passed / 10 skipped,ruff 全绿):

**安全与金钱(硬红线):**
- **审批闸金额硬闸**(`core/approval.py`):`amount_usd=nan/inf/负数` 此前可**同时绕过预算记账
  与数值策略**(nan 的一切比较恒 False),现由 `_amount_deny` 在闸口直接 deny——即便 policy
  误配为 auto 也拦下;预算记账只认有限非负值。
- **支付来源闸内置化 + 原子预留**(`adapters/payments.py`):`pay()` 的 `source` 改为**必填**且
  内部强制 `assert_human_initiated`(不再靠调用方自觉);`reserve→charge→finalize`,**结算失败自动
  退款**释放额度;`check→charge` 的 TOCTOU 窗口用同锁原子预留关闭(并发多笔不越日/月上限)。
- **A2A 信封校验**(`adapters/a2a.py`):`handle_task` 对签名/身份不符的信封**降级来源身份**
  (`from_agent_id=None`),`delegate()` 发送完整签名信封而非裸 payload。
- 成本账本负 usage 钳零(`adapters/cost_ledger.py`);payment_guard 集成负向测试改为验证 `pay()`
  **内部**拒绝非人类来源(而非测试自证)。

**可用性(默认档不再静默退化为 500):**
- **可观测性能力探测**(`adapters/observability.py`):`TracedLLM` 曾把 `chat_tools/chat_stream`
  定义成实例方法,使 `hasattr(wrapper, "chat_tools")` **恒真**——api 据此判 autonomy,会把 echo 等
  非 function-calling 模型误判为可用 → 运行时 500;改为**仅当底层确有该能力**时才绑定暴露。
- **优雅停机按所有权关闭**(`services/api.py`/`embed_service.py`):只关闭本 app 自建的客户端,
  注入(测试/多 app 共享)的 llm/memory 归调用方所有、不代关;并补 `QdrantMemoryStore.aclose()`
  与嵌入服务的连接释放,消除重启泄漏。

**健壮性:**
- 群体资产库举报**去重按不同上报者计数**(`core/commons_metrics.py`):单实例连报不再叠加,广采条目
  须更多**不同**举报者方降级(防刷举报打压)。
- conductor 建 VM 失败**标 failed 释放并发槽/预算**、不再永久卡 provisioning(`core/conductor.py`)。
- 分级规则引擎数值谓词遇类型不符**落空回落**、不冒 TypeError 中断整条评估(`core/policy_engine.py`)。
- `--port 0`、`max_tools/max_steps` 非正值等边界钳制(`core/launch.py`/`tool_agent.py`/`swarm.py`);
  第三方工具装载失败改**记 warning** 不再静默 `pass`(`core/tools.py`)。

## 0.12.0 — 一键运行(M31:双击即用,不用 clone/命令行)

让"跑起来"从"clone + 装环境 + 敲命令"变成**双击就用**:

- **`core/launch.py` + `memory-agent start`**:零配置也能跑——没 `.env`/没设 `MEMORY_AGENT_LLM__*`
  时自动回落 **demo 档**(echo+fake+内存库,零 key/GPU),启动后**自动打开浏览器**到聊天页;
  已配置则尊重用户配置、不覆盖;无 GUI 环境静默跳过开浏览器。
- **双击启动器**:`run.bat`(Windows)/ `run.sh`(Linux/mac)——首次自动装 `uv`+依赖,起服务开浏览器。
- **独立可执行文件(不用 clone/Python)**:`packaging/memory-agent.spec`(PyInstaller 单文件,排除
  torch 等重依赖)+ `.github/workflows/release.yml` 为 Windows/Linux 自动构建、跑 `/healthz` 自检、
  挂到 **Releases**。用户下载双击 → 自动开浏览器聊天页。**已在 Linux 实测**:66MB 单文件,脱离仓库
  从 /tmp 运行,healthz 全绿、`/chat/stream` 正常。
- `build` 可选 extra(pyinstaller);docs/RUN.md 三种上手方式 + README 置顶指引;版本 0.12.0。

**全项目排查(3 个并行子 agent 扫全库)→ 修 3 处默认档静默退化 + 1 处文档不符:**
- M8:`autonomy=tools`(默认)/swarm/supervisor 此前**不记检索事件**(`set_retrieval_logger`
  只在 MemoryAgent 上),导致 `/feedback` + 代谢管线在默认档静默失效。现 ToolAgent/SwarmAgent
  也记录 RetrievalEvent。
- M29:`TracedLLM` 此前只埋点 `chat()`,默认工具循环走的 `chat_tools()`、流式走的 `chat_stream()`
  没 span,执行树缺"生成"节点。现两者都埋点。
- `memory-agent start` 补 `--host/--port`(docs/RUN.md 宣传过但子命令没受理);删一处死代码。
- 安全不变量(审批 deny 优先 / 注入防御 / 第三方工具不可自升级 / provenance 剥除 / 令牌预算原子性
  / 无密钥入库 / 启动器默认 127.0.0.1)与一致性(版本/文档/config/红线/七类插件/各档测试)全部复核通过。

## 0.11.0 — 作用域授权令牌 + 来源可信闸(M30)

借鉴 B2B 多 agent 白皮书里我们**尚缺**的两块治理(身份/审批/审计/支付笼子等已有):

- **① Delegation Token(`core/delegation.py`)**:给一次自主运行套"临时工牌"——`permissions`
  (许可动作 fnmatch)/`max_budget_usd`(累计花费上限)/`valid_until`(时效)/`transferable`
  (不可转授权)。由 agent 身份 **Ed25519 签名**(复用 M5,不引新依赖),`verify()` 校验签名+时效。
  审批闸强制其作用域,且**先于 level_override**——安全工具(auto)也绕不过过期/越权/超支的
  令牌(与"显式 deny 盖过 auto"同一不变量)。默认关(`delegation.enabled`)。
- **② 来源可信闸(provenance)**:`approval.require_verified_source` 列出的动作必须依据可信
  来源数据(`params._source ∈ trusted_sources`),否则 deny——**LLM 臆造的值不得触发改动性
  动作**。关键:agent 在进闸前**剥除 LLM 自称的 `_source`**(`sanitize_tool_args`),故模型无法
  自证来源;pure-LLM 循环里受限动作 fail-closed,须可信上游注入 `_source` 才放行。
- 安全加固(对抗式审计 6 项 → 已修/已记):预算改**原子预留+失败退款**(杜绝并发 TOCTOU
  超支);负额直接拒(修"负数充值预算");金额兼容字符串(修 fail-open);confirm 批准后
  **重校验时效**(不用陈旧授权执行);verify 自证与 spent 非持久化两项边界已在 docstring 载明。
- `services` 启用时签发令牌并挂到审批闸;`doctor` 预检 permissions/provenance;config.yaml 示例。

## 0.10.0 — 编排层可观测性(M29:agent 执行树进 Langfuse)

- **`TracedAgent`**:把 agent 编排层也接入 M20 的 OTel/Langfuse。此前只追踪 LLM/嵌入/记忆
  调用;现在 `run`/`chat`/`chat_stream` 各开一个 agent span,内部那些调用(已各自埋点)自动
  挂到它下面形成**完整执行树**。chat_stream 把 M28 的 step 事件(工具/转交/委派)记为 span
  **event**(带时间戳的编排时间线;避开 OTel 在异步生成器里跨 yield 传播 current-span 的
  脆弱性)。span 带 agent.type / session / prompt·completion 摘要 / step_count。
- 装配同款红线:`instrument_agent` 在 services 组装处包裹;`observability.enabled=false` 原样
  返回(零依赖零开销),enabled=true 且缺 extra 则 fail-fast。core 零改动。
- 验证:FakeTracer 单测(不依赖 OTel)+ 真实 OpenTelemetry 端到端(InMemorySpanExporter
  确认 span 名/属性/step 事件)。

## 0.9.0 — 流式进度事件(M28:工具/多 agent 的实时步骤)

- **`step` 进度事件**:此前只有 chat 档真流式,工具/swarm/supervisor 档只在结束吐一坨。现在
  `/chat/stream` 在这些档也逐步吐 `data: {type:"step", kind:"tool"|"handoff"|"delegate",
  name, status:"start"|"done", …}`——客户端能实时看到"在调用 recall""intake→tech""委派
  writer",而非干等。浏览器 UI 与 `memory-agent chat` CLI 都渲染这些步骤。
- **实现即重构、无重复循环**:`ToolAgent`/`SwarmAgent` 的循环抽成唯一的 `_stream` 生成器,
  `run()`(返回 ChatResponse)与 `chat_stream()`(吐 SSE 事件)都消费它——单一真相源。
  supervisor(本身是 ToolAgent)委派显示 `delegate` 步骤;worker 内部步骤不外泄(隔离)。
- 对抗式审计确认重构**行为等价**:run() 结果、记忆写入次数/时机(含 write_back)、loop_capped
  收尾、转录合法性、handoff 仅在真正切换时发事件——全部与重构前逐行一致。

## 0.8.0 — 开箱即用(M27:CLI 流式 + 场景模板)

- **`memory-agent chat` CLI 改走流式**(`/chat/stream`),逐 token 打印;工具/多 agent 档整段
  一次性(同一 SSE 通道)。至此流式覆盖 HTTP / 浏览器 UI / 终端三个界面。
- **现成场景模板**(`examples/`,用 `MEMORY_AGENT_CONFIG` 指向即用,只覆盖差异键):
  `swarm-customer-service.yaml`(客服分流 swarm:接单→技术/财务→总结)、
  `supervisor-research-write.yaml`(研究+写作 supervisor:协调者委派 researcher/writer)。
  附 `examples/README.md` 用法。测试校验随附模板加载合法、能装配、doctor 结构项通过——
  绝不发坏模板。

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
