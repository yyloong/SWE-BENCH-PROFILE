#!/usr/bin/env python3
"""One command: install/reuse pinned user dependencies, prepare, serve, solve, grade, report."""
import argparse
import contextlib
import grp
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile

if __package__:
    from . import pro_test as pro
else:
    import pro_test as pro

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT/'bootstrap'
LOCK = pro.read(BUNDLE/'versions.json')
LEGACY = Path('/home/longyy')


def log(message):
    print(time.strftime('[%H:%M:%S] ') + message, flush=True)


def call(argv, **kwargs):
    log(shlex.join(map(str, argv)))
    return subprocess.run(list(map(str, argv)), check=True, text=True, **kwargs)


def proxy_env(proxy):
    env = os.environ.copy()
    for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:
        env[key] = proxy
    for key in ['NO_PROXY','no_proxy']:
        env[key] = 'localhost,127.0.0.1,172.18.0.1'
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['HF_HUB_DISABLE_XET'] = '1'
    return env


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_bundle():
    for name, expected in LOCK['templates'].items():
        if sha(BUNDLE/name) != expected:
            raise RuntimeError(f'Frozen bootstrap input changed: {name}')


def docker_access():
    if not shutil.which('docker'):
        raise RuntimeError('Docker Engine is required on the host. Install/start it before running this entry point.')
    result = subprocess.run(['docker','info'], capture_output=True, text=True)
    if result.returncode == 0:
        return
    # Common on this machine: group membership was granted after login.
    try:
        member = __import__('getpass').getuser() in grp.getgrnam('docker').gr_mem
    except KeyError:
        member = False
    if member and not os.environ.get('PRO_DOCKER_REEXEC'):
        env = os.environ.copy(); env['PRO_DOCKER_REEXEC'] = '1'
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
        os.execvpe('sg', ['sg','docker','-c',command], env)
    raise RuntimeError('Docker daemon unavailable or permission denied: '+result.stderr.strip())


def package_versions(python, names):
    if not Path(python).exists():
        return {}
    code = 'from importlib import metadata; import json,sys; print(json.dumps({n:metadata.version(n) for n in sys.argv[1:]}))'
    r = subprocess.run([str(python), '-c', code, *names], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def python_env(folder, candidates, requirements, expected, env, base_python=None):
    # Never pip-install into a reused environment, especially an active model server.
    for python in candidates:
        if package_versions(python, list(expected)) == expected:
            log(f'Reuse Python environment: {python}')
            return str(python)
    python = folder/'bin/python'
    if not python.exists():
        base = base_python or next((p for p in [shutil.which('python3.12'), str(LEGACY/'anaconda3/envs/vllm/bin/python')] if p and Path(p).exists()), None)
        if not base:
            raise RuntimeError('Python 3.12 with venv support is required to install the pinned environments.')
        call([base, '-m','venv',folder])
    call([python,'-m','pip','install','--disable-pip-version-check','-r',requirements], env=env)
    if package_versions(python, list(expected)) != expected:
        raise RuntimeError(f'Installed environment failed version check: {folder}')
    return str(python)


def git_checkout(path, existing, url, commit, env):
    for candidate in [*existing, path]:
        if not (candidate/'.git').exists():
            continue
        r = subprocess.run(['git','-C',str(candidate),'rev-parse','HEAD'],capture_output=True,text=True)
        if r.returncode == 0 and r.stdout.strip() == commit:
            log(f'Reuse source: {candidate} @ {commit[:12]}')
            return candidate
    if path.exists():
        raise RuntimeError(f'Existing checkout has a different revision; preserve it and choose another --env-dir: {path}')
    path.mkdir(parents=True)
    call(['git','init',path])
    call(['git','-C',path,'remote','add','origin',url])
    call(['git','-C',path,'fetch','--depth','1','origin',commit],env=env)
    call(['git','-C',path,'checkout','--detach','FETCH_HEAD'])
    return path


def fetch(url, destination, proxy):
    destination.parent.mkdir(parents=True,exist_ok=True)
    partial = destination.with_suffix(destination.suffix+'.part')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy}))
    log('Download '+url)
    with opener.open(url,timeout=120) as response, partial.open('wb') as output:
        shutil.copyfileobj(response,output)
    partial.replace(destination)


