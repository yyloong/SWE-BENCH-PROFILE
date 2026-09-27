# 核心效率指标：Qwen 与独立 Codex subagent

同一组固定十题，每组各十次求解；保留全部完成样本，包括未通过的题。耗时及阶段占比均为逐题等权平均；工具并行上界的减少比例单独按均值之比计算。排除镜像准备、环境初始化、补丁导出和官方评分。

Qwen 两组都使用相同的通用 subagent 鼓励词，差别是是否增加执行路径提示。新增列为此前执行这十题的 Codex subagent（gpt-6-astra / xhigh）；求解和工具时间为实测，标 * 的 decode 指标按同题 Qwen 无路径提示组 TPS 换算。

| 指标 | Qwen：无路径提示 | Qwen：有路径提示 | Codex subagent：实测 / *估算 |
|---|---:|---:|---:|
| 通过率 | 90.00% | 80.00% | 90.00% |
| 平均求解时间（秒/题） | 1,106.62 | 846.34 | 267.42 |
| 平均 Decode TPS（token/s） | 67.10 | 69.78 | 67.10 *（借用基线） |
| Decode | 762.31 秒 / 66.38% | 596.70 秒 / 72.71% | 102.28 秒 * / N/A |
| Decode：reasoning | 615.37 秒 / 53.59% | 473.01 秒 / 57.78% | N/A |
| Decode：非 reasoning | 146.94 秒 / 12.79% | 123.69 秒 / 14.94% | N/A |
| Prefill | 54.14 秒 / 3.97% | 27.06 秒 / 3.32% | N/A |
| 工具执行 | 229.60 秒 / 22.26% | 177.60 秒 / 19.16% | 80.71 秒 / 28.63% † |
| 工具：普通工具 | 184.38 秒 / 18.23% | 171.19 秒 / 18.61% | 80.71 秒 / 28.63% † |
| 工具：subagent | 45.22 秒 / 4.03% | 6.41 秒 / 0.55% | 0.00 秒 / 0.00% |
| 其余（含跨类重叠） | 60.56 秒 / 7.38% | 44.98 秒 / 4.81% | N/A |
| 非普通工具区间（尚未拆分，仅 Codex） | — | — | 186.70 秒 / 71.37% |

各阶段展示「平均秒/题 / 平均占比」。通过率分母为本列纳入的十题，三组都已完成有效官方评分。百分比先逐题计算再平均，不是平均秒数之比；父行已包含子行。

## 工具并行优化的理想上界

三组使用相同的工具活跃区间口径，包含 subagent；时间只展示平均秒/题。Qwen 的主级普通工具各为一项，同一 subagent 的包装调用与完整观测生命周期取并集合为一项，嵌套子会话折叠到顶层 subagent。Codex solver 没有继续委派，使用其内部普通工具调用。

| 指标 | Qwen：无路径提示 | Qwen：有路径提示 | Codex subagent |
|---|---:|---:|---:|
| 当前工具活跃时间（并集） | 249.27 | 193.68 | 80.71 |
| 理想工具时间下界（最长调用） | 107.48 | 70.98 | 72.88 |
| 最多可减少工具时间 | 141.79 | 122.69 | 7.83 |
| 最多可减少比例（减少均值 / 当前并集均值） | 56.88% | 63.35% | 9.70% |

```text
每题 U = 所有主级工具活跃区间的并集长度（含完整观测 subagent）
每题 L = 最长单项工具的观测时长
平均最多可减少时间 = mean(U − L)
最多可减少比例 = mean(U − L) / mean(U) × 100%
```

比例的分母是本列当前工具并集的平均时间，**不是完整求解时间，也不是各题减少百分比的平均**。Codex 的 9.70% = 7.83 / 80.71（实际计算使用未舍入值）。三列 U 都保留工具与模型的重叠，所以 Qwen 的 249.27 / 193.68 秒与上表互斥分区中的 229.60 / 177.60 秒不同。

这是固定现有调用及其观测耗时、忽略依赖和资源限制、假设其他工作都能与最长调用重叠的乐观估计。L 是时间下界，U−L 是仅调整重叠安排的收益上界；读/改/测依赖通常使可达收益更小。不能据此承诺 Qwen 会减少 56.88% / 63.35% 的工具时间，也不能把这些比例当作完整求解时间降幅。改写命令、缩短等待、复用构建或改变验证范围后不再受此估计约束。

最长调用还受工具接口粒度影响：Codex 可以在单次 Bash 调用中组合读、改、测试，OpenCode 的调用往往拆得更细。若允许拆分这些调用，L 也会改变，因此三组上界差异不能单独证明哪一组更接近全局最优。

