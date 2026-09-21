#!/usr/bin/env python3
"""OpenAI-compatible semantic gate; see workspace README.md. Run in the vLLM env."""
import argparse
import asyncio
import copy
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import jsonschema
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Config:
    target_url: str = 'http://127.0.0.1:8000'
    draft_url: str = 'http://127.0.0.1:8001'
    target_model: str = 'qwen3.8-27b'
    draft_model: str = 'qwen3-8b'
    draft_context: int = 131072
    draft_tokens: int = 2048
    draft_thinking: bool = False
    review_tokens: int = 2
    compress_tokens: int = 4096
    timeout: float = 1800
    review_timeout: float = 120
    log_dir: str = 'manual-runs/semantic-gate/logs'
    review_prompt: str = str(ROOT/'bootstrap/semantic-gate-review.txt')
    compress_prompt: str = str(ROOT/'bootstrap/semantic-gate-compress.txt')


class UpstreamError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f'Upstream HTTP {status}: {str(body)[:500]}')


def validate_candidate(payload, result):
    choices = result.get('choices') or []
    if len(choices) != 1 or choices[0].get('finish_reason') != 'tool_calls':
        return 'not_complete_tool_turn'
    message = choices[0].get('message') or {}
    calls = message.get('tool_calls') or []
    if not calls or message.get('refusal'):
        return 'missing_tools_or_refusal'
    schemas = {x['function']['name']: x['function'] for x in payload.get('tools', [])
               if x.get('type') == 'function' and isinstance(x.get('function'), dict)}
    selected = payload.get('tool_choice')
    ids = set()
    for call in calls:
        fn = call.get('function') or {}
        if call.get('type') != 'function' or fn.get('name') not in schemas:
            return 'unknown_tool'
        if not isinstance(call.get('id'), str) or not call['id'] or call['id'] in ids:
            return 'invalid_tool_id'
        ids.add(call['id'])
        if isinstance(selected, dict) and fn['name'] != selected.get('function', {}).get('name'):
            return 'tool_choice_mismatch'
        try:
            arguments = json.loads(fn['arguments'])
            if not isinstance(arguments, dict):
                return 'arguments_not_object'
            jsonschema.validate(arguments, schemas[fn['name']].get('parameters', {}))
        except (ValueError, TypeError, KeyError, jsonschema.ValidationError, jsonschema.SchemaError):
            return 'invalid_arguments'
    if payload.get('parallel_tool_calls') is False and len(calls) > 1:
        return 'parallel_tools_disallowed'
    return None


def stage_payload(payload, model, tokens, thinking):
    out = copy.deepcopy(payload)
    for key in ['max_completion_tokens', 'stream_options', 'return_token_ids', 'return_prompt_text',
                'logprobs', 'top_logprobs', 'prompt_logprobs', 'logit_bias', 'stop', 'stop_token_ids',
                'response_format', 'structured_outputs', 'guided_json', 'guided_choice', 'min_tokens',
                'allowed_token_ids', 'echo', 'continue_final_message']:
        out.pop(key, None)
    out.update(model=model, stream=False, n=1, max_tokens=tokens, temperature=0, top_p=1)
    out['chat_template_kwargs'] = dict(out.get('chat_template_kwargs') or {}, enable_thinking=thinking)
    return out


