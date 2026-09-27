import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pro import paths, pro_test
from scripts.semantic_gate import services


class RunPathTests(unittest.TestCase):
    def test_most_specific_move_preserves_old_run_and_service_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'manual-runs'
            batch=root/'details/evaluations/native/details';batch.mkdir(parents=True)
            service=root/'details/services/native';service.mkdir(parents=True)
            (root/'details/path-map.json').write_text(json.dumps(dict(moves=[
                {'from':'old-service','to':'details/services/native'},
                {'from':'old-service/fixed10','to':'details/evaluations/native'}])))
            (batch/'config.json').write_text('{}')
            (batch/'selected.json').write_text('[]')
            (service/'services.json').write_text('{}')
            for name in ['server-27b','server-8b','proxy']:
                (service/(name+'.json')).write_text(json.dumps(dict(pid=-1,port=8000,start_ticks=None)))
            old=root/'old-service/fixed10'
            self.assertEqual(pro_test.workdir(old),batch)
            self.assertEqual(paths.resolve_saved_path(root/'old-service/gateway-logs'),service/'gateway-logs')
            self.assertEqual(paths.resolve_saved_path(root/'old-service-other'),root/'old-service-other')
            with patch('sys.argv',['pro_test.py','status','--output',str(old)]),contextlib.redirect_stdout(io.StringIO()) as out:
                pro_test.main()
            self.assertEqual(json.loads(out.getvalue())['planned'],0)
            with patch('sys.argv',['services.py','status','--output',str(root/'old-service')]),contextlib.redirect_stdout(io.StringIO()) as out:
                services.main()
            self.assertEqual(len(out.getvalue().splitlines()),3)
            self.assertFalse(any(json.loads(line)['alive'] for line in out.getvalue().splitlines()))

    def test_existing_paths_are_not_redirected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'details').mkdir();(root/'old').mkdir()
            (root/'details/path-map.json').write_text(json.dumps(dict(moves=[{'from':'old','to':'details/new'}])))
            self.assertEqual(paths.resolve_saved_path(root/'old'),root/'old')

    def test_original_absolute_path_resolves_after_workspace_move(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)/'new-project';root=workspace/'manual-runs'
            (root/'details').mkdir(parents=True)
            (root/'details/path-map.json').write_text(json.dumps(dict(original_root='/previous/project/manual-runs',
                moves=[{'from':'old-service','to':'details/services/new'}])))
            with patch.object(paths,'__file__',str(workspace/'scripts/pro/paths.py')):
                self.assertEqual(paths.resolve_saved_path('/previous/project/manual-runs/old-service/gateway-logs'),
                                 root/'details/services/new/gateway-logs')


if __name__=='__main__':
    unittest.main()