def binary(path, candidates, digest, url, member, proxy):
    for candidate in [*candidates, path]:
        if candidate.is_file() and sha(candidate) == digest:
            return candidate
    if path.exists():
        raise RuntimeError(f'Unexpected existing binary; refusing replacement: {path}')
    with tempfile.TemporaryDirectory(prefix='pro-binary-') as tmp:
        archive=Path(tmp)/('download.zip' if url.endswith('.zip') else 'download.tar.gz')
        fetch(url,archive,proxy)
        if url.endswith('.zip'):
            with zipfile.ZipFile(archive) as z: data=z.read(member)
        else:
            with tarfile.open(archive) as z: data=z.extractfile(member).read()
        if hashlib.sha256(data).hexdigest()!=digest:
            raise RuntimeError(f'Downloaded binary differs from frozen experiment version: {url}')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data);path.chmod(0o755)
    return path


def model_complete(path, target):
    if not (path/'config.json').is_file():return False
    if target and not (path/'tokenizer.json').is_file():return False
    index=path/'model.safetensors.index.json'
    if index.exists():
        return all((path/name).is_file() and (path/name).stat().st_size>0 for name in set(pro.read(index)['weight_map'].values()))
    return any(f.stat().st_size>0 for f in path.glob('*.safetensors'))


def model_snapshot(spec, python, cache, env, target):
    key='models--'+spec['repo'].replace('/','--')
    # Prefer the exact frozen revision already on disk; never follow mutable main.
    for base in [Path.home()/'.cache/huggingface/hub', LEGACY/'.cache/huggingface/hub', cache]:
        candidate=base/key/'snapshots'/spec['revision']
        if model_complete(candidate,target):
            log('Reuse model '+str(candidate));return str(candidate)
    code='from huggingface_hub import snapshot_download; import sys; print(snapshot_download(repo_id=sys.argv[1],revision=sys.argv[2],cache_dir=sys.argv[3],allow_patterns=["*.json","*.safetensors","*.model","*.txt","*.tiktoken","*.jinja"]))'
    call([python,'-c',code,spec['repo'],spec['revision'],cache],env=env)
    candidate=cache/key/'snapshots'/spec['revision']
    if not model_complete(candidate,target):raise RuntimeError('Incomplete model snapshot: '+str(candidate))
    return str(candidate)


