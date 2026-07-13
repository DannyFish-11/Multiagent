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
    # M26 流式:是否请求 stream_options.include_usage(供成本记账)。默认关——部分兼容
    # 网关不认此字段会 400 打断整条流;仅在确认供应商支持时开启。
    stream_usage: bool = False


class LLMSettings(BaseModel):
    # mode=local:走 vLLM 本地路径(PHASE 1,保留一键切回)
    # mode=api  :走外部 OpenAI 兼容 API(PHASE2.5 M-A,chat/memory 双角色)
    # mode=echo :离线 demo 档(M20 A1),零 key 回显"检索到的记忆+问题",
    #            仅验证记忆存取闭环,不产生真实推理(配 embedder=fake + vectordb=memory)
    # mode=litellm:经 LiteLLM 调 100+ 家(M21;需 --extra litellm;model 用 provider/model 写法)
    # 值为插件名(内置 local/api/echo/litellm;第三方插件名亦可,由注册表校验)
    mode: str = "local"
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
    # 插件名(内置 local/remote/jina_api/fake;第三方插件名亦可,由注册表校验)
    backend: str = "local"
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
    # 插件名(内置 qdrant/simplemem;第三方插件名亦可,由注册表校验)
    backend: str = "qdrant"
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
    # M22:tools = 自主工具循环(默认开;需 function-calling 模型 api/litellm,否则自动
    # 回落记忆问答);chat = 纯记忆增强问答。默认工具集只含安全的 recall/remember
    # (自动放行);危险工具(上网/付款等)需显式加入 tools 且按 config 审批分级。
    # M22 tools;M24 swarm = 去中心化多成员手递手;M25 supervisor = 中心调度委派 worker
    # (swarm 需 swarm.members、supervisor 需 supervisor.workers;非 fc 模型均安全回落)
    autonomy: str = "tools"      # chat | tools | swarm | supervisor
    tools: list[str] = Field(default_factory=lambda: ["recall", "remember"])
    # M23 Harness Profile:按模型打包的脚手架(系统提示/采样/工具循环参数),让开源模型
    # 发挥真实水平。auto = 按 chat 模型名自动选内置 profile(匹配不到回落 default,零侵入);
    # 也可填具体名(glm/deepseek/kimi/qwen/gemma/…,make plugins 看 profile 类)或 none。
    profile: str = "auto"


class SwarmMemberSettings(BaseModel):
    """swarm 一个成员 = 名字 + 人设 prompt + 私有工具 + 可转交给谁。"""
    name: str
    prompt: str = ""
    tools: list[str] = Field(default_factory=list)     # 该成员自己的工具名(空=无)
    handoffs: list[str] = Field(default_factory=list)  # 可转交的其他成员名(空=终点)


class SwarmSettings(BaseModel):
    """M24 去中心化多 agent:成员之间手递手传任务,无中央调度器(蜂群式)。
    默认空 → autonomy=swarm 未配成员时安全回落。"""
    entry: str = ""                                    # 起始 active 成员名
    members: list[SwarmMemberSettings] = Field(default_factory=list)


class SupervisorWorkerSettings(BaseModel):
    """supervisor 的一个 worker = 名字 + 人设 prompt + 私有工具。"""
    name: str
    prompt: str = ""
    tools: list[str] = Field(default_factory=list)


class SupervisorSettings(BaseModel):
    """M25 中心调度多 agent:协调者拆解任务、委派给 worker、汇总结果(与 swarm 互补)。
    控制权始终在协调者;worker 只返回结果不接管。默认空 → autonomy=supervisor 未配时回落。"""
    prompt: str = ""                                   # 协调者人设(空=用内置默认)
    workers: list[SupervisorWorkerSettings] = Field(default_factory=list)


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
    # M30 ② Provenance:这些动作(fnmatch)必须依据可信来源数据,否则 deny——LLM 臆造的
    # 值(_source=llm_output 或缺失)不得触发改动性动作。params["_source"] ∈ trusted_sources 才放行。
    require_verified_source: list[str] = Field(default_factory=list)
    trusted_sources: list[str] = Field(
        default_factory=lambda: ["erp_verified", "verified", "system", "user"])


class InjectionSettings(BaseModel):
    """M33 提示词注入检测:主动扫描进入模型的不可信内容(网页正文等)里的注入特征。

    启发式确定性检测默认开、零成本;LLM 二次分类默认关。on_detect 只对 malicious 生效
    (suspicious 一律仅加告警横幅,不拦,避免误伤);block 是人类显式选择的硬拦策略。"""
    enabled: bool = True
    llm_classifier: bool = False        # LLM 二次分类(启发式判 clean 时兜底):需 LLM、有成本
    on_detect: Literal["annotate", "redact", "block"] = "annotate"   # 对 malicious 的处置
    suspicious_score: int = 3           # 判 suspicious 的权重阈值
    malicious_score: int = 6            # 判 malicious 的权重阈值


