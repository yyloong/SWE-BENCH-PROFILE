import concurrent.futures,hashlib,json,os,subprocess,time
from pathlib import Path
P=Path(__file__).resolve().parent
for k in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ[k]='http://ubuntu-lab.zinyy.tech:20172'
os.environ['NO_PROXY']=os.environ['no_proxy']='localhost,127.0.0.1'
def source_env(proxy):
 env=os.environ.copy()
 for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:env[key]=proxy
 env['NO_PROXY']=env['no_proxy']='localhost,127.0.0.1'
 return env

def pull(d):
 import fcntl
 iid=d['instance_id'];image='jefzda/sweap-images:'+d['dockerhub_tag'];out=P/'images'/iid;out.mkdir(parents=True,exist_ok=True)
 with (out/'pull.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  start=time.time();attempts=[]
  inspect=lambda:subprocess.run(['docker','image','inspect',image],capture_output=True,text=True)
  current=inspect()
  if current.returncode==0:rc=0
  else:
   if (out/'status.json').exists():
    (out/f"status-before-retry-{time.time_ns()}.json").write_bytes((out/'status.json').read_bytes())
    (out/'status.json').unlink()
   # Resolve the immutable manifest at the official origin; mirrors receive only
   # this digest, never an unchecked mutable tag.
   origin_env=source_env('http://ubuntu-lab.zinyy.tech:20172')
   manifest=None
   for _ in range(3):
    try:
     ref=subprocess.run(['/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo','--registries-conf','/home/longyy/swe-bench-env/registries.conf','--command-timeout','45s','inspect','--raw','docker://docker.io/'+image],capture_output=True,env=origin_env,timeout=50)
     if ref.returncode==0:manifest=ref.stdout;break
    except subprocess.TimeoutExpired:pass
   if manifest is None:
    meta=dict(instance_id=iid,image=image,returncode=1,start=start,end=time.time(),error='Could not resolve official manifest; no unverified mirror used')
    temp=out/'status.json.tmp';temp.write_text(json.dumps(meta,indent=2));temp.replace(out/'status.json');return meta
   manifest_digest='sha256:'+hashlib.sha256(manifest).hexdigest()
   (out/'official-manifest.json').write_bytes(manifest)
   official_config=None
   for _ in range(3):
    try:
     config_ref=subprocess.run(['/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo','--registries-conf','/home/longyy/swe-bench-env/registries.conf','--command-timeout','45s','inspect','--config','docker://docker.io/jefzda/sweap-images@'+manifest_digest],capture_output=True,env=origin_env,timeout=50)
     if config_ref.returncode==0:
      official_config=json.loads(config_ref.stdout);break
    except (subprocess.TimeoutExpired,json.JSONDecodeError):pass
   if official_config is None:
    meta=dict(instance_id=iid,image=image,returncode=1,start=start,end=time.time(),error='Official small config verification failed; no image layers downloaded')
    temp=out/'status.json.tmp';temp.write_text(json.dumps(meta,indent=2));temp.replace(out/'status.json');return meta
   (out/'official-config.json').write_bytes(config_ref.stdout)
   routes=[('docker.1ms.run','http://pc.zinyy.tech:7899')]*3
   rc=1
   for attempt in range(3):
    logfile=out/f'pull-{time.time_ns()}.log';began=time.time()
    with logfile.open('w') as log:
     try:
      rc=subprocess.run(['/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo','--registries-conf','/home/longyy/swe-bench-env/registries.conf','copy','--retry-times','5','docker://'+routes[attempt][0]+'/jefzda/sweap-images@'+manifest_digest,'docker-daemon:'+image],stdout=log,stderr=subprocess.STDOUT,env=source_env(routes[attempt][1]),timeout=1800).returncode
     except subprocess.TimeoutExpired:rc=124
    attempts.append(dict(source=routes[attempt][0],proxy=routes[attempt][1],manifest_digest=manifest_digest,attempt=attempt+1,start=began,end=time.time(),returncode=rc,log=str(logfile)))
    if rc==0:break
    if attempt<2:time.sleep(5*(attempt+1))
  meta=dict(instance_id=iid,image=image,returncode=rc,start=start,end=time.time(),attempts=attempts)
  if rc==0:
   current=inspect()
   if current.returncode:meta.update(returncode=current.returncode,error='image absent after successful copy')
   else:
    meta['inspect']=json.loads(current.stdout)[0]
    if 'manifest' in locals():
     expected=json.loads(manifest).get('config',{}).get('digest')
     meta['official_manifest_digest']=manifest_digest
     # docker-daemon transport can reserialize config JSON, changing Image ID.
     # Compare the runtime config and uncompressed layer digests instead.
     actual=meta['inspect']
     match=(official_config['config']==actual['Config'] and official_config['rootfs']['diff_ids']==actual['RootFS']['Layers'] and official_config.get('os')==actual.get('Os') and official_config.get('architecture')==actual.get('Architecture'))
     meta['official_config_and_layers_match']=match
     if not match:meta.update(returncode=1,error='Imported runtime config or filesystem differs from official image')
  temp=out/'status.json.tmp';temp.write_text(json.dumps(meta,indent=2));temp.replace(out/'status.json')
  print(iid,meta['returncode'],round(time.time()-start,1),flush=True)
  return meta
if __name__=='__main__':
 import shutil
 tasks=json.loads((P/'selected.json').read_text())
 for d in tasks:
  if (P/'stop-downloader').exists():break
  if (P/'runs'/d['instance_id']/'result.json').exists():continue
  while True:
   if (P/'stop-downloader').exists():raise SystemExit(0)
   ready=sum((P/'images'/x['instance_id']/'status.json').exists() and not (P/'runs'/x['instance_id']/'result.json').exists() for x in tasks)
   if ready<12 and shutil.disk_usage('/').free>45*1024**3:break
   time.sleep(10)
  pull(d)
