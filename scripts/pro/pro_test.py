#!/usr/bin/env python3
"""Local SWE-bench Pro / OpenCode entry point. See the workspace README.md."""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
PYTHON = '/home/longyy/anaconda3/envs/swebench-eval/bin/python'

def read(path):
    return json.loads(Path(path).read_text())

def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)

def command(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)

def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

@contextlib.contextmanager
def locked(path):
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Already running: {path}')
        yield

def init(args):
    p = Path(args.output).resolve()
    if p.exists():
        raise ValueError('Output directory already exists. Use a NEW directory; old results are never overwritten by init.')
    manifest = Path(getattr(args, 'environment_file', None) or ROOT/'.pro-env/environment.json')
    if not manifest.is_file():
        raise ValueError('Prepared environment manifest missing. Run run_pro.sh first, or supply --environment-file.')
    environment = read(manifest)
    template = ROOT/'bootstrap/templates'
    data = json.loads(gzip.decompress((template/'public.json.gz').read_bytes()))
    fixed_ids = read(template/'fixed10.json')
    by_id = {d['instance_id']: d for d in data}
    if args.cohort == 'fixed10':
        chosen = [by_id[iid] for iid in fixed_ids]
    else:
        chosen = data
    if args.ids_file:
        ids = [x.strip() for x in Path(args.ids_file).read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
        unknown = set(ids)-set(by_id)
        if unknown:
            raise ValueError('Unknown instance IDs: '+', '.join(sorted(unknown)))
        chosen = [by_id[iid] for iid in ids]
    if args.indices:
        indices = [int(x) for x in args.indices.split(',')]
        if any(i < 1 or i > len(chosen) for i in indices):
            raise ValueError('Indices are 1-based and must be within the selected cohort.')
        chosen = [chosen[i-1] for i in indices]
    if not chosen or len({d['instance_id'] for d in chosen}) != len(chosen):
        raise ValueError('Selection must be nonempty, without duplicate instance IDs.')
    ports = [int(x) for x in args.ports.split(',')]
    gpus = [[int(x) for x in group.split(',')] for group in args.gpus.split(';')]
    flat = [g for group in gpus for g in group]
    if len(ports) != len(gpus) or len(set(ports)) != len(ports) or len(flat) != len(set(flat)):
        raise ValueError('Give one disjoint GPU group per unique port.')
    if any(len(group) != 4 for group in gpus) or any(port < 1024 or port > 65535 for port in ports):
        raise ValueError('This local Qwen27B template uses TP4. Give exactly four GPU indices per server and valid ports.')
    if args.context_file and len(chosen) != 1:
        raise ValueError('--context-file applies to exactly one selected task; use --context-dir for multiple tasks.')
    if args.task_timeout <= 0:
        raise ValueError('--task-timeout must be positive.')
    if args.prompt_file and args.no_delegation_prompt:
        raise ValueError('Choose either --prompt-file or --no-delegation-prompt.')
    contexts = {}
    if args.context_file:
        contexts[chosen[0]['instance_id']] = Path(args.context_file).read_text()
    if args.context_dir:
        for d in chosen:
            f = Path(args.context_dir) / (d['instance_id'] + '.md')
            if not f.exists():
                raise ValueError(f'Missing context: {f}')
            contexts[d['instance_id']] = f.read_text()
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else (template/'delegation-prompt.txt').read_text()
    if args.no_delegation_prompt:
        prompt = ''
    gateway = None
    if getattr(args, 'gateway_run_dir', None):
        gateway_path = Path(args.gateway_run_dir).resolve()
        gateway = read(gateway_path/'services.json')
        if len(ports) != 1 or gpus != [gateway['target_gpus']]:
            raise ValueError('A semantic-gate experiment uses one task queue and the target GPU group.')
    # Freeze a copy of the already-run harness; only adapt routing and optional context.
    source = (template/'runner.py').read_text()
    replacements = {
        'TASK_TIMEOUT=3600': f'TASK_TIMEOUT={args.task_timeout}',
        'port=8000+server': "port=json.loads((P/'config.json').read_text())['ports'][server]",
        "'pathctx-'+P.name+'-grade-'": "'procli-'+hashlib.sha256(str(P).encode()).hexdigest()[:8]+'-grade-'",
        "'pathctx-'+P.name+'-agent-'": "'procli-'+hashlib.sha256(str(P).encode()).hexdigest()[:8]+'-agent-'",
        "  if P.name=='path':\n   planfile=P.parent.parent/'plans'/f\"{d['experiment_index']:02d}.md\"":
            "  planfile=P/'contexts'/(iid+'.md')\n  if planfile.exists():",
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise ValueError(f'Harness template changed; inspect adaptation: {old!r}')
        source = source.replace(old, new)
    if gateway:
        old = "'--upstream',f'http://127.0.0.1:{port}'"
        if source.count(old) != 1:
            raise ValueError('Inspect gateway routing adaptation in the frozen runner')
        source = source.replace(old, "'--upstream',"+repr(gateway['gateway_url']))
    for old, new in {
        '/home/longyy/swe-bench-env/SWE-bench_Pro-os': environment['official_root'],
        PYTHON: environment['python'],
        '/home/longyy/opencode': environment['opencode_root'],
        '/home/longyy/.bun': environment['bun_root'],
        'http://ubuntu-lab.zinyy.tech:20172': environment['proxy'],
    }.items():
        source = source.replace(old, new)
    # The evaluator may live inside the OpenCode checkout. Its raw task rows,
    # grading files and other runs must not ride along with the harness mount.
    workspace_mask = None
    try:
        relative_workspace = ROOT.relative_to(Path(environment['opencode_root']).resolve())
    except ValueError:
        relative_workspace = None
    if relative_workspace is not None:
        if str(relative_workspace) == '.':
            raise ValueError('Evaluator workspace cannot be the OpenCode checkout root')
        workspace_mask = '/opencode/'+relative_workspace.as_posix()
        anchor = "   (td/'observation').mkdir(exist_ok=True);run(args,check=True,capture_output=True)"
        if source.count(anchor) != 1 or source.count('   baseline=dex(') != 1:
            raise ValueError('Inspect benchmark-workspace isolation adaptation in runner')
        source = source.replace(anchor,
            f"   args[args.index(image):args.index(image)]=['--tmpfs',{workspace_mask+':ro,nosuid,nodev,noexec,size=1m'!r}]\n"+anchor)
        audit = f"   hidden=dex(name,'find '+shlex.quote({workspace_mask!r})+' -mindepth 1 -print -quit',capture_output=True)\n"
        audit += "   if hidden.returncode or hidden.stdout.strip():raise RuntimeError('Benchmark workspace is visible in the agent container')\n"
        audit += f"   result['benchmark_workspace_mask']={{'path':{workspace_mask!r},'empty':True}}\n"
        source = source.replace('   baseline=dex(',audit+'   baseline=dex(')
    compile(source, 'generated-runner.py', 'exec')
    p.mkdir(parents=True)
    for name in ['assets', 'infra-fix']:
        (p/name).symlink_to(Path(environment[name]).resolve(), target_is_directory=True)
    for name in ['trace_proxy.py', 'trace-plugin.ts', 'analyze.py', 'efficiency_metrics.py']:
        shutil.copy2(template/name, p/name)
    shutil.copy2(ROOT/'scripts/metrics/key_metrics.py', p/'key_metrics.py')
    for name in ['pull_images.py', 'pull_one.py']:
        code = (template/name).read_text()
        for old, new in {
            '/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo': environment['skopeo'],
            '/home/longyy/swe-bench-env/registries.conf': str(ROOT/'bootstrap/templates/registries.conf'),
            'http://ubuntu-lab.zinyy.tech:20172': environment['proxy'],
            'http://pc.zinyy.tech:7899': environment['image_proxy'],
        }.items():
            code = code.replace(old, new)
        compile(code, name, 'exec')
        (p/name).write_text(code)
    (p/'runner.py').write_text(source)
    (p/'delegation-prompt.txt').write_text(prompt)
    (p/'contexts').mkdir()
    for iid, text in contexts.items():
        (p/'contexts'/(iid+'.md')).write_text(text)
    chosen = [dict(d, experiment_index=i+1) for i, d in enumerate(chosen)]
    save(p/'selected.json', chosen)
    config = dict(ports=ports, gpus=gpus, task_timeout=args.task_timeout, cohort=args.cohort,
        tasks=len(chosen), created_at=time.time(), model='qwen3.8-27b', python=PYTHON,
        official_root=str(ROOT/'SWE-bench_Pro-os'), opencode_root='/home/longyy/opencode',
        template_source=str(template), mutable_docker_images_shared=False)
    config.update({k:environment[k] for k in ['python','official_root','opencode_root']})
    config.update(environment=environment)
    config['benchmark_workspace_mask'] = workspace_mask
    if gateway:
        config.update(gateway_run_dir=str(gateway_path), gateway_url=gateway['gateway_url'])
    save(p/'config.json', config)
    template = list(environment['server_argv'])
    servers = []
    for port, group in zip(ports, gpus):
        argv = list(template)
        argv[argv.index('--port')+1] = str(port)
        argv[argv.index('--host')+1] = '127.0.0.1'
        servers.append(dict(port=port, gpus=group, argv=argv))
    save(p/'server-templates.json', servers)
    save(p/'input-hashes.json', {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in list(p.glob('*.py'))+list(p.glob('*.ts'))+list(p.glob('*.json'))+list((p/'contexts').glob('*.md'))+[p/'delegation-prompt.txt']})
    print(f'Created {p}: {len(chosen)} tasks, ports {ports}. No inference or download started.')

def image_ready(d):
    result = subprocess.run(['docker','image','inspect','jefzda/sweap-images:'+d['dockerhub_tag']],capture_output=True,text=True)
    return result.returncode == 0

def checks(p, require_images=False):
    cfg = read(p/'config.json')
    try:
        relative_workspace = ROOT.relative_to(Path(cfg['opencode_root']).resolve())
    except ValueError:
        relative_workspace = None
    if relative_workspace is not None and cfg.get('benchmark_workspace_mask') != '/opencode/'+relative_workspace.as_posix():
        raise RuntimeError('Reinitialize in a new directory: this run lacks the benchmark-workspace mount isolation.')
    command(['docker','info'],capture_output=True)
    for f in [Path(cfg['python']),Path(cfg['official_root'])/'swe_bench_pro_eval.py',Path(cfg['opencode_root'])/'packages/opencode/src/index.ts',p/'assets/config',p/'infra-fix/sanitize-git.sh',p/'infra-fix/bun-linux-x64-musl/bun',p/'assets/ripgrep-15.1.0-x86_64-unknown-linux-musl/rg']:
        if not f.exists():raise RuntimeError(f'Missing local dependency: {f}')
    net = read_json_command(['docker','network','inspect','swe-isolated'])[0]
    if not net['Internal'] or not any(x.get('Gateway')=='172.18.0.1' for x in net['IPAM']['Config']):
        raise RuntimeError('The frozen harness expects internal swe-isolated with gateway 172.18.0.1.')
    missing=[d['instance_id'] for d in read(p/'selected.json') if not image_ready(d)]
    if missing and require_images:raise RuntimeError(f'{len(missing)} images absent. Run the pull command first.')
    return dict(tasks=cfg['tasks'],cached_images=cfg['tasks']-len(missing),missing_images=missing,network='internal',ports=cfg['ports'])

def read_json_command(argv):
    return json.loads(command(argv,capture_output=True).stdout)

def pull(p):
    command(['docker','info'],capture_output=True)
    downloader=module(p/'pull_images.py','manual_pull')
    for d in read(p/'selected.json'):
        r=downloader.pull(d)
        if r['returncode']:
            raise RuntimeError(f"Download failed: {d['instance_id']}; see images/<id>/status.json and pull logs. No origin-layer fallback.")

def gpu_guard(cfg):
    gpu_text=command(['nvidia-smi','--query-gpu=index,uuid','--format=csv,noheader,nounits'],capture_output=True).stdout
    mapping={int(x.split(',')[0]):x.split(',')[1].strip() for x in gpu_text.splitlines()}
    if any(g not in mapping for group in cfg['gpus'] for g in group):
        raise RuntimeError('A selected GPU index is absent on this machine.')
    wanted={mapping[g] for group in cfg['gpus'] for g in group}
    apps=command(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader,nounits'],capture_output=True).stdout
    busy=[line for line in apps.splitlines() if line.split(',')[0].strip() in wanted]
    if busy:raise RuntimeError('Selected GPUs already have compute processes; leave existing jobs alone: '+repr(busy))
    for port in cfg['ports']:
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port))
            except OSError:raise RuntimeError(f'Port {port} is occupied. Choose another free port/GPU group or wait for the owner to finish.')

def serve(p):
    cfg=read(p/'config.json')
    if cfg.get('gateway_run_dir'):
        raise RuntimeError('This run uses the semantic gate; manage services with scripts/semantic_gate/services.py.')
    lockdir=ROOT/'.pro-service-locks';lockdir.mkdir(exist_ok=True)
    with contextlib.ExitStack() as locks:
        locks.enter_context(locked(p/'serve.lock'))
        for gpu in sorted(g for group in cfg['gpus'] for g in group):
            locks.enter_context(locked(lockdir/f'gpu-{gpu}.lock'))
        gpu_guard(cfg)
        children=[];stop=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set())
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        save(p/'serve-supervisor.json',dict(pid=os.getpid(),at=time.time()))
        extra=cfg.get('server_env',{})
        try:
            for s in read(p/'server-templates.json'):
                env=os.environ.copy();env.update(extra)
                env.update(CUDA_VISIBLE_DEVICES=','.join(map(str,s['gpus'])),HF_HUB_OFFLINE='1')
                with (p/f"server-{s['port']}.log").open('a') as log:
                    child=subprocess.Popen(s['argv'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                children.append(child);save(p/f"server-{s['port']}.json",dict(s,pid=child.pid,at=time.time()))
                print(f"Server {s['port']}, PID {child.pid}; log: {p}/server-{s['port']}.log",flush=True)
            while not stop.wait(2):
                for child in children:
                    if child.poll() is not None:raise RuntimeError(f'Server exited: PID={child.pid}, code={child.returncode}')
        finally:
            for child in children:
                try:os.killpg(child.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            for child in children:
                try:child.wait(timeout=30)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL)
            save(p/'servers-stopped.json',dict(at=time.time()))

def verify_servers(p):
    cfg=read(p/'config.json')
    if cfg.get('gateway_run_dir'):
        controller=module(ROOT/'scripts/semantic_gate/services.py','semantic_gate_services')
        services=controller.verify(cfg['gateway_run_dir'])
        if services['gateway_url'] != cfg['gateway_url']:
            raise RuntimeError('Gateway URL differs from the frozen experiment configuration')
        return
    for port in read(p/'config.json')['ports']:
        f=p/f'server-{port}.json'
        if not f.exists():raise RuntimeError(f'Start serve for THIS output directory first; refusing to borrow an unrelated server on port {port}.')
        pid=read(f)['pid'];cmdline=Path(f'/proc/{pid}/cmdline')
        if not cmdline.exists() or b'vllm' not in cmdline.read_bytes():raise RuntimeError(f'Server {port} is not alive; inspect its log and start serve.')
        urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5).close()

def metrics(p):
    if (p/'invalid-experiment.json').exists():
        raise RuntimeError('This run is marked invalid; do not include it in evaluation metrics.')
    python=read(p/'config.json')['python'] if (p/'config.json').exists() else PYTHON
    if (p/'efficiency_metrics.py').exists():
        command([python,str(p/'efficiency_metrics.py'),str(p),'--output-dir',str(p/'metrics-output')], stdout=subprocess.DEVNULL)
    compact=p/'key_metrics.py' if (p/'key_metrics.py').exists() else ROOT/'scripts/metrics/key_metrics.py'
    command([python,str(compact),str(p),'--output-dir',str(p/'metrics-output')])
    cfg=read(p/'config.json') if (p/'config.json').exists() else {}
    if cfg.get('gateway_run_dir'):
        log=Path(cfg['gateway_run_dir'])/'gateway-logs/requests.jsonl'
        if log.exists():
            command([python,str(ROOT/'scripts/semantic_gate/metrics.py'),str(log),'--run-dir',str(p),
                     '--output-dir',str(p/'metrics-output')])
            report=p/'metrics-output/key-metrics.md'
            if report.exists():
                report.write_text('> 此实验使用 semantic gate。下表的模型阶段列不包含完整的 8B/审核/压缩开销，请同时查看 gateway-metrics.md；工具墙钟与官方评分仍可参考。\n\n'+report.read_text())

def run(p):
    with locked(p/'runner.lock'):
        print(json.dumps(checks(p,require_images=True)),flush=True)
        for key in ['NO_PROXY','no_proxy']:
            os.environ[key]=','.join(filter(None,[os.environ.get(key,''),'localhost','127.0.0.1','172.18.0.1']))
        verify_servers(p)
        cfg=read(p/'config.json');ds=read(p/'selected.json');stop=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set())
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        save(p/'runner-process.json',dict(pid=os.getpid(),at=time.time()))
        for d in ds:
            folder=p/'images'/d['instance_id'];folder.mkdir(parents=True,exist_ok=True)
            if not (folder/'status.json').exists():save(folder/'status.json',dict(returncode=0,source='local Docker cache inspected before run'))
        worker_module=module(p/'runner.py','manual_runner')
        os.chdir(cfg['official_root'])
        q=queue.Queue()
        for d in ds:
            if not (p/'runs'/d['instance_id']/'result.json').exists():q.put(d)
        def worker(server):
            while not stop.is_set():
                try:d=q.get_nowait()
                except queue.Empty:return
                worker_module.task(d,server);q.task_done()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cfg['ports'])) as pool:
            list(pool.map(worker,range(len(cfg['ports']))))
        save(p/('stopped.json' if stop.is_set() else 'finished.json'),dict(at=time.time(),planned=len(ds)))
        metrics(p)
        print('Run ended. Model services remain in the serve terminal; stop them with Ctrl-C when finished.')