def setup(args):
    verify_bundle()
    if not shutil.which('git') or not shutil.which('nvidia-smi'):
        raise RuntimeError('Host prerequisites: Git and a working NVIDIA driver (nvidia-smi).')
    home=Path(args.env_dir).resolve();home.mkdir(parents=True,exist_ok=True)
    # The copied harness contains Python literals for paths; reject unsupported quotes.
    if any(c in str(home)+args.proxy+args.image_proxy for c in "'\"\\\n\r"):
        raise ValueError('Environment path/proxy cannot contain quotes, backslashes or newlines.')
    with pro.locked(home/'setup.lock'):
        env=proxy_env(args.proxy)
        eval_expected={n:v for n,v in (line.strip().split('==') for line in (BUNDLE/'requirements-eval.txt').read_text().splitlines())}
        python=python_env(home/'eval',[home/'eval/bin/python',LEGACY/'anaconda3/envs/swebench-eval/bin/python'],BUNDLE/'requirements-eval.txt',eval_expected,env)
        vexpected={'vllm':LOCK['vllm_version'],'torch':LOCK['torch_version'],'transformers':LOCK['transformers_version']}
        vpython=python_env(home/'vllm',[home/'vllm/bin/python',LEGACY/'anaconda3/envs/vllm/bin/python'],BUNDLE/'requirements-vllm.txt',vexpected,env)
        # Read package files without initializing CUDA or reserving device memory.
        code='from importlib import metadata; from pathlib import Path; p=Path(metadata.distribution("vllm").locate_file("vllm")); assert (p/"v1/spec_decode/dflash.py").exists(); assert "enable_per_request_metrics" in (p/"entrypoints/launchers/cli_args.py").read_text(), "per-request metrics support absent"'
        call([vpython,'-c',code],capture_output=True)
        official=git_checkout(home/'SWE-bench_Pro-os',[ROOT/'SWE-bench_Pro-os'],'https://github.com/scaleapi/SWE-bench_Pro-os.git',LOCK['official_commit'],env)
        opencode=git_checkout(home/'opencode',[ROOT.parent,ROOT.parent/'opencode'],'https://github.com/anomalyco/opencode.git',LOCK['opencode_commit'],env)
        bun=binary(home/'bun/bin/bun',[Path.home()/'.bun/bin/bun',LEGACY/'.bun/bin/bun'],LOCK['bun_sha256'],'https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64.zip','bun-linux-x64/bun',args.proxy)
        if not (opencode/'node_modules').exists():
            call([bun,'install','--frozen-lockfile'],cwd=opencode,env=env)
        # Use a private config/cache location for preflight, not the user's credentials.
        cli_env=env|{'OPENCODE_DISABLE_MODELS_FETCH':'1','OPENCODE_DISABLE_AUTOUPDATE':'1','XDG_CONFIG_HOME':str(home/'preflight-config'),'XDG_DATA_HOME':str(home/'preflight-data'),'XDG_CACHE_HOME':str(home/'preflight-cache')}
        call([bun,'run',opencode/'packages/opencode/src/index.ts','--version'],cwd=home,env=cli_env,capture_output=True,timeout=180)
        assets=home/'assets';infra=home/'infra-fix';assets.mkdir(exist_ok=True);infra.mkdir(exist_ok=True)
        musl=binary(infra/'bun-linux-x64-musl/bun',[ROOT/'.pro-env/infra-fix/bun-linux-x64-musl/bun'],LOCK['bun_musl_sha256'],'https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64-musl.zip','bun-linux-x64-musl/bun',args.proxy)
        rg=binary(assets/'ripgrep-15.1.0-x86_64-unknown-linux-musl/rg',[ROOT/'.pro-env/shared-assets/ripgrep-15.1.0-x86_64-unknown-linux-musl/rg'],LOCK['rg_sha256'],'https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz','ripgrep-15.1.0-x86_64-unknown-linux-musl/rg',args.proxy)
        for source,dest in [(musl,infra/'bun-linux-x64-musl/bun'),(rg,assets/'ripgrep-15.1.0-x86_64-unknown-linux-musl/rg')]:
            if source!=dest and not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
        sanitizer=infra/'sanitize-git.sh'
        if sanitizer.exists() and sha(sanitizer)!=sha(BUNDLE/'templates/sanitize-git.sh'):
            raise RuntimeError('Git sanitizer differs; use a new --env-dir.')
        if not sanitizer.exists():shutil.copy2(BUNDLE/'templates/sanitize-git.sh',sanitizer)
        config=assets/'config'
        old_config=ROOT/'.pro-env/shared-assets/config'
        if not config.exists():
            if (old_config/'node_modules/@opencode-ai/plugin/package.json').exists() and pro.read(old_config/'node_modules/@opencode-ai/plugin/package.json')['version']=='1.18.30':
                config.symlink_to(old_config.resolve(),target_is_directory=True)
            else:
                config.mkdir()
                shutil.copy2(BUNDLE/'templates/plugin-package.json',config/'package.json')
                shutil.copy2(BUNDLE/'templates/plugin-package-lock.json',config/'package-lock.json')
        if not (config/'node_modules/@opencode-ai/plugin/package.json').exists():
            call([bun,'install','--ignore-scripts'],cwd=config,env=env)
        skopeo=next((str(p) for p in [Path(shutil.which('skopeo') or '/nonexistent'),LEGACY/'anaconda3/envs/opencode-dev/bin/skopeo',home/'image-tools/bin/skopeo'] if p.is_file()),None)
        if not skopeo:
            conda=shutil.which('conda')
            if not conda:raise RuntimeError('Install skopeo or make conda available; bootstrap can install skopeo through conda-forge.')
            call([conda,'create','-y','-p',home/'image-tools','-c','conda-forge','skopeo=1.24.0'],env=env)
            skopeo=str(home/'image-tools/bin/skopeo')
        call([skopeo,'--version'],capture_output=True)
        model=model_snapshot(LOCK['model'],python,home/'models',env,True)
        draft=model_snapshot(LOCK['draft'],python,home/'models',env,False)
        net=subprocess.run(['docker','network','inspect','swe-isolated'],capture_output=True,text=True)
        if net.returncode:
            call(['docker','network','create','--internal','--subnet','172.18.0.0/16','swe-isolated'])
        else:
            n=json.loads(net.stdout)[0]
            if not n['Internal'] or not any(x.get('Gateway')=='172.18.0.1' for x in n['IPAM']['Config']):
                raise RuntimeError('Existing swe-isolated network differs; refusing to alter a shared network.')
        argv=pro.read(BUNDLE/'templates/server-argv.json')
        argv[:2]=[vpython,str(Path(vpython).parent/'vllm')]
        argv[argv.index('serve')+1]=model
        spec=json.loads(argv[argv.index('--speculative-config')+1]);spec['model']=draft
        argv[argv.index('--speculative-config')+1]=json.dumps(spec)
        manifest=dict(python=python,vllm_python=vpython,official_root=str(official),opencode_root=str(opencode),bun_root=str(bun.parent.parent),assets=str(assets),skopeo=skopeo,proxy=args.proxy,image_proxy=args.image_proxy,server_argv=argv,model=model,draft=draft,created_at=time.time(),versions=LOCK)
        manifest['infra-fix']=str(infra)
        pro.save(home/'environment.json',manifest)
        log('Environment ready: '+str(home/'environment.json'))
        return home/'environment.json'


