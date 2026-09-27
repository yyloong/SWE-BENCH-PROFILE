#!/usr/bin/env python3
"""Replay recorded request prefixes without executing tools or exposing future turns."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx

if __package__:
    from .proxy import Config, Gate, stage_payload, validate_candidate
else:
    from proxy import Config, Gate, stage_payload, validate_candidate


async def run(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config(draft_url=args.draft_url, draft_context=args.context, log_dir=str(out/'internal'))
    report = dict(kind='request_replay_not_task_accuracy', label=args.label, calls=[])
    salts = {}
    async with httpx.AsyncClient(trust_env=False, timeout=1800) as client:
        gate = Gate(cfg, client)
        for case in json.loads(Path(args.inputs).read_text()):
            for repeat in range(args.repeat):
                modes = [False, True] if args.compare_thinking else [False]
                if repeat % 2:
                    modes.reverse()
                for thinking in modes:
                    payload = stage_payload(case['payload'], cfg.draft_model, args.tokens, thinking)
                    payload['cache_salt'] = salts.setdefault(('tool', case['index'], thinking), uuid.uuid4().hex)
                    count = await gate.tokenize(payload, cfg.draft_url, {})
                    if count + args.tokens > args.context:
                        raise ValueError(f"Case {case['index']}: {count}+{args.tokens}>{args.context}")
                    # Separate salt per input/mode; one-token warmup makes the measured
                    # prefill cached equally without changing the input history.
                    warmup = dict(payload, max_tokens=1)
                    await gate.request(cfg.draft_url, '/v1/chat/completions', warmup, {})
                    record = dict(index=case['index'], repeat=repeat, thinking=thinking,
                                  original_request_id=case['original_request_id'], stages=[])
                    result = await gate.call(record, 'draft', payload, cfg.draft_url, {})
                    record.update(candidate=result['choices'][0]['message'],
                                  local_rejection=validate_candidate(case['payload'], result))
                    report['calls'].append(record)
                    (out/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    s = record['stages'][0]
                    u = s['usage']; m = s['metrics']
                    print(args.label, case['index'], 'thinking', thinking,
                          'prompt', u['prompt_tokens'], 'tokens', u['completion_tokens'],
                          'wall', round(s['wall_seconds'], 3), 'tps',
                          round((u['completion_tokens']-1)/(m['generation_time_ms']/1000), 2),
                          s['finish_reason'], flush=True)

        if args.fixed_length_probe:
            # Exact-length synthetic prompts separate long-context decode cost
            # from varying tool-call lengths. No generated command is executed.
            async def encode(text):
                return (await gate.request(cfg.draft_url, '/tokenize',
                        dict(model=cfg.draft_model, prompt=text, add_special_tokens=False), {}))['tokens']
            prefix = await encode('<|im_start|>system\nYou are a Python coding assistant.<|im_end|>\n'
                                  '<|im_start|>user\nRepository reference snippets:\n')
            filler = await encode('def normalize_items(values):\n    return sorted(set(values))\n\n' * 5000)
            suffix = await encode('\nWrite a comprehensive Python unittest module for list utilities, '
                                  'including normalization, grouping, filtering, sorting, and edge cases. '
                                  'Output only Python code.\n<|im_end|>\n<|im_start|>assistant\n'
                                  '<think>\n\n</think>\n\n')
            for length in [1024, 8192, 16384, args.context-512]:
                tokens = prefix+filler[:length-len(prefix)-len(suffix)]+suffix
                assert len(tokens) == length
                for repeat in range(args.repeat):
                    payload = dict(model=cfg.draft_model, prompt=tokens, max_tokens=512,
                                   temperature=0, top_p=1, seed=20260913, stream=False,
                                   ignore_eos=True, cache_salt=salts.setdefault(('fixed', length), uuid.uuid4().hex))
                    await gate.request(cfg.draft_url, '/v1/completions', dict(payload, max_tokens=1), {})
                    start = time.monotonic()
                    result = await gate.request(cfg.draft_url, '/v1/completions', payload, {})
                    row = dict(prompt_tokens=length, repeat=repeat, wall_seconds=time.monotonic()-start,
                               usage=result.get('usage'), metrics=result.get('metrics'),
                               choices=result['choices'])
                    report.setdefault('fixed_length_calls', []).append(row)
                    (out/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(args.label, 'fixed_length', length, 'tps',
                          round((row['usage']['completion_tokens']-1)/(row['metrics']['generation_time_ms']/1000), 2),
                          'wall', round(row['wall_seconds'], 3), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--draft-url', default='http://127.0.0.1:8003')
    parser.add_argument('--context', type=int, default=32768)
    parser.add_argument('--tokens', type=int, default=4096)
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--compare-thinking', action='store_true')
    parser.add_argument('--fixed-length-probe', action='store_true')
    asyncio.run(run(parser.parse_args()))
