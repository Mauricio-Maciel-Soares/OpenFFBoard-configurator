"""Passive firmware telemetry and version-specific CAN heartbeat decoding."""
import csv
import io
import math

AXIS_FIELDS = "version sequence time_us dt_us min_dt_us max_dt_us clip_count slew_count effect_counts requested_counts clipped_counts output_counts position_speed_deg_s encoder_speed_deg_s speed_deg_s accel_deg_s2 encoder_valid encoder_used driver_ready".split()
ODRIVE_FIELDS = "version time_ms iq_sequence iq_age_ms iq_setpoint_a iq_measured_a encoder_sequence encoder_age_ms velocity_turns_s tx_sequence tx_age_ms queued_torque_nm tx_errors".split()

# ODrive fw-v0.5.6, tools/odrive/enums.py: AXIS_ERROR_* (CAN heartbeat).
# https://github.com/odriverobotics/ODrive/blob/fw-v0.5.6/tools/odrive/enums.py
AXIS_ERRORS_056 = {
    0x1: 'INVALID_STATE', 0x40: 'MOTOR_FAILED', 0x80: 'SENSORLESS_ESTIMATOR_FAILED',
    0x100: 'ENCODER_FAILED', 0x200: 'CONTROLLER_FAILED', 0x800: 'WATCHDOG_TIMER_EXPIRED',
    0x1000: 'MIN_ENDSTOP_PRESSED', 0x2000: 'MAX_ENDSTOP_PRESSED', 0x4000: 'ESTOP_REQUESTED',
    0x20000: 'HOMING_WITHOUT_ENDSTOP', 0x40000: 'OVER_TEMP', 0x80000: 'UNKNOWN_POSITION',
}


def decode_axis_errors(code, version='raw'):
    if not 0 <= code <= 0xffffffff:
        raise ValueError('Axis error must be a uint32')
    raw = '0x{:08X}'.format(code)
    if code == 0:
        return raw + ' — None'
    if version != '0.5.6':
        return raw + ' — select the ODrive firmware version to decode'
    names = [name for bit, name in AXIS_ERRORS_056.items() if code & bit]
    unknown = code & ~sum(AXIS_ERRORS_056)
    if unknown:
        names.append('UNKNOWN_BITS 0x{:08X}'.format(unknown))
    return raw + ' — ' + ', '.join(names)


def parse_payload(payload, fields):
    parts = payload.strip().split(',')
    if len(parts) != len(fields) or parts[0] != '1':
        raise ValueError('Unsupported diagnostic schema')
    values = list(map(float, parts))
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Non-finite diagnostic sample')
    return dict(zip(fields, values))


def loop_rates(previous, current):
    if previous is None:
        return {}
    elapsed = (int(current['time_us']) - int(previous['time_us'])) & 0xffffffff
    if not 0 < elapsed < 10000000:
        return {}
    result = {}
    for field, name in (('sequence', 'loop_hz'), ('clip_count', 'clip_events_s'), ('slew_count', 'slew_events_s')):
        delta = (int(current[field]) - int(previous[field])) & 0xffffffff
        # Counter resets must not appear as billions of events per second.
        if delta <= 100000:
            result[name] = delta * 1000000 / elapsed
    return result


def export_csv(rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode('utf-8-sig')
