#!/usr/bin/env python3
"""Summarize actual draft/review/compression/target requests, never replayed SSE speed."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def summarize(records):
    routes=Counter(x.get('route','error') for x in records)
    eligible=routes['accepted']+routes['fallback']
    total=len(records)
    phases={}
    for phase in ['draft','review','compress','target']:
        stages=[s for r in records for s in r['stages'] if s['phase']==phase]
        valid=[s for s in stages if ((s.get('metrics') or {}).get('generation_time_ms') or 0)>0
               and ((s.get('usage') or {}).get('completion_tokens') or 0)>1]
        usages=[s['usage'] for s in stages if s.get('usage') and s['usage'].get('completion_tokens') is not None]
        decode_seconds=sum(s['metrics']['generation_time_ms']/1000 for s in valid)
        decode_tokens=sum(s['usage']['completion_tokens']-1 for s in valid)
        phases[phase]=dict(calls=len(stages),wall_seconds=sum(s['wall_seconds'] for s in stages),
                          mean_call_seconds=statistics.mean(s['wall_seconds'] for s in stages) if stages else None,
                          mean_completion_tokens=statistics.mean(u['completion_tokens'] for u in usages) if usages else None,
                          usage_covered_calls=len(usages),
                          prompt_tokens=sum((s.get('usage') or {}).get('prompt_tokens',0) for s in stages),
                          completion_tokens=sum((s.get('usage') or {}).get('completion_tokens',0) for s in stages),
                          decode_tps=decode_tokens/decode_seconds if decode_seconds else None,
                          tps_covered_calls=len(valid),decode_seconds=decode_seconds,
                          prefill_seconds=sum(((s.get('metrics') or {}).get('time_to_first_token_ms') or 0)/1000 for s in stages),
                          mean_request_time_percent=statistics.mean(100*sum(s['wall_seconds'] for s in r['stages'] if s['phase']==phase)/r['wall_seconds'] for r in records) if total else None)
    stage_seconds=sum(x['wall_seconds'] for x in phases.values())
    request_seconds=sum(x['wall_seconds'] for x in records)
    return dict(requests=total,routes=dict(routes),eligible_requests=eligible,
                acceptance_percent=100*routes['accepted']/eligible if eligible else None,
                review_acceptance_percent=100*sum(x.get('verdict')=='A' and x.get('route')=='accepted' for x in records)/sum(any(s['phase']=='review' for s in x['stages']) for x in records) if any(any(s['phase']=='review' for s in x['stages']) for x in records) else None,
                compression=dict(Counter(x.get('compression','not_applicable') for x in records)),
                mean_request_seconds=request_seconds/total if total else None,
                phases=phases,other_seconds=max(0,request_seconds-stage_seconds),
                mean_other_percent=statistics.mean(100*max(0,r['wall_seconds']-sum(s['wall_seconds'] for s in r['stages']))/r['wall_seconds'] for r in records) if total else None,
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
    text+=f"请求数：{result['requests']}；路由：{result['routes']}。工具候选流程接受率：{show(result['acceptance_percent'])}%（accepted / (accepted + fallback)，含候选格式/后端失败）；实际审核接受率：{show(result['review_acceptance_percent'])}%。\n\n"
    text+=f"平均请求等待：{show(result['mean_request_seconds'])} 秒；压缩：{result['compression']}。\n\n"
    text+='| 阶段 | 物理调用数 | 平均单次秒数 | 平均响应 token | Decode TPS | Prompt token 合计 | Response token 合计 | 平均请求时间占比 |\n|---|---:|---:|---:|---:|---:|---:|---:|\n'
    labels={'draft':'8B 候选','review':'27B 审核','compress':'27B 压缩','target':'27B 正常生成'}
    for name,p in result['phases'].items():
        text+=f"| {labels[name]} | {p['calls']} | {show(p['mean_call_seconds'])} | {show(p['mean_completion_tokens'])} | {show(p['decode_tps'])} | {p['prompt_tokens']} | {p['completion_tokens']} | {show(p['mean_request_time_percent'])}% |\n"
    text+=f"\n其余占比：{show(result['mean_other_percent'])}%（tokenization、格式校验和转接等）。\n\n"
    text+='TPS 来自各后端 vLLM 的真实 usage 和 generation_time_ms，不使用接受后集中发送 SSE 的速度；按有效请求累计 token/累计 decode 秒计算。标准 key-metrics 的模型阶段列不能覆盖这个转接口内部的所有阶段，应结合本报告；通过率与整题墙钟仍看官方 result/grade。接受不代表任务最终通过。\n'
    (out/'gateway-metrics.md').write_text(text)
    print(text)


if __name__=='__main__':
    main()