def status(p):
    if (p/'invalid-experiment.json').exists():
        print(json.dumps(read(p/'invalid-experiment.json'),ensure_ascii=False,indent=2))
        return
    rows=[]
    for d in read(p/'selected.json'):
        td=p/'runs'/d['instance_id'];r=read(td/'result.json') if (td/'result.json').exists() else {}
        stage=None;start=end=None
        if (td/'lifecycle.jsonl').exists():
            for line in (td/'lifecycle.jsonl').read_text().splitlines():
                try:e=json.loads(line)
                except json.JSONDecodeError:continue
                if e.get('kind')=='stage.start':stage=e['stage']
                if e.get('stage')=='agent':
                    if e['kind']=='stage.start':start=e['at']
                    elif e['kind']=='stage.end':end=e['at']
        rows.append(dict(instance_id=d['instance_id'],state='ended' if r else stage or 'queued',
            agent_seconds=r.get('agent_wall_seconds',(end or time.time())-start if start else None),
            resolved=(r.get('grade') or {}).get('resolved'),infra_failure=r.get('infra_failure') or (r.get('grade') or {}).get('infra_failure'),error=r.get('error')))
    print(json.dumps(dict(ended=sum(x['state']=='ended' for x in rows),planned=len(rows),rows=rows),ensure_ascii=False,indent=2))

