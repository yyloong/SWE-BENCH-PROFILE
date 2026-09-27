#!/usr/bin/env python3
"""Summarize actual draft/review/compression/target requests, never replayed SSE speed."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def wall_buckets(record):
    """Partition one logical request into disjoint physical-stage combinations."""
    start, end = record['started_at'], record['ended_at']
    stages = [(max(start,s['started_at']),min(end,s['ended_at']),s['phase'])
              for s in record['stages']]
    points = sorted({start,end} | {x for a,b,_ in stages if b>a for x in [a,b]})
    buckets = Counter()
    for a,b in zip(points,points[1:]):
        active = sorted({phase for x,y,phase in stages if x<b and y>a})
        buckets['+'.join(active) or 'other'] += b-a
    return buckets


def summarize(records):
    routes=Counter(x.get('route','error') for x in records)
    eligible=routes['accepted']+routes['fallback']+routes['target_won']
    total=len(records)
    phases={}
    for phase in ['draft','review','compress','target']:
        stages=[s for r in records for s in r['stages'] if s['phase']==phase]
        complete=[s for s in stages if not s.get('cancelled') and not s.get('error')
                  and s.get('usage_complete') is not False and s.get('finish_reason')]
        valid=[s for s in complete if ((s.get('metrics') or {}).get('generation_time_ms') or 0)>0
               and ((s.get('usage') or {}).get('completion_tokens') or 0)>1]
        usages=[s['usage'] for s in complete if s.get('usage') and s['usage'].get('completion_tokens') is not None]
        decode_seconds=sum(s['metrics']['generation_time_ms']/1000 for s in valid)
        decode_tokens=sum(s['usage']['completion_tokens']-1 for s in valid)
        phases[phase]=dict(calls=len(stages),wall_seconds=sum(s['wall_seconds'] for s in stages),
                          mean_call_seconds=statistics.mean(s['wall_seconds'] for s in stages) if stages else None,
                          mean_completion_tokens=statistics.mean(u['completion_tokens'] for u in usages) if usages else None,
                          usage_covered_calls=len(usages),
                          prompt_tokens=sum(u.get('prompt_tokens',0) for u in usages),
                          completion_tokens=sum(u.get('completion_tokens',0) for u in usages),
                          cancelled_calls=sum(bool(s.get('cancelled')) for s in stages),
                          incomplete_observed_completion_tokens_lower_bound=sum(max((s.get('usage') or {}).get('completion_tokens',0),s.get('observed_completion_token_ids',0)) for s in stages if s not in complete),
                          decode_tps=decode_tokens/decode_seconds if decode_seconds else None,
                          mean_decode_tps=statistics.mean((s['usage']['completion_tokens']-1)/(s['metrics']['generation_time_ms']/1000) for s in valid) if valid else None,
                          tps_covered_calls=len(valid),decode_seconds=decode_seconds,
                          prefill_seconds=sum(((s.get('metrics') or {}).get('time_to_first_token_ms') or 0)/1000 for s in complete),
                          mean_request_time_percent=statistics.mean(100*sum(s['wall_seconds'] for s in r['stages'] if s['phase']==phase)/r['wall_seconds'] for r in records) if total else None)
    request_seconds=sum(x['wall_seconds'] for x in records)
    buckets=[wall_buckets(r) for r in records]
    labels=sorted({label for bucket in buckets for label in bucket})
    exclusive={label:dict(seconds=sum(b[label] for b in buckets),
                         mean_request_time_percent=statistics.mean(100*b[label]/r['wall_seconds'] for b,r in zip(buckets,records)))
               for label in labels}
    return dict(requests=total,routes=dict(routes),eligible_requests=eligible,
                acceptance_percent=100*routes['accepted']/eligible if eligible else None,
                review_acceptance_percent=100*sum(x.get('verdict')=='A' and x.get('route')=='accepted' for x in records)/sum(any(s['phase']=='review' for s in x['stages']) for x in records) if any(any(s['phase']=='review' for s in x['stages']) for x in records) else None,
                compression=dict(Counter(x.get('compression','not_applicable') for x in records)),
                routing_policy=dict(Counter(x.get('routing_policy','legacy_wait_review' if x.get('overlap_target') else 'serial') for x in records)),
                mean_request_seconds=request_seconds/total if total else None,
                phases=phases,exclusive_wall_buckets=exclusive,
                background_target=dict(Counter(r['background_target_disposition'] for r in records if r.get('background_target_disposition'))),
                other_seconds=sum(b['other'] for b in buckets),
                mean_other_percent=exclusive.get('other',{}).get('mean_request_time_percent'),
                mean_overlap_percent=sum(v['mean_request_time_percent'] for k,v in exclusive.items() if '+' in k) if total else None,
                errors=sum(bool(x.get('error')) for x in records),cancelled=sum(bool(x.get('cancelled')) for x in records))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log',help='gateway-logs/requests.jsonl or gateway-logs directory')
    parser.add_argument('--run-dir',help='Only include request starts within this Pro run task agent intervals')
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args()
    path=Path(args.log)
    if path.is_dir():path=path/'requests.jsonl'
    by_id={}
    for line in path.read_text().splitlines():
        try:row=json.loads(line)
        except json.JSONDecodeError:continue
        by_id[row['id']]=row
    records=list(by_id.values())
    intervals=[]
    if args.run_dir:
        if (Path(args.run_dir)/'invalid-experiment.json').exists():
            raise SystemExit('This run is marked invalid and cannot be reported as evaluation data.')
        for file in (Path(args.run_dir)/'runs').glob('*/result.json'):
            data=json.loads(file.read_text())
            if data.get('agent_start') and data.get('agent_end'):
                intervals.append((data['agent_start'],data['agent_end']))
        records=[r for r in records if any(a<=r['started_at']<=b for a,b in intervals)]
    result=summarize(records)
    result.update(source=str(path.resolve()),run_dir=args.run_dir,
                  scope='physical model requests within gateway; fractions are mean per API request, not task wall time',
                  tps_definition='pooled sum(completion_tokens-1)/sum(server generation_time_seconds), matching valid calls')
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    (out/'gateway-metrics.json').write_text(json.dumps(result,indent=2))
    def show(value):return 'N/A' if value is None else f'{value:.2f}'
    text='# Semantic gate 实际模型开销\n\n'
    text+='这些比例的分母是每个 API 请求的等待时间，先逐请求计算再平均；不含工具执行，也不是整题求解时间占比。并发请求的耗时不能直接相加当作批次墙钟。\n\n'
    text+=f"请求数：{result['requests']}；路由：{result['routes']}；策略：{result['routing_policy']}。候选被选中率：{show(result['acceptance_percent'])}%（accepted / (accepted + fallback + target_won)，含 27B 抢先完成）；发起审核后最终选择候选的比例：{show(result['review_acceptance_percent'])}%。target_won 表示 27B 先完成、立即采用并取消另一分支。\n\n"
    text+=f"平均请求等待：{show(result['mean_request_seconds'])} 秒；压缩：{result['compression']}。\n\n"
    text+='阶段活动时间可重叠，最后一列不能相加作为总占比。Token 与 TPS 列仅含完成且有完整计数的调用。\n\n'
    text+='| 阶段 | 物理调用数 | 平均单次秒数 | 平均响应 token | 平均 Decode TPS | 完成调用 Prompt 合计 | 完成调用 Response 合计 | 平均请求活动时间占比 |\n|---|---:|---:|---:|---:|---:|---:|---:|\n'
    labels={'draft':'8B 候选','review':'27B 审核','compress':'27B 压缩','target':'27B 正常生成'}
    for name,p in result['phases'].items():
        text+=f"| {labels[name]} | {p['calls']} | {show(p['mean_call_seconds'])} | {show(p['mean_completion_tokens'])} | {show(p['mean_decode_tps'])} | {p['prompt_tokens']} | {p['completion_tokens']} | {show(p['mean_request_time_percent'])}% |\n"
    text+='\n下表按区间并集分割，互斥占比合计约 100%；这是 HTTP 调用存续区间，不能据此断言 GPU kernel 同时运行。\n\n| 请求墙钟区间 | 平均占比 |\n|---|---:|\n'
    for label,p in result['exclusive_wall_buckets'].items():
        text+=f"| {label} | {show(p['mean_request_time_percent'])}% |\n"
    text+=f"\n后台 27B 去向：{result['background_target']}。平均重叠占比：{show(result['mean_overlap_percent'])}%。\n\n"
    for name,p in result['phases'].items():
        if p['cancelled_calls'] or p['incomplete_observed_completion_tokens_lower_bound']:
            text+=f"{labels[name]}：取消 {p['cancelled_calls']} 次；未完成调用已观察输出至少 {p['incomplete_observed_completion_tokens_lower_bound']} token，未计入完整调用 token/TPS 列。取消不是零成本，部分计数不能当精确总消耗。\n\n"
    text+='TPS 来自后端 vLLM usage 和 generation_time_ms，单次 (completion_tokens−1)/decode 秒再平均；JSON 另保留累计口径 decode_tps。不能使用集中发送 SSE 的速度。标准 key-metrics 的模型阶段列不能覆盖全部内部开销；通过率与整题墙钟仍看官方 result/grade。接受不代表任务最终通过。\n'
    (out/'gateway-metrics.md').write_text(text)
    print(text)


if __name__=='__main__':
    main()
