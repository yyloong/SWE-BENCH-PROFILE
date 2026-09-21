#!/usr/bin/env python3
"""Small, offline report of per-task MEAN efficiency metrics (Python 3.9+).

Inputs: campaign, runs, single task, or paired campaign with arms/*/runs.
Requires result.json, requests.json, tools.json and timing.json for each task.
This project's pinned vLLM exports scheduled->first-token (prefill latency) as
metrics.time_to_first_token_ms. Client ttft_wall_seconds is NOT prefill.
Other exporters should use prefill_time_ms, or --prefill-field prefill_time_ms.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import sys

LEAVES = ('reasoning', 'non_reasoning', 'prefill', 'ordinary_tool', 'subagent', 'other')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load(path):
    return json.loads(path.read_text())


def mean(values):
    return statistics.mean(values) if values else None


def span(record, start_key, end_key, end):
    a, b = record.get(start_key), record.get(end_key)
    b = end if b is None else b
    if not number(a) or not number(b) or b < a:
        raise ValueError('Invalid interval: ' + str(record.get('request_id', record.get('call_id', ''))))
    return a, b


def exclusive_partition(intervals, start, end):
    """Same-label concurrency is unioned; cross-label overlaps go to other."""
    events = defaultdict(Counter)
    for a, b, label in intervals:
        a, b = max(a, start), min(b, end)
        if b > a:
            events[a][label] += 1
            events[b][label] -= 1
    active, totals = Counter(), Counter({k: 0.0 for k in LEAVES})
    previous, overlap = start, 0.0
    for at in sorted(set(events) | {end}):
        labels = {k for k, count in active.items() if count > 0}
        label = next(iter(labels)) if len(labels) == 1 else 'other'
        totals[label] += at - previous
        if len(labels) > 1:
            overlap += at - previous
        active.update(events[at])
        previous = at
    assert math.isclose(sum(totals.values()), end-start, abs_tol=1e-5)
    return dict(totals), overlap


def merged_spans(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def tool_parallel_bound(jobs, start, end):
    """Optimistic scheduling bound with fixed observed, opaque job durations.

    Each job is a list of intervals: a child's wrapper and observed lifespan
    belong to one job. Union within that job first; unobserved gaps are not
    invented. Model overlaps are retained, unlike the exclusive phase table.
    """
    jobs = [merged_spans((max(a, start), min(b, end)) for a, b in job) for job in jobs]
    durations = [sum(b-a for a, b in job) for job in jobs]
    current = sum(b-a for a, b in merged_spans(interval for job in jobs for interval in job))
    lower = max(durations, default=0.0)
    # Floating point sums may differ by a few ulps for disjoint child spans.
    assert lower <= current + 1e-5
    lower = min(lower, current)
    return dict(tool_union_seconds=current, ideal_lower_bound_seconds=lower,
                maximum_reduction_seconds=current-lower)


def parent_tool_jobs(tools, child_spans, children, sessions, end):
    """Ordinary parent calls plus opaque top-level children, including wrappers."""
    parents = {sid: info.get('parentID') for sid, info in sessions.items()}
    for t in tools:
        sid = (t.get('metadata') or {}).get('sessionId')
        if t.get('tool') == 'task' and sid and not parents.get(sid):
            parents[sid] = t.get('session_id')

    def top_child(sid):
        seen = set()
        while parents.get(sid) in children:
            if sid in seen:
                raise ValueError('Cyclic child-session ancestry')
            seen.add(sid)
            sid = parents[sid]
        return sid

    child_jobs = defaultdict(list)
    for sid, spans in child_spans.items():
        if spans:
            child_jobs[top_child(sid)].append((min(a for a, b in spans), max(b for a, b in spans)))
    jobs = []
    for index, t in enumerate(tools):
        interval = span(t, 'start', 'end', end)
        child = t.get('role') == 'subagent' or t.get('session_id') in children
        if t.get('tool') == 'task':
            sid = (t.get('metadata') or {}).get('sessionId')
            if sid:
                key = top_child(sid)
            elif child:
                key = top_child(t.get('session_id'))
            else:
                # An unlinked wrapper is still observed work; do not infer a
                # missing child's duration or merge unrelated delegations.
                key = ('unlinked_wrapper', index)
            child_jobs[key].append(interval)
        elif not child:
            jobs.append([interval])
    return jobs + list(child_jobs.values())


def mean_tool_parallel_bound(bounds):
    current = mean([b['tool_union_seconds'] for b in bounds])
    lower = mean([b['ideal_lower_bound_seconds'] for b in bounds])
    reduction = mean([b['maximum_reduction_seconds'] for b in bounds])
    return dict(mean_tool_union_seconds=current, mean_ideal_lower_bound_seconds=lower,
                mean_maximum_reduction_seconds=reduction,
                maximum_reduction_percent_of_mean_tool_union=100*reduction/current if current else None)


def reasoning_fraction(q, quality):
    """Estimate the monotonic reasoning -> visible/tool-argument transition."""
    bounds = q.get('phase_boundaries') or {}
    reason = bounds.get('reasoning')
    visible = [bounds[k] for k in ('visible_output', 'tool_arguments') if k in bounds]
    values = ([reason] if reason else []) + visible
    if values and all(len(v) == 2 and all(number(x) for x in v) and v[1] >= v[0] for v in values):
        if not reason:
            return 0.0
        if not visible:
            return 1.0
        first = min(v[0] for v in values)
        last = max(v[1] for v in values)
        non_first = min(v[0] for v in visible)
        if last > first and reason[1] <= non_first:
            # The switch occurred somewhere between the last reasoning delivery
            # and first non-reasoning delivery. Use their midpoint, not tokens.
            transition = (reason[1] + non_first) / 2
            return max(0.0, min(1.0, (transition-first)/(last-first)))
    # Older exports or interleaved phases: approximate by delivered phase time.
    reasoning = non_reasoning = 0.0
    for label, duration in (q.get('phase_delivery_seconds') or {}).items():
        if not number(duration) or duration < 0:
            continue
        parts = set(label.split('+'))
        r, n = 'reasoning' in parts, bool(parts & {'visible_output', 'tool_arguments'})
        if r and n:
            reasoning += duration / 2
            non_reasoning += duration / 2
        elif r:
            reasoning += duration
        elif n:
            non_reasoning += duration
    if reasoning + non_reasoning > 0:
        quality['phase_duration_fallback_requests'] += 1
        return reasoning / (reasoning + non_reasoning)
    quality['unknown_decode_split_requests'] += 1
    return None


def task_metrics(td, result, prefill_field):
    start, end = result['agent_start'], result['agent_end']
    requests, tools, timing = [load(td / (name + '.json')) for name in ('requests', 'tools', 'timing')]
    sessions = timing.get('sessions') or {}
    children = {sid for sid, info in sessions.items() if info.get('parentID')}
    children.update(q.get('session_id') for q in requests if q.get('role') == 'subagent')
    children.update(t.get('session_id') for t in tools if t.get('role') == 'subagent')
    children.update((t.get('metadata') or {}).get('sessionId') for t in tools if t.get('tool') == 'task')
    children.discard(None)

    def is_child(record):
        return record.get('role') == 'subagent' or record.get('session_id') in children

    # A child is a tool for its entire observed lifespan, including internal gaps.
    # All descendants share the same category: interval union prevents counting
    # their internal models/tools, or nested child lifespans, more than once.
    child_spans = defaultdict(list)
    for q in requests:
        if is_child(q):
            child_spans[q.get('session_id')].append(span(q, 'started_at', 'ended_at', end))
    for t in tools:
        if is_child(t):
            child_spans[t.get('session_id')].append(span(t, 'start', 'end', end))
    intervals = [(min(a for a, b in xs), max(b for a, b in xs), 'subagent')
                 for xs in child_spans.values() if xs]
    for t in tools:
        a, b = span(t, 'start', 'end', end)
        if t.get('tool') == 'task':
            intervals.append((a, b, 'subagent'))
        elif not is_child(t):
            if t.get('role') != 'main':
                raise ValueError(str(td) + ': unattributed tool role')
            intervals.append((a, b, 'ordinary_tool'))

    quality = Counter()
    tokens_after_first, generation_seconds = 0, 0.0
    for q in requests:
        if is_child(q):
            continue
        if q.get('role') not in ('main', 'title', 'compaction', 'summary'):
            raise ValueError(str(td) + ': unattributed request role')
        a, b = span(q, 'started_at', 'ended_at', end)
        if b <= start or a >= end:
            continue
        quality['parent_requests'] += 1
        metrics, usage = q.get('metrics') or {}, q.get('usage') or {}
        decode_ms = metrics.get('generation_time_ms')
        # Prefer an explicit prefill field if a newer exporter provides it.
        prefill_ms = metrics.get('prefill_time_ms', metrics.get(prefill_field))
        tokens = usage.get('completion_tokens')
        if (not q.get('in_progress') and a >= start and b <= end and
                number(decode_ms) and decode_ms > 0 and number(tokens) and tokens >= 1):
            tokens_after_first += tokens - 1
            generation_seconds += decode_ms / 1000
            quality['tps_matched_requests'] += 1
        valid_decode = number(decode_ms) and decode_ms >= 0
        valid_prefill = number(prefill_ms) and prefill_ms >= 0
        if q.get('in_progress') or not valid_decode:
            intervals.append((a, b, 'other'))
            quality['unattributed_timing_requests'] += 1
            continue
        decode = decode_ms / 1000
        prefill = prefill_ms / 1000 if valid_prefill else 0.0
        if decode + prefill > b - a + 1e-6:
            intervals.append((a, b, 'other'))
            quality['invalid_timing_requests'] += 1
            continue
        # Server logs export durations, not wall-clock phase endpoints. Align
        # the server's last-token time with the proxy's request_end timestamp.
        # Queue/transport/unaccounted request time remains in other. The phase
        # placement and reasoning split are estimates, not a GPU kernel trace.
        decode_start = max(a, b - decode)
        prefill_start = max(a, decode_start - prefill)
        intervals.append((a, prefill_start, 'other'))
        if valid_prefill:
            intervals.append((prefill_start, decode_start, 'prefill'))
            quality['prefill_timed_requests'] += 1
        else:
            quality['missing_prefill_requests'] += 1
        if decode > 0:
            fraction = reasoning_fraction(q, quality)
            if fraction is None:
                intervals.append((decode_start, b, 'other'))
            else:
                switch = decode_start + (b-decode_start) * fraction
                intervals.extend([(decode_start, switch, 'reasoning'), (switch, b, 'non_reasoning')])

    seconds, overlap = exclusive_partition(intervals, start, end)
    seconds['decode'] = seconds['reasoning'] + seconds['non_reasoning']
    seconds['tools'] = seconds['ordinary_tool'] + seconds['subagent']
    grade = result.get('grade') or (load(td/'grade.json') if (td/'grade.json').exists() else {})
    grade_known = isinstance(grade.get('resolved'), bool) and not grade.get('infra_failure')
    return dict(instance_id=td.name, wall_seconds=end-start, seconds=seconds,
                grade_known=grade_known, passed=grade_known and grade['resolved'],
                tool_parallel_bound=tool_parallel_bound(
                    parent_tool_jobs(tools, child_spans, children, sessions, end), start, end),
                decode_tps=tokens_after_first/generation_seconds if generation_seconds else None,
                cross_category_overlap_seconds=overlap, quality=dict(quality))


def candidates(root):
    if (root / 'result.json').exists():
        return [root]
    runs = root / 'runs' if (root / 'runs').is_dir() else root
    if not runs.is_dir():
        raise ValueError('Input does not exist: ' + str(runs))
    if (root / 'selected.json').exists():
        return [runs / row['instance_id'] for row in load(root / 'selected.json')]
    return sorted(p for p in runs.iterdir() if p.is_dir() and p.name not in
                  ('stopped-by-user', 'analysis-final', 'reference', 'presentation'))


def campaign(root, exclude_censored=False, prefill_field='time_to_first_token_ms'):
    rows, excluded = [], Counter()
    for td in candidates(root):
        if not (td / 'result.json').exists():
            excluded['unfinished_or_no_result'] += 1
            continue
        r = load(td / 'result.json')
        a, b = r.get('agent_start'), r.get('agent_end')
        if not number(a) or not number(b) or b <= a:
            excluded['no_model_solving'] += 1
            continue
        if exclude_censored and (r.get('censored') or r.get('termination_reason') in ('wall_time_limit', 'turn_limit')):
            excluded['budget_or_time_censored'] += 1
            continue
        if any(not (td / (name + '.json')).exists() for name in ('requests', 'tools', 'timing')):
            excluded['analysis_not_ready'] += 1
            continue
        rows.append(task_metrics(td, r, prefill_field))
    if not rows:
        raise ValueError('No completed tasks with analysis: ' + str(root))
    quality = Counter()
    for row in rows:
        quality.update(row['quality'])
    percentages = {k: mean([100*r['seconds'][k]/r['wall_seconds'] for r in rows]) for k in rows[0]['seconds']}
    assert math.isclose(sum(percentages[k] for k in LEAVES), 100, abs_tol=1e-6)
    tps = [r['decode_tps'] for r in rows if r['decode_tps'] is not None]
    graded = sum(r['grade_known'] for r in rows)
    passed = sum(r['passed'] for r in rows)
    return dict(input=str(root), tasks=len(rows), tps_tasks=len(tps), excluded=dict(excluded),
                graded_tasks=graded, passed=passed,
                pass_rate_percent=100*passed/len(rows) if graded == len(rows) else None,
                mean_wall_seconds=mean([r['wall_seconds'] for r in rows]), mean_decode_tps=mean(tps),
                mean_seconds={k: mean([r['seconds'][k] for r in rows]) for k in rows[0]['seconds']},
                mean_percent=percentages, quality=dict(quality),
                tool_parallel_bound=mean_tool_parallel_bound([r['tool_parallel_bound'] for r in rows]))


def expand_roots(paths):
    roots = []
    for path in paths:
        root = path.resolve()
        arms = root / 'arms'
        if arms.is_dir():
            roots.extend(sorted(p for p in arms.iterdir() if (p / 'runs').is_dir()))
        else:
            roots.append(root)
    if len(set(roots)) != len(roots):
        raise ValueError('Duplicate input directories')
    return roots


def build_report(paths, exclude_censored=False, prefill_field='time_to_first_token_ms'):
    roots = expand_roots(paths)
    labels = [r.parent.name if r.name == 'runs' else r.name for r in roots]
    if len(set(labels)) != len(labels):
        labels = list(map(str, roots))
    return dict(schema_version=2, averaging='arithmetic mean over tasks; task TPS uses matched token/time sums; tool parallel bound percentage is a ratio of means',
                scope='parent model including title/compaction; child lifespans folded into subagent tools',
                attribution='exclusive leaf categories; all cross-category overlaps and unknown time in other',
                phase_estimation='server durations aligned to proxy request end; reasoning split estimated from stream boundaries',
                tool_parallel_bound_definition='U = union of all parent tool jobs, including opaque child lifespans and wrappers; L = longest job duration after within-job union; reduction upper bound = U - L. Fixed observed durations, no dependencies or resource limits. Percentage = mean(U-L)/mean(U), not mean((U-L)/U).',
                prefill_field=prefill_field, exclude_censored=exclude_censored,
                groups={label: campaign(root, exclude_censored, prefill_field) for label, root in zip(labels, roots)})


def fmt(value):
    return 'N/A' if value is None else f'{value:,.2f}'


def markdown(report):
    groups = report['groups']
    lines = ['# 核心效率指标：逐题平均', '',
             '样本：' + '；'.join(f'{name} {g["tasks"]} 题' for name, g in groups.items()) + '。', '',
             '| 指标 | ' + ' | '.join(groups) + ' |', '|---|' + '---:|' * len(groups)]
    def row(label, values):
        lines.append('| ' + label + ' | ' + ' | '.join(values) + ' |')
    row('通过率', [f'{g["pass_rate_percent"]:.2f}%' if g['pass_rate_percent'] is not None else 'N/A（评分未齐）' for g in groups.values()])
    row('平均求解时间（秒/题）', [fmt(g['mean_wall_seconds']) for g in groups.values()])
    row('平均 Decode TPS（token/s）', [fmt(g['mean_decode_tps']) for g in groups.values()])
    for label, key in [('Decode', 'decode'), ('Decode：reasoning', 'reasoning'),
                       ('Decode：非 reasoning', 'non_reasoning'), ('Prefill', 'prefill'),
                       ('工具执行', 'tools'), ('工具：普通工具', 'ordinary_tool'),
                       ('工具：subagent', 'subagent'), ('其余（含跨类重叠）', 'other')]:
        row(label, [f'{g["mean_seconds"][key]:,.2f} 秒 / {g["mean_percent"][key]:.2f}%' for g in groups.values()])
    lines += ['', '通过率 = 官方评分通过题数 / 本表纳入的求解题数；评分未齐或有评分环境失败时标为 N/A，不把未知评分当作未通过。',
              '', '各阶段展示「平均秒/题 / 平均占比」。先算每题占比、每题 TPS，再对题目等权平均；不是累计秒数的比例，也不是逐请求 TPS 平均。',
              '', 'Decode + Prefill + 工具执行 + 其余 = 100%。子行已包含在父行；不同细分类同时活跃的区间归入其余，同类并发取并集。',
              '', '模型指标仅统计非子会话请求（含 title/compaction）；subagent 按完整观测生命周期计为工具，内部模型与工具不重复计入。非 reasoning 包含可见文本和工具参数。',
              '', 'Prefill 使用服务端 scheduled→first-token 时长，不含队列等待；阶段位置按请求结束时间对齐，reasoning 分界按流式边界估算。其余还包括排队、传输、编排等待及无法归因的时间。']
    lines += ['', '工具并行优化的理想上界估计（含 subagent，时间均为平均秒/题）：', '',
              '| 指标 | ' + ' | '.join(groups) + ' |', '|---|' + '---:|' * len(groups)]
    for label, key in [('当前工具活跃时间（并集）', 'mean_tool_union_seconds'),
                       ('理想工具时间下界（最长调用）', 'mean_ideal_lower_bound_seconds'),
                       ('最多可减少工具时间', 'mean_maximum_reduction_seconds')]:
        row(label, [fmt(g['tool_parallel_bound'][key]) for g in groups.values()])
    row('最多可减少比例（减少均值 / 当前并集均值）', [
        f'{g["tool_parallel_bound"]["maximum_reduction_percent_of_mean_tool_union"]:.2f}%'
        if g['tool_parallel_bound']['maximum_reduction_percent_of_mean_tool_union'] is not None else 'N/A（无工具时间）'
        for g in groups.values()])
    lines += ['', '每题 U = 全部工具活跃区间的并集，L = 最长单项工具时长；先算每题 U−L，再取平均。'
              '本表比例 = mean(U−L) / mean(U)，不同于上方阶段占比的逐题百分比平均。',
              '', '固定现有调用及其观测耗时，忽略依赖和资源限制，假设其他工具都能与最长调用重叠。'
              '主级普通工具各算一项；同一 subagent 的包装调用与观测生命周期取并集合为一项，嵌套子会话折叠到顶层 subagent。',
              '', '这里保留与模型重叠的工具时间，因此 U 不等于上方互斥分区中的“工具执行”。'
              '估计不是实际提速预测，也不是完整求解时间的降幅；改变命令、缓存或验证范围后不再受此估计约束。'
              '最长调用受接口粒度影响，拆分一个包含多个命令的工具调用也会改变下界。'
              '区间裁剪到求解起止，超时或中止的调用只代表已观测部分。']
    for name, g in groups.items():
        q = g['quality']
        missing = {k: q[k] for k in ('unattributed_timing_requests', 'invalid_timing_requests',
                   'missing_prefill_requests', 'unknown_decode_split_requests', 'phase_duration_fallback_requests') if q.get(k)}
        if missing or g['excluded'] or g['tps_tasks'] != g['tasks'] or g['graded_tasks'] != g['tasks']:
            lines += ['', f'{name} 数据覆盖：有效评分 {g["graded_tasks"]}/{g["tasks"]} 题，已确认通过 {g["passed"]} 题；TPS {g["tps_tasks"]}/{g["tasks"]} 题；'
                      f'计时缺口 {json.dumps(missing, ensure_ascii=False)}；排除 {json.dumps(g["excluded"], ensure_ascii=False)}。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('directories', nargs='*', type=Path, default=[Path.cwd()])
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--exclude-censored', action='store_true')
    parser.add_argument('--prefill-field', default='time_to_first_token_ms',
                        help='This project exports prefill under time_to_first_token_ms; use prefill_time_ms for other exporters')
    args = parser.parse_args()
    try:
        report = build_report(args.directories, args.exclude_censored, args.prefill_field)
        text = markdown(report)
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for name, content in [('key-metrics.md', text), ('key-metrics.json', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')]:
                path = args.output_dir / name
                tmp = path.with_suffix(path.suffix + '.tmp')
                tmp.write_text(content)
                tmp.replace(path)
        print(text, end='')
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, 'ERROR: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
