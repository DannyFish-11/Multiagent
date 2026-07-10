# M19 世代演化实验:遗传是否抬升群体

> ⚠️ **冒烟运行**:小种群 × 少世代,验证血统树/世代曲线可复算,**不构成结论**。
> 满配需 M18 全部安全带就位 + 人类授权预算后上云跑。

## 世代平均成功率曲线

- **evolve**:[0.0, 0.0, 0.0]
- **control**:[0.0, 0.0, 0.0]
- **shuffle**:[0.0, 0.0, 0.0]

## 遗传记忆引用率(转世者真在用继承经验吗)

- **evolve**:[0.0, 0.0, 0.0]
- **control**:[0.0, 0.0, 0.0]
- **shuffle**:[0.0, 0.0, 0.0]

## 多样性(警惕早熟收敛)

- **evolve**:[1.0, 0.8667, 0.8667]
- **control**:[1.0, 1.0, 1.0]
- **shuffle**:[1.0, 0.9333, 0.8667]

## 血统树(末世代始祖存活)

- **evolve**:{'evolve-g0-i0': 2, 'evolve-g0-i1': 2, 'evolve-g0-i2': 1, 'evolve-g0-i3': 1}
- **control**:{'control-g0-i0': 1, 'control-g0-i1': 1, 'control-g0-i2': 1, 'control-g0-i3': 1, 'control-g0-i4': 1, 'control-g0-i5': 1}
- **shuffle**:{'shuffle-g0-i0': 2, 'shuffle-g0-i1': 1, 'shuffle-g0-i2': 2, 'shuffle-g0-i3': 1}

## 诚实红线
- evolve 若不优于 control → 遗传无效,如实报告;
- evolve vs shuffle 判别'遗传有效'与'仅淘汰坏运气':shuffle 继承随机源,
  若 evolve ≈ shuffle 则优势来自淘汰而非遗传内容。
