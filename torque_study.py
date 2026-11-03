"""Passive torque study protocol and analysis. Also shipped in the configurator.

Iq*Kt estimates electromagnetic torque, not measured shaft torque. The CAN RX
clock timestamps reception; it cannot establish drive-loop or one-way latency.
"""
import math
from collections import defaultdict

UINT32_MAX = 0xffffffff
AXIS_V2_EXTRA = 'window_count window_mean_us window_stddev_us window_min_us window_max_us velocity_source_switches velocity_transition slew_profile slew_rate_counts_ms'.split()
ODRIVE_V2_EXTRA = ('time_us iq_time_us encoder_time_us tx_time_us voltage_age_us bus_voltage_v encoder_valid encoder_fresh_streak invalid_encoder_frames invalid_iq_frames can_errors'.split()
                   + [prefix + '_' + field for prefix in ('iq_interval', 'encoder_interval', 'tx_interval')
                      for field in ('count', 'last_us', 'min_us', 'max_us', 'mean_us', 'stddev_us')])
TRACE_FIELDS = 'sequence time_us iq_setpoint_a iq_measured_a queued_torque_nm velocity_turns_s bus_voltage_v torque_age_us encoder_age_us voltage_age_us tx_sequence encoder_sequence axis_errors axis_state encoder_valid'.split()
FLOAT_FIELDS = {'iq_setpoint_a', 'iq_measured_a', 'queued_torque_nm', 'velocity_turns_s', 'bus_voltage_v'}


def _uint(value):
    number = float(value)
    if not math.isfinite(number) or number != int(number) or not 0 <= number <= UINT32_MAX:
        raise ValueError('Expected a uint32 diagnostic value')
    return int(number)


def parse_trace(payload):
    lines = payload.strip().splitlines()
    header = lines[0].split(',') if lines else []
    if len(header) != 5 or header[0] != '1':
        raise ValueError('Unsupported current trace schema')
    oldest, newest, gap, count = map(_uint, header[1:])
    if count > 16 or len(lines) != count + 1:
        raise ValueError('Truncated or oversized current trace')
    rows = []
    for line in lines[1:]:
        parts = line.split(',')
        if len(parts) != len(TRACE_FIELDS):
            raise ValueError('Incompatible current trace record')
        row = {key: float(value) if key in FLOAT_FIELDS else _uint(value)
               for key, value in zip(TRACE_FIELDS, parts)}
        if not all(math.isfinite(value) for value in row.values()) or row['bus_voltage_v'] < 0:
            raise ValueError('Non-finite/invalid current trace record')
        if row['encoder_valid'] not in (0, 1):
            raise ValueError('Invalid encoder validity flag')
        if ((newest - row['sequence']) & UINT32_MAX) >= 64 or ((row['sequence'] - oldest) & UINT32_MAX) >= 64:
            raise ValueError('Trace sequence outside retention window')
        if rows and ((row['sequence'] - rows[-1]['sequence']) & UINT32_MAX) != 1:
            raise ValueError('Non-contiguous trace batch')
        rows.append(row)
    return {'oldest': oldest, 'newest': newest, 'gap': gap, 'rows': rows}


def estimates(row, kt):
    if not math.isfinite(kt) or kt <= 0:
        raise ValueError('Kt must be positive and finite')
    error = row['iq_setpoint_a'] - row['iq_measured_a']
    result = dict(kt_nm_a=kt, iq_setpoint_torque_nm=kt * row['iq_setpoint_a'],
                  estimated_torque_nm=kt * row['iq_measured_a'], iq_tracking_error_a=error,
                  iq_tracking_error_nm=kt * error)
    result['command_minus_iq_estimate_nm'] = (row['queued_torque_nm'] - result['estimated_torque_nm']
                                              if row['tx_sequence'] and row['torque_age_us'] != UINT32_MAX else '')
    result['motor_rpm'] = (row['velocity_turns_s'] * 60 if row['encoder_valid']
                           and row['encoder_age_us'] <= 5000 else '')
    result['fresh_bus_voltage_v'] = row['bus_voltage_v'] if row['voltage_age_us'] <= 2000000 else ''
    return result


