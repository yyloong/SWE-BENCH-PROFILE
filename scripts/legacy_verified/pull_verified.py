#!/home/longyy/anaconda3/envs/opencode-dev/bin/python
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/home/longyy/swe-bench-env")
SKOPEO = "/home/longyy/anaconda3/envs/opencode-dev/bin/skopeo"
REGISTRIES = str(ROOT / "registries.conf")
PROXY = "http://ubuntu-lab.zinyy.tech:20172"
STATUS = ROOT / "status.json"

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
            f"https://datasets-server.huggingface.co/rows?{query}", timeout=120
        ) as response:
            result.extend(item["row"]["instance_id"] for item in json.load(response)["rows"])
    if len(result) != 500:
        raise RuntimeError(f"Expected 500 instances, received {len(result)}")
    (ROOT / "instance_ids.txt").write_text("\n".join(result) + "\n")
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
    source = f"docker://ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance.lower()}:latest"
    compatible = instance.lower().replace("__", "_1776_")
    destination = f"sweb.eval.x86_64/sweb.eval.x86_64.{compatible}:latest"
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
    completed = 0
    skipped = 0
    failures = []
    instances = instance_ids()
    update_status(completed, skipped, failures, instances[0])
    for index, instance in enumerate(instances, 1):
        print(f"[{index}/500] {instance}", flush=True)
        result, destination = pull(instance)
        if result == "completed":
            completed += 1
        elif result == "skipped":
            skipped += 1
        else:
            failures.append(instance)
        update_status(completed, skipped, failures, instance)
        print(f"{result}: {destination}", flush=True)
    (ROOT / "failed_ids.txt").write_text("\n".join(failures) + ("\n" if failures else ""))
    update_status(completed, skipped, failures, None)
    print(
        f"finished: completed={completed} skipped={skipped} failed={len(failures)}",
        flush=True,
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
