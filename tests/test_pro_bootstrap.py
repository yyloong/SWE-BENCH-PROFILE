"""Bootstrap control-plane tests; no model inference or external downloads."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts.pro import pro_bootstrap as boot
from scripts.pro import pro_test as pro


FAKE_SERVICE = r'''
import json,os,signal,sys,time
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
p=Path(sys.argv[sys.argv.index('--output')+1]);cfg=json.loads((p/'config.json').read_text())
action=sys.argv[1]
if action=='serve':
 if cfg.get('fail_start'):sys.exit(7)
 port=cfg['ports'][0]
 (p/f'server-{port}.json').write_text(json.dumps({'pid':os.getpid()}))
 class H(BaseHTTPRequestHandler):
  def do_GET(self):self.send_response(200);self.end_headers()
  def log_message(self,*a):pass
 HTTPServer(('127.0.0.1',port),H).serve_forever()
elif action=='run':
 (p/'fake-run-started').write_text('yes')
 if cfg.get('wait_for_signal'):
  stopped=[False]
  signal.signal(signal.SIGTERM,lambda *_:stopped.__setitem__(0,True))
  deadline=time.monotonic()+10
  while not stopped[0] and time.monotonic()<deadline:time.sleep(.02)
  if not stopped[0]:sys.exit(8)
  time.sleep(.1)
  (p/'drained').write_text('current task finished')
 td=p/'runs/case';td.mkdir(parents=True,exist_ok=True)
 (td/'result.json').write_text('{}')
 sys.exit(cfg.get('runner_exit',0))
'''


class BootstrapTests(unittest.TestCase):
    def fixture(self,p,**options):
        # cmdline contains "vllm" from the temporary root name for ownership check.
        (p/'scripts/pro').mkdir(parents=True)
        (p/'scripts/pro/pro_test.py').write_text(FAKE_SERVICE)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        pro.save(p/'config.json',dict(python=sys.executable,ports=[port],**options))
        pro.save(p/'selected.json',[dict(instance_id='case')])
        return port

    def assert_server_stopped(self,p,port):
        pid=pro.read(p/f'server-{port}.json')['pid']
        self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_success_cleans_up_owned_server(self):
        with tempfile.TemporaryDirectory(prefix='vllm-bootstrap-test-') as t:
            p=Path(t);port=self.fixture(p)
            with patch.object(boot,'ROOT',p),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(boot.workflow(p,10),0)
            self.assertTrue((p/'runs/case/result.json').exists())
            self.assert_server_stopped(p,port)

    def test_runner_failure_cleans_up_server(self):
        with tempfile.TemporaryDirectory(prefix='vllm-bootstrap-test-') as t:
            p=Path(t);port=self.fixture(p,runner_exit=9)
            with patch.object(boot,'ROOT',p),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'code 9'):boot.workflow(p,10)
            self.assert_server_stopped(p,port)

    def test_start_failure_never_launches_runner(self):
        with tempfile.TemporaryDirectory(prefix='vllm-bootstrap-test-') as t:
            p=Path(t);self.fixture(p,fail_start=True)
            with patch.object(boot,'ROOT',p),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'startup failed'):boot.workflow(p,10)
            self.assertFalse((p/'fake-run-started').exists())

    def test_sigint_drains_before_stopping_server(self):
        with tempfile.TemporaryDirectory(prefix='vllm-bootstrap-test-') as t:
            p=Path(t);port=self.fixture(p,wait_for_signal=True)
            def interrupt():
                deadline=time.monotonic()+10
                while not (p/'fake-run-started').exists() and time.monotonic()<deadline:time.sleep(.05)
                if (p/'fake-run-started').exists():
                    time.sleep(.1);os.kill(os.getpid(),signal.SIGINT)
            thread=threading.Thread(target=interrupt);thread.start()
            with patch.object(boot,'ROOT',p),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(boot.workflow(p,10),130)
            thread.join()
            self.assertTrue((p/'drained').exists())
            self.assert_server_stopped(p,port)

    def test_fresh_venv_installs_local_wheel_and_reuses_it(self):
        with tempfile.TemporaryDirectory(prefix='pro-install-test-') as t:
            p=Path(t);wheel=p/'pro_bootstrap_probe-1.0.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel,'w') as z:
                z.writestr('pro_bootstrap_probe.py','VALUE=42\n')
                prefix='pro_bootstrap_probe-1.0.0.dist-info/'
                z.writestr(prefix+'METADATA','Metadata-Version: 2.1\nName: pro-bootstrap-probe\nVersion: 1.0.0\n')
                z.writestr(prefix+'WHEEL','Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                z.writestr(prefix+'RECORD','')
            requirements=p/'requirements.txt';requirements.write_text(str(wheel)+'\n')
            env=os.environ|{'PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1'}
            with contextlib.redirect_stdout(io.StringIO()):
                python=boot.python_env(p/'env',[],requirements,{'pro-bootstrap-probe':'1.0.0'},env,base_python=sys.executable)
                self.assertEqual(subprocess.check_output([python,'-c','import pro_bootstrap_probe; print(pro_bootstrap_probe.VALUE)'],text=True).strip(),'42')
                with patch.object(boot,'call',side_effect=AssertionError('Healthy environment must not be mutated')):
                    self.assertEqual(boot.python_env(p/'env',[Path(python)],requirements,{'pro-bootstrap-probe':'1.0.0'},env),python)

    def test_generated_run_uses_environment_manifest(self):
        with tempfile.TemporaryDirectory(prefix='pro-manifest-test-') as t:
            p=Path(t);env=p/'environment.json'
            config=dict(python='/custom/eval/bin/python',official_root='/custom/official',opencode_root='/custom/opencode',bun_root='/custom/bun',assets='/custom/assets',skopeo='/custom/bin/skopeo',proxy='http://proxy.test:123',image_proxy='http://mirror.test:123',server_argv=pro.read(boot.BUNDLE/'templates/server-argv.json'))
            config['infra-fix']='/custom/infra'
            pro.save(env,config)
            with patch('sys.argv',['pro_test.py','init','--output',str(p/'run'),'--indices','4','--environment-file',str(env)]),contextlib.redirect_stdout(io.StringIO()):pro.main()
            runner=(p/'run/details/runner.py').read_text();pull=(p/'run/details/pull_images.py').read_text()
            self.assertIn("PY='/custom/eval/bin/python'",runner)
            self.assertIn("OFFICIAL=Path('/custom/official')",runner)
            self.assertIn('/custom/opencode:/opencode:ro',runner)
            self.assertIn('/custom/bin/skopeo',pull)
            self.assertNotIn('/home/longyy',runner)
            compile(runner,'runner.py','exec');compile(pull,'pull_images.py','exec')

    def test_resume_new_layout_uses_internal_state_without_starting_models(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'run';p=output/'details';p.mkdir(parents=True)
            pro.save(p/'config.json',{})
            pro.save(p/'selected.json',[dict(instance_id='case')])
            pro.save(p/'one-click-inputs.json',dict(environment_file=str(Path(temp)/'env/environment.json')))
            (p/'runs/case').mkdir(parents=True)
            pro.save(p/'runs/case/result.json',{})
            from argparse import Namespace
            args=Namespace(output=str(output),resume=True,environment_file=None,install_only=False,prepare_only=False)
            with patch.object(boot,'docker_access'),patch.object(boot,'setup',return_value=Path(temp)/'env/environment.json'),patch.object(pro,'checks'),patch.object(pro,'pull'),patch.object(pro,'metrics') as metrics,patch.object(boot,'workflow',side_effect=AssertionError('must not start models')):
                self.assertEqual(boot.execute(args),0)
            metrics.assert_called_once_with(p)
            self.assertFalse((output/'one-click.lock').exists())


if __name__=='__main__':unittest.main()
