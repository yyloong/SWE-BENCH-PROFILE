#!/usr/bin/env python3
"""Start/inspect/stop the two vLLM services and semantic gateway owned by an experiment."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def start_ticks(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None


def alive(record):
    return bool(record.get('start_ticks') and start_ticks(record['pid']) == record['start_ticks'])


def check_gpu(gpus):
    query = subprocess.check_output(['nvidia-smi', '--query-gpu=index,memory.used', '--format=csv,noheader,nounits'], text=True)
    memory = {int(row.split(',')[0]): int(row.split(',')[1]) for row in query.splitlines()}
    busy = {gpu: memory[gpu] for gpu in gpus if memory[gpu] > 200}
    if busy:
        raise RuntimeError(f'Refusing to occupy busy GPUs: {busy}')


def health(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+'/health', timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def verify(path):
    path = Path(path).resolve()
    cfg = json.loads((path/'services.json').read_text())
    for name in ['server-27b', 'server-8b', 'proxy']:
        record = json.loads((path/(name+'.json')).read_text())
        if not alive(record):
            raise RuntimeError(f'{name} from {path} is not alive')
    if not health(cfg['gateway_url']):
        raise RuntimeError('Gateway or one of its model services is not healthy')
    return cfg


def start(args):
    path = Path(args.output).resolve()
    path.mkdir(parents=True, exist_ok=True)
    envinfo = json.loads((ROOT/'.pro-env/environment.json').read_text())
    python = envinfo['vllm_python']
    draft = Path(args.draft_path).expanduser().resolve()
    if not (draft/'model.safetensors.index.json').exists():
        raise RuntimeError(f'Download the draft checkpoint first: {draft}')
    index = json.loads((draft/'model.safetensors.index.json').read_text())
    if any(not (draft/name).exists() for name in set(index['weight_map'].values())):
        raise RuntimeError('Draft checkpoint download is incomplete')
    config = dict(target_url='http://127.0.0.1:8000', draft_url='http://127.0.0.1:8001',
                  gateway_url='http://127.0.0.1:8002', target_gpus=[0,1,2,3], draft_gpus=[4,5],
                  target_model='qwen3.8-27b', draft_model='qwen3-8b',
                  target_context=262144, draft_context=args.draft_context,
                  draft_path=str(draft), target_path=envinfo['model'])
    configfile = path/'services.json'
    if configfile.exists() and json.loads(configfile.read_text()) != config:
        raise RuntimeError('Use a new service directory to change model configuration')
    target = list(envinfo['server_argv'])
    target[target.index('--host')+1] = '127.0.0.1'
    small = [python, '-m', 'vllm.entrypoints.cli.main', 'serve', str(draft),
             '--host', '127.0.0.1', '--port', '8001', '--served-model-name', 'qwen3-8b',
             '--tensor-parallel-size', '2', '--dtype', 'bfloat16', '--gpu-memory-utilization', '0.95',
             '--max-model-len', str(args.draft_context), '--max-num-seqs', '8',
             '--max-num-batched-tokens', '8192', '--max-cudagraph-capture-size', '16',
             '--reasoning-parser', 'qwen3', '--enable-auto-tool-choice', '--tool-call-parser', 'hermes',
             '--enable-per-request-metrics', '--enable-prefix-caching']
    if args.draft_context > 40960:
        small += ['--hf-overrides', json.dumps({'rope_scaling': {'rope_type':'yarn', 'factor':4.0,
                                                        'original_max_position_embeddings':32768}})]
    proxy = [python, str(ROOT/'scripts/semantic_gate/proxy.py'), '--port', '8002',
             '--draft-context', str(args.draft_context), '--log-dir', str(path/'gateway-logs')]
    configfile.write_text(json.dumps(config, indent=2))
    started = []
    try:
        for name, argv, gpus, port in [('server-27b', target, [0,1,2,3], 8000),
                                      ('server-8b', small, [4,5], 8001), ('proxy', proxy, [], 8002)]:
            recordfile = path/(name+'.json')
            if recordfile.exists() and alive(json.loads(recordfile.read_text())):
                print(name+': already running', flush=True)
                continue
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
            if gpus:
                check_gpu(gpus)
            env = os.environ.copy()
            for key in ['ALL_PROXY', 'all_proxy', 'HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy']:
                env.pop(key, None)
            env.update(PATH=str(Path(python).parent)+os.pathsep+env.get('PATH', ''),
                       HF_HUB_OFFLINE='1', CUDA_VISIBLE_DEVICES=','.join(map(str,gpus)),
                       NO_PROXY='localhost,127.0.0.1,172.18.0.1', no_proxy='localhost,127.0.0.1,172.18.0.1')
            with (path/(name+'.log')).open('a') as log:
                child = subprocess.Popen(argv, env=env, cwd=ROOT, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            started.append(child)
            recordfile.write_text(json.dumps(dict(pid=child.pid, start_ticks=start_ticks(child.pid),
                                                  argv=argv, gpus=gpus, port=port, at=time.time()), indent=2))
            print(f'{name}: PID {child.pid}; {path}/{name}.log', flush=True)
    except Exception:
        for child in started:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start','status','stop','restart-proxy'])
    parser.add_argument('--output', required=True)
    parser.add_argument('--draft-context', type=int, default=131072)
    parser.add_argument('--draft-path', default='/home/longyy/.cache/huggingface/hub/models--Qwen--Qwen3-8B/snapshots/b968826d9c46dd6066d109eabc6255188de91218')
    args = parser.parse_args()
    path = Path(args.output).resolve()
    if args.action in {'start','restart-proxy'}:
        path.mkdir(parents=True, exist_ok=True)
        with (path/'services.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.action == 'restart-proxy' and (path/'proxy.json').exists():
                record = json.loads((path/'proxy.json').read_text())
                if alive(record):
                    os.killpg(record['pid'], signal.SIGTERM)
                    deadline = time.monotonic()+15
                    while alive(record) and time.monotonic()<deadline:
                        time.sleep(.1)
                    if alive(record):
                        raise RuntimeError('Proxy did not stop; inspect active requests before retrying')
            start(args)
        return
    for name in ['proxy','server-8b','server-27b']:
        recordfile = path/(name+'.json')
        if not recordfile.exists():
            print(name+': no record')
            continue
        record = json.loads(recordfile.read_text())
        live = alive(record)
        if args.action == 'stop' and live:
            os.killpg(record['pid'], signal.SIGTERM)
        print(json.dumps(dict(name=name,pid=record['pid'],alive=live,
                              healthy=health(f'http://127.0.0.1:{record["port"]}') if live and args.action=='status' else None)))


if __name__ == '__main__':
    main()
