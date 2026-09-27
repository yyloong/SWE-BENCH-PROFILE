#!/usr/bin/env python3
"""Paired real-prefix latency replay: target alone, serial gate, overlapping gate.

No tool executes. This tests request latency and cancellation, not task accuracy.
Run with otherwise idle model services, then start the isolated Pro evaluation.
"""
import argparse
import asyncio
import copy
import json
from pathlib import Path
from collections import Counter
import statistics
import time
import uuid

import httpx

if __package__:
    from .proxy import Config, Gate, stage_payload
    from .metrics import summarize
else:
    from proxy import Config, Gate, stage_payload
    from metrics import summarize


async def running(client, url):
    response=await client.get(url+'/metrics')
    response.raise_for_status()
    return sum(float(line.split()[-1]) for line in response.text.splitlines()
               if line.startswith(('vllm:num_requests_running{','vllm:num_requests_waiting{')))


def annotate_report(out, report):
    measured=[r for r in report['calls'] if r['repeat']>0]
    arms=['target','serial','overlap']
    report['finish_counts']={arm:dict(Counter(r['result']['choices'][0]['finish_reason'] for r in measured if r['arm']==arm)) for arm in arms}
    for arm in arms:
        (out/arm/'measured-requests.jsonl').write_text(''.join(json.dumps(r['request'],ensure_ascii=False)+'\n' for r in measured if r['arm']==arm))
    grouped={}
    for r in measured:
        grouped.setdefault((r['index'],r['repeat']),{})[r['arm']]=r
    complete=[rows for rows in grouped.values() if len(rows)==3 and all(r['result']['choices'][0]['finish_reason']!='length' for r in rows.values())]
    report['completed_triplet_summary']={arm:summarize([rows[arm]['request'] for rows in complete]) for arm in arms}
    report['length_censored_triplets']=len(grouped)-len(complete)
    text='\n## 输出截断与工作量说明\n\n'
    text+=f"首次完整生成剔除后，各组结束原因：{report['finish_counts']}。有 {report['length_censored_triplets']} 组配对包含达到输出上限的请求。"
    text+=('前面的全量均值包含这些截断负载，不能解释为完整工具步骤加速。' if report['length_censored_triplets'] else '本次测量没有输出截断。')+'\n\n'
    text+='| 请求前缀 | 纯 27B 秒 / token | 串行秒 / token | 并行秒 / token |\n|---|---:|---:|---:|\n'
    for index in sorted({r['index'] for r in measured}):
        text+=f'| {index} |'
        for arm in arms:
            rows=[r for r in measured if r['index']==index and r['arm']==arm]
            text+=f" {statistics.mean(r['request']['wall_seconds'] for r in rows):.3f} / {statistics.mean(r['result']['usage']['completion_tokens'] for r in rows):.1f} |"
        text+='\n'
    if complete:
        means={arm:report['completed_triplet_summary'][arm]['mean_request_seconds'] for arm in arms}
        text+=f"\n仅保留三组都未截断的 {len(complete)} 组配对：平均请求墙钟 target={means['target']:.3f}s，serial={means['serial']:.3f}s，overlap={means['overlap']:.3f}s；并行相对串行缩短 {100*(1-means['overlap']/means['serial']):.2f}%，相对纯 27B 缩短 {100*(1-means['overlap']/means['target']):.2f}%（负值代表变慢）。\n"
    text+='\n并行可能改变后端批次形状以及模型输出；相同请求不保证生成相同 token。特别是长 reasoning 的输出长度变化，不能全部归因于 overlap。请求接受率也不是解题通过率。\n'
    (out/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    path=out/'comparison.md'
    original=path.read_text().split('\n## 输出截断与工作量说明')[0]
    path.write_text(original+text)
    print(text,flush=True)


async def run(args):
    out=Path(args.output).resolve()
    out.mkdir(parents=True,exist_ok=False)
    report=dict(kind='paired_request_replay_not_task_accuracy',routing_policy='first_ready_v2',warmup='one token per backend and arm; first measured repetition excluded',
                config=vars(args),calls=[])
    async with httpx.AsyncClient(trust_env=False,timeout=1800) as client:
        gates={arm:Gate(Config(draft_context=32768,overlap_target=arm=='overlap',log_dir=str(out/arm)),client)
               for arm in ['target','serial','overlap']}
        cases=json.loads(Path(args.inputs).read_text())
        for case in cases:
            salts={arm:uuid.uuid4().hex for arm in gates}
            for repeat in range(args.repeat):
                arms=list(gates)
                arms=arms[repeat%3:]+arms[:repeat%3]
                for arm in arms:
                    gate=gates[arm]
                    payload=copy.deepcopy(case['payload'])
                    payload.pop('max_completion_tokens',None)
                    payload.pop('stream_options',None)
                    payload.update(max_tokens=args.tokens,stream=False,cache_salt=salts[arm],temperature=0,top_p=1)
                    # Equalize prefix cache warming without showing future turns.
                    await gate.request(gate.config.target_url,'/v1/chat/completions',dict(payload,max_tokens=1),{})
                    draft=stage_payload(payload,gate.config.draft_model,1,gate.config.draft_thinking)
                    await gate.request(gate.config.draft_url,'/v1/chat/completions',draft,{})
                    start=time.time()
                    if arm=='target':
                        record=dict(id=uuid.uuid4().hex,started_at=start,stages=[],route='target_only')
                        result=await gate.target(record,payload,{},asyncio.Queue())
                        record.update(ended_at=time.time(),wall_seconds=time.time()-start)
                        gate.log(record)
                    else:
                        result=await gate.run(payload,{'x-pro-session':f'probe-{arm}-{case["index"]}'},asyncio.Queue())
                        record=json.loads((out/arm/'requests.jsonl').read_text().splitlines()[-1])
                    after=time.monotonic()
                    counts=[]
                    while True:
                        target_count,draft_count=await asyncio.gather(running(client,gate.config.target_url),running(client,gate.config.draft_url))
                        count=target_count+draft_count
                        counts.append(dict(after_return_seconds=time.monotonic()-after,running_or_waiting=count,
                                           target_running_or_waiting=target_count,draft_running_or_waiting=draft_count))
                        if count==0 or time.monotonic()-after>5:break
                        await asyncio.sleep(.05)
                    row=dict(index=case['index'],repeat=repeat,arm=arm,request=record,
                             result=result,post_return_backend_requests=counts,
                             original_request_id=case['original_request_id'])
                    report['calls'].append(row)
                    (out/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                    print(case['index'],repeat,arm,record['route'],round(record['wall_seconds'],3),
                          result['choices'][0]['finish_reason'],record.get('background_target_disposition'),flush=True)
        measured=[r for r in report['calls'] if r['repeat']>0]
        report['summary']={arm:summarize([r['request'] for r in measured if r['arm']==arm]) for arm in gates}
        for arm in ['serial','overlap']:
            report['summary'][arm]['mean_request_wall_reduction_vs_target_percent']=100*(1-report['summary'][arm]['mean_request_seconds']/report['summary']['target']['mean_request_seconds'])
        report['overlap_wall_reduction_vs_serial_percent']=100*(1-report['summary']['overlap']['mean_request_seconds']/report['summary']['serial']['mean_request_seconds'])
        report['returned_tool_call_arguments_equal_serial_overlap']=sum(
            [c.get('function') for c in a['result']['choices'][0]['message'].get('tool_calls') or []]
            ==[c.get('function') for c in b['result']['choices'][0]['message'].get('tool_calls') or []]
            for a in measured if a['arm']=='serial' for b in measured
            if b['arm']=='overlap' and (a['index'],a['repeat'])==(b['index'],b['repeat']))
        (out/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        text='# 串行与并行竞速语义审核：真实请求前缀重放\n\n'
        text+='并行使用 first_ready_v2：27B 先完成立即返回；8B 候选先通过审核则返回候选；另一分支取消。\n\n'
        text+='8B 普通 TP4 / 32768；27B DFlash TP4 / 262144。相同原始历史、采样参数及输出上限；每个输入每组单独缓存盐并预热，剔除首次完整生成。只生成、不执行工具，不能据此判断 SWE 通过率或整题加速。\n\n'
        text+='| 方式 | 请求数 | 平均墙钟秒 | 候选接受率 | 相对纯 27B 缩短 |\n|---|---:|---:|---:|---:|\n'
        for arm,s in report['summary'].items():
            text+=f"| {arm} | {s['requests']} | {s['mean_request_seconds']:.3f} | {s['acceptance_percent']} | {s.get('mean_request_wall_reduction_vs_target_percent',0):.2f}% |\n"
        text+=f"\n并行相对串行平均请求墙钟缩短 {report['overlap_wall_reduction_vs_serial_percent']:.2f}%。分母是串行版本平均请求墙钟。\n"
        text+='\n后台生成取消后的请求队列观测、逐阶段时间和 token 明细见 comparison.json；被取消的部分 usage 只作下界，不能当完整消耗。\n'
        (out/'comparison.md').write_text(text)
        print(text,flush=True)
        annotate_report(out,report)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--repeat',type=int,default=3)
    parser.add_argument('--tokens',type=int,default=4096)
    parser.add_argument('--summarize-only',action='store_true',help='Rebuild censoring/completed-triplet analysis from existing comparison.json; no model calls')
    args=parser.parse_args()
    if args.repeat<2:parser.error('At least two repetitions: first is excluded as warmup')
    if args.summarize_only:
        annotate_report(Path(args.output),json.loads((Path(args.output)/'comparison.json').read_text()))
    else:
        asyncio.run(run(args))
