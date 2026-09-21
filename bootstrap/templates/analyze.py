import collections,csv,json,re,sys,time
from pathlib import Path
def read(path):
 if not path.exists():return []
 out=[]
 for line in path.open():
  try:out.append(json.loads(line))
  except json.JSONDecodeError:pass
 return out
def union(xs):
 out=[]
 for a,b in sorted(xs):
  if b<=a:continue
  if out and a<=out[-1][1]:out[-1][1]=max(out[-1][1],b)
  else:out.append([a,b])
 return out
def seconds(xs):return sum(b-a for a,b in union(xs))
def partition(groups,start,end):
 events=collections.defaultdict(collections.Counter)
 for name,xs in groups.items():
  for a,b in union(xs):
   a=max(start,a);b=min(end,b)
   if b>a:events[a][name]+=1;events[b][name]-=1
 counts=collections.Counter();result=collections.Counter();last=start
 for at,changes in sorted(events.items()):
  label=' + '.join(sorted(k for k,v in counts.items() if v>0)) or 'orchestration_or_idle'
  result[label]+=at-last;counts.update(changes);last=at
 label=' + '.join(sorted(k for k,v in counts.items() if v>0)) or 'orchestration_or_idle';result[label]+=end-last
 return dict(result)
def analyze(td):
 result=json.loads((td/'result.json').read_text()) if (td/'result.json').exists() else {}
 life=read(td/'lifecycle.jsonl');stage_starts={};stage_ends={};stages={}
 for e in life:
  if e['kind']=='stage.start':stage_starts[e['stage']]=e['at']
  if e['kind']=='stage.end' and e['stage'] in stage_starts:stages[e['stage']]=e['at']-stage_starts[e['stage']];stage_ends[e['stage']]=e['at']
 start=result.get('agent_start',stage_starts.get('agent',time.time()));end=result.get('agent_end',stage_ends.get('agent',time.time()))
 sessions={};parts={};messages={};before={};after={}
 for e in read(td/'observation/events.jsonl'):
  if e['at']>end:continue
  data=e['data']
  if e['kind']=='tool.before':before[(data['input']['sessionID'],data['input']['callID'])]=e
  if e['kind']=='tool.after':after[(data['input']['sessionID'],data['input']['callID'])]=e
  if e['kind']!='event':continue
  props=data.get('properties',{});typ=data.get('type','')
  if typ in ['session.created','session.updated']:
   info=props.get('info',{});sessions[info.get('id')]=info
  if typ=='message.updated':
   info=props.get('info',{});messages[info.get('id')]=info
  if typ=='message.part.updated':
   part=props.get('part',{});parts[part.get('id')]=part
 roots={sid for sid,s in sessions.items() if not s.get('parentID')}
 def role(sid,agent):
  if agent in ['title','compaction','summary']:return agent
  if sessions.get(sid,{}).get('parentID'):return 'subagent'
  if sid in roots or agent=='build':return 'main'
  return 'unattributed'
 requests={}
 for e in read(td/'model-requests.jsonl'):
  observed=e.get('received_at',e.get('ended_at',e.get('first_token_at',e.get('started_at',0))))
  if observed and observed>end:continue
  rid=e['request_id'];r=requests.setdefault(rid,{'request_id':rid,'chunks':[]})
  if e['event']=='request_start':
   r.update({k:e.get(k) for k in ['session_id','agent','message_id','started_at','kind']});r['tools_offered']=[t.get('function',{}).get('name') for t in (e.get('payload') or {}).get('tools',[])]
   r['max_tokens']=(e.get('payload') or {}).get('max_tokens')
  if e['event']=='request_end':r.update({k:e.get(k) for k in ['ended_at','first_token_at','usage','status']})
  if e['event']=='response_chunk':r['chunks'].append((e['received_at'],e['chunk']))
 groups=collections.defaultdict(list);rows=[];timeline=[]
 for r in requests.values():
  if not r.get('started_at') or r['started_at']>end:continue
  r['in_progress']=not bool(r.get('ended_at'))
  r['role']=role(r.get('session_id'),r.get('agent'));ids=[];metrics={};bounds={};previous=r['started_at'];phases=collections.defaultdict(list)
  for at,ch in r.pop('chunks'):
   if ch.get('metrics'):metrics=ch['metrics']
   if ch.get('usage'):r['usage']=ch['usage']
   active=set()
   for choice in ch.get('choices',[]):
    delta=choice.get('delta',{});ids+=choice.get('token_ids') or []
    if delta.get('reasoning') or delta.get('reasoning_content'):active.add('reasoning')
    if delta.get('content'):active.add('visible_output')
    if delta.get('tool_calls'):active.add('tool_arguments')
   if active:
    label='+'.join(sorted(active))
    if not bounds:r['first_content_delivery_at']=at
    else:phases[label].append((previous,at))
    for label2 in active:bounds.setdefault(label2,[]).append(at)
    previous=at
  r['ended_at']=r.get('ended_at') or end;r['wall_seconds']=r['ended_at']-r['started_at'];r['metrics']=metrics;r['token_ids_count']=len(ids);r['usage']=r.get('usage') or {}
  r['reasoning_tokens_from_ids']=ids.index(248069) if 248069 in ids else None
  r['phase_delivery_seconds']={k:seconds(v) for k,v in phases.items()};r['phase_boundaries']={k:[v[0],v[-1]] for k,v in bounds.items()}
  first=r.get('first_content_delivery_at');r['ttft_wall_seconds']=(first-r['started_at']) if first else None
  groups['model'].append((r['started_at'],r['ended_at']))
  if first:groups['waiting_first_token'].append((r['started_at'],first))
  for ph,xs in phases.items():groups[ph].extend(xs)
  timeline.append(dict(kind='model',label=r['role']+':'+str(r.get('agent')),session_id=r.get('session_id'),start=r['started_at'],end=r['ended_at'],request_id=r['request_id']))
  rows.append(r)
 tools=[]
 for part in parts.values():
  if part.get('type')!='tool':continue
  state=part.get('state',{});timing=state.get('time',{});sid=part.get('sessionID');cid=part.get('callID');key=(sid,cid)
  a=timing.get('start',0)/1000 or before.get(key,{}).get('at');b=timing.get('end',0)/1000 or after.get(key,{}).get('at')
  if not a:continue
  tool=dict(tool=part['tool'],session_id=sid,call_id=cid,start=a,end=b or end,censored=not bool(b),status=state.get('status'),input=state.get('input'),output=state.get('output'),error=state.get('error'),metadata=state.get('metadata'),role=role(sid,None))
  tool['wall_seconds']=tool['end']-tool['start'];tools.append(tool)
  command=str((tool.get('input') or {}).get('command',''))
  if tool['tool']=='bash':
   categories=[]
   for category,pattern in [('test',r'pytest|ansible-test|\bmocha\b|\bjest\b|\bvitest\b|\bgo test\b|\b(npm|yarn|pnpm|bun)\s+(run\s+)?test\b'),('dependency_or_build',r'\b(pip|npm|yarn|pnpm)\s+(install|ci)\b|\b(make|cmake|cargo build|go build)\b'),('git',r'\bgit\s'),('search_or_read',r'\b(rg|grep|cat|sed|head|tail|find|ls)\s'),('network_attempt',r'https?://|\b(curl|wget)\s')]:
    if re.search(pattern,command):categories.append(category)
   tool['command_categories_heuristic']=categories or ['other']
  # task is a parent wait span, not external execution; child model/tool spans are observed separately.
  if tool['tool']=='task':groups['subagent_parent_wait'].append((a,tool['end']))
  else:groups['external_tool'].append((a,tool['end']))
  timeline.append(dict(kind='tool',label=tool['tool'],session_id=sid,start=a,end=tool['end'],call_id=cid))
 def summarize(rs):
  specs=[r['metrics'].get('speculative_decoding',{}) for r in rs];steps=sum(s.get('num_spec_steps',0) for s in specs);accepted=sum(s.get('num_accepted_draft_tokens',0) for s in specs)
  return dict(requests=len(rs),requests_in_progress=sum(r['in_progress'] for r in rs),received_token_ids=sum(r['token_ids_count'] for r in rs),request_seconds_sum=sum(r['wall_seconds'] for r in rs),request_wall_union=seconds([(r['started_at'],r['ended_at']) for r in rs]),prompt_tokens=sum(r['usage'].get('prompt_tokens',0) for r in rs),completion_tokens=sum(r['usage'].get('completion_tokens',0) for r in rs),reasoning_tokens=sum(r['usage'].get('completion_tokens_details',{}).get('reasoning_tokens',0) for r in rs),server_generation_seconds_sum=sum(r['metrics'].get('generation_time_ms',0)/1000 for r in rs),server_ttft_seconds_sum=sum(r['metrics'].get('time_to_first_token_ms',0)/1000 for r in rs),server_queue_seconds_sum=sum(r['metrics'].get('queue_time_ms',0)/1000 for r in rs),speculative_steps=steps,accepted_draft_tokens=accepted,mean_acceptance_length=(1+accepted/steps) if steps else None)
 bytool={}
 for name in sorted({t['tool'] for t in tools}):
  ts=[t for t in tools if t['tool']==name];bytool[name]=dict(count=len(ts),seconds_sum=sum(t['wall_seconds'] for t in ts),wall_union=seconds([(t['start'],t['end']) for t in ts]),errors=sum(t['status']=='error' for t in ts),unfinished=sum(t['censored'] for t in ts))
 subs=[]
 for sid,s in sessions.items():
  if not s.get('parentID'):continue
  rs=[r for r in rows if r.get('session_id')==sid];ts=[t for t in tools if t['session_id']==sid]
  spans=[(r['started_at'],r['ended_at']) for r in rs]+[(t['start'],t['end']) for t in ts]
  subs.append(dict(session_id=sid,parent_id=s.get('parentID'),title=s.get('title'),model=summarize(rs),tool_calls=len(ts),active_wall_union=seconds(spans),observed_lifetime=(max(b for a,b in spans)-min(a for a,b in spans)) if spans else 0))
 samples=read(td/'server-metrics.jsonl');sampled_counters={}
 def counters(text):
  values=collections.Counter()
  for line in text.splitlines():
   match=re.match(r'(vllm:[A-Za-z0-9_:]+)(?:\{[^}]*\})?\s+([-+0-9.eE]+)$',line)
   if match and (match[1].endswith('_sum') or match[1].endswith('_total')):values[match[1]]+=float(match[2])
  return values
 if len(samples)>=2:
  first,last=counters(samples[0]['metrics']),counters(samples[-1]['metrics'])
  sampled_counters=dict(start=samples[0]['at'],end=samples[-1]['at'],delta={k:last[k]-first.get(k,0) for k in last},note='Sampled service counters, not exact task-boundary deltas; requests completing after the last snapshot are excluded.')
 if samples and result.get('agent_end'):
  global_samples=[s for s in read(td.parent.parent/'service-counters.jsonl') if s.get('port')==result.get('port') and s.get('values') and s['at']>=end]
  if global_samples:
   after=min(global_samples,key=lambda s:s['at']);first=counters(samples[0]['metrics']);last=after['values']
   sampled_counters=dict(start=samples[0]['at'],end=after['at'],post_agent_sample_delay_seconds=after['at']-end,delta={k:last[k]-first[k] for k in first if k in last},note='Before-agent local snapshot to first global snapshot after agent exit; exclusive server per task. Inspect sample delay and lifecycle before treating as task counters.')
 summary=dict(instance_id=result.get('instance_id',td.name),in_progress=not (td/'result.json').exists(),agent_wall_seconds=max(0,end-start),censored=result.get('censored'),stages=stages,exclusive_wall_partition=partition({k:groups[k] for k in ['model','external_tool']},start,end),phase_overlap_partition=partition({k:v for k,v in groups.items() if k not in ['model','subagent_parent_wait']},start,end),model=summarize(rows),model_by_role={role_:summarize([r for r in rows if r['role']==role_]) for role_ in sorted({r['role'] for r in rows})},tools=bytool,subagents=subs,sessions=sessions,grade=result.get('grade'),measurement_notes=['Wall partitions use interval unions; sums can exceed task wall time under concurrency.','task tool is parent waiting on a subagent; excluded from external-tool partition to avoid double counting.','TTFT includes queue, prefill and transport; server metric breakdown retained separately.','Phase delivery intervals are stream-boundary estimates; mixed chunks retained as mixed, first chunk generation not attributed to a phase.','Timing instrumentation and plugin overhead included in agent wall time; not a CUDA-kernel profile.'])
 summary['sampled_server_counter_deltas']=sampled_counters
 summary['tool_categories_heuristic']={category:dict(calls=len(ts),seconds_sum=sum(t['wall_seconds'] for t in ts),wall_union=seconds([(t['start'],t['end']) for t in ts])) for category in sorted({c for t in tools for c in t.get('command_categories_heuristic',[])}) for ts in [[t for t in tools if category in t.get('command_categories_heuristic',[])]]}
 summary['subagent_tool_offered']=any('task' in r['tools_offered'] for r in rows)
 for filename,data in [('timing.json',summary),('requests.json',rows),('tools.json',tools),('timeline.json',timeline),('messages.json',messages)]:
  import os
  temp=td/(filename+f'.{os.getpid()}.tmp');temp.write_text(json.dumps(data,indent=2));temp.replace(td/filename)
 with (td/'timeline.csv').open('w') as f:
  w=csv.DictWriter(f,fieldnames=['kind','label','session_id','start','end','request_id','call_id'],extrasaction='ignore');w.writeheader();w.writerows(timeline)
 import html
 lanes=sorted({str(x.get('session_id'))+' / '+x['kind']+' / '+x['label'] for x in timeline});duration=max(1,end-start);width=1200;height=70+24*len(lanes)
 svg=[f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
 for i in range(11):
  x=300+880*i/10;svg.append(f'<line x1="{x}" x2="{x}" y1="30" y2="{height}" stroke="#ddd"/><text x="{x}" y="20" font-size="11">{duration*i/10:.0f}s</text>')
 for n,lane in enumerate(lanes):
  y=50+n*24;svg.append(f'<text x="4" y="{y+11}" font-size="10">{html.escape(lane[-53:])}</text>')
 for item in timeline:
  lane=str(item.get('session_id'))+' / '+item['kind']+' / '+item['label'];y=50+lanes.index(lane)*24;x=300+880*max(0,item['start']-start)/duration;w=max(.5,880*(item['end']-item['start'])/duration);color='#4679c6' if item['kind']=='model' else '#dd9340'
  desc=f"{item['label']} | {item['start']-start:.3f}–{item['end']-start:.3f}s | {item['end']-item['start']:.3f}s | {item.get('session_id')}"
  svg.append(f'<rect class="{item["kind"]}" x="{x}" y="{y}" width="{w}" height="15" fill="{color}"><title>{html.escape(desc)}</title></rect>')
 svg.append('</svg>')
 (td/'timeline.html').write_text('<!doctype html><meta charset="utf-8"><title>任务时间线</title><style>body{font:15px system-ui;margin:24px;color:#222}svg{background:#fafafa}#chart{overflow:auto}label{margin-right:20px}</style><h1>'+html.escape(td.name)+'</h1><p>蓝色：模型请求；橙色：工具。每行对应 session 和调用类型。悬停查看时间；task 行是父 agent 等待区间，不能与子任务耗时重复相加。</p><label><input checked type="checkbox" onchange="document.querySelectorAll(\'.model\').forEach(x=>x.style.opacity=this.checked?1:0)">模型</label><label><input checked type="checkbox" onchange="document.querySelectorAll(\'.tool\').forEach(x=>x.style.opacity=this.checked?1:0)">工具</label><label>缩放 <input type="range" min="1" max="8" step=".5" value="1" oninput="document.querySelector(\'svg\').style.width=(1200*this.value)+\'px\'"></label><div id="chart">'+''.join(svg)+'</div><p><a href="timing.json">完整统计</a> · <a href="requests.json">模型请求</a> · <a href="tools.json">工具参数与结果</a></p>')
 return summary
if __name__=='__main__':
 td=Path(sys.argv[1]);print(json.dumps(analyze(td),indent=2))
