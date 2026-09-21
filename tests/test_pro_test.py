"""Control-plane checks only: no inference, download, image modification or grading."""
import contextlib
import hashlib
import io
import json
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pro import pro_test

class WorkflowTests(unittest.TestCase):
    def invoke(self, *args):
        with patch('sys.argv', ['pro_test.py', *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            pro_test.main()
        return output.getvalue()

    def test_fresh_task_and_context_routing(self):
        with tempfile.TemporaryDirectory(prefix='pro-entry-test-') as temp:
            p=Path(temp)/'experiment'
            context=Path(temp)/'context.md'
            context.write_text('Inspect the relevant implementation and tests.\n')
            self.invoke('init','--output',str(p),'--indices','4','--ports','8100',
                '--context-file',str(context))
            selected=pro_test.read(p/'selected.json')
            self.assertEqual(len(selected),1)
            self.assertEqual(selected[0]['repo'],'ansible/ansible')
            self.assertEqual(pro_test.read(p/'config.json')['ports'],[8100])
            self.assertEqual((p/'contexts'/(selected[0]['instance_id']+'.md')).read_text(),context.read_text())
            self.assertEqual(pro_test.read(p/'config.json')['template_source'],str(pro_test.ROOT/'bootstrap/templates'))
            runner=pro_test.module(p/'runner.py','test_manual_runner')
            self.assertEqual(runner.P,p)
            self.assertEqual(runner.TASK_TIMEOUT,3600)
            source=(p/'runner.py').read_text()
            self.assertIn("port=json.loads((P/'config.json').read_text())['ports'][server]",source)
            self.assertIn("planfile=P/'contexts'/(iid+'.md')",source)
            self.assertNotIn("if P.name=='path'",source)
            self.assertNotIn("['docker','image','rm'",source)
            self.assertFalse((p/'runs').exists())
            self.assertEqual(pro_test.read(p/'input-hashes.json')['key_metrics.py'],
                             hashlib.sha256((p/'key_metrics.py').read_bytes()).hexdigest())
            with self.assertRaises(SystemExit):self.invoke('init','--output',str(p))

    def test_metrics_preserves_detail_and_prints_mean_report(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            (p/'efficiency_metrics.py').write_text('')
            with patch.object(pro_test, 'command') as command:
                pro_test.metrics(p)
            self.assertEqual(command.call_count,2)
            detail, compact=command.call_args_list
            self.assertEqual(detail.kwargs['stdout'],subprocess.DEVNULL)
            self.assertEqual(compact.args[0][1],str(pro_test.ROOT/'scripts/metrics/key_metrics.py'))
            (p/'key_metrics.py').write_text('')
            with patch.object(pro_test, 'command') as command:
                pro_test.metrics(p)
            self.assertEqual(command.call_args.args[0][1],str(p/'key_metrics.py'))

    def test_gpu_groups_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'experiment'
            with self.assertRaises(SystemExit):
                self.invoke('init','--output',str(p),'--ports','8100,8101','--gpus','0,1,2,3;0,5,6,7')
            self.assertFalse(p.exists())

    def test_gateway_routes_only_the_copied_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            service=Path(temp)/'service';service.mkdir()
            (service/'services.json').write_text(json.dumps(dict(target_gpus=[0,1,2,3],gateway_url='http://127.0.0.1:8002')))
            output=Path(temp)/'run'
            self.invoke('init','--output',str(output),'--indices','4','--gateway-run-dir',str(service))
            self.assertEqual(pro_test.read(output/'config.json')['gateway_run_dir'],str(service))
            self.assertIn("'--upstream','http://127.0.0.1:8002'",(output/'runner.py').read_text())
            self.assertIn("'--upstream',f'http://127.0.0.1:{port}'",(pro_test.ROOT/'bootstrap/templates/runner.py').read_text())
            with self.assertRaisesRegex(RuntimeError,'semantic gate'):
                pro_test.serve(output)

    def test_cannot_borrow_existing_server(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);pro_test.save(p/'config.json',dict(ports=[8000]))
            with patch('urllib.request.urlopen') as network:
                with self.assertRaisesRegex(RuntimeError,'unrelated server'):pro_test.verify_servers(p)
                network.assert_not_called()

    def test_busy_gpu_is_rejected(self):
        results=[subprocess.CompletedProcess([],0,'0, GPU-a\n1, GPU-b\n2, GPU-c\n3, GPU-d\n'),
                 subprocess.CompletedProcess([],0,'GPU-a, 12345\n')]
        with patch.object(pro_test,'command',side_effect=results):
            with self.assertRaisesRegex(RuntimeError,'already have compute'):pro_test.gpu_guard(dict(ports=[8000],gpus=[[0,1,2,3]]))

    def test_status_stops_agent_clock_during_grade(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);pro_test.save(p/'selected.json',[dict(instance_id='case')])
            td=p/'runs/case';td.mkdir(parents=True)
            events=[dict(kind='stage.start',stage='agent',at=100),dict(kind='stage.end',stage='agent',at=125),dict(kind='stage.start',stage='grading',at=130)]
            (td/'lifecycle.jsonl').write_text('\n'.join(map(json.dumps,events)))
            with contextlib.redirect_stdout(io.StringIO()) as output:pro_test.status(p)
            row=json.loads(output.getvalue())['rows'][0]
            self.assertEqual(row['agent_seconds'],25)
            self.assertEqual(row['state'],'grading')

if __name__=='__main__':unittest.main()
