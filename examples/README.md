# 场景模板(开箱即用)

现成的多 agent 配置,拿来即用——用 `MEMORY_AGENT_CONFIG` 指向即可,只覆盖与默认不同的键,
其余沿用根目录 `config.yaml`。都需 function-calling 模型(`llm.mode=api` 或 `litellm`),
供应商 `base_url/model/key` 经 `.env` 注入(密钥绝不入库)。

| 模板 | 模式 | 说明 |
|---|---|---|
| `swarm-customer-service.yaml` | swarm(M24) | 客服分流:接单 → 技术/财务专员 → 总结,成员手递手 |
| `supervisor-research-write.yaml` | supervisor(M25) | 研究+写作:协调者委派 researcher/writer 并汇总 |

## 用法

```bash
# 1) 先配好 .env(供应商 base_url / model / key)
cp .env.example .env   # 然后填入;或用 `uv run memory-agent setup`

# 2) 体检(校验成员/worker 配置)
MEMORY_AGENT_CONFIG=examples/swarm-customer-service.yaml uv run memory-agent doctor

# 3) 起服务
MEMORY_AGENT_CONFIG=examples/swarm-customer-service.yaml uv run memory-agent run
# 另开一个终端对话(流式):
uv run memory-agent chat
```

改造:直接编辑成员的 `prompt` / `tools` / `handoffs`(swarm)或 `workers`(supervisor)。
可用工具名见 `docs/PLUGINS.md`;swarm 与 supervisor 的选型指引也在那里。
