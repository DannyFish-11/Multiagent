"""单一配置源 config.yaml + 环境变量覆盖(pydantic-settings)。

覆盖规则:前缀 MEMORY_AGENT_,嵌套键用 __,如 MEMORY_AGENT_LLM__BASE_URL。
配置文件路径可用 MEMORY_AGENT_CONFIG 指定,默认取项目根 config.yaml。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HardwareSettings(BaseModel):
    tier: Literal["A", "B", "C", "unset"] = "unset"


class LLMEndpoint(BaseModel):
    """一个 OpenAI 兼容端点三元组(PHASE2.5 M-A)。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""


class LLMRoleSettings(LLMEndpoint):
    """一个 LLM 角色(chat / memory):主端点 + 备用端点 + 重试/切换参数。"""

    fallbacks: list[LLMEndpoint] = Field(default_factory=list)
    failover_threshold: int = 3   # 主端点连续失败 N 次后切换
    max_retries: int = 3          # 单端点指数退避重试上限
    retry_backoff_s: float = 0.5  # 退避基数(测试可置 0)
    timeout_s: float = 120.0


class LLMSettings(BaseModel):
    # mode=local:走 vLLM 本地路径(PHASE 1,保留一键切回)
    # mode=api  :走外部 OpenAI 兼容 API(PHASE2.5 M-A,chat/memory 双角色)
    mode: Literal["local", "api"] = "local"
    base_url: str = "http://localhost:8000/v1"
    model: str = "gemma-4"
    model_by_tier: dict[str, str] = Field(default_factory=dict)
    api_key: str = "EMPTY"
    timeout_s: float = 120.0
    chat: LLMRoleSettings = Field(default_factory=LLMRoleSettings)
    memory: LLMRoleSettings = Field(default_factory=LLMRoleSettings)


class BudgetSettings(BaseModel):
    """成本护栏(M-A CostLedger):单价表 + 日预算 + 账本路径。"""

    daily_usd: float = 5.0
    # 每百万 token 美元:{model: {input: x, output: y}}
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    ledger_path: str = "./data/cost_ledger.json"


class EmbedderSettings(BaseModel):
    backend: Literal["local", "remote", "jina_api", "fake"] = "local"
    model_name: str = "jinaai/jina-embeddings-v5-omni-small"
    model_by_tier: dict[str, str] = Field(default_factory=dict)
    dim: int = 1024
    matryoshka_dim: int | None = None
    base_url: str = "http://localhost:8001"
    device: str = "auto"
    jina_api_key: str | None = None
    # PHASE2.5 M-B:API 化参数(批量上限/重试/模态能力以 Jina 文档为准,经 config 可调)
    api_batch_size: int = 64
    api_max_retries: int = 3
    api_retry_backoff_s: float = 0.5
    api_supports_audio: bool = False  # API 版暂不支持的模态显式拒绝,不静默降级

    @property
    def effective_dim(self) -> int:
        return self.matryoshka_dim or self.dim


class VectorDBSettings(BaseModel):
    mode: Literal["memory", "local", "server"] = "server"
    url: str = "http://localhost:6333"
    path: str = "./data/qdrant"
    collection: str = "memories"


class ConsolidationSettings(BaseModel):
    similarity_threshold: float = 0.92


class SimpleMemSettings(BaseModel):
    data_dir: str = "./data/simplemem"
    gateway_base_url: str = "http://localhost:8001/v1"


class PromotionSettings(BaseModel):
    policy: Literal["manual", "grader"] = "manual"
    grader_threshold: float = 0.7


class MemorySettings(BaseModel):
    backend: Literal["qdrant", "simplemem"] = "qdrant"
    extraction: Literal["llm", "verbatim"] = "llm"
    consolidation: ConsolidationSettings = Field(default_factory=ConsolidationSettings)
    simplemem: SimpleMemSettings = Field(default_factory=SimpleMemSettings)
    # M5.3 私有/共享分区:共享池为独立 collection,多实例可挂同一池
    shared_collection: str = "memories_shared"
    promotion: PromotionSettings = Field(default_factory=PromotionSettings)


class ServiceSettings(BaseModel):
    embed_host: str = "0.0.0.0"
    embed_port: int = 8001
    api_host: str = "0.0.0.0"
    api_port: int = 8002


