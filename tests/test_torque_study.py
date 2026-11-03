import math
import unittest
from torque_study import TraceCapture, parse_trace, analyze, TRACE_FIELDS, AXIS_V2_EXTRA, ODRIVE_V2_EXTRA
from diagnostics import parse_payload, AXIS_FIELDS, ODRIVE_FIELDS


def record(sequence=1,time=1000,**changes):
    row=dict(zip(TRACE_FIELDS,[sequence,time,3,2.5,3.5,1,48,900,1000,600,1,2,0,8,1]))
    row.update(changes)
    return ','.join(str(row[key]) for key in TRACE_FIELDS)


def batch(rows,oldest=1,newest=None,gap=0):
    return '1,{},{},{},{}{}'.format(oldest,newest if newest is not None else len(rows),gap,len(rows),
                                    '\n'+'\n'.join(rows) if rows else '')


class TorqueStudyTests(unittest.TestCase):
    def test_actual_v2_field_counts_and_legacy(self):
        for fields,extra in ((AXIS_FIELDS,AXIS_V2_EXTRA),(ODRIVE_FIELDS,ODRIVE_V2_EXTRA)):
            self.assertEqual(len(parse_payload('2,'+','.join(['0']*(len(fields)+len(extra)-1)),fields)),len(fields)+len(extra))
            self.assertEqual(len(parse_payload('1,'+','.join(['0']*(len(fields)-1)),fields)),len(fields))
            with self.assertRaises(ValueError):parse_payload('2,0',fields)
    def test_can_sample_estimate_and_units(self):
        capture=TraceCapture(1.11)
        row=capture.accept(batch([record()]))[0]
        self.assertAlmostEqual(row['estimated_torque_nm'],2.775)
        self.assertAlmostEqual(row['iq_tracking_error_nm'],.555)
        self.assertEqual(row['motor_rpm'],60)
        self.assertEqual(row['torque_age_us'],900)
    def test_rollover_of_sequence_and_microsecond_time(self):
        c=TraceCapture();c.cursor=0xfffffffe
        a=c.accept(batch([record(0xffffffff,0xffffff00)],oldest=0xffffffff,newest=0xffffffff))[0]
        b=c.accept(batch([record(0,744)],oldest=0xffffffff,newest=0))[0]
        self.assertEqual(a['sample_time_s'],0)
        self.assertAlmostEqual(b['sample_time_s'],.001)
        self.assertEqual(c.cursor,0)
        self.assertEqual(c.restarts,0)
    def test_gaps_and_reset_are_explicit(self):
        c=TraceCapture();c.cursor=1
        rows=c.accept(batch([record(8),record(9,2000)],oldest=8,newest=71,gap=6))
        self.assertEqual(c.lost,6);self.assertEqual([r['lost_before'] for r in rows],[6,0])
        rows=c.accept(batch([record(1)],newest=1,gap=0xffffffff))
        self.assertEqual(c.restarts,1);self.assertEqual(rows[0]['capture_epoch'],1)
    def test_reject_bad_data_and_unexpected_duplicate(self):
        for payload in ('', '1,1,1,0,1',batch([record(iq_measured_a='nan')]),
                        batch([record(encoder_valid=2)]),batch([record(sequence=1.5)]),
                        batch([record(),record(3)],newest=3)):
            with self.assertRaises(ValueError):parse_trace(payload)
        c=TraceCapture();c.accept(batch([record()]))
        with self.assertRaises(ValueError):c.accept(batch([record()]))
    def test_stale_velocity_and_errors_excluded_from_study(self):
        c=TraceCapture()
        rows=c.accept(batch([record(1),record(2,2000,encoder_age_us=6000),record(3,3000,axis_errors=64)]))
        result=analyze(rows)
        self.assertEqual(result['rejected_rows'],2)
        self.assertEqual(result['groups'][0]['samples'],1)
        self.assertAlmostEqual(result['groups'][0]['iq_error_rmse_a'],.5)
    def test_separate_direction_and_regeneration_missing_voltage(self):
        c=TraceCapture()
        rows=c.accept(batch([record(1),record(2,2000,velocity_turns_s=-1),record(3,3000,voltage_age_us=0xffffffff)]))
        result=analyze(rows,30)
        self.assertEqual(len(result['groups']),2)
        positive=next(g for g in result['groups'] if g['direction']=='positive')
        negative=next(g for g in result['groups'] if g['direction']=='negative')
        self.assertEqual(positive['voltage_samples'],1)
        self.assertEqual(negative['quadrant'],'regenerating')
        self.assertEqual(positive['rpm_min'],60)
        with self.assertRaises(ValueError):TraceCapture(math.nan)
        with self.assertRaises(ValueError):analyze(rows,0)


if __name__=='__main__':unittest.main()
