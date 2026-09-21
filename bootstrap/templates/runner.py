import signal,fcntl,ast,concurrent.futures,contextlib,hashlib,importlib.util,json,os,queue,shlex,subprocess,sys,threading,time,traceback,urllib.request
from pathlib import Path
P=Path(__file__).resolve().parent
OFFICIAL=Path('/home/longyy/swe-bench-env/SWE-bench_Pro-os')
PY='/home/longyy/anaconda3/envs/swebench-eval/bin/python'
PROXY='http://ubuntu-lab.zinyy.tech:20172'
TASK_TIMEOUT=3600
STOP=threading.Event()
LOCK=threading.Lock()
def save(path,data):
 temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(data,indent=2));temp.replace(path)
def run(args,**kw):return subprocess.run(args,text=True,**kw)
def event(td,kind,**data):
 with LOCK:
  with (td/'lifecycle.jsonl').open('a') as f:f.write(json.dumps(dict(kind=kind,at=time.time(),mono=time.monotonic(),**data))+'\n')
@contextlib.contextmanager
def stage(td,name):
 event(td,'stage.start',stage=name)
 try:yield
 finally:event(td,'stage.end',stage=name)
def dex(name,script,**kw):return run(['docker','exec',name,'bash','-lc',script],**kw)
def metric(port):
 try:return urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics',timeout=5).read().decode()
 except Exception as e:return '# ERROR '+repr(e)
def monitor(td,port,stop):
 with (td/'server-metrics.jsonl').open('a') as f:
  while not stop.is_set():
   f.write(json.dumps(dict(at=time.time(),metrics=metric(port)))+'\n');f.flush();stop.wait(2)
def grade(td,d,patch):
 # Reuse upstream assembly, run script, parser and pass-set criterion. Only local lifecycle differs.
 sys.path.insert(0,str(OFFICIAL));import swe_bench_pro_eval as official
 workspace=td/'grading-workspace';workspace.mkdir(exist_ok=True)
 files,entry=official.assemble_workspace_files(d['instance_id'],str(OFFICIAL/'run_scripts'),patch,d)
 official.write_files_local(str(workspace),files)
 name='pathctx-'+P.name+'-grade-'+hashlib.sha256(d['instance_id'].encode()).hexdigest()[:12]
 image='jefzda/sweap-images:'+d['dockerhub_tag']
 args=['docker','run','-d','--name',name,'--cpus','8','--memory','32g','--entrypoint','/bin/bash','-v',f'{workspace}:/workspace:rw']
 for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:args+=['-e',f'{key}={PROXY}']
 args+=['-e','NO_PROXY=localhost,127.0.0.1',image,'-c','bash /workspace/entryscript.sh']
 result={'resolved':False,'infra_failure':False}
 try:
  run(args,check=True,capture_output=True)
  wait=run(['docker','wait',name],capture_output=True,timeout=3600);result['container_exit']=wait.stdout.strip()
  (td/'grading-container.log').write_text(run(['docker','logs',name],capture_output=True).stdout)
  if not (workspace/'output.json').exists():result.update(infra_failure=True,error='official parser output missing')
  else:
   output=json.loads((workspace/'output.json').read_text());passed={x['name'] for x in output['tests'] if x['status']=='PASSED'}
   f2p=set(ast.literal_eval(d['fail_to_pass']));p2p=set(ast.literal_eval(d['pass_to_pass']))
   result.update(resolved=(f2p|p2p)<=passed,fail_to_pass_missing=sorted(f2p-passed),pass_to_pass_missing=sorted(p2p-passed),tests=len(output['tests']),passed=len(passed))
   if not output['tests']:result.update(failure_category='zero_tests_needs_review',error='zero parsed tests; unresolved under official criterion; inspect compilation/test/environment logs')
 except Exception as e:result.update(infra_failure=True,error=repr(e))
 finally:run(['docker','rm','-f',name],capture_output=True)
 save(td/'grade.json',result)
 return result