区间裁剪到求解起止；同一子任务内先去除重叠，不补充未观测到的包装/生命周期间隙。被中止调用只代表已观测部分。Qwen 数值由 `key_metrics.py` 生成并保存在 [key-metrics.json](key-metrics.json) 的 `tool_parallel_bound`；Codex 数值使用同一计算函数，从十题 `summary.json` 的 `calls[].start/end` 复算，其工具并集与原报告一致。

## Codex decode 的 Qwen 基线估算

按题目 ID 一一配对，使用本轮 `arms/baseline` 的主级模型 decode TPS，与精简报告相同；没有使用更早历史批次的 TPS，也没有统一除以 67.10 这个十题平均值。

```text
每题 Qwen TPS = Σ(Qwen 输出 token − 1) / Σ(Qwen 服务端 decode 秒数)
每题 Codex 估计 decode 秒数 = Σ max(Codex 每请求输出 token − 1, 0) / 同题 Qwen TPS
表内平均值 = 十题估计 decode 秒数的算术平均 = 102.28 秒/题
```

输出 token 包含 reasoning；每请求扣掉首 token，以保持 TPS 分子与 decode 定义一致。保留 Codex 的实际输出工作量，仅假设其生成速度等于同题 Qwen 基线。原始计算和每题参照值见 [codex-qwen-baseline-decode-estimate.json](codex-qwen-baseline-decode-estimate.json)。

* 这是**条件估计的请求 decode 时长之和**，并非 Codex 实测 decode，也不等于 Qwen 真实重跑 Codex 路径后的耗时。两个模型原生 tokenizer 的 token 数近似视为可比，没有重建与工具的重叠，不能直接转换为互斥墙钟占比，所以占比仍标 N/A。
* 平均求解时间 267.42 秒仍为 Codex 实测值，不会因为补上 decode 估算而替换或相加；reasoning/非 reasoning 的时长和 prefill 暂未估算。

## 计时与可比性

- **† Codex 工具值是实测调用区间的并集，包含可能与模型重叠的时间；Qwen 表中是排除跨类重叠后的独占时间。**Codex 记录缺少对等的模型阶段区间，不能严格构造同口径的模型/工具互斥分区。因此标记值不能直接用于声称同口径的独占工具时间降幅。
- Codex 保存了完整求解时间、工具起止及 token usage，但没有与当前 vLLM 对等的逐请求服务端 decode/prefill 时长。decode 总时长按上述方法估算，尚未估计或无法测得的项目仍标为 N/A；`求解时间 − 工具并集` 包含模型生成、prefill、API 等待、编排等，不能称为 decode。reasoning token 数也不能直接换算成实测 reasoning 秒数。
- 这里把每个独立 Codex subagent 当作被测 solver，统计其内部执行过程。它们没有继续委派，因此工具细分中的 subagent 为零。若改从外层父 agent 看，整个 solver 都是一次子任务；不能再把完整生命周期与内部工具时间相加。
- Qwen 的模型指标统计非子会话请求，包含 title/compaction 等辅助请求；子会话整个观测生命周期归入工具。不同细分类并发交集归入其余。服务端阶段位置和 reasoning 分界仍为日志对齐估算，详见 [精简报告](key-metrics.md) 与 [计时定义](../../KEY_METRICS.md)。
- 三组模型、harness、工具接口及自行验证范围不完全相同；这是同题的观察性比较。Codex 被要求不再委派，Qwen 允许委派；不能把差异全部归因于 prompt 或并行策略。

## 数据来源

- Qwen 平均值与通过率：[key-metrics.json](key-metrics.json)。两列数值与当前精简报告一致。
- 十题与 Codex 记录对应关系：[manifest.json](../manifest.json)，已逐题核对 instance_id。
- Codex 来源为该清单每题 `path` 下的 `summary.json`、`meta.json` 和工具调用记录；第 01 题来自 `subagent-speed-20260917/typical/`，第 02–10 题来自 `subagent-speed-20260917/batch10/tasks/`。
- Codex 原始实验报告：[batch10/REPORT.md](../../subagent-speed-20260917/batch10/REPORT.md)；验证范围差异：[COVERAGE_NOTES.md](../../subagent-speed-20260917/batch10/COVERAGE_NOTES.md)。

Codex 平均求解时间由每题 `actual_runtime_seconds` 求均值；工具平均值由 `internal_tool_union_seconds` 求均值；平均工具占比为 `mean(internal_tool_union_seconds / actual_runtime_seconds)`。工具并集已从原始调用区间复算，求解时间已与记录的 `completion.duration_ms` 核对。