def stop_child(child):
    if child and child.poll() is None:
        child.send_signal(signal.SIGTERM)
        try:child.wait(timeout=90)
        except subprocess.TimeoutExpired:
            # The serve supervisor owns separate vLLM process groups. Do not kill
            # that supervisor before it can clean them up.
            raise RuntimeError(f'Owned child did not stop: PID {child.pid}; inspect its log.')


def workflow(p, health_timeout):
    """Own both process groups, drain the current task on SIGINT, always stop serve."""
    p=pro.workdir(p)
    stop=threading.Event();server=None;runner=None
    previous={s:signal.getsignal(s) for s in [signal.SIGINT,signal.SIGTERM]}
    def interrupt(*_):
        if not stop.is_set():log('Stopping new dispatch; waiting for current task/grade, then releasing model services.')
        stop.set()
    for s in previous:signal.signal(s,interrupt)
    python=pro.read(p/'config.json')['python']
    try:
        with (p/'serve.log').open('a') as out:
            server=subprocess.Popen([python,str(ROOT/'scripts/pro/pro_test.py'),'serve','--output',str(p)],stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        log(f'Model supervisor PID {server.pid}; waiting for health (logs: {p}/server-*.log)')
        deadline=time.monotonic()+health_timeout;next_update=0
        while not stop.is_set():
            if server.poll() is not None:raise RuntimeError(f'Model startup failed; see {p}/serve.log and server logs.')
            try:pro.verify_servers(p);break
            except (RuntimeError,OSError,ValueError):pass
            if time.monotonic()>deadline:raise RuntimeError('Model readiness timeout; inspect server logs.')
            if time.monotonic()>next_update:log('Waiting for model readiness...');next_update=time.monotonic()+30
            stop.wait(2)
        if stop.is_set():return 130
        with (p/'run.log').open('a') as out:
            runner=subprocess.Popen([python,str(ROOT/'scripts/pro/pro_test.py'),'run','--output',str(p)],stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        log(f'Evaluation PID {runner.pid}; progress: python3 {shlex.quote(str(ROOT/"scripts/pro/pro_test.py"))} status --output {shlex.quote(str(p))}')
        drained=False;next_update=0
        while runner.poll() is None:
            if server.poll() is not None and not drained:
                runner.send_signal(signal.SIGTERM);drained=True
                log('Model supervisor exited; waiting for in-flight task cleanup.')
            if stop.is_set() and not drained:
                runner.send_signal(signal.SIGTERM);drained=True
            if time.monotonic()>next_update:
                count=sum((p/'runs'/d['instance_id']/'result.json').exists() for d in pro.read(p/'selected.json'))
                log(f'Finished {count}/{len(pro.read(p/"selected.json"))} tasks; live detail: {p}/run.log')
                next_update=time.monotonic()+30
            time.sleep(2)
        if runner.returncode:raise RuntimeError(f'Evaluation exited with code {runner.returncode}; see {p}/run.log')
        if server.poll() is not None:raise RuntimeError('Model server exited during evaluation; inspect task outcomes.')
        log('Efficiency report: '+str(pro.output_root(p)/'key-metrics.md'))
        return 130 if stop.is_set() else 0
    finally:
        # Normal signals are handled above. Unexpected exceptions still drain the
        # runner before stopping the service it needs for the current task.
        if runner and runner.poll() is None:
            runner.send_signal(signal.SIGTERM)
            while runner.poll() is None:time.sleep(1)
        stop_child(server)
        for s,handler in previous.items():signal.signal(s,handler)


def execute(args):
    p=pro.resolve_saved_path(args.output)
    if args.environment_file:
        raise ValueError('run_pro.sh manages --environment-file; use --env-dir instead.')
    verify_bundle()
    if p.exists() and not args.resume:
        raise ValueError('Output exists. Use --resume for the same inputs, or choose a new --output.')
    if args.resume:
        p=pro.workdir(p)
        if not (p/'config.json').exists() or not (p/'one-click-inputs.json').exists():
            raise ValueError('--resume requires an output previously created by run_pro.sh.')
        # Continue preparing the environment selected on the first invocation.
        args.env_dir=str(Path(pro.read(p/'one-click-inputs.json')['environment_file']).parent)
        # Do not mix attempts when a process was killed before writing result.json.
        for d in pro.read(p/'selected.json'):
            td=p/'runs'/d['instance_id']
            if td.exists() and any(td.iterdir()) and not (td/'result.json').exists():
                raise ValueError(f'Incomplete attempt retained: {td}; select this task in a new output directory.')
    docker_access()
    manifest=setup(args)
    if args.install_only:return 0
    args.environment_file=str(manifest)
    if not args.resume:
        pro.init(args)
        p=pro.workdir(p)
        pro.save(p/'one-click-inputs.json',dict(created_at=time.time(),environment_file=str(manifest)))
    else:
        log('Resume uses frozen configuration/context in '+str(p))
    with pro.locked(p/'one-click.lock'):
        log('Check environment and download selected images (local cache first).')
        pro.checks(p)
        pro.pull(p)
        pro.checks(p,require_images=True)
        if args.prepare_only:
            log('Prepared; no model inference started. Rerun with --resume to execute.');return 0
        if all((p/'runs'/d['instance_id']/'result.json').exists() for d in pro.read(p/'selected.json')):
            log('All tasks already ended; regenerate metrics without starting models.')
            pro.metrics(p);return 0
        pro.gpu_guard(pro.read(p/'config.json'))
        # Explicitly bypass external proxies for loopback readiness requests.
        for key in ['NO_PROXY','no_proxy']:os.environ[key]='localhost,127.0.0.1,172.18.0.1'
        return workflow(p,args.health_timeout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    pro.init_arguments(parser)
    parser.add_argument('--env-dir',default=str(ROOT/'.pro-env'),help='Managed user-space dependency directory; reuse valid installed versions first')
    parser.add_argument('--proxy',default='http://ubuntu-lab.zinyy.tech:20172',help='Source/packages/model download proxy')
    parser.add_argument('--image-proxy',default='http://pc.zinyy.tech:7899',help='Domestic Docker layer proxy')
    parser.add_argument('--prepare-only',action='store_true',help='Install/reuse dependencies, initialize output, prepare images, then exit without inference')
    parser.add_argument('--install-only',action='store_true',help='Install/reuse dependencies only; output is not created')
    parser.add_argument('--resume',action='store_true',help='Continue this output using its frozen configuration; completed failures are not retried')
    parser.add_argument('--health-timeout',type=int,default=1800)
    args=parser.parse_args()
    if args.gateway_run_dir:
        parser.error('For the semantic-gate experiment use pro_test.py init --gateway-run-dir, then pro_test.py run; services are managed separately.')
    if args.health_timeout<=0:parser.error('--health-timeout must be positive')
    if args.resume:
        changed=[x for x in sys.argv[1:] if x.split('=')[0] in {'--cohort','--indices','--ids-file','--ports','--gpus','--task-timeout','--prompt-file','--no-delegation-prompt','--context-file','--context-dir','--env-dir'}]
        if changed:parser.error('--resume uses stored inputs; remove selection/configuration arguments: '+', '.join(changed))
    try:return execute(args)
    except (ValueError,RuntimeError,OSError,subprocess.CalledProcessError) as error:
        log(type(error).__name__+': '+str(error));return 1
    except KeyboardInterrupt:
        log('Interrupted during preparation; cached downloads are retained.');return 130


if __name__=='__main__':sys.exit(main())