class SimulationSettings(BaseModel):
    """M32 Gecko-lite 预执行模拟:危险动作真实执行前先做参数校验 + 效果预览。

    确定性、零成本的环节(规则层校验 + 工具自报/schema 摘要预览)默认开;要花钱、可能
    "自信地错"的 LLM 环节(语义校验 / LLM 效果估计)默认关,显式开启才用。"""
    enabled: bool = True                    # 规则层参数校验 + 效果预览(确定性,默认开)
    semantic_validation: bool = False       # LLM 语义参数校验(schema 合法但语义不对):需 LLM
    llm_preview: bool = False               # 工具无自报预览时用 LLM 估计副作用:需 LLM,标"估计"
    preview_max_chars: int = 500            # 预览文本上限


class DelegationSettings(BaseModel):
    """M30 ① 作用域授权令牌:给自主运行套"临时工牌"。默认关(需显式签发);
    开启后由 agent 身份签发一张令牌,审批闸强制其 permissions/预算/时效(治理刚性)。"""
    enabled: bool = False
    task: str = ""
    permissions: list[str] = Field(default_factory=list)   # 许可动作 fnmatch(空=什么都不许)
    max_budget_usd: float = 0.0                             # 0=不限
    ttl_s: float = 0.0                                     # 0=不过期
    transferable: bool = False


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


class LoopSettings(BaseModel):
    """M14.1 循环硬上限:全局默认 + 按点覆盖(触顶记 loop_capped 事件,非静默停)。"""

    default_max_iterations: int = 8
    per_point: dict[str, int] = Field(default_factory=dict)  # {"vote_rounds": 3, "delegation_chain": 5, ...}

    def limit(self, point: str) -> int:
        return int(self.per_point.get(point, self.default_max_iterations))


class VoteSettings(BaseModel):
    """M15.1 VotePolicy:准入投票裁决规则。"""

    rule: Literal["simple_majority", "supermajority", "weighted"] = "simple_majority"
    supermajority_ratio: float = 0.66
    max_rounds: int = 3          # 受 loops 约束


class ExperimentSettings(BaseModel):
    """M14.2 实验运行器默认值。"""

    dir: str = "./experiments"
    snapshot_dir: str = "./experiments/snapshots"


class CloudSettings(BaseModel):
    """M17 云端底座(供应商与机型由人类选定,停点)。"""

    # 插件名(内置 local/generic_rest/ray/none;第三方插件名亦可,由注册表校验)
    provider: str = "local"
    base_url: str = ""             # generic_rest:API 前缀;ray:集群地址(留空=本机多核)
    api_key: str = ""
    machine_type: str = ""
    region: str = ""
    image_id: str = ""
    upload_base_url: str = ""       # 对象存储数据包上传前缀


class ConductorSettings(BaseModel):
    """M17/M18 队列与熔断。"""

    state_path: str = "./data/conductor_state.json"
    max_concurrent_vms: int = 2
    breaker_state_path: str = "./data/global_breaker.json"
    global_daily_usd: float = 50.0
    global_monthly_usd: float = 500.0
    single_experiment_max_ratio: float = 0.5
    pause_wait_timeout_s: float = 86400.0


class ObservabilitySettings(BaseModel):
    """M20 B:可观测性(Langfuse via OpenTelemetry)。默认关 → 零依赖零开销。

    enabled=false 时:不导入任何 OTel/Langfuse 依赖、不装配 exporter、adapter 埋点
    直通(instrument_* 原样返回),保持"克隆即测试全绿"。
    enabled=true 时:adapter 层出入口发 OTLP span 到 Langfuse(model/token/耗时/
    检索命中/工具调用 + session/experiment/agent 维度标签)。密钥经 env 注入。
    """

    enabled: bool = False
    # Langfuse OTLP 接收端点(自托管:http://localhost:3000/api/public/otel/v1/traces)
    otlp_endpoint: str = "http://localhost:3000/api/public/otel/v1/traces"
    public_key: str = ""            # Langfuse project public key(pk-...)
    secret_key: str = ""            # Langfuse project secret key(sk-...);仅经 .env 注入
    service_name: str = "memory-agent"
    # 输入/输出摘要在 span 上的最大字符数(避免把长 prompt/正文整段外发)
    io_summary_max_chars: int = 512


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMORY_AGENT_",
        env_nested_delimiter="__",
        env_file=".env",           # 本地运行(make run-api)自动读 .env(setup 向导写入)
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    vectordb: VectorDBSettings = Field(default_factory=VectorDBSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    swarm: SwarmSettings = Field(default_factory=SwarmSettings)
    supervisor: SupervisorSettings = Field(default_factory=SupervisorSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    a2a: A2ASettings = Field(default_factory=A2ASettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    metabolism: MetabolismSettings = Field(default_factory=MetabolismSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    injection: InjectionSettings = Field(default_factory=InjectionSettings)
    delegation: DelegationSettings = Field(default_factory=DelegationSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    gmail_poll: GmailPollSettings = Field(default_factory=GmailPollSettings)
    payments: PaymentsSettings = Field(default_factory=PaymentsSettings)
    loops: LoopSettings = Field(default_factory=LoopSettings)
    vote: VoteSettings = Field(default_factory=VoteSettings)
    experiment: ExperimentSettings = Field(default_factory=ExperimentSettings)
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    conductor: ConductorSettings = Field(default_factory=ConductorSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

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
        # 优先级:init > 进程环境变量 > .env > config.yaml(密钥经 .env 注入即生效)
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if yaml_path.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path))
        return tuple(sources)


def load_config(**overrides) -> AppConfig:
    return AppConfig(**overrides)