def task(d,server):
 iid=d['instance_id'];td=P/'runs'/iid;td.mkdir(parents=True,exist_ok=True)
 if (td/'result.json').exists():return
 port=8000+server;name='pathctx-'+P.name+'-agent-'+hashlib.sha256(iid.encode()).hexdigest()[:12];image='jefzda/sweap-images:'+d['dockerhub_tag'];proc=None;proxy=None;stop=threading.Event();monitor_thread=None
 result=dict(infra_revision=1,instance_id=iid,repo=d['repo'],server=server,port=port,timeout_seconds=TASK_TIMEOUT,started_at=time.time())
 event(td,'task.start',**result)
 try:
  with stage(td,'wait_for_image'):
   status=P/'images'/iid/'status.json'
   while not status.exists():time.sleep(2)
   if json.loads(status.read_text())['returncode'] or run(['docker','image','inspect',image],capture_output=True).returncode:
    with (td/'demand-pull.log').open('a') as download_log:
     downloaded=run([PY,str(P/'pull_one.py'),iid],stdout=download_log,stderr=subprocess.STDOUT)
    if downloaded.returncode:raise RuntimeError('image download failed after whole-copy retries')
  with stage(td,'environment_setup'):
   po=(td/'proxy.log').open('w');proxy=subprocess.Popen([PY,str(P/'trace_proxy.py'),'--listen-host','0.0.0.0','--upstream',f'http://127.0.0.1:{port}','--trace',str(td/'model-requests.jsonl'),'--ready',str(td/'proxy-ready.json')],stdout=po,stderr=subprocess.STDOUT)
   for _ in range(100):
    if (td/'proxy-ready.json').exists():break
    if proxy.poll() is not None:raise RuntimeError('trace proxy startup failed')
    time.sleep(.1)
   proxyport=json.loads((td/'proxy-ready.json').read_text())['port']
   config={'model':'local-qwen/qwen3.8-27b','provider':{'local-qwen':{'npm':'@ai-sdk/openai-compatible','name':'Local Qwen','options':{'baseURL':f'http://172.18.0.1:{proxyport}/v1'},'models':{'qwen3.8-27b':{'name':'Qwen3.8 27B','reasoning':True,'interleaved':{'field':'reasoning'},'limit':{'context':262144,'output':32768}}}}},'plugin':['file:///swe/trace-plugin.ts'],'permission':{'*':'allow','webfetch':'deny','websearch':'deny'}}
   save(td/'opencode.json',config)
   args=['docker','run','-d','--pull','never','--name',name,'--cpus','8','--memory','32g','--network','swe-isolated','--entrypoint','/bin/bash','-v','/home/longyy/opencode:/opencode:ro','-v','/home/longyy/.bun:/root/.bun:ro','-v',f'{td}/opencode.json:/swe/opencode.json:ro','-v',f'{P}/trace-plugin.ts:/swe/trace-plugin.ts:ro','-v',f'{td}/observation:/swe/output:rw','-e','OPENCODE_CONFIG=/swe/opencode.json','-e','OPENCODE_DISABLE_MODELS_FETCH=1','-e','OPENCODE_DISABLE_AUTOUPDATE=1','-e','OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true','-e','PRO_TRACE_PATH=/swe/output/events.jsonl',image,'-c','sleep infinity']
   image_index=args.index(image)
   args[image_index:image_index]=['-v',f'{P}/infra-fix/sanitize-git.sh:/swe/sanitize-git.sh:ro','-v',f'{P}/infra-fix/bun-linux-x64-musl/bun:/swe/bun-musl:ro','-v',f'{P}/assets/config:/root/.config/opencode:ro','-v',f'{P}/assets/ripgrep-15.1.0-x86_64-unknown-linux-musl/rg:/usr/local/bin/rg:ro']
   (td/'observation').mkdir(exist_ok=True);run(args,check=True,capture_output=True)
   result['environment_runtime_start']=time.time();runtime_deadline=time.monotonic()+TASK_TIMEOUT
   base=shlex.quote(d['base_commit'])
   native=run(['docker','exec',name,'/root/.bun/bin/bun','--version'],capture_output=True)
   bun='/root/.bun/bin/bun' if native.returncode==0 else '/swe/bun-musl'
   result['bun_path']=bun
   runtime=run(['docker','exec',name,bun,'--version'],capture_output=True)
   (td/'runtime-preflight.log').write_text(native.stdout+native.stderr+runtime.stdout+runtime.stderr)
   runtime.check_returncode()
   if runtime.stdout.strip()!='1.4.2':raise RuntimeError('Unexpected Bun runtime version')
   cli=dex(name,f'cd /app && {shlex.quote(bun)} run /opencode/packages/opencode/src/index.ts --version',capture_output=True,timeout=180)
   (td/'cli-preflight.log').write_text(cli.stdout+cli.stderr);cli.check_returncode()
   setup=f'cd /app && bash /swe/sanitize-git.sh {base} && rg --version'
   setup_result=dex(name,setup,capture_output=True,timeout=300);(td/'environment-setup.log').write_text(setup_result.stdout+setup_result.stderr);setup_result.check_returncode()
   audit=dex(name,'cd /app && git remote -v && git fsck --full --no-reflogs --unreachable && git show-ref && ip route',capture_output=True,timeout=120)
   (td/'isolation-audit.log').write_text(audit.stdout+audit.stderr)
   baseline=dex(name,'cd /app && git diff --name-only -z',check=True,capture_output=True).stdout.split('\0');baseline=[x for x in baseline if x]
   untracked=set(filter(None,dex(name,'cd /app && git ls-files --others --exclude-standard -z',check=True,capture_output=True).stdout.split('\0')))
   save(td/'baseline.json',dict(base_commit=d['base_commit'],dirty=baseline,untracked=sorted(untracked)))
  sys.path.insert(0,str(OFFICIAL/'helper_code'))
  from create_problem_statement import create_problem_statement
  task_text=create_problem_statement(d)
  (td/'official-task.txt').write_text(task_text)
  prompt='Resolve the following software engineering task in the repository at /app.\n\n'+task_text
  prompt+='\n\nInspect the repository, implement the requested behavior, and run relevant tests. Do not modify existing tests, create commits, or retrieve upstream solutions. Work autonomously until the implementation is complete.'
  prompt+='\n\n'+(P/'delegation-prompt.txt').read_text()
  if P.name=='path':
   planfile=P.parent.parent/'plans'/f"{d['experiment_index']:02d}.md"
   plantext=planfile.read_text()
   prompt+='\n\nAdditional execution-route context (Markdown):\n\n'+plantext
   (td/'execution-route.md').write_text(plantext)
   result['execution_route_sha256']=hashlib.sha256(plantext.encode()).hexdigest()
  result['cache_isolation']='per-attempt cache_salt; within-task reuse preserved'
  result['experiment_arm']=P.name
  result['experiment_index']=d['experiment_index']
  (td/'prompt.txt').write_text(prompt)
  monitor_thread=threading.Thread(target=monitor,args=(td,port,stop),daemon=True);monitor_thread.start()
  with stage(td,'agent'):
   result['agent_start']=time.time()
   with (td/'opencode.log').open('w') as f:
    proc=subprocess.Popen(['docker','exec','-i',name,'bash','-lc',f'cd /app && {shlex.quote(bun)} run /opencode/packages/opencode/src/index.ts run --format json --model local-qwen/qwen3.8-27b'],stdin=subprocess.PIPE,stdout=f,stderr=subprocess.STDOUT,text=True)
    try:proc.communicate(prompt,timeout=max(0.001,runtime_deadline-time.monotonic()));result['agent_exit']=proc.returncode;result['censored']=False
    except subprocess.TimeoutExpired:
     result['censored']=True
     dex(name,"pkill -TERM -f '[b]un.*run /opencode' || true",capture_output=True)
     try:proc.wait(timeout=10)
     except subprocess.TimeoutExpired:proc.kill();proc.wait()
     result['agent_exit']=proc.returncode
   result['turn_budget']=json.loads((td/'turn-budget.json').read_text()) if (td/'turn-budget.json').exists() else {};result['termination_reason']='wall_time_limit' if result.get('censored') else ('turn_limit' if result['turn_budget'].get('exhausted') else 'agent_exit');result['agent_end']=time.time();result['agent_wall_seconds']=result['agent_end']-result['agent_start']
  stop.set();monitor_thread.join(timeout=8)
  if result['agent_exit']==127:raise RuntimeError('Agent runtime launch failed')
  with stage(td,'patch_extraction'):
   args=['docker','exec',name,'git','-C','/app','diff','--binary',d['base_commit'],'--','.']+[':(exclude)'+x for x in baseline]
   patch=run(args,check=True,capture_output=True).stdout
   now=set(filter(None,dex(name,'cd /app && git ls-files --others --exclude-standard -z',check=True,capture_output=True).stdout.split('\0')))
   for path in sorted(now-untracked):patch+=run(['docker','exec',name,'git','-C','/app','diff','--binary','--no-index','--','/dev/null',path],capture_output=True).stdout
   (td/'model.patch').write_text(patch);save(td/'prediction.json',[dict(instance_id=iid,patch=patch,prefix='opencode-qwen-pro-20260913')])
   result['patch_bytes']=len(patch.encode());result['patch_sha256']=hashlib.sha256(patch.encode()).hexdigest()
   run(['docker','cp',name+':/root/.local/share/opencode',str(td/'opencode-state')],capture_output=True)
  with stage(td,'container_cleanup'):run(['docker','rm','-f',name],capture_output=True)
  with stage(td,'grading'):result['grade']=grade(td,d,patch)
 except Exception as e:
  result.update(infra_failure=True,error=repr(e));(td/'error.log').write_text(traceback.format_exc())
 finally:
  stop.set();run(['docker','rm','-f',name],capture_output=True)
  if proxy is not None:
   proxy.terminate()
   try:proxy.wait(timeout=10)
   except subprocess.TimeoutExpired:proxy.kill()
   po.close()
  result['ended_at']=time.time();save(td/'result.json',result);event(td,'task.end',**result)
  run([PY,str(P/'analyze.py'),str(td)],stdout=(td/'analysis.log').open('w'),stderr=subprocess.STDOUT)
  print(json.dumps(result),flush=True)
def main():
 (P/'runner.pid').write_text(str(os.getpid()))
 lockfile=(P/'runner.lock').open('w');fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
 signal.signal(signal.SIGTERM,lambda *_:STOP.set())
 signal.signal(signal.SIGINT,lambda *_:STOP.set())
 os.chdir(OFFICIAL)
 for k in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ[k]=PROXY
 os.environ['NO_PROXY']=os.environ['no_proxy']='localhost,127.0.0.1,172.18.0.1'
 ds=json.loads((P/'selected.json').read_text());q=queue.Queue()
 for d in ds:q.put(d)
 def worker(server):
  while not STOP.is_set():
   try:
    if not (P/f"server-{8000+server}.json").exists():time.sleep(2);continue
    urllib.request.urlopen(f"http://127.0.0.1:{8000+server}/health",timeout=2);break
   except Exception:time.sleep(2)
  while not STOP.is_set():
   try:d=q.get_nowait()
   except queue.Empty:return
   task(d,server);q.task_done()
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(worker,[0,1]))
 save(P/('stopped.json' if STOP.is_set() else 'finished.json'),dict(at=time.time(),tasks=len(ds)))
if __name__=='__main__':main()
