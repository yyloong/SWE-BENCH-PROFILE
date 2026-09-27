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
            self.assertEqual({x.name for x in p.iterdir()}, {'details','key-metrics.md'})
            p=p/'details'
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
            output=pro_test.workdir(output)
            self.assertEqual(pro_test.read(output/'config.json')['gateway_run_dir'],str(service))
            self.assertIn("'--upstream','http://127.0.0.1:8002'",(output/'runner.py').read_text())
            self.assertIn("'--tmpfs','/opencode/swe-bench-env:ro,nosuid,nodev,noexec,size=1m'",(output/'runner.py').read_text())
            self.assertIn("Benchmark workspace is visible",(output/'runner.py').read_text())
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

    def test_old_run_without_workspace_mask_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            pro_test.save(p/'config.json',dict(opencode_root=str(pro_test.ROOT.parent)))
            with self.assertRaisesRegex(RuntimeError,'Reinitialize'):
                pro_test.checks(p)

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

    def test_new_layout_offline_metrics_and_status(self):
        from tests.test_key_metrics import fixture, request
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'batch'
            self.invoke('init','--output',str(output),'--indices','4')
            internal=pro_test.workdir(output)
            iid=pro_test.read(internal/'selected.json')[0]['instance_id']
            fixture(internal/'runs'/iid, [request()])
            self.invoke('metrics','--output',str(output))
            self.assertEqual({x.name for x in output.iterdir()}, {'details','key-metrics.md'})
            self.assertIn('平均 Decode TPS', (output/'key-metrics.md').read_text())
            self.assertTrue((internal/'metrics-output/key-metrics.json').exists())
            self.assertTrue((internal/'metrics-output/metrics.md').exists())
            self.assertEqual(json.loads(self.invoke('status','--output',str(output)))['ended'],1)
            before=(output/'key-metrics.md').read_bytes()
            self.invoke('metrics','--output',str(output))
            self.assertEqual(before,(output/'key-metrics.md').read_bytes())

    def test_organize_preserves_data_links_and_old_metrics(self):
        import sys
        from tests.test_key_metrics import fixture, request
        from scripts.metrics import key_metrics
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'batch';p.mkdir()
            pro_test.save(p/'config.json',dict(python=sys.executable))
            pro_test.save(p/'selected.json',[dict(instance_id='case')])
            fixture(p/'runs/case', [request()])
            (p/'raw.jsonl').write_text('{"original":"keep exactly"}\n')
            (p/'selector').symlink_to(p/'runs/case')
            (p/'key-metrics.md').write_text('existing report\n')
            before=key_metrics.build_report([p])['groups']['batch']
            self.invoke('organize','--output',str(p))
            self.assertEqual({x.name for x in p.iterdir()},{'details','key-metrics.md'})
            self.assertEqual((p/'details/raw.jsonl').read_text(),'{"original":"keep exactly"}\n')
            self.assertEqual((p/'details/selector').resolve(),p/'details/runs/case')
            self.assertEqual(before,key_metrics.build_report([p])['groups']['batch'])
            self.assertEqual(json.loads(self.invoke('status','--output',str(p)))['ended'],1)
            self.invoke('organize','--output',str(p))

    def test_organize_refuses_locked_run_and_existing_details(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            pro_test.save(p/'config.json',{})
            pro_test.save(p/'selected.json',[])
            with pro_test.locked(p/'runner.lock'):
                with self.assertRaises(SystemExit):self.invoke('organize','--output',str(p))
            self.assertTrue((p/'config.json').exists())
            self.assertFalse((p/'details').exists())
            (p/'details').mkdir()
            with self.assertRaises(SystemExit):self.invoke('organize','--output',str(p))
            self.assertTrue((p/'config.json').exists())

if __name__=='__main__':unittest.main()
