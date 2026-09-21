#!/home/longyy/anaconda3/envs/opencode-dev/bin/python
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path("/home/longyy/swe-bench-env")
SKOPEO = "/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo"
REGISTRIES = str(ROOT / "registries.conf")
PROXY = "http://ubuntu-lab.zinyy.tech:20172"
STATUS = ROOT / "official-image-status.json"
RETRY_DELAY_SECONDS = 300
PRIMARY_BEFORE_RETRY = 3

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


def instance_ids():
    cached = ROOT / "instance_ids.txt"
    if cached.exists():
        result = [line for line in cached.read_text().splitlines() if line]
        if len(result) == 500:
            return result
    result = []
    for offset in range(0, 500, 100):
        query = urllib.parse.urlencode(
            {
                "dataset": "princeton-nlp/SWE-bench_Verified",
                "config": "default",
                "split": "test",
                "offset": offset,
                "length": 100,
            }
        )
        with urllib.request.urlopen(
            f"https://datasets-server.huggingface.co/rows?{query}",
            timeout=120,
        ) as response:
            result.extend(item["row"]["instance_id"] for item in json.load(response)["rows"])
    if len(result) != 500:
        raise RuntimeError(f"Expected 500 instances, received {len(result)}")
    cached.write_text("\n".join(result) + "\n")
    return result


def update_status(completed, skipped, failed, current):
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "total": 500,
                "completed": completed,
                "skipped": skipped,
                "failed": failed,
                "current": current,
                "source": "docker.io/swebench",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
        + "\n"
    )
    temporary.replace(STATUS)


def exists(image):
    return (
        subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def pull(instance):
    compatible = instance.lower().replace("__", "_1776_")
    source = f"docker://docker.io/swebench/sweb.eval.x86_64.{compatible}:latest"
    destination = f"swebench-official-cache/sweb.eval.x86_64.{compatible}:latest"
    if exists(destination):
        return "skipped", destination
    command = [
        SKOPEO,
        "--registries-conf",
        REGISTRIES,
        "copy",
        "--retry-times",
        "5",
        source,
        f"docker-daemon:{destination}",
    ]
    for attempt in range(1, 4):
        print(f"pull attempt {attempt}/3: {instance}", flush=True)
        if subprocess.run(command).returncode == 0:
            return "completed", destination
        time.sleep(attempt * 15)
    return "failed", destination


def main():
    instances = instance_ids()
    pending = deque(enumerate(instances, 1))
    retries = {}
    states = {}
    primary_since_retry = 0
    update_status(0, 0, [], instances[0])
    while pending or retries:
        due = [
            (ready_at, instance)
            for instance, ready_at in retries.items()
            if ready_at <= time.monotonic()
        ]
        if due and (not pending or primary_since_retry >= PRIMARY_BEFORE_RETRY):
            _, instance = min(due)
            retries.pop(instance)
            primary_since_retry = 0
            print(f"[retry] {instance}", flush=True)
        elif pending:
            index, instance = pending.popleft()
            primary_since_retry += 1
            print(f"[{index}/500] {instance}", flush=True)
        else:
            delay = max(0, min(retries.values()) - time.monotonic())
            print(f"waiting {delay:.0f}s for failed image retry", flush=True)
            time.sleep(delay)
            continue
        result, destination = pull(instance)
        states[instance] = result
        if result == "failed":
            retries[instance] = time.monotonic() + RETRY_DELAY_SECONDS
        failures = [item for item in instances if states.get(item) == "failed"]
        (ROOT / "official-failed-ids.txt").write_text(
            "\n".join(failures) + ("\n" if failures else "")
        )
        update_status(
            sum(result == "completed" for result in states.values()),
            sum(result == "skipped" for result in states.values()),
            failures,
            instance,
        )
        print(f"{result}: {destination}", flush=True)
    completed = sum(result == "completed" for result in states.values())
    skipped = sum(result == "skipped" for result in states.values())
    update_status(completed, skipped, [], None)
    print(
        f"finished: completed={completed} skipped={skipped} failed=0",
        flush=True,
    )


if __name__ == "__main__":
    main()
