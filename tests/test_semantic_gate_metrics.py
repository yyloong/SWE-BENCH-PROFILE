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


if __name__=='__main__':
    unittest.main()