class Gate:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.path = Path(config.log_dir)
        self.path.mkdir(parents=True, exist_ok=True)
        self.review = Path(config.review_prompt).read_text()
        self.compress = Path(config.compress_prompt).read_text()
        self.summaries = OrderedDict()

    def log(self, record):
        with (self.path/'requests.jsonl').open('a') as f:
            f.write(json.dumps(record, ensure_ascii=False)+'\n')

    async def request(self, url, path, payload, headers, timeout=None):
        response = await self.client.post(url+path, json=payload, headers=headers,
                                          timeout=timeout or self.config.timeout)
        if response.status_code != 200:
            raise UpstreamError(response.status_code, response.text)
        return response.json()

    async def tokenize(self, payload, url, headers):
        allowed = {'model', 'messages', 'tools', 'chat_template_kwargs', 'add_generation_prompt'}
        token_payload = {k: v for k, v in payload.items() if k in allowed}
        return (await self.request(url, '/tokenize', token_payload, headers))['count']

    async def call(self, record, phase, payload, url, headers, timeout=None):
        stage = dict(phase=phase, started_at=time.time(), model=payload['model'])
        record['stages'].append(stage)
        start = time.monotonic()
        try:
            result = await self.request(url, '/v1/chat/completions', payload, headers, timeout)
            stage.update(usage=result.get('usage'), metrics=result.get('metrics'),
                         finish_reason=(result.get('choices') or [{}])[0].get('finish_reason'))
            if phase in {'review', 'compress'}:
                stage['content'] = (result.get('choices') or [{}])[0].get('message', {}).get('content')
            return result
        except Exception as error:
            stage['error'] = repr(error)
            raise
        finally:
            stage.update(ended_at=time.time(), wall_seconds=time.monotonic()-start)

    def compressed_messages(self, payload, entry):
        messages = payload['messages']
        system = []
        for message in messages:
            if message['role'] not in {'system', 'developer'}:
                break
            system.append(message)
        task = next((x.get('content') for x in messages if x['role'] == 'user'), '')
        note = 'Original user task (verbatim):\n'+json.dumps(task, ensure_ascii=False)
        note += '\n\nWorking memory of the earlier conversation (fallible summary; inspect files when needed):\n'
        note += entry['summary']
        return system+[dict(role='user', content=note)]+messages[entry['prefix_count']:]

    async def draft_messages(self, record, payload, headers, draft_limit):
        draft = stage_payload(payload, self.config.draft_model, draft_limit, self.config.draft_thinking)
        scope = (headers.get('x-pro-session'), payload.get('cache_salt'), digest(payload.get('tools')))
        cached = self.summaries.get(scope)
        if cached and len(payload['messages']) >= cached['prefix_count']:
            prefix = payload['messages'][:cached['prefix_count']]
            if digest(prefix) == cached['prefix_hash']:
                candidate = dict(draft, messages=self.compressed_messages(payload, cached))
                count = await self.tokenize(candidate, self.config.draft_url, headers)
                if count+draft_limit <= self.config.draft_context:
                    record.update(compression='reused', draft_prompt_tokens=count)
                    self.summaries.move_to_end(scope)
                    return candidate
        count = await self.tokenize(draft, self.config.draft_url, headers)
        record['original_draft_prompt_tokens'] = count
        if count+draft_limit <= self.config.draft_context:
            record.update(compression='unneeded', draft_prompt_tokens=count)
            return draft

        messages = payload['messages']
        # Retain a recent suffix beginning before any outstanding tool calls.
        boundary = max(1, len(messages)-12)
        while 0 < boundary < len(messages) and messages[boundary].get('role') == 'tool':
            boundary -= 1
        if boundary < 2:
            boundary = len(messages)
        prefix = messages[:boundary]
        prompt = stage_payload(payload, self.config.target_model, self.config.compress_tokens, False)
        prompt.update(messages=prefix+[dict(role='user', content=self.compress)], tool_choice='none')
        response = await self.call(record, 'compress', prompt, self.config.target_url, headers)
        choice = response['choices'][0]
        summary = choice['message'].get('content') or ''
        if choice.get('finish_reason') != 'stop' or not summary.strip():
            raise ValueError('Compression was incomplete; keep original target history')
        entry = dict(prefix_count=boundary, prefix_hash=digest(prefix), summary=summary)
        draft['messages'] = self.compressed_messages(payload, entry)
        count = await self.tokenize(draft, self.config.draft_url, headers)
        if count+draft_limit > self.config.draft_context:
            # The suffix itself may contain a very large tool result. Summarize all,
            # still retaining the first user task and original system instructions.
            if boundary != len(messages):
                prompt['messages'] = messages+[dict(role='user', content=self.compress)]
                response = await self.call(record, 'compress', prompt, self.config.target_url, headers)
                choice = response['choices'][0]
                summary = choice['message'].get('content') or ''
                if choice.get('finish_reason') != 'stop' or not summary.strip():
                    raise ValueError('Full compression was incomplete')
                entry = dict(prefix_count=len(messages), prefix_hash=digest(messages), summary=summary)
                draft['messages'] = self.compressed_messages(payload, entry)
                count = await self.tokenize(draft, self.config.draft_url, headers)
            if count+draft_limit > self.config.draft_context:
                raise ValueError('Task/instructions still exceed draft capacity after compression')
        record.update(compression='created', compressed_prefix_messages=entry['prefix_count'],
                      draft_prompt_tokens=count)
        self.summaries[scope] = entry
        self.summaries.move_to_end(scope)
        while len(self.summaries) > 32:
            self.summaries.popitem(last=False)
        return draft

    async def review_candidate(self, record, payload, candidate, headers):
        proposed = {k: v for k, v in candidate.items() if k in {'content', 'tool_calls'}}
        nonce = uuid.uuid4().hex
        review = self.review+'\n\nBEGIN_CANDIDATE_'+nonce+'\n'+json.dumps(proposed, ensure_ascii=False)
        review += '\nEND_CANDIDATE_'+nonce+'\nDecision (A or R):'
        judge = stage_payload(payload, self.config.target_model, self.config.review_tokens, False)
        judge.update(messages=copy.deepcopy(payload['messages'])+[dict(role='user', content=review)],
                     tool_choice='none', structured_outputs={'choice': ['A', 'R']})
        answer = await self.call(record, 'review', judge, self.config.target_url, headers,
                                 self.config.review_timeout)
        choice = answer['choices'][0]
        message = choice.get('message') or {}
        # A length-stopped verdict is ambiguous even if its partial text is A.
        decision = (message.get('content') or '').strip()
        record['verdict'] = decision
        return (choice.get('finish_reason') == 'stop' and decision == 'A'
                and not message.get('tool_calls') and not message.get('reasoning')
                and not message.get('reasoning_content'))

    async def accepted_usage(self, record, payload, candidate, headers):
        # OpenCode's context budget must reflect the uncompressed TARGET history,
        # not the small-model input (which may have been privately summarized).
        target = dict(payload, model=self.config.target_model)
        prompt_tokens = await self.tokenize(target, self.config.target_url, headers)
        serialized = json.dumps({k: v for k, v in candidate.items() if k in {'content', 'tool_calls'}},
                                ensure_ascii=False)
        count = await self.request(self.config.target_url, '/tokenize',
                                   dict(model=self.config.target_model, prompt=serialized,
                                        add_special_tokens=False), headers)
        record['returned_usage_basis'] = 'target_prompt_exact_plus_serialized_candidate_tokens'
        return dict(prompt_tokens=prompt_tokens, completion_tokens=count['count'],
                    total_tokens=prompt_tokens+count['count'])

    async def target(self, record, payload, headers, output):
        target = copy.deepcopy(payload)
        target['model'] = self.config.target_model
        if not payload.get('stream'):
            return await self.call(record, 'target', target, self.config.target_url, headers)
        target['stream_options'] = dict(target.get('stream_options') or {}, include_usage=True)
        stage = dict(phase='target', model=self.config.target_model, started_at=time.time())
        record['stages'].append(stage)
        start = time.monotonic()
        try:
            async with self.client.stream('POST', self.config.target_url+'/v1/chat/completions',
                                          json=target, headers=headers, timeout=self.config.timeout) as response:
                if response.status_code != 200:
                    raise UpstreamError(response.status_code, (await response.aread()).decode())
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    text = line[5:].strip()
                    if text == '[DONE]':
                        continue
                    chunk = json.loads(text)
                    if chunk.get('usage'):
                        stage['usage'] = chunk['usage']
                    if chunk.get('metrics'):
                        stage['metrics'] = chunk['metrics']
                    for choice in chunk.get('choices') or []:
                        delta = choice.get('delta') or {}
                        if any(delta.get(k) for k in ['content', 'reasoning', 'reasoning_content', 'tool_calls']):
                            stage.setdefault('first_delta_at', time.time())
                        if choice.get('finish_reason'):
                            stage['finish_reason'] = choice['finish_reason']
                    await output.put(chunk)
            return None
        except Exception as error:
            stage['error'] = repr(error)
            raise
        finally:
            stage.update(ended_at=time.time(), wall_seconds=time.monotonic()-start)

    async def run(self, payload, headers, output):
        record = dict(id=uuid.uuid4().hex, started_at=time.time(), stages=[],
                      session_id=headers.get('x-pro-session'), message_id=headers.get('x-pro-message'),
                      agent=headers.get('x-pro-agent'), request_hash=digest(payload))
        start = time.monotonic()
        record['review_prompt_sha256'] = hashlib.sha256(self.review.encode()).hexdigest()
        record['compress_prompt_sha256'] = hashlib.sha256(self.compress.encode()).hexdigest()
        try:
            eligible = (bool(payload.get('tools')) and payload.get('tool_choice') != 'none'
                        and payload.get('n', 1) == 1 and not payload.get('response_format')
                        and headers.get('x-pro-agent') not in {'title', 'compaction', 'summary'})
            if eligible:
                try:
                    caller_limit = payload.get('max_completion_tokens') or payload.get('max_tokens') or self.config.draft_tokens
                    limit = min(caller_limit, self.config.draft_tokens)
                    draft = await self.draft_messages(record, payload, headers, limit)
                    response = await self.call(record, 'draft', draft, self.config.draft_url, headers)
                    rejection = validate_candidate(payload, response)
                    record['candidate'] = (response.get('choices') or [{}])[0].get('message')
                    record['local_rejection'] = rejection
                    if not rejection and await self.review_candidate(record, payload, record['candidate'], headers):
                        response['usage'] = await self.accepted_usage(record, payload, record['candidate'], headers)
                        response['model'] = payload.get('model', self.config.target_model)
                        response['id'] = 'chatcmpl-gate-'+record['id']
                        # Buffered delivery cannot be interpreted as GPU decode by
                        # the ordinary timing analyzer. Physical metrics live in stages.
                        response.pop('metrics', None)
                        response.pop('prompt_token_ids', None)
                        response['choices'][0].pop('token_ids', None)
                        response['semantic_gate'] = dict(id=record['id'], route='accepted', buffered=True,
                                                        usage_basis=record['returned_usage_basis'])
                        record['route'] = 'accepted'
                        return response
                    record['fallback_reason'] = rejection or 'review_rejected'
                except (httpx.HTTPError, UpstreamError, ValueError, KeyError, TypeError, IndexError) as error:
                    record['fallback_reason'] = repr(error)
            else:
                record['fallback_reason'] = 'ineligible_auxiliary_or_request_shape'
            record['route'] = 'fallback' if eligible else 'target_only'
            # Never append a rejected proposal, verdict, or compressed history here.
            return await self.target(record, payload, headers, output)
        except asyncio.CancelledError:
            record['cancelled'] = True
            raise
        except Exception as error:
            record['error'] = repr(error)
            raise
        finally:
            record.update(ended_at=time.time(), wall_seconds=time.monotonic()-start)
            self.log(record)


