"""Checks for time attribution, child folding and equal task weighting; no inference."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.metrics import key_metrics as km


def request(start=0, end=10, prefill=2, decode=6, tokens=13, sid='parent', role='main'):
    return dict(request_id=f'{sid}-{start}', session_id=sid, role=role,
                started_at=start, ended_at=end, in_progress=False,
                usage={'completion_tokens': tokens},
                metrics={'time_to_first_token_ms': prefill*1000, 'generation_time_ms': decode*1000,
                         'queue_time_ms': 2000},
                phase_boundaries={'reasoning': [start+4, start+6], 'tool_arguments': [start+6, end]})


def fixture(td, requests, tools=None, sessions=None, start=0, end=10):
    td.mkdir(parents=True)
    result = dict(agent_start=start, agent_end=end, agent_wall_seconds=end-start)
    for name, value in [('result', result), ('requests', requests), ('tools', tools or []),
                        ('timing', {'sessions': sessions or {}})]:
        (td/(name+'.json')).write_text(json.dumps(value))
    return result


class KeyMetricsTests(unittest.TestCase):
    def test_nested_layout_cli_keeps_only_markdown_at_output_root(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'batch';out=Path(temp)/'report'
            q=request();q['usage']['prompt_tokens']=100
            fixture(p/'details/runs/a',[q])
            subprocess.run([sys.executable,str(Path(km.__file__)),str(p),'--output-dir',str(out)],check=True,capture_output=True)
            self.assertEqual({x.name for x in out.iterdir()},{'key-metrics.md','details'})
            data=json.loads((out/'details/key-metrics.json').read_text())
            self.assertEqual(data['groups']['batch']['mean_prompt_tokens'],100)
            self.assertEqual(data['groups']['batch']['mean_response_tokens'],13)

    def test_gateway_report_masks_buffered_model_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);fixture(p/'details/runs/a',[request()])
            (p/'details/config.json').write_text(json.dumps(dict(gateway_run_dir='/service')))
            (p/'details/metrics-output').mkdir()
            (p/'details/metrics-output/gateway-metrics.json').write_text(json.dumps(dict(
                acceptance_percent=10,mean_request_seconds=4,phases={
                    'draft':dict(mean_decode_tps=120,mean_call_seconds=2,mean_completion_tokens=100,
                                 prompt_tokens=400,usage_covered_calls=2,tps_covered_calls=2,calls=3)},
                exclusive_wall_buckets={'draft+target':dict(mean_request_time_percent=100)})))
            text=km.markdown(km.build_report([p]))
            self.assertIn('平均 Decode TPS（token/s） | N/A',text)
            self.assertIn('8B 候选 | 120.00',text)
            self.assertIn('draft+target | 100.00%',text)
            self.assertNotIn('| Decode | 6.00 秒',text)

    def test_pass_rate_uses_same_cohort_and_does_not_invent_missing_grades(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            for name, passed in [('a', True), ('b', False)]:
                td = p/'runs'/name
                fixture(td, [request()])
                (td/'grade.json').write_text(json.dumps({'resolved': passed, 'infra_failure': False}))
            group = km.campaign(p)
            self.assertEqual(group['pass_rate_percent'], 50)
            self.assertEqual(group['graded_tasks'], 2)
            self.assertEqual(group['passed'], 1)
            (p/'runs/b/grade.json').write_text(json.dumps({'resolved': False, 'infra_failure': True}))
            group = km.campaign(p)
            self.assertIsNone(group['pass_rate_percent'])
            self.assertEqual(group['graded_tasks'], 1)
            self.assertIn('N/A（评分未齐）', km.markdown(km.build_report([p])))

    def test_same_label_union_and_cross_label_overlap(self):
        seconds, overlap = km.exclusive_partition([
            (0, 4, 'reasoning'), (1, 3, 'reasoning'), (2, 6, 'ordinary_tool'),
            (-2, 0, 'prefill'), (10, 12, 'prefill')], 0, 10)
        self.assertEqual(seconds['reasoning'], 2)
        self.assertEqual(seconds['ordinary_tool'], 2)
        self.assertEqual(seconds['other'], 6)
        self.assertEqual(overlap, 2)
        self.assertEqual(sum(seconds.values()), 10)

    def test_server_prefill_excludes_queue_and_decode_splits(self):
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)/'case'
            result = fixture(td, [request()])
            row = km.task_metrics(td, result, 'time_to_first_token_ms')
            self.assertEqual(row['seconds']['prefill'], 2)
            self.assertEqual(row['seconds']['reasoning'], 2)
            self.assertEqual(row['seconds']['non_reasoning'], 4)
            self.assertEqual(row['seconds']['other'], 2)
            self.assertEqual(row['decode_tps'], 2)

    def test_child_lifecycle_and_nested_children_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)/'case'
            root = request(end=6, prefill=0, decode=6)
            root['phase_boundaries'] = {'visible_output': [0, 6]}
            child = request(start=2, end=10, prefill=0, decode=8, tokens=8001, sid='child', role='subagent')
            nested = request(start=4, end=5, prefill=0, decode=1, tokens=2001, sid='nested', role='title')
            tools = [dict(tool='task', start=2, end=2.01, session_id='parent', role='main', metadata={'sessionId': 'child'}),
                     dict(tool='bash', start=7, end=9, session_id='parent', role='main'),
                     dict(tool='bash', start=3, end=4, session_id='child', role='subagent'),
                     dict(tool='task', start=4, end=5, session_id='child', role='subagent', metadata={'sessionId': 'nested'})]
            sessions = {'child': {'parentID': 'parent'}, 'nested': {'parentID': 'child'}}
            result = fixture(td, [root, child, nested], tools, sessions)
            row = km.task_metrics(td, result, 'time_to_first_token_ms')
            self.assertEqual(row['seconds']['non_reasoning'], 2)
            self.assertEqual(row['seconds']['subagent'], 2)
            self.assertEqual(row['seconds']['ordinary_tool'], 0)
            self.assertEqual(row['seconds']['other'], 6)
            self.assertEqual(row['decode_tps'], 2)
            self.assertEqual(row['quality']['parent_requests'], 1)
            # The independent bound retains model/tool overlap. Nested tools
            # are absorbed into the opaque child's eight-second lifetime.
            self.assertEqual(row['tool_parallel_bound']['tool_union_seconds'], 8)
            self.assertEqual(row['tool_parallel_bound']['ideal_lower_bound_seconds'], 8)
            self.assertEqual(row['tool_parallel_bound']['maximum_reduction_seconds'], 0)

    def test_parallel_bound_merges_wrapper_and_nested_child_into_one_job(self):
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)/'case'
            requests = [request(start=2, end=6, sid='child', role='subagent'),
                        request(start=5, end=10, sid='nested', role='subagent'),
                        request(start=11, end=13, sid='sibling', role='subagent')]
            tools = [dict(tool='task', start=1, end=3, session_id='parent', role='main', metadata={'sessionId': 'child'}),
                     dict(tool='task', start=5, end=5.1, session_id='child', role='subagent', metadata={'sessionId': 'nested'}),
                     dict(tool='task', start=11, end=11.1, session_id='parent', role='main', metadata={'sessionId': 'sibling'}),
                     dict(tool='bash', start=0, end=1, session_id='parent', role='main')]
            sessions = {'child': {'parentID': 'parent'}, 'nested': {'parentID': 'child'}, 'sibling': {'parentID': 'parent'}}
            result = fixture(td, requests, tools, sessions, end=15)
            bound = km.task_metrics(td, result, 'time_to_first_token_ms')['tool_parallel_bound']
            self.assertEqual(bound['tool_union_seconds'], 12)
            self.assertEqual(bound['ideal_lower_bound_seconds'], 9)
            self.assertEqual(bound['maximum_reduction_seconds'], 3)

    def test_parallel_bound_clips_and_does_not_invent_unobserved_gaps(self):
        # First job: two observed pieces, one clipped. They have three seconds
        # of observed work, not the five-second envelope or five-second sum.
        bound = km.tool_parallel_bound([[(-2, 1), (3, 5)], [(4, 9)], [(11, 12)]], 0, 8)
        self.assertEqual(bound['tool_union_seconds'], 6)
        self.assertEqual(bound['ideal_lower_bound_seconds'], 4)
        self.assertEqual(bound['maximum_reduction_seconds'], 2)
        empty = km.mean_tool_parallel_bound([km.tool_parallel_bound([], 0, 8)])
        self.assertEqual(empty['mean_tool_union_seconds'], 0)
        self.assertEqual(empty['mean_maximum_reduction_seconds'], 0)
        self.assertIsNone(empty['maximum_reduction_percent_of_mean_tool_union'])

    def test_parallel_bound_percentage_is_ratio_of_means(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            for name, intervals in [('a', [(0, 8), (8, 10)]), ('b', [(0, 1), (1, 2)])]:
                fixture(p/'runs'/name, [], [dict(tool='bash', start=a, end=b, session_id='parent', role='main')
                                          for a, b in intervals])
            bound = km.campaign(p)['tool_parallel_bound']
            self.assertEqual(bound['mean_tool_union_seconds'], 6)
            self.assertEqual(bound['mean_ideal_lower_bound_seconds'], 4.5)
            self.assertEqual(bound['mean_maximum_reduction_seconds'], 1.5)
            # Mean of per-task percentages would be 35%, not 25%.
            self.assertEqual(bound['maximum_reduction_percent_of_mean_tool_union'], 25)

    def test_equal_task_mean_is_not_ratio_of_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            fixture(p/'runs/a', [request()])
            q = request(start=100, end=130, prefill=0, decode=30, tokens=121)
            q['phase_boundaries'] = {'visible_output': [100, 130]}
            fixture(p/'runs/b', [q], start=100, end=130)
            group = km.campaign(p)
            self.assertEqual(group['mean_wall_seconds'], 20)
            self.assertEqual(group['mean_seconds']['decode'], 18)
            self.assertEqual(group['mean_percent']['decode'], 80)
            self.assertEqual(group['mean_decode_tps'], 3)
            self.assertEqual(sum(group['mean_percent'][k] for k in km.LEAVES), 100)

    def test_missing_metrics_and_unfinished_request_stay_in_other(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            q = request(start=5, end=15)
            q['in_progress'] = True
            q['metrics'] = {}
            fixture(p/'runs/a', [q])
            fixture(p/'runs/b', [])
            group = km.campaign(p)
            self.assertIsNone(group['mean_decode_tps'])
            self.assertEqual(group['mean_percent']['other'], 100)
            self.assertEqual(group['quality']['unattributed_timing_requests'], 1)

    def test_explicit_prefill_export_and_missing_phase_split(self):
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)/'case'
            q = request()
            q['metrics']['prefill_time_ms'] = 1000
            q['phase_boundaries'] = {}
            result = fixture(td, [q])
            row = km.task_metrics(td, result, 'time_to_first_token_ms')
            self.assertEqual(row['seconds']['prefill'], 1)
            self.assertEqual(row['seconds']['other'], 9)
            self.assertEqual(row['decode_tps'], 2)
            self.assertEqual(row['quality']['unknown_decode_split_requests'], 1)

    def test_paired_discovery_and_budget_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            for arm in ['baseline', 'path']:
                fixture(p/'arms'/arm/'runs/a', [request()])
                td = p/'arms'/arm/'runs/b'
                r = fixture(td, [request()])
                r['termination_reason'] = 'turn_limit'
                (td/'result.json').write_text(json.dumps(r))
            report = km.build_report([p], exclude_censored=True)
            self.assertEqual(set(report['groups']), {'baseline', 'path'})
            self.assertEqual(report['groups']['path']['tasks'], 1)
            self.assertEqual(report['groups']['path']['excluded'], {'budget_or_time_censored': 1})
            self.assertIn('平均 Decode TPS', km.markdown(report))


if __name__ == '__main__':
    unittest.main()
