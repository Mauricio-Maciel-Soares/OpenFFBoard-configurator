"""Pure parser and slew-observation tests for diag3."""
import unittest
from diagnostics import AXIS_FIELDS, parse_payload, slew_observations

class LiveSlewDiagnosticsTests(unittest.TestCase):
    def test_diag3_decodes_asymmetric_configuration_and_counters(self):
        from torque_study import AXIS_V2_EXTRA
        from diagnostics import AXIS_V3_EXTRA
        values = [3] + [0] * (len(AXIS_FIELDS) + len(AXIS_V2_EXTRA) + len(AXIS_V3_EXTRA) - 1)
        values[0] = 3
        data = parse_payload(','.join(map(str, values)), AXIS_FIELDS)
        self.assertEqual(data['version'], 3)
        self.assertIn('slew_mode', data)
        self.assertIn('slew_up_counts_ms', data)
        self.assertIn('slew_down_counts_ms', data)
        self.assertIn('slew_delta_counts', data)

    def test_observed_rate_uses_magnitude_and_elapsed_firmware_time(self):
        previous = {'time_us': 1000, 'output_counts': -100, 'slew_delta_counts': 1000,
                    'slew_limited_updates': 10, 'slew_power_clip_updates': 2}
        current = {'time_us': 3000, 'output_counts': -300, 'slew_delta_counts': 1300,
                   'slew_limited_updates': 12, 'slew_power_clip_updates': 2}
        result = slew_observations(previous, current)
        self.assertEqual(result['slew_observed_direction'], 'rise')
        self.assertEqual(result['slew_observed_magnitude_rate_counts_ms'], 100)
        self.assertEqual(result['slew_delta_counts_increment'], 300)
        self.assertEqual(result['slew_limited_updates_increment'], 2)
        self.assertEqual(result['slew_power_clip_updates_increment'], 0)

    def test_invalid_elapsed_time_returns_no_rate(self):
        self.assertEqual(slew_observations({'time_us': 5}, {'time_us': 5}), {})

if __name__ == '__main__':
    unittest.main()
