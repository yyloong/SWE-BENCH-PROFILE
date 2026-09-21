"""Real local HTTP peers exercise routing; no GPUs or benchmark data are used."""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

try:
    import httpx
    from fastapi.testclient import TestClient
    from scripts.semantic_gate.proxy import Config, Gate, create_app, validate_candidate
except ImportError as error:
    raise unittest.SkipTest('Run gateway tests in the vLLM Python environment: '+str(error))


TOOLS = [dict(type='function', function=dict(name='read', parameters={
    'type':'object', 'properties':{'path':{'type':'string'}}, 'required':['path'], 'additionalProperties':False}))]
CALL = dict(id='call_1', type='function', function=dict(name='read', arguments='{"path":"src/app.py"}'))


def completion(model, message, reason='stop'):
    return dict(id='chatcmpl-test', object='chat.completion', created=1, model=model,
                choices=[dict(index=0, message=message, finish_reason=reason)],
                usage=dict(prompt_tokens=50, completion_tokens=5, total_tokens=55),
                metrics=dict(generation_time_ms=10, time_to_first_token_ms=5))


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        state=self.server.state
        state['requests'].append((self.path, payload))
        if self.path=='/tokenize':
            count=len(json.dumps(payload.get('messages', payload.get('prompt',''))).split())
            data=dict(count=count,max_model_len=4096,tokens=[])
        else:
            if payload['model']=='small':
                if state.get('draft_error'):
                    self.send_response(500);self.end_headers();self.wfile.write(b'failed');return
                data=completion('small',dict(role='assistant',content=None,tool_calls=[copy.deepcopy(state.get('call',CALL))]),'tool_calls')
            elif payload.get('structured_outputs'):
                data=completion('large',dict(role='assistant',content=state.get('verdict','A')),state.get('verdict_finish','stop'))
            elif payload['messages'][-1].get('content','').startswith('Compress the preceding'):
                data=completion('large',dict(role='assistant',content='Observed src/app.py. Tests pending.'))
            else:
                data=completion('large',dict(role='assistant',content='Target response.'))
            if payload.get('stream'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                chunks=[dict(id='x',created=1,model='large',object='chat.completion.chunk',choices=[dict(index=0,delta=dict(role='assistant',content='Target response.'),finish_reason=None)]),
                        dict(id='x',created=1,model='large',object='chat.completion.chunk',choices=[dict(index=0,delta={},finish_reason='stop')],usage=data['usage'],metrics=data['metrics'])]
                for chunk in chunks:self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n');return
        body=json.dumps(data).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        self.server.state={'requests':[]}
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        url=f'http://127.0.0.1:{self.server.server_port}'
        self.cfg=Config(target_url=url,draft_url=url,target_model='large',draft_model='small',
                        draft_context=600,draft_tokens=100,compress_tokens=80,log_dir=self.temp.name)
        self.client=httpx.AsyncClient(trust_env=False)
        self.gate=Gate(self.cfg,self.client)
        self.payload=dict(model='large',messages=[dict(role='system',content='Follow the task.'),dict(role='user',content='Inspect src/app.py.')],tools=TOOLS,max_tokens=200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close();self.thread.join()
        self.temp.cleanup()

    def generations(self):
        return [p for path,p in self.server.state['requests'] if path=='/v1/chat/completions']

    def records(self):
        return [json.loads(x) for x in (Path(self.temp.name)/'requests.jsonl').read_text().splitlines()]

    async def test_accepted_tools_and_review_sees_full_history(self):
        result=await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(result['choices'][0]['message']['tool_calls'],[CALL])
        self.assertEqual(self.generations()[1]['messages'][:-1],self.payload['messages'])
        self.assertEqual(self.generations()[1]['max_tokens'],2)
        self.assertFalse(self.generations()[1]['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('metrics',result)
        self.assertEqual(self.records()[0]['route'],'accepted')
        self.assertEqual([s['phase'] for s in self.records()[0]['stages']],['draft','review'])

    async def test_rejection_falls_back_with_original_payload(self):
        self.server.state['verdict']='R'
        original=copy.deepcopy(self.payload)
        result=await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(self.payload,original)
        self.assertEqual(self.generations()[-1],original)
        self.assertEqual(result['choices'][0]['message']['content'],'Target response.')
        self.assertEqual(self.records()[0]['route'],'fallback')

    async def test_bad_schema_skips_review(self):
        self.server.state['call']=dict(CALL,function=dict(name='read',arguments='{"path":123}'))
        await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(len(self.generations()),2)
        self.assertEqual(self.records()[0]['local_rejection'],'invalid_arguments')

    async def test_length_stopped_accept_is_rejected(self):
        self.server.state['verdict_finish']='length'
        await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(self.records()[0]['route'],'fallback')

    async def test_draft_error_falls_back(self):
        self.server.state['draft_error']=True
        await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(self.generations()[-1],self.payload)

    async def test_auxiliary_bypasses_draft(self):
        self.payload.pop('tools')
        await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(len(self.generations()),1)
        self.assertEqual(self.records()[0]['route'],'target_only')

    async def test_compression_reuses_only_exact_prefix_and_preserves_review(self):
        self.payload['messages'] += [dict(role='assistant',content='Observed log: '+'line '*1000)]
        original=copy.deepcopy(self.payload['messages'])
        await self.gate.run(self.payload,{'x-pro-session':'s'},asyncio.Queue())
        self.assertEqual(self.records()[0]['compression'],'created')
        review=next(p for p in self.generations() if p.get('structured_outputs'))
        self.assertEqual(review['messages'][:-1],original)
        self.payload['messages'].append(dict(role='user',content='Continue inspecting.'))
        await self.gate.run(self.payload,{'x-pro-session':'s'},asyncio.Queue())
        self.assertEqual(self.records()[1]['compression'],'reused')
        self.payload['messages'][1]['content']='Different task.'
        await self.gate.run(self.payload,{'x-pro-session':'s'},asyncio.Queue())
        self.assertEqual(self.records()[2]['compression'],'created')

    async def test_compressed_rejection_still_uses_original_history(self):
        self.payload['messages'].append(dict(role='assistant',content='Observed: '+'line '*1000))
        self.server.state['verdict']='R'
        await self.gate.run(self.payload,{},asyncio.Queue())
        self.assertEqual(self.generations()[-1],self.payload)

    async def test_stream_fallback_is_live_and_usage_retained(self):
        self.server.state['verdict']='R'
        self.payload['stream']=True
        q=asyncio.Queue()
        self.assertIsNone(await self.gate.run(self.payload,{},q))
        first=await q.get()
        self.assertEqual(first['choices'][0]['delta']['content'],'Target response.')
        self.assertEqual(self.records()[0]['stages'][-1]['usage']['completion_tokens'],5)

    async def test_http_sse_acceptance_contains_only_approved_call(self):
        # FastAPI lifespan and its real HTTP client talk to the local peer.
        def invoke():
            with TestClient(create_app(self.cfg)) as client:
                return client.post('/v1/chat/completions',json=dict(self.payload,stream=True))
        response=await asyncio.to_thread(invoke)
        self.assertEqual(response.status_code,200)
        chunks=[json.loads(x[6:]) for x in response.text.splitlines() if x.startswith('data: {')]
        calls=[x['choices'][0]['delta']['tool_calls'] for x in chunks if x['choices'] and x['choices'][0]['delta'].get('tool_calls')]
        self.assertEqual(calls,[[dict(CALL,index=0)]])
        self.assertIn('data: [DONE]',response.text)

    async def test_tool_choice_and_atomic_batch(self):
        result=completion('small',dict(role='assistant',tool_calls=[CALL,dict(CALL,id='call_2')]),'tool_calls')
        self.assertEqual(validate_candidate(dict(self.payload,parallel_tool_calls=False),result),'parallel_tools_disallowed')
        result['choices'][0]['message']['tool_calls'][1]['function']={'name':'unknown','arguments':'{}'}
        self.assertEqual(validate_candidate(self.payload,result),'unknown_tool')


if __name__=='__main__':
    unittest.main()
