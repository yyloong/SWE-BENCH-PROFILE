#!/usr/bin/env bash
set -euo pipefail

parent_pid="$1"
instance_id="$2"
status_file="/home/longyy/swe-bench-env/evaluation-status-strict-v3.json"
python="/home/longyy/anaconda3/envs/swebench-eval/bin/python"
evaluator="/home/longyy/swe-bench-env/evaluate_verified.py"
log="/home/longyy/swe-bench-env/logs/evaluate-verified-strict-v3.log"

echo "waiting for ${instance_id} to enter official evaluation"
while kill -0 "$parent_pid" 2>/dev/null; do
  read -r current phase < <(
    "$python" -c \
      'import json, sys; state=json.load(open(sys.argv[1])); print(state.get("current"), state.get("phase"))' \
      "$status_file"
  )
  if [[ "$current" == "$instance_id" && "$phase" == "evaluating" ]]; then
    break
  fi
  sleep 2
done

if kill -0 "$parent_pid" 2>/dev/null; then
  kill -STOP "$parent_pid"
  echo "old evaluator paused; waiting for official evaluation child"
  while true; do
    active=0
    while read -r state; do
      if [[ -n "$state" && "$state" != Z* ]]; then
        active=1
      fi
    done < <(ps -o stat= --ppid "$parent_pid")
    if [[ "$active" == 0 ]]; then
      break
    fi
    sleep 5
  done
  kill -TERM "$parent_pid" 2>/dev/null || true
  kill -CONT "$parent_pid" 2>/dev/null || true
  sleep 1
fi

echo "starting evaluator with clock-time tracing"
nohup sg docker -c "exec $python $evaluator" >>"$log" 2>&1 </dev/null &
echo "new evaluator wrapper pid: $!"
