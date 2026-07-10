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