class AgentSettings(BaseModel):
    top_k: int = 5
    system_prompt: str = "你是一个拥有长期记忆的助手。"


class IdentitySettings(BaseModel):
    dir: str = "./data/identity"


class A2ASettings(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8003
    base_url: str = "http://localhost:8003"


class GmailSettings(BaseModel):
    enabled: bool = False
    # 现成 Gmail MCP server(评估结论见 README M6):不自研 OAuth
    mcp_command: str = "npx"
    mcp_args: list[str] = Field(default_factory=lambda: ["-y", "@gongrzhe/server-gmail-autoauth-mcp"])


class MetabolismSettings(BaseModel):
    events_path: str = "./logs/retrieval_events.jsonl"
    report_dir: str = "./reports"


# ---------------- PHASE 3 ----------------

class ConcurrencySettings(BaseModel):
    """M9.1 并发限额:会话数与 LLM 并发信号量(防 API 限流雪崩)。"""

    max_concurrent_sessions: int = 32
    max_concurrent_llm_calls: int = 8


class PolicyRule(BaseModel):
    """M9.2 声明式分级规则:动作类型 × 参数条件 → auto|confirm|deny。

    action 支持 fnmatch 通配(如 gmail_send*);when 为参数谓词,支持算子后缀:
    field / field__gte / field__lte / field__in / field__not_in / field__regex。
    规则自上而下首条命中生效。
    """

    action: str
    when: dict = Field(default_factory=dict)
    level: Literal["auto", "confirm", "deny"]
    reason: str = ""


class ApprovalSettings(BaseModel):
    """M9.2 审批中枢。"""

    timeout_s: float = 300.0            # confirm 超时未批 → 取消
    audit_path: str = "./logs/audit.jsonl"
    notify: Literal["none", "webhook", "email"] = "none"
    webhook_url: str = ""
    default_level: Literal["auto", "confirm", "deny"] = "confirm"  # 无规则命中时保守 confirm
    policies: list[PolicyRule] = Field(default_factory=list)


class WebSettings(BaseModel):
    """M10 上网:搜索 API(供应商由人类选定,停点要 key)+ 抓取 + 域名黑白名单。"""

    search_provider: Literal["tavily", "serper", "none"] = "none"
    search_api_key: str = ""
    search_base_url: str = ""           # 留空用 provider 默认
    fetch_timeout_s: float = 30.0
    fetch_max_bytes: int = 2_000_000
    domain_blacklist: list[str] = Field(default_factory=list)
    domain_whitelist: list[str] = Field(default_factory=list)  # 非空则白名单模式


class GmailPollSettings(BaseModel):
    """M11 收件驱动(默认关):按标签轮询,阅读→按需入记忆→仅草拟回复。"""

    enabled: bool = False
    interval_s: float = 30.0
    label: str = "agent-inbox"


class PaymentsSettings(BaseModel):
    """M12 支付解锁 + 硬性笼子(缺一不得上线;enabled=false 时维持附录 A 拒付)。"""

    enabled: bool = False
    provider: Literal["virtual_card", "x402", "none"] = "none"
    provider_base_url: str = ""
    provider_api_key: str = ""
    per_tx_usd: float = 10.0            # 单笔上限
    daily_usd: float = 20.0             # 日累计上限
    monthly_usd: float = 100.0          # 月累计上限
    confirm_threshold_usd: float = 1.0  # 达阈值必须 confirm;低于则 auto+事后通知
    whitelist_enabled: bool = False
    payee_whitelist: list[str] = Field(default_factory=list)
    ledger_path: str = "./data/payments_ledger.json"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORY_AGENT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    vectordb: VectorDBSettings = Field(default_factory=VectorDBSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    a2a: A2ASettings = Field(default_factory=A2ASettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    metabolism: MetabolismSettings = Field(default_factory=MetabolismSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    gmail_poll: GmailPollSettings = Field(default_factory=GmailPollSettings)
    payments: PaymentsSettings = Field(default_factory=PaymentsSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_path = Path(os.environ.get("MEMORY_AGENT_CONFIG", PROJECT_ROOT / "config.yaml"))
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if yaml_path.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        return tuple(sources)


def load_config(**overrides) -> AppConfig:
    return AppConfig(**overrides)
