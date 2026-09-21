#!/usr/bin/env python3
"""Offline efficiency metrics from SWE/OpenCode exported task records; Python 3.9+.

python efficiency_metrics.py [CAMPAIGN_OR_RUNS_DIR] [--output-dir metrics-output]
Requires runs/<task>/{result,timing,requests,tools}.json, including compact exports.
No inference, network access, third-party packages or input-file modifications.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import sys


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def ratio(a, b, scale=1):
    return scale * a / b if b else None


def partition(intervals, start, end):
    """Clip to task lifetime, merge overlaps by label, return exclusive seconds."""
    events = defaultdict(Counter)
    for a, b, label in intervals:
        if not number(a) or not number(b):
            raise ValueError('Non-finite interval endpoint')
        a, b = max(start, a), min(end, b)
        if b > a:
            events[a][label] += 1
            events[b][label] -= 1
    result, active, previous = Counter(), Counter(), start
    for at, change in sorted(events.items()):
        label = ' + '.join(sorted(k for k, v in active.items() if v > 0)) or 'idle'
        result[label] += at - previous
        active.update(change)
        previous = at
    label = ' + '.join(sorted(k for k, v in active.items() if v > 0)) or 'idle'
    result[label] += end - previous
    assert math.isclose(sum(result.values()), end - start, abs_tol=1e-5)
    return dict(result)


def interval(record, start_key, end_key, task_end):
    a, b = record.get(start_key), record.get(end_key)
    if a is None:
        raise ValueError('Missing interval start: ' + start_key)
    # An unfinished interval is right-censored at the task boundary, not dropped.
    return a, b if b is not None else task_end


def load(path):
    return json.loads(path.read_text())


def token_stats(requests):
    out = Counter(requests=len(requests))
    for q in requests:
        u = q.get('usage') or {}
        if not u:
            out['requests_missing_usage'] += 1
            out['received_tokens_missing_usage'] += q.get('token_ids_count', 0)
        else:
            out['requests_with_usage'] += 1
            for key in ('prompt_tokens', 'completion_tokens'):
                out[key] += u.get(key, 0)
            out['reasoning_tokens'] += (u.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
        ms = (q.get('metrics') or {}).get('generation_time_ms')
        tokens = u.get('completion_tokens')
        # Match numerator and denominator on the SAME request set.
        if number(ms) and ms > 0 and number(tokens) and tokens >= 1:
            out['decode_requests'] += 1
            out['decode_response_tokens'] += tokens
            out['decode_tokens_after_first'] += tokens - 1
            out['decode_seconds_sum'] += ms / 1000
        if q.get('in_progress'):
            out['right_censored_requests'] += 1
    return dict(out)


def read_task(td, r):
    start, end = r.get('agent_start'), r.get('agent_end')
    if not number(start) or not number(end) or end <= start:
        raise ValueError(str(td) + ': invalid agent_start/agent_end')
    qs, ts, timing = (load(td / (name + '.json')) for name in ('requests', 'tools', 'timing'))
    source, parent = [], []
    roles = defaultdict(list)
    for q in qs:
        role = q.get('role')
        if role not in ('main', 'subagent', 'title', 'compaction', 'summary'):
            raise ValueError(str(td) + ': missing or unattributed request role')
        a, b = interval(q, 'started_at', 'ended_at', end)
        source.append((a, b, 'model'))
        if role != 'subagent':
            parent.append((a, b, 'model'))
        roles[role].append(q)
    task_calls = 0
    tools = defaultdict(Counter)
    for t in ts:
        if t.get('role') not in ('main', 'subagent'):
            raise ValueError(str(td) + ': missing or unattributed tool role')
        a, b = interval(t, 'start', 'end', end)
        name = t['tool']
        tools[name]['calls'] += 1
        tools[name]['seconds_sum'] += max(0, min(b, end) - max(a, start))
        if name == 'task':
            task_calls += 1
        else:
            source.append((a, b, 'tool'))
        if t.get('role') != 'subagent':
            parent.append((a, b, 'subagent_tool' if name == 'task' else 'tool'))
    return dict(
        instance_id=td.name, group='with_subagent' if task_calls else 'without_subagent',
        passed=bool((r.get('grade') or {}).get('resolved')), agent_seconds=end-start,
        termination=r.get('termination_reason', 'unknown'), task_calls=task_calls,
        tokens=token_stats(qs), roles={k: token_stats(v) for k, v in roles.items()},
        source_view=partition(source, start, end), parent_view=partition(parent, start, end),
        stages=timing.get('stages', {}), tools={k: dict(v) for k, v in tools.items()},
        counters=(timing.get('sampled_server_counter_deltas') or {}).get('delta', {}))


def derived_tokens(s, wall):
    s = dict(s)
    s.update(decode_tps=ratio(s.get('decode_tokens_after_first', 0), s.get('decode_seconds_sum', 0)),
             task_effective_tps=ratio(s.get('completion_tokens', 0), wall),
             reasoning_token_percent=ratio(s.get('reasoning_tokens', 0), s.get('completion_tokens', 0), 100))
    return s


def aggregate(rows):
    wall = sum(r['agent_seconds'] for r in rows)
    out = dict(tasks=len(rows), passed=sum(r['passed'] for r in rows), agent_seconds=wall,
               mean_task_seconds=statistics.mean(r['agent_seconds'] for r in rows) if rows else None,
               task_calls=sum(r['task_calls'] for r in rows),
               terminations=dict(Counter(r['termination'] for r in rows)))
    sums = {k: Counter() for k in ('tokens', 'source_view', 'parent_view', 'stages', 'counters')}
    roles, tools = defaultdict(Counter), defaultdict(Counter)
    for r in rows:
        for k in sums:
            sums[k].update(r[k])
        for k, v in r['roles'].items():
            roles[k].update(v)
        for k, v in r['tools'].items():
            tools[k].update(v)
    out['tokens'] = derived_tokens(sums['tokens'], wall)
    out['roles'] = {k: derived_tokens(v, wall) for k, v in roles.items()}
    for name in ('source_view', 'parent_view'):
        out[name] = {k: {'seconds': v, 'percent_of_agent': ratio(v, wall, 100)} for k, v in sorted(sums[name].items())}
    pv = sums['parent_view']
    exclusive = sum(v for k, v in pv.items() if 'tool' in k and 'model' not in k)
    inclusive = sum(v for k, v in pv.items() if 'tool' in k)
    out['tools_including_subagent'] = dict(exclusive_seconds=exclusive,
        exclusive_percent=ratio(exclusive, wall, 100), inclusive_seconds=inclusive,
        inclusive_percent=ratio(inclusive, wall, 100))
    stage_sum = sum(sums['stages'].values())
    out['stages'] = {k: {'seconds': v, 'percent_of_recorded_stages': ratio(v, stage_sum, 100)} for k, v in sums['stages'].items()}
    out['tools_calls_summed_not_exclusive'] = {k: dict(v) for k, v in tools.items()}
    c = sums['counters']
    out['server_sampled_counters_not_wall_partition'] = dict(
        decode_seconds_sum=c.get('vllm:request_decode_time_seconds_sum'),
        prefill_seconds_sum=c.get('vllm:request_prefill_time_seconds_sum'),
        queue_seconds_sum=c.get('vllm:request_queue_time_seconds_sum'),
        cache_hit_percent=ratio(c.get('vllm:prefix_cache_hits_total', 0), c.get('vllm:prefix_cache_queries_total', 0), 100))
    return out


def fmt(x):
    return 'N/A' if x is None else f'{x:,.2f}'


def markdown(report):
    gs = report['groups']; names = list(gs)
    lines = ['# Efficiency metrics', '', f"数据目录：{report['input']}", '',
             '时间占比分母为已纳入任务的求解 wall time 之和；不是两个 server 的日历时间。', '',
             '| 指标 | ' + ' | '.join(names) + ' |', '|---|' + '---:|'*len(names)]
    def row(label, fn):
        lines.append('| ' + label + ' | ' + ' | '.join(fmt(fn(gs[k])) for k in names) + ' |')
    row('任务数', lambda g: g['tasks'])
    row('累计求解秒', lambda g: g['agent_seconds'])
    row('平均求解秒', lambda g: g['mean_task_seconds'])
    for label, key in [('请求数', 'requests'), ('Prompt tokens', 'prompt_tokens'), ('Response tokens', 'completion_tokens'),
                       ('Reasoning tokens', 'reasoning_tokens'), ('Decode TPS', 'decode_tps'), ('任务有效 TPS', 'task_effective_tps'),
                       ('有匹配 decode 计时的请求数', 'decode_requests'), ('缺少 usage 的请求数', 'requests_missing_usage')]:
        row(label, lambda g, key=key: g['tokens'].get(key))
    for name, title in [('source_view', '活动来源：子模型计入模型'), ('parent_view', '父任务视角：subagent计作工具')]:
        lines += ['', '## ' + title, '', '| 互斥时间分类（%） | ' + ' | '.join(names) + ' |', '|---|' + '---:|'*len(names)]
        keys = sorted({key for g in gs.values() for key in g[name]})
        for key in keys:
            row(key, lambda g, key=key: g[name].get(key, {}).get('percent_of_agent', 0 if g['tasks'] else None))
    lines += ['', '## 含 subagent 的工具时间', '', '| 指标（%） | ' + ' | '.join(names) + ' |', '|---|' + '---:|'*len(names)]
    row('工具独占（不含与主模型重叠）', lambda g: g['tools_including_subagent']['exclusive_percent'])
    row('工具活跃（含与主模型重叠）', lambda g: g['tools_including_subagent']['inclusive_percent'])
    lines += ['', '## 口径与覆盖', ''] + ['- ' + n for n in report['notes']]
    lines += ['', '排除情况：`' + json.dumps(dict(Counter(x['reason'] for x in report['excluded'])), ensure_ascii=False) + '`', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('directory', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output-dir', type=Path, help='Write metrics.json and metrics.md into this directory')
    parser.add_argument('--exclude-censored', action='store_true', help='Exclude task wall-time/turn-budget terminations')
    args = parser.parse_args()
    root = args.directory.resolve(); runs = root / 'runs' if (root / 'runs').is_dir() else root
    if not runs.is_dir():
        parser.error('Input directory does not exist')
    rows, excluded = [], []
    for td in sorted(x for x in runs.iterdir() if x.is_dir()):
        if td.name in ('analysis-final', 'presentation', 'reference', 'stopped-by-user'):
            continue
        f = td / 'result.json'
        if not f.exists():
            excluded.append(dict(instance_id=td.name, reason='unfinished_or_no_result'))
            continue
        r = load(f)
        if not r.get('agent_wall_seconds'):
            excluded.append(dict(instance_id=td.name, reason='no_model_solving'))
            continue
        if args.exclude_censored and (r.get('censored') or r.get('termination_reason') in ('wall_time_limit', 'turn_limit')):
            excluded.append(dict(instance_id=td.name, reason='budget_or_time_censored'))
            continue
        rows.append(read_task(td, r))
    if not rows:
        parser.error('No completed, eligible tasks found')
    report = dict(input=str(root), exclude_censored=args.exclude_censored, excluded=excluded,
        groups={k: aggregate(rows if k == 'all' else [r for r in rows if r['group'] == k])
                for k in ('all', 'with_subagent', 'without_subagent')}, per_task=rows,
        notes=[
            'model包含title/compaction等辅助请求；source_view也包含子模型。tool不含task包装。',
            'parent_view只保留非子会话请求和工具；subagent_tool是父task调用开始到返回的区间。后台返回后的子任务生命周期不是父任务等待。',
            '两个视图均按区间裁剪和去重重新计算，每组互斥占比合计100%，不是逐题百分比的简单平均。',
            'decode_tps = sum(response_tokens - 1) / sum(generation_time_ms / 1000)，分子分母使用同一批有usage和正decode计时的请求。',
            'task_effective_tps = 所有角色的已报告response tokens / 累计求解wall秒；不是GPU吞吐或有效进展速度。',
            'usage缺失请求保留其已观测时间，token缺口单列；未结束区间裁剪至任务结束。',
            '默认纳入已结束的预算超限任务；--exclude-censored可排除。无result及启动前失败排除；不扫描stopped-by-user。',
            'prefill/decode服务端采样之和和工具逐次耗时均非互斥wall分区，并发时不能直接相加。',
            'with/without按本轮实际task调用分组，非随机对照。脚本依赖已有导出的时间戳，不重新解析SSE原始日志。'])
    text = markdown(report)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'metrics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        (args.output_dir / 'metrics.md').write_text(text)
    print(text)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        sys.exit('ERROR: ' + str(exc))
