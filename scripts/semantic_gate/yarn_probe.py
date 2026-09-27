#!/usr/bin/env python3
"""Compare compression thresholds on ONE YaRN backend; not a native/YaRN ablation.

Replays saved request prefixes without executing tools. Each arm gets a fresh
gateway summary cache and prefix-cache salt. End-to-end Pro remains a separate run.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
import uuid

import httpx

if __package__:
    from .proxy import Config, Gate
    from .overlap_probe import running
    from .metrics import summarize
else:
    from proxy import Config, Gate
    from overlap_probe import running
    from metrics import summarize


def save(out, report):
    report['summary']={arm:summarize([r['request'] for r in report['calls'] if r['arm']==arm])
                       for arm in ['threshold32k','threshold128k']}
    text='# YaRN 后端：32K 与 128K 压缩门限配对重放\n\n'
    text+='两组使用同一个 TP4、YaRN factor=4 的 8B 后端；仅代理容量/压缩门限不同。不是原生 32K 模型与 YaRN 模型的直接质量比较。8B thinking=true、8192 输出，27B TP4 DFlash 同步生成。\n\n'
    text+='真实历史前缀，不执行工具；每次新摘要缓存和 cache_salt，测量包含冷历史 prefill；启动前做短请求预热。两次重复交换顺序，全部计入。这是小样本请求测试，不能代表 SWE 通过率或整题加速。\n\n'
    text+='| 门限 | 请求数 | 平均秒 | 候选采用率 | 压缩调用数 | 压缩取消数 |\n|---|---:|---:|---:|---:|---:|\n'
    for arm,s in report['summary'].items():
        if not s['requests']:continue
        text+=f"| {arm} | {s['requests']} | {s['mean_request_seconds']:.3f} | {s['acceptance_percent']:.2f}% | {s['phases']['compress']['calls']} | {s['phases']['compress']['cancelled_calls']} |\n"
    text+='\n| 历史 | 8B 原始输入 | 重复 | 门限 | 秒 | 路由 | 结束原因 |\n|---|---:|---:|---|---:|---|---|\n'
    for r in report['calls']:
        text+=f"| {r['index']} | {r['original_prompt_tokens']} | {r['repeat']} | {r['arm']} | {r['request']['wall_seconds']:.3f} | {r['request'].get('route')} | {r['finish_reason']} |\n"
    if all(s['requests'] for s in report['summary'].values()):
        a=report['summary']['threshold32k']['mean_request_seconds']
        b=report['summary']['threshold128k']['mean_request_seconds']
        text+=f'\n平均等待时间变化：{100*(b/a-1):+.2f}%（分母为 32K 门限均值；正值变慢）。只在全部配对完成后比较，长度截断须单列。\n'
    (out/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (out/'comparison.md').write_text(text)


async def run(args):
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    cases=json.loads(Path(args.inputs).read_text())
    report=dict(kind='same_yarn_backend_threshold_ablation_not_end_to_end',started_at=time.time(),config=vars(args),calls=[])
    async with httpx.AsyncClient(trust_env=False,timeout=1800) as client:
        async def warm(url, model):
            response=await client.post(url+'/v1/chat/completions',json=dict(model=model,
                messages=[dict(role='user',content='Return OK.')],max_tokens=1,temperature=0))
            response.raise_for_status()
        await asyncio.gather(warm('http://127.0.0.1:8000','qwen3.8-27b'),warm('http://127.0.0.1:8001','qwen3-8b'))
        for repeat in range(args.repeat):
            for case in cases:
                arms=[('threshold32k',32768),('threshold128k',131072)]
                if repeat%2:arms.reverse()
                for arm,context in arms:
                    gate=Gate(Config(draft_context=context,draft_tokens=8192,draft_thinking=True,
                        overlap_target=True,log_dir=str(out/arm)),client)
                    payload=dict(case['payload'],stream=False,cache_salt=uuid.uuid4().hex)
                    response=await gate.run(payload,{'x-pro-session':str(case['index'])},asyncio.Queue())
                    record=json.loads((out/arm/'requests.jsonl').read_text().splitlines()[-1])
                    deadline=time.monotonic()+10
                    while True:
                        counts=await asyncio.gather(running(client,gate.config.target_url),running(client,gate.config.draft_url))
                        if not sum(counts):break
                        if time.monotonic()>deadline:raise RuntimeError('Backend still busy after cancellation; stop paired timing')
                        await asyncio.sleep(.1)
                    report['calls'].append(dict(index=case['index'],repeat=repeat,arm=arm,
                        original_request_id=case['original_request_id'],original_prompt_tokens=case['original_8b_prompt_tokens'],
                        finish_reason=response['choices'][0]['finish_reason'],result=response,request=record))
                    save(out,report)
                    print(case['index'],repeat,arm,record['route'],round(record['wall_seconds'],3),flush=True)
    report['ended_at']=time.time();save(out,report)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--repeat',type=int,default=2)
    args=parser.parse_args()
    if args.repeat<1:parser.error('repeat must be positive')
    asyncio.run(run(args))