class TraceCapture:
    def __init__(self, kt=1.11):
        if not math.isfinite(kt) or kt <= 0:
            raise ValueError('Kt must be positive and finite')
        self.kt = kt
        self.cursor = -1
        self.lost = self.restarts = self.total = 0
        self.last_time = None
        self.elapsed_us = 0

    def accept(self, payload):
        batch = parse_trace(payload)
        if batch['gap'] == UINT32_MAX:
            self.restarts += 1
            self.last_time = None
            self.elapsed_us = 0
        else:
            self.lost += batch['gap']
        output = []
        for index, record in enumerate(batch['rows']):
            if self.cursor != -1 and not batch['gap'] and ((record['sequence'] - self.cursor) & UINT32_MAX) != 1:
                raise ValueError('Unexpected trace sequence; start a new capture')
            if self.last_time is not None:
                delta = (record['time_us'] - self.last_time) & UINT32_MAX
                if delta >= 0x80000000:
                    self.restarts += 1
                    self.elapsed_us = 0
                else:
                    self.elapsed_us += delta
            self.last_time = record['time_us']
            self.cursor = record['sequence']
            self.total += 1
            row = dict(record, sample_time_s=self.elapsed_us / 1000000,
                       capture_epoch=self.restarts, lost_before=batch['gap'] if index == 0 else 0)
            row.update(estimates(record, self.kt))
            output.append(row)
        if not batch['rows'] and self.cursor == -1:
            self.cursor = batch['newest']
        return output


def analyze(rows, rpm_bin=30):
    """Group valid observations by speed/direction/quadrant, without lag fitting."""
    if not math.isfinite(rpm_bin) or rpm_bin <= 0:
        raise ValueError('RPM bin width must be positive and finite')
    groups = defaultdict(list)
    rejected = total = lost = resets = 0
    for raw in rows:
        total += 1
        try:
            row = {key: float(raw[key]) for key in TRACE_FIELDS}
            if not all(math.isfinite(v) for v in row.values()):
                raise ValueError('Non-finite record')
            gap = _uint(raw.get('lost_before', 0))
            if gap == UINT32_MAX:
                resets += 1
            else:
                lost += gap
            kt = float(raw.get('kt_nm_a', 1.11))
            derived = estimates(row, kt)
            rpm = derived['motor_rpm']
            if rpm == '' or row['axis_errors'] != 0 or row['axis_state'] != 8:
                raise ValueError('Invalid operating state or velocity')
            direction = 'positive' if rpm > 0 else 'negative' if rpm < 0 else 'stationary'
            product = rpm * row['iq_setpoint_a']
            quadrant = 'motoring' if product > 0 else 'regenerating' if product < 0 else 'zero'
            key = (int(abs(rpm) // rpm_bin), direction, quadrant)
            groups[key].append(dict(row, **derived))
        except (ValueError, KeyError, TypeError):
            rejected += 1
    summaries = []
    for (band, direction, quadrant), values in sorted(groups.items()):
        n = len(values)
        errors = [v['iq_tracking_error_a'] for v in values]
        absolute = sorted(map(abs, errors))
        voltages = [v['fresh_bus_voltage_v'] for v in values if v['fresh_bus_voltage_v'] != '']
        summaries.append(dict(rpm_min=band*rpm_bin, rpm_max=(band+1)*rpm_bin,
                              direction=direction, quadrant=quadrant, samples=n,
                              iq_error_mean_a=sum(errors)/n,
                              iq_error_rmse_a=math.sqrt(sum(e*e for e in errors)/n),
                              iq_error_p95_abs_a=absolute[max(0, math.ceil(.95*n)-1)],
                              iq_measured_peak_abs_a=max(abs(v['iq_measured_a']) for v in values),
                              estimated_torque_peak_abs_nm=max(abs(v['estimated_torque_nm']) for v in values),
                              voltage_samples=len(voltages),
                              bus_voltage_min_v=min(voltages) if voltages else None,
                              bus_voltage_max_v=max(voltages) if voltages else None))
    return dict(total_rows=total, rejected_rows=rejected, lost_samples=lost, detected_resets=resets,
                rpm_bin_width=rpm_bin, groups=summaries,
                interpretation='Observational current tracking by speed. Iq*Kt is electromagnetic torque estimate; '
                               'CAN timestamps are receive times. No measured shaft torque, one-way latency, '
                               'current-loop bandwidth or causal electrical limit is inferred.')
