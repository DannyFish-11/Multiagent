# Changelog

本项目遵循分阶段交付。以下为面向"完整、稳定、易用、可交付"的近期迭代。

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
