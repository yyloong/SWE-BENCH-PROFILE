#!/home/longyy/anaconda3/envs/swebench-eval/bin/python
import json
import os

os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from swebench.harness.utils import load_swebench_dataset

ROOT = Path("/home/longyy/swe-bench-env")
RUNS = ROOT / "runs" / "opencode-qwen-strict-v5"
PREDICTIONS = ROOT / "predictions-strict-v5.jsonl"
STATUS = ROOT / "evaluation-status-strict-v5.json"
MODEL = "opencode/qwen3.8-27b"
PROXY = "http://ubuntu-lab.zinyy.tech:20172"
EVALUATION_WORKERS = int(os.environ.get("SWEBENCH_EVALUATION_WORKERS", "8"))

os.environ.update(
    {
        "HTTP_PROXY": PROXY,
        "HTTPS_PROXY": PROXY,
        "http_proxy": PROXY,
        "https_proxy": PROXY,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
)


def run(command, **kwargs):
    return subprocess.run(command, text=True, **kwargs)


def vllm_metrics():
    with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=30) as response:
        text = response.read().decode()
    names = (
        "request_prompt_tokens_sum",
        "request_generation_tokens_sum",
        "request_success_total",
        "e2e_request_latency_seconds_sum",
        "request_queue_time_seconds_sum",
        "request_inference_time_seconds_sum",
        "request_prefill_time_seconds_sum",
        "request_decode_time_seconds_sum",
        "request_prefill_kv_computed_tokens_sum",
    )
    return {
        name: sum(
            float(match.group(1))
            for match in re.finditer(
                rf"^vllm:{name}\{{[^}}]*model_name=\"qwen3\.8-27b\"[^}}]*\}}\s+([0-9.eE+-]+)$",
                text,
                re.MULTILINE,
            )
        )
        for name in names
    }


