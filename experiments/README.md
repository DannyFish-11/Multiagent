# 实验(PHASE 4)

## M14 基础设施(已完成)

- 循环硬上限:`core/loop_guard.py`(config.loops,触顶记 loop_capped)
- 委托上下文预算:`A2AClientAdapter(context_budget_tokens=...)`,LLM 摘要压缩(实验变量)
- 记账维度:`CostLedger.record(..., experiment_id/task_id/agent_id/purpose)` + 按实验独立预算
- 运行器:`core/experiment.py::ExperimentRunner`;任务源 synthetic / replay(脱敏)
- 冒烟:`experiments/smoke.yaml`(3 实例 / 50 任务 / $2 预算),离线用 fake 后端可跑通,
  出 CSV + 快照 + 元数据指纹;烧穿预算暂停可续跑;同 seed 注入序列逐位一致

一键(目标机器,配好 key 后):
    python -m core.experiment_run experiments/smoke.yaml   # 见下方 TODO

## M13 群体资产库(已完成)

`core/commons.py`:统一信封 CommonsEnvelope(签名+hash 校验)、三种货物
(memory/skill tar+SKILL.md/mcp_entry)、三级准入(GraderAdmission/SandboxAdmission/
HumanAdmission)、采用拉取制 + 举报降级 + revoke 传播、metrics 端点原语。
三条硬负向测试全过(mcp 不可绕过人工、skill 未过沙箱不可采用、revoke 传播移除)。

## M15 治理对照:管道已跑通(冒烟),满配待授权

`core/m15_governance.py` 三臂(grader/vote/natural)共用测试条目流(良品+四类坏品带真值),
指标:坏品拦截率(按四类)/良品误杀率/裁决延迟/成本。**冒烟已跑通并产出
`reports/m15_governance.md`**(确定性替身打分,零花费,报告醒目标注"不构成结论")。
→ 满配跑数(≥3 seeds × 3 臂、真实模型)需人类授权预算 + 模型档位 + key。

## M16 分工涌现(人类停点 —— 未开跑)

M15(治理对照:grader vs vote vs 自然筛选)与 M16(分工涌现)的**机制已就位**:
- VotePolicy:`core/promotion.py::VotePolicy`(simple_majority/supermajority/weighted,全量审计)
- commons metrics 原语:`core/commons_metrics.py`(引用/举报/降级 → C 臂自然筛选依据)

但实验本体**未运行**,原因(需人类决策):
1. 规格明写"M15 设计定案是人类停点"——测试条目集构成(良品 + 四类坏品的真值标签)、
   三臂/两臂参数、seed 数需人类签字。
2. 首次"实验烧钱":满配 3 臂 × 3 seeds 预计数十万~百万 token,须人类声明预算 + 选模型档位 + 给 key。
3. M13 前置有缺口:群体资产库的"统一信封/三级准入/metrics 原语"此前只交付了共享池 +
   两级策略 + 签名信封;本轮补齐了 VotePolicy(第三级)与 metrics 原语,但完整 M13
   commons 模块(统一信封 schema、准入编排)未见独立规格,未擅自补全。

→ 待人类:确认 M13 补齐范围、定案 M15 实验设计、授权预算与模型档位,再开跑并出
  reports/m15_governance.md / reports/m16_specialization.md。
