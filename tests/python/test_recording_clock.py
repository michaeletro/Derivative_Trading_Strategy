import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('check_recording_clock', Path(__file__).resolve().parents[2] / 'tools/check_recording_clock.py')
clock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clock)


class RecordingClockTests(unittest.TestCase):
    def test_constant_offsets_do_not_imply_drift(self):
        result = clock.summarize([{'wall_ns': 100_000_000_000 + i * 1_000_000_000,
                                  'monotonic_ns': i * 1_000_000_000} for i in range(4)])
        self.assertEqual(result['status'], 'consistent_during_check')
        self.assertEqual(result['maximum_wall_monotonic_divergence_seconds'], 0)
        self.assertIsNone(result['raw_elapsed_seconds'])

    def test_temporary_step_is_not_hidden_by_matching_endpoints(self):
        samples = [{'wall_ns': w * 1_000_000_000, 'monotonic_ns': m * 1_000_000_000}
                   for w, m in [(100, 0), (103, 1), (102, 2)]]
        result = clock.summarize(samples)
        self.assertEqual(result['status'], 'inconsistent')
        self.assertEqual(result['maximum_wall_monotonic_divergence_seconds'], 2)
        self.assertEqual(result['wall_regressions'], 1)

    def test_monotonic_regression_rejected_even_with_small_divergence(self):
        result = clock.summarize([{'wall_ns': 100, 'monotonic_ns': 20},
                                  {'wall_ns': 110, 'monotonic_ns': 19}])
        self.assertEqual(result['status'], 'inconsistent')
        self.assertEqual(result['monotonic_regressions'], 1)

    def test_missing_and_float_timestamps_are_not_accepted(self):
        for samples in [[], [{'wall_ns': 1, 'monotonic_ns': 1}],
                        [{'wall_ns': 1.0, 'monotonic_ns': 1}] * 2]:
            with self.assertRaises(ValueError):
                clock.summarize(samples)


if __name__ == '__main__':
    unittest.main()