def token_count(text):
    if not text:
        return 0
    request = urllib.request.Request(
        "http://127.0.0.1:8000/tokenize",
        data=json.dumps({"model": "qwen3.8-27b", "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())["count"]


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return merged


def interval_seconds(intervals):
    return sum(end - start for start, end in merge_intervals(intervals))


def overlap_seconds(left, right):
    left = merge_intervals(left)
    right = merge_intervals(right)
    total = 0
    i = 0
    j = 0
    while i < len(left) and j < len(right):
        total += max(0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] <= right[j][1]:
            i += 1
            continue
        j += 1
    return total


def clipped(intervals, start, end):
    return [
        (max(item_start, start), min(item_end, end))
        for item_start, item_end in intervals
        if item_end > start and item_start < end
    ]


def tool_metrics(log):
    tools = {}
    intervals = []
    timestamps = []
    assistant_text_tokens = 0
    step_tokens = {
        "prompt": 0,
        "output": 0,
        "reasoning": 0,
        "generation": 0,
    }
    for line in log.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "timestamp" in event:
            timestamps.append(event["timestamp"])
        if event.get("type") == "text":
            assistant_text_tokens += token_count(event["part"].get("text", ""))
            continue
        if event.get("type") == "step_finish":
            tokens = event["part"].get("tokens", {})
            step_tokens["prompt"] += tokens.get("input", 0)
            step_tokens["output"] += tokens.get("output", 0)
            step_tokens["reasoning"] += tokens.get("reasoning", 0)
            step_tokens["generation"] += tokens.get("output", 0) + tokens.get("reasoning", 0)
            continue
        if event.get("type") != "tool_use":
            continue
        part = event["part"]
        timing = part["state"].get("time", {})
        if "start" not in timing or "end" not in timing:
            continue
        item = tools.setdefault(
            part["tool"],
            {
                "count": 0,
                "wall_seconds_sum": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        input_text = json.dumps(
            part["state"].get("input", {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        output_text = part["state"].get("output", "")
        if not isinstance(output_text, str):
            output_text = json.dumps(output_text, ensure_ascii=False, separators=(",", ":"))
        item["count"] += 1
        item["wall_seconds_sum"] += (timing["end"] - timing["start"]) / 1000
        item["input_tokens"] += token_count(input_text)
        item["output_tokens"] += token_count(output_text)
        intervals.append((timing["start"] / 1000, timing["end"] / 1000))
    for item in tools.values():
        item["wall_seconds_sum"] = round(item["wall_seconds_sum"], 3)
    return {
        "tools": tools,
        "input_tokens": sum(item["input_tokens"] for item in tools.values()),
        "output_tokens": sum(item["output_tokens"] for item in tools.values()),
        "duration_sum_seconds": round(
            sum(item["wall_seconds_sum"] for item in tools.values()),
            3,
        ),
        "wall_seconds": round(interval_seconds(intervals), 3),
        "intervals": intervals,
        "first_event": min(timestamps) / 1000 if timestamps else None,
        "last_event": max(timestamps) / 1000 if timestamps else None,
        "assistant_visible_text_tokens": assistant_text_tokens,
        "main_step_tokens": step_tokens,
    }


def model_metrics(trace, agent_start, agent_end):
    starts = {}
    requests = []
    if trace.exists():
        for line in trace.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "request_start":
                starts[event["request_id"]] = event
                continue
            if event.get("event") != "request_end":
                continue
            start = starts.get(event["request_id"], {})
            requests.append(
                {
                    "request_id": event["request_id"],
                    "kind": event.get("kind", start.get("kind", "unknown")),
                    "asynchronous": event.get("kind", start.get("kind", "unknown")) != "main",
                    "method": start.get("method"),
                    "path": start.get("path"),
                    "status": event.get("status"),
                    "started_at": event["started_at"],
                    "first_token_at": event.get("first_token_at"),
                    "ended_at": event["ended_at"],
                    "duration_seconds": event["duration_seconds"],
                    "time_to_first_token_seconds": event.get("time_to_first_token_seconds"),
                    "decode_wall_seconds": event.get("decode_wall_seconds"),
                    "usage": event.get("usage") or {},
                }
            )

    def summarize(items):
        intervals = [(item["started_at"], item["ended_at"]) for item in items]
        first_token = [
            (item["started_at"], item["first_token_at"])
            for item in items
            if item["first_token_at"] is not None
        ]
        decoding = [
            (item["first_token_at"], item["ended_at"])
            for item in items
            if item["first_token_at"] is not None
        ]
        prompt_tokens = sum(item["usage"].get("prompt_tokens", 0) for item in items)
        generation_tokens = sum(item["usage"].get("completion_tokens", 0) for item in items)
        reasoning_tokens = sum(
            item["usage"].get("completion_tokens_details", {}).get("reasoning_tokens", 0)
            for item in items
        )
        wall = interval_seconds(intervals)
        duration_sum = sum(item["duration_seconds"] for item in items)
        ttft_wall = interval_seconds(first_token)
        decode_wall = interval_seconds(decoding)
        return {
            "request_count": len(items),
            "wall_seconds": round(wall, 3),
            "request_duration_sum_seconds": round(duration_sum, 3),
            "concurrent_request_overlap_seconds": round(max(0, duration_sum - wall), 3),
            "time_to_first_token_wall_seconds": round(ttft_wall, 3),
            "decode_wall_seconds": round(decode_wall, 3),
            "ttft_decode_overlap_seconds": round(overlap_seconds(first_token, decoding), 3),
            "prompt_tokens": prompt_tokens,
            "generation_tokens": generation_tokens,
            "reasoning_tokens": reasoning_tokens,
            "output_tokens": generation_tokens - reasoning_tokens,
        }

    model_requests = [
        item
        for item in requests
        if item["path"]
        and (
            item["path"].endswith("/chat/completions")
            or item["path"].endswith("/responses")
        )
    ]
    intervals = clipped(
        [(item["started_at"], item["ended_at"]) for item in model_requests],
        agent_start,
        agent_end,
    )
    return {
        **summarize(model_requests),
        "main": summarize([item for item in model_requests if item["kind"] == "main"]),
        "asynchronous": summarize(
            [item for item in model_requests if item["kind"] != "main"]
        ),
        "by_kind": {
            kind: summarize([item for item in model_requests if item["kind"] == kind])
            for kind in sorted({item["kind"] for item in model_requests})
        },
        "intervals": intervals,
        "requests": model_requests,
        "non_model_http_request_count": len(requests) - len(model_requests),
        "incomplete_request_count": len(starts) - len(requests),
    }


def start_model_proxy(task_dir):
    trace = task_dir / "model-requests.jsonl"
    ready = task_dir / "model-proxy-ready.json"
    ready.unlink(missing_ok=True)
    output = (task_dir / "model-proxy.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "model_trace_proxy.py"),
            "--listen-host",
            "172.18.0.1",
            "--listen-port",
            "0",
            "--upstream",
            "http://127.0.0.1:8000",
            "--trace",
            str(trace),
            "--ready",
            str(ready),
        ],
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    for _ in range(100):
        if ready.exists():
            return process, output, trace, json.loads(ready.read_text())["port"]
        if process.poll() is not None:
            output.close()
            raise RuntimeError((task_dir / "model-proxy.log").read_text())
        time.sleep(0.1)
    process.terminate()
    process.wait()
    output.close()
    raise TimeoutError("model tracing proxy did not start")


def stop_model_proxy(process, output):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if output is not None:
        output.close()


def write_opencode_config(task_dir, port):
    config = json.loads((ROOT / "opencode.jsonc").read_text())
    config["provider"]["local-qwen"]["options"]["baseURL"] = (
        f"http://172.18.0.1:{port}/v1"
    )
    path = task_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def image_names(instance_id):
    compatible = instance_id.lower().replace("__", "_1776_")
    return (
        f"swebench-official-cache/sweb.eval.x86_64.{compatible}:latest",
        f"swebench/sweb.eval.x86_64.{compatible}:latest",
    )


def available_images():
    result = run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
    )
    if result.returncode:
        return set()
    return set(result.stdout.splitlines())


def predictions():
    if not PREDICTIONS.exists():
        return {}
    records = [
        json.loads(line)
        for line in PREDICTIONS.read_text().splitlines()
        if line.strip()
    ]
    return {record["instance_id"]: record for record in records}


def update_status(total, generated, evaluated, resolved, failed, current, phase, evaluations):
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "total": total,
                "generated": generated,
                "completed": evaluated,
                "resolved": resolved,
                "failed": failed,
                "current": current,
                "phase": phase,
                "evaluation_workers": EVALUATION_WORKERS,
                "evaluations_in_flight": evaluations,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
        + "\n"
    )
    temporary.replace(STATUS)


def git(container, *args):
    result = run(
        ["docker", "exec", container, "git", "-C", "/testbed", *args],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


def generate_patch(instance):
    instance_id = instance["instance_id"]
    safe = instance_id.lower().replace("__", "-").replace("_", "-")
    container = f"oc-{safe}"[:63]
    task_dir = RUNS / instance_id
    task_dir.mkdir(parents=True, exist_ok=True)
    local_image, expected_image = image_names(instance_id)
    run(["docker", "tag", local_image, expected_image], check=True)
    run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy_process = None
    proxy_output = None
    try:
        proxy_process, proxy_output, trace, proxy_port = start_model_proxy(task_dir)
        config = write_opencode_config(task_dir, proxy_port)
        run(
            [
                "docker",
                "run",
                "-d",
                "--pull",
                "never",
                "--name",
                container,
                "--network",
                "swe-isolated",
                "-v",
                "/home/longyy/opencode:/opencode:ro",
                "-v",
                "/home/longyy/.bun:/root/.bun:ro",
                "-v",
                f"{config}:/swe/opencode.json:ro",
                "-e",
                "OPENCODE_CONFIG=/swe/opencode.json",
                "-e",
                "OPENCODE_DISABLE_MODELS_FETCH=1",
                "-e",
                "OPENCODE_DISABLE_AUTOUPDATE=1",
                expected_image,
                "sleep",
                "infinity",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        base_commit = git(container, "rev-parse", "HEAD").strip()
        baseline_dirty = set(git(container, "diff", "--name-only").splitlines())
        baseline_untracked = set(
            git(container, "ls-files", "--others", "--exclude-standard").splitlines()
        )
        prompt = (
            "Fix the following issue in the repository at /testbed.\n\n"
            + instance["problem_statement"]
            + "\n\nInspect the implementation, identify the root cause, implement the minimal "
            "correct fix, and run relevant tests. Do not modify tests and do not create commits. "
            "Work autonomously until the implementation is complete."
        )
        log = task_dir / "opencode.log"
        command = [
            "docker",
            "exec",
            "-i",
            container,
            "bash",
            "-lc",
            "cd /testbed && /root/.bun/bin/bun run /opencode/packages/opencode/src/index.ts "
            "run --format json --model local-qwen/qwen3.8-27b",
        ]
        before = vllm_metrics()
        started_wall = time.time()
        started = time.monotonic()
        with log.open("w") as output:
            process = subprocess.Popen(
                command,
                text=True,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            process.stdin.write(prompt)
            process.stdin.close()
            try:
                process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                output.write("\nTIMEOUT after 1800 seconds\n")
        ended = time.monotonic()
        ended_wall = time.time()
        after = vllm_metrics()
        exclusions = [f":(exclude){path}" for path in sorted(baseline_dirty)]
        patch = git(container, "diff", "--binary", base_commit, "--", ".", *exclusions)
        current_untracked = set(
            git(container, "ls-files", "--others", "--exclude-standard").splitlines()
        )
        for path in sorted(current_untracked - baseline_untracked):
            extra = run(
                [
                    "docker",
                    "exec",
                    container,
                    "git",
                    "-C",
                    "/testbed",
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    "/dev/null",
                    path,
                ],
                capture_output=True,
            ).stdout
            patch += extra
        (task_dir / "model.patch").write_text(patch)
        events = tool_metrics(log)
        delta = {key: after[key] - before[key] for key in before}
        agent_seconds = ended_wall - started_wall
        model = model_metrics(trace, started_wall, ended_wall)
        model_intervals = model.pop("intervals")
        tool_intervals = clipped(events.pop("intervals"), started_wall, ended_wall)
        model_wall = interval_seconds(model_intervals)
        tool_wall = interval_seconds(tool_intervals)
        model_tool_overlap = overlap_seconds(model_intervals, tool_intervals)
        combined_wall = interval_seconds(model_intervals + tool_intervals)
        startup = max(0, (events["first_event"] or started_wall) - started_wall)
        shutdown = max(0, ended_wall - (events["last_event"] or ended_wall))
        wall_breakdown = {
            "model_only_seconds": round(model_wall - model_tool_overlap, 3),
            "tool_only_seconds": round(tool_wall - model_tool_overlap, 3),
            "model_tool_overlap_seconds": round(model_tool_overlap, 3),
            "other_orchestration_and_idle_seconds": round(
                max(0, agent_seconds - combined_wall),
                3,
            ),
        }
        (task_dir / "timing.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "clock_basis": (
                        "Wall-clock interval unions. Concurrent model requests and tools "
                        "are counted once in the top-level breakdown."
                    ),
                    "agent_wall_seconds": round(agent_seconds, 3),
                    "agent_monotonic_seconds": round(ended - started, 3),
                    "wall_breakdown": wall_breakdown,
                    "startup_seconds": round(startup, 3),
                    "shutdown_seconds": round(shutdown, 3),
                    "model": model,
                    "tool_execution": events,
                    "tokens": {
                        "task_prompt_tokens": token_count(prompt),
                        "model_all_requests": {
                            "prompt": model["prompt_tokens"],
                            "generation": model["generation_tokens"],
                            "reasoning": model["reasoning_tokens"],
                            "output": model["output_tokens"],
                        },
                        "model_main_steps_from_opencode": events["main_step_tokens"],
                        "assistant_visible_text": events["assistant_visible_text_tokens"],
                        "tool_call_arguments": events["input_tokens"],
                        "tool_outputs": events["output_tokens"],
                        "notes": {
                            "tool_call_arguments": (
                                "Generated by the model and already included in model "
                                "generation tokens."
                            ),
                            "tool_outputs": (
                                "Tokens returned by tools and admitted back into later "
                                "model contexts."
                            ),
                        },
                    },
                    "vllm_global_metric_deltas": {
                        **{key: round(value, 6) for key, value in delta.items()},
                        "warning": (
                            "Diagnostic only. These are process-global counter deltas and "
                            "request-duration sums, not exclusive wall-clock partitions."
                        ),
                    },
                },
                indent=2,
            )
            + "\n"
        )
        return patch
    finally:
        run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stop_model_proxy(proxy_process, proxy_output)


def append_prediction(instance_id, patch):
    prediction = {
        "instance_id": instance_id,
        "model_name_or_path": MODEL,
        "model_patch": patch,
    }
    with PREDICTIONS.open("a") as output:
        output.write(json.dumps(prediction) + "\n")
    path = RUNS / instance_id / "prediction.jsonl"
    path.write_text(json.dumps(prediction) + "\n")
    return path


def run_id(instance_id):
    safe = instance_id.lower().replace("__", "-").replace("_", "-")
    return f"opencode-qwen-strict-v5-{safe}"


def evaluation_report(instance_id):
    return ROOT / "logs" / "evaluation" / run_id(instance_id) / "results.json"


def evaluation_outcome(instance_id):
    report = evaluation_report(instance_id)
    if not report.exists():
        return None
    return instance_id in json.loads(report.read_text())["resolved_ids"]


def write_prediction(instance_id, prediction):
    path = RUNS / instance_id / "prediction.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prediction) + "\n")
    return path


def evaluate(instance_id, prediction):
    log = RUNS / instance_id / "evaluation.log"
    with log.open("w") as output:
        result = run(
            [
                "/home/longyy/anaconda3/envs/swebench-eval/bin/python",
                "-m",
                "swebench.harness.run_evaluation",
                "--dataset_name",
                "SWE-bench/SWE-bench_Verified",
                "--predictions_path",
                str(prediction),
                "--max_workers",
                "1",
                "--run_id",
                run_id(instance_id),
                "--instance_ids",
                instance_id,
            ],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    report = evaluation_report(instance_id)
    if result.returncode or not report.exists():
        return False
    return instance_id in json.loads(report.read_text())["resolved_ids"]


def main():
    dataset = load_swebench_dataset("SWE-bench/SWE-bench_Verified", split="test")
    records = predictions()
    outcomes = {
        instance_id: outcome
        for instance_id in records
        if (outcome := evaluation_outcome(instance_id)) is not None
    }
    generation_failed = set()
    evaluations = {}

    def status(current, phase):
        failed = sorted(
            generation_failed
            | {instance_id for instance_id, outcome in outcomes.items() if not outcome}
        )
        update_status(
            len(dataset),
            len(records),
            len(outcomes) + len(generation_failed),
            sum(outcomes.values()),
            failed,
            current,
            phase,
            sorted(evaluations.values()),
        )

    def collect(block):
        if not evaluations:
            return
        done, _ = wait(
            evaluations,
            timeout=None if block else 0,
            return_when=FIRST_COMPLETED,
        )
        for future in done:
            instance_id = evaluations.pop(future)
            try:
                outcomes[instance_id] = future.result()
            except Exception as error:
                outcomes[instance_id] = False
                (RUNS / instance_id / "evaluation-error.log").write_text(repr(error) + "\n")

    with ThreadPoolExecutor(max_workers=EVALUATION_WORKERS) as executor:
        for instance_id, prediction in records.items():
            if instance_id in outcomes:
                continue
            evaluations[
                executor.submit(
                    evaluate,
                    instance_id,
                    write_prediction(instance_id, prediction),
                )
            ] = instance_id

        pending = [
            instance for instance in dataset if instance["instance_id"] not in records
        ]
        while pending:
            collect(False)
            images = available_images()
            ready = next(
                (
                    (index, instance)
                    for index, instance in enumerate(pending)
                    if image_names(instance["instance_id"])[0] in images
                ),
                None,
            )
            if ready is None:
                status(None, "waiting_for_any_image")
                time.sleep(30)
                continue
            index, instance = ready
            pending.pop(index)
            instance_id = instance["instance_id"]
            status(instance_id, "generating")
            try:
                patch = generate_patch(instance)
                prediction = append_prediction(instance_id, patch)
                records[instance_id] = {
                    "instance_id": instance_id,
                    "model_name_or_path": MODEL,
                    "model_patch": patch,
                }
                evaluations[
                    executor.submit(evaluate, instance_id, prediction)
                ] = instance_id
            except Exception as error:
                generation_failed.add(instance_id)
                task_dir = RUNS / instance_id
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "error.log").write_text(repr(error) + "\n")

        while evaluations:
            status(None, "draining_evaluations")
            collect(True)
    status(None, "finished")


if __name__ == "__main__":
    main()