def init_arguments(make):
    make.add_argument('--output',required=True)
    make.add_argument('--cohort',choices=['fixed10','public'],default='fixed10')
    make.add_argument('--indices',help='1-based indices within cohort, e.g. 4 or 1,3,4')
    make.add_argument('--ids-file',help='Explicit instance IDs, one per line; takes precedence over cohort')
    make.add_argument('--ports',default='8000')
    make.add_argument('--gpus',default='0,1,2,3',help='One TP4 group per port; quote two groups as "0,1,2,3;4,5,6,7"')
    make.add_argument('--task-timeout',type=int,default=3600)
    make.add_argument('--prompt-file',help='Replace the additional general collaboration prompt, keeping the official task and constraints')
    make.add_argument('--no-delegation-prompt',action='store_true',help='Remove encouragement only; task/subagent tool remains available')
    make.add_argument('--environment-file',help='Prepared environment manifest produced by run_pro.sh; defaults to .pro-env/environment.json')
    make.add_argument('--gateway-run-dir',help='Experimental: use the owned services in this semantic-gate directory; one task queue')
    ctx=make.add_mutually_exclusive_group()
    ctx.add_argument('--context-file',help='Append this Markdown to the sole selected task')
    ctx.add_argument('--context-dir',help='One <instance_id>.md per selected task')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    make=sub.add_parser('init',help='Create a new isolated output directory, without running or downloading anything.')
    init_arguments(make)
    for action in ['doctor','pull','serve','run','status','metrics']:
        cmd=sub.add_parser(action)
        cmd.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.action=='init':init(args)
        else:
            p=Path(args.output).resolve()
            if args.action=='doctor':print(json.dumps(checks(p),ensure_ascii=False,indent=2))
            else:globals()[args.action](p)
    except (ValueError,RuntimeError,OSError,subprocess.CalledProcessError) as error:
        parser.exit(1,f'{type(error).__name__}: {error}\n')

if __name__=='__main__':
    main()
