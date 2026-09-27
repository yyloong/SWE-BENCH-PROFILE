import unittest

from scripts.semantic_gate.metrics import summarize


class MetricsTests(unittest.TestCase):
    def test_overlap_is_partitioned_without_double_counting(self):
        record=dict(started_at=0,ended_at=10,wall_seconds=10,route='accepted',
                    background_target_disposition='cancelled',stages=[
            dict(phase='target',started_at=0,ended_at=9,wall_seconds=9,cancelled=True,
                 usage=dict(prompt_tokens=50,completion_tokens=30),observed_completion_token_ids=32,
                 metrics=dict(generation_time_ms=1000),finish_reason='stop',usage_complete=False),
            dict(phase='draft',started_at=1,ended_at=5,wall_seconds=4,finish_reason='tool_calls',
                 usage=dict(prompt_tokens=40,completion_tokens=11),metrics=dict(generation_time_ms=1000)),
            dict(phase='review',started_at=6,ended_at=8,wall_seconds=2,finish_reason='stop',
                 usage=dict(prompt_tokens=55,completion_tokens=2),metrics=dict(generation_time_ms=100))])
        result=summarize([record])
        buckets=result['exclusive_wall_buckets']
        self.assertEqual(buckets['draft+target']['mean_request_time_percent'],40)
        self.assertEqual(buckets['review+target']['mean_request_time_percent'],20)
        self.assertEqual(result['mean_other_percent'],10)
        self.assertEqual(sum(v['mean_request_time_percent'] for v in buckets.values()),100)
        self.assertEqual(result['mean_overlap_percent'],60)
        self.assertIsNone(result['phases']['target']['decode_tps'])
        self.assertEqual(result['phases']['target']['completion_tokens'],0)
        self.assertEqual(result['phases']['target']['incomplete_observed_completion_tokens_lower_bound'],32)
        self.assertEqual(result['phases']['draft']['mean_decode_tps'],10)

    def test_empty_logs(self):
        result=summarize([])
        self.assertEqual(result['requests'],0)
        self.assertEqual(result['exclusive_wall_buckets'],{})

    def test_cli_filters_nested_run_intervals(self):
        import json, subprocess, sys, tempfile
        from pathlib import Path
        from scripts.semantic_gate import metrics
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);task=p/'batch/details/runs/a';task.mkdir(parents=True)
            (task/'result.json').write_text(json.dumps(dict(agent_start=10,agent_end=20)))
            rows=[dict(id=str(start),started_at=start,ended_at=start+2,wall_seconds=2,
                       route='target_won',stages=[]) for start in [1,12]]
            (p/'requests.jsonl').write_text('\n'.join(map(json.dumps,rows)))
            subprocess.run([sys.executable,str(Path(metrics.__file__)),str(p/'requests.jsonl'),
                            '--run-dir',str(p/'batch'),'--output-dir',str(p/'report')],check=True,capture_output=True)
            result=json.loads((p/'report/gateway-metrics.json').read_text())
            self.assertEqual(result['requests'],1)
            self.assertEqual(result['eligible_requests'],1)


if __name__=='__main__':
    unittest.main()
