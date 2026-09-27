#!/usr/bin/env python3
"""Sequential, single-request TP/DFlash comparison using recorded SWE prefixes."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

if __package__:
    from .services import alive, check_gpu, health, start_ticks
else:
    from services import alive, check_gpu, health, start_ticks


def stop(record):
    if not alive(record):
        return
    os.killpg(record['pid'], signal.SIGTERM)
    for _ in range(60):
        if not alive(record):
            return
        time.sleep(0.5)
    if alive(record):
        os.killpg(record['pid'], signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-server', required=True, help='Saved argv JSON; must describe the matched baseline')
    parser.add_argument('--draft', required=True)
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--spec-tokens', type=int, default=15)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out/'sweep.json').exists():
        raise RuntimeError('Use a fresh output directory; do not overwrite a sweep')
    template = json.loads(Path(args.base_server).read_text())['argv']
    port = int(template[template.index('--port')+1])
    report = dict(kind='isolated_model_latency_not_end_to_end_SWE_accuracy', configurations=[])
    for tp, speculative in [(1, True), (2, False), (2, True), (4, False), (4, True)]:
        label = f'tp{tp}-'+('dflash' if speculative else 'plain')
        dest = out/label
        dest.mkdir()
        gpus = {1:[6], 2:[6,7], 4:[4,5,6,7]}[tp]
        # Driver processes can outlive their supervisor briefly during shutdown.
        for attempt in range(60):
            try:
                check_gpu(gpus)
                break
            except RuntimeError:
                if attempt == 59:
                    raise
                time.sleep(1)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
        argv = list(template)
        argv[argv.index('--tensor-parallel-size')+1] = str(tp)
        if speculative:
            argv += ['--per-request-spec-decode-metrics', 'detailed', '--speculative-config',
                     json.dumps(dict(method='dflash', model=str(Path(args.draft).resolve()),
                                     num_speculative_tokens=args.spec_tokens))]
        env = os.environ.copy()
        for key in ['ALL_PROXY','all_proxy','HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy']:
            env.pop(key, None)
        env.update(CUDA_VISIBLE_DEVICES=','.join(map(str, gpus)), HF_HUB_OFFLINE='1',
                   PATH=str(Path(argv[0]).parent)+os.pathsep+env.get('PATH', ''))
        with (dest/'server.log').open('w') as log:
            child = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record = dict(pid=child.pid, start_ticks=start_ticks(child.pid), argv=argv, gpus=gpus,
                      port=port, started_at=time.time(), label=label)
        (dest/'server.json').write_text(json.dumps(record, indent=2))
        report['configurations'].append(record)
        print('START', label, child.pid, flush=True)
        try:
            for _ in range(120):
                if child.poll() is not None:
                    record.update(status='startup_failed', returncode=child.returncode)
                    print('STARTUP_FAILED', label, flush=True)
                    break
                if health(f'http://127.0.0.1:{port}'):
                    record['ready_at'] = time.time()
                    cmd = [sys.executable, str(Path(__file__).with_name('replay.py')),
                           '--inputs', str(Path(args.inputs).resolve()), '--output', str(dest/'measurements'),
                           '--label', label, '--repeat', str(args.repeat), '--fixed-length-probe',
                           '--draft-url', f'http://127.0.0.1:{port}']
                    record['measurement_argv'] = cmd
                    with (dest/'measurements.log').open('w') as log:
                        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
                    record.update(status='completed' if result.returncode == 0 else 'measurement_failed',
                                  returncode=result.returncode)
                    print(record['status'].upper(), label, flush=True)
                    break
                time.sleep(5)
            else:
                record['status'] = 'startup_timeout'
        finally:
            stop(record)
            child.wait(timeout=30)
            record['ended_at'] = time.time()
            (dest/'server.json').write_text(json.dumps(record, indent=2))
            (out/'sweep.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
