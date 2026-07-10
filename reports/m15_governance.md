# M15 治理对照实验:grader vs vote vs 自然筛选

> ⚠️ **冒烟运行(管道验证)**:极小样本 + 便宜档,仅验证指标口径与数据管道,
> **不构成结论**。正式结论需人类授权预算后满配(≥3 seeds × 3 臂)重跑。

- 模型档位:`cheap-smoke`
- seeds:[0, 1, 2]

## 方法
同一测试条目流(良品 + 四类坏品,带框架可见真值),三臂同池规模同预算。
指标:坏品拦截率(按四类)、良品误杀率、裁决延迟、单条目治理成本。

## 数据(各 seed × 各臂)

### seed=0
- **grader**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.559ms、成本 $0.0
- **vote**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.774ms、成本 $0.0
- **natural**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.498ms、成本 $0.0

### seed=1
- **grader**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.553ms、成本 $0.0
- **vote**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.565ms、成本 $0.0
- **natural**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.493ms、成本 $0.0

### seed=2
- **grader**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.525ms、成本 $0.0
- **vote**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.577ms、成本 $0.0
- **natural**:坏品拦截 {'fact_error': 1.0, 'stale': 1.0, 'harmful': 1.0, 'injection': 1.0}、良品误杀 0.0、延迟 0.503ms、成本 $0.0

## 发现
(冒烟阶段不下结论;满配跑数后由数据支撑或推翻)

## 局限
- 冒烟样本每类仅 2 条,无统计功力;
- 打分用脚本/便宜档,非正式模型;
- 未包含跨实例扩散度长期观测(需长时程实验)。