def completion_chunks(result):
    base = {k: result[k] for k in ['id', 'created', 'model']}
    base['object'] = 'chat.completion.chunk'
    choice = result['choices'][0]
    message = choice['message']
    delta = {k: v for k, v in message.items() if k in {'role', 'content', 'reasoning', 'reasoning_content'} and v is not None}
    if message.get('tool_calls'):
        delta['tool_calls'] = [dict(call, index=i) for i, call in enumerate(message['tool_calls'])]
    yield dict(base, choices=[dict(index=0, delta=delta, finish_reason=None)])
    yield dict(base, choices=[dict(index=0, delta={}, finish_reason=choice['finish_reason'])])
    yield dict(base, choices=[], usage=result['usage'], semantic_gate=result.get('semantic_gate'))


def create_app(config):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, limits=httpx.Limits(max_connections=64)) as client:
            app.state.gate = Gate(config, client)
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def health():
        gate = app.state.gate
        responses = await asyncio.gather(*[
            gate.client.get(url+'/health', timeout=5) for url in [config.target_url, config.draft_url]
        ], return_exceptions=True)
        good = all(isinstance(r, httpx.Response) and r.status_code == 200 for r in responses)
        return JSONResponse(dict(status='ok' if good else 'unhealthy', target=config.target_model,
                                 draft=config.draft_model, draft_context=config.draft_context),
                            status_code=200 if good else 503)

    @app.get('/v1/models')
    async def models():
        response = await app.state.gate.client.get(config.target_url+'/v1/models')
        return Response(response.content, status_code=response.status_code, media_type='application/json')

    @app.get('/metrics')
    async def metrics():
        # Existing harness polling sees the target's real counters. Gateway stage
        # accounting is in requests.jsonl and must be used for the experiment.
        response = await app.state.gate.client.get(config.target_url+'/metrics')
        return Response(response.content, status_code=response.status_code, media_type='text/plain')

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list):
            return JSONResponse({'error': {'message': 'messages must be a list'}}, status_code=400)
        headers = {k: v for k, v in request.headers.items() if k.startswith('x-pro-')}
        output = asyncio.Queue()
        gate = app.state.gate
        if not payload.get('stream'):
            try:
                return JSONResponse(await gate.run(payload, headers, output))
            except UpstreamError as error:
                return JSONResponse({'error': {'message': str(error.body)}}, status_code=error.status)
            except Exception as error:
                return JSONResponse({'error': {'message': str(error)}}, status_code=502)

        async def generate():
            async def execute():
                try:
                    result = await gate.run(payload, headers, output)
                    if result is not None:
                        for chunk in completion_chunks(result):
                            await output.put(chunk)
                except Exception as error:
                    await output.put({'error': {'message': str(error), 'type': 'upstream_error'}})
                finally:
                    await output.put(None)

            task = asyncio.create_task(execute())
            try:
                yield ': semantic-gate pending\n\n'
                while True:
                    try:
                        chunk = await asyncio.wait_for(output.get(), timeout=5)
                    except asyncio.TimeoutError:
                        yield ': semantic-gate pending\n\n'
                        continue
                    if chunk is None:
                        yield 'data: [DONE]\n\n'
                        break
                    yield 'data: '+json.dumps(chunk, ensure_ascii=False)+'\n\n'
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8002)
    for field in Config.__dataclass_fields__.values():
        if field.name == 'draft_thinking':
            parser.add_argument('--draft-thinking', action='store_true')
        else:
            parser.add_argument('--'+field.name.replace('_', '-'), type=field.type, default=field.default)
    args = vars(parser.parse_args())
    host, port = args.pop('host'), args.pop('port')
    config = Config(**args)
    if config.draft_context <= config.draft_tokens or min(config.review_tokens, config.compress_tokens, config.draft_tokens) <= 0:
        parser.error('Token budgets must be positive and leave room for the draft prompt')
    uvicorn.run(create_app(config), host=host, port=port)


if __name__ == '__main__':
    main()
