#!/usr/bin/env python3
"""Compare 8B thinking modes on identical saved histories without executing tools.

Matched sampling isolates the thinking switch. Greedy non-thinking reproduces
the earlier draft setting. Repeated short reviews expose reviewer variability.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import time
import uuid

import httpx

if __package__:
    from .proxy import Config, Gate, stage_payload, validate_candidate
else:
    from proxy import Config, Gate, stage_payload, validate_candidate


def summarize(report):
    summary={}
    for arm in ['off_greedy','off_matched','on_matched']:
        rows=[r for r in report['calls'] if r['arm']==arm]
        if not rows:continue
        usage=[r['stages'][0]['usage'] for r in rows]
        votes=[v for r in rows for v in r['reviews']]
        summary[arm]=dict(calls=len(rows),valid_tool_calls=sum(r['local_rejection'] is None for r in rows),
            length_stopped=sum(r['stages'][0]['finish_reason']=='length' for r in rows),
            majority_accepted=sum(r['majority_accepted'] for r in rows),
            majority_accepted_percent=100*sum(r['majority_accepted'] for r in rows)/len(rows),
            mean_completion_tokens=statistics.mean(u['completion_tokens'] for u in usage),
            mean_reasoning_tokens=statistics.mean((u.get('completion_tokens_details') or {}).get('reasoning_tokens',0) for u in usage),
            mean_draft_seconds=statistics.mean(r['stages'][0]['wall_seconds'] for r in rows),
            mean_decode_tps=statistics.mean((r['stages'][0]['usage']['completion_tokens']-1)/(r['stages'][0]['metrics']['generation_time_ms']/1000) for r in rows if r['stages'][0]['usage']['completion_tokens']>1),
            mean_review_seconds=statistics.mean(v['wall_seconds'] for v in votes) if votes else None,
            review_votes=len(votes),review_accepted_votes=sum(v['accepted'] for v in votes),
            candidates_with_disagreeing_reviews=sum(len({v['accepted'] for v in r['reviews']})>1 for r in rows))
    report['summary']=summary
    return summary


def save(out, report):
    summary=summarize(report)
    (out/'thinking-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    text='# 8B thinking 开关：候选审核与时长\n\n'
    text+='相同历史前缀，不执行工具。off_matched / on_matched 使用相同 temperature=0.6、top_p=0.95、top_k=20、min_p=0 和成对 seed；仅 thinking 开关不同。off_greedy 复现此前 temperature=0、top_p=1 设置。所有组总输出预算相同，包含 reasoning。为避免把 thinking 与 greedy 的组合缺陷混入主比较，不运行 thinking+greedy。\n\n'
    text+=f"本次上限 {report['config']['tokens']} token（实际代理旧默认 2048）；每个有效候选审核 {report['config']['reviews']} 次，审核保持原 prompt、不看 draft reasoning、不执行工具。不同 nonce 引起的判决波动另外记录；多数票仅供本次诊断，未修改在线审核策略。单次生成前两端一 token 预热，耗时不含预热；不等于竞速端到端收益或 SWE 通过率。\n\n"
    text+='| 组别 | 生成数 | 完整合法工具候选 | 达到上限 | 多数票接受 | 平均生成秒 | 平均输出 token | 其中 reasoning | 平均 decode TPS |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n'
    for arm,s in summary.items():
        text+=f"| {arm} | {s['calls']} | {s['valid_tool_calls']} | {s['length_stopped']} | {s['majority_accepted']}/{s['calls']} | {s['mean_draft_seconds']:.3f} | {s['mean_completion_tokens']:.1f} | {s['mean_reasoning_tokens']:.1f} | {s['mean_decode_tps']:.2f} |\n"
    text+='\n| 前缀 | seed | 组别 | 输出 token | 秒 | 结束原因 | A 票 / 审核数 | 本地拒绝原因 |\n|---|---:|---|---:|---:|---|---|---|\n'
    for r in report['calls']:
        s=r['stages'][0]
        text+=f"| {r['index']} | {r['seed']} | {r['arm']} | {s['usage']['completion_tokens']} | {s['wall_seconds']:.3f} | {s['finish_reason']} | {sum(v['accepted'] for v in r['reviews'])}/{len(r['reviews'])} | {r['local_rejection'] or '—'} |\n"
    text+='\nQwen 官方建议 thinking 模式使用非 greedy 采样：https://huggingface.co/Qwen/Qwen3-8B#best-practices\n'
    (out/'thinking-comparison.md').write_text(text)


async def run(args):
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    inputs=Path(args.inputs).read_bytes()
    report=dict(kind='paired_candidate_review_not_swe_accuracy',config=vars(args),
                input_sha256=hashlib.sha256(inputs).hexdigest(),started_at=time.time(),calls=[])
    arms=[('off_greedy',False,0,1),('off_matched',False,.6,.95),('on_matched',True,.6,.95)]
    async with httpx.AsyncClient(trust_env=False,timeout=1800) as client:
        gate=Gate(Config(draft_context=32768,log_dir=str(out/'internal')),client)
        for case in json.loads(inputs):
            salts={arm:uuid.uuid4().hex for arm,_,_,_ in arms}
            for repeat in range(args.repeat):
                seed=20260913+repeat
                order=arms[repeat%len(arms):]+arms[:repeat%len(arms)]
                for arm,thinking,temperature,top_p in order:
                    original=dict(case['payload'],cache_salt=salts[arm])
                    draft=stage_payload(original,gate.config.draft_model,args.tokens,thinking)
                    draft.update(temperature=temperature,top_p=top_p,top_k=20,min_p=0,seed=seed)
                    count=await gate.tokenize(draft,gate.config.draft_url,{})
                    if count+args.tokens>32768:
                        raise ValueError(f'{case["index"]} exceeds native context; no compression allowed in this paired test')
                    await gate.request(gate.config.draft_url,'/v1/chat/completions',dict(draft,max_tokens=1),{})
                    warm=stage_payload(original,gate.config.target_model,1,False)
                    await gate.request(gate.config.target_url,'/v1/chat/completions',warm,{})
                    row=dict(index=case['index'],original_request_id=case['original_request_id'],
                             repeat=repeat,seed=seed,arm=arm,thinking=thinking,stages=[],reviews=[])
                    result=await gate.call(row,'draft',draft,gate.config.draft_url,{})
                    row['candidate']=result['choices'][0]['message']
                    row['local_rejection']=validate_candidate(original,result)
                    if row['local_rejection'] is None:
                        for review in range(args.reviews):
                            verdict=dict(stages=[])
                            start=time.monotonic()
                            accepted=await gate.review_candidate(verdict,original,row['candidate'],{})
                            verdict.update(accepted=accepted,wall_seconds=time.monotonic()-start)
                            row['reviews'].append(verdict)
                    row['majority_accepted']=bool(row['reviews']) and sum(v['accepted'] for v in row['reviews'])>len(row['reviews'])/2
                    report['calls'].append(row);save(out,report)
                    s=row['stages'][0]
                    print(case['index'],repeat,arm,'tokens',s['usage']['completion_tokens'],'seconds',round(s['wall_seconds'],3),s['finish_reason'],
                          'review',sum(v['accepted'] for v in row['reviews']), '/',len(row['reviews']),flush=True)
    report['ended_at']=time.time();save(out,report)
    print(json.dumps(report['summary'],ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--tokens',type=int,default=4096)
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--reviews',type=int,default=3)
    args=parser.parse_args()
    if min(args.tokens,args.repeat,args.reviews)<1:parser.error('Budgets and repeat counts must be positive')
    asyncio.run(run(args))
