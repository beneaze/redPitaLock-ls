import os
import sys
import time
import logging
from collections import deque

os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PyQt5')

import numpy as np

if not hasattr(np, 'VisibleDeprecationWarning'):
    np.VisibleDeprecationWarning = UserWarning
if not hasattr(np, 'ComplexWarning'):
    np.ComplexWarning = UserWarning
if not hasattr(np, 'complex'):
    np.complex = complex

from blacs.tab_base_classes import Worker

LOG = logging.getLogger(__name__)

# Worker-side timing for BLACS queue diagnosis (see docs/rp_lockbox_worker_timing.md).
_WORKER_TIMING_TRACE_INTERVAL_S = 5.0
_WORKER_TIMING_TRACE_SLOW_MS = 400.0

# ASG: never use frequency=0 (invalid for internal timing). DC uses a dummy Hz.
ASG_MIN_FREQ_HZ = 0.1
ASG_DC_FREQ_PLACEHOLDER_HZ = 1e3

# Default decimation for live monitoring scope grabs.  Low decimation = fast
# acquisition but short time window.  64 -> 16384*64/125e6 ~ 8 ms per trace.
MONITOR_DECIMATION = 64

# Ring buffer length for the voltage-vs-time display (one entry per scope grab).
TRACE_BUFLEN = 600


def _read_scope_raw(scope, decimation):
    """Trigger the scope and read both channel buffers directly.

    Bypasses scope.single() (which hangs on the wwlyn FPGA bitfile because
    curve_ready() never becomes True).  Instead we poke the FPGA trigger
    registers, wait for the acquisition to finish, then bulk-read the data
    buffers.

    Returns (ch1_volts, ch2_volts) as 1-D float64 arrays of length
    scope.data_length.
    """
    scope.decimation = decimation
    scope.average = False

    scope._reset_writestate_machine = True
    scope._trigger_delay_register = scope.data_length
    scope._trigger_armed = True
    scope._trigger_source_register = 'immediately'

    # Wait for the acquisition to fill the buffer.
    acq_time = scope.data_length * decimation / 125e6
    time.sleep(max(0.005, acq_time * 1.2))

    n = scope.data_length

    raw1 = np.array(scope._reads(0x10000, n), dtype=np.int16)
    raw1[raw1 >= 2 ** 13] -= 2 ** 14
    ch1 = raw1.astype(np.float64) / 2 ** 13

    raw2 = np.array(scope._reads(0x20000, n), dtype=np.int16)
    raw2[raw2 >= 2 ** 13] -= 2 ** 14
    ch2 = raw2.astype(np.float64) / 2 ** 13

    return ch1, ch2


class RPLockboxWorker(Worker):

    def init(self):
        try:
            from qtutils.qt.QtCore import QCoreApplication
            if QCoreApplication.instance() is None:
                QCoreApplication(sys.argv)
        except Exception:
            LOG.debug('QCoreApplication bootstrap skipped', exc_info=True)

        from pyrpl import Pyrpl

        # reloadserver=True in rp_lockbox.yml makes PyRPL scp pyrpl_server over the
        # running binary on every connect ("Text file busy"), which breaks the
        # socket server and makes PID/BLACS flaky.  Override here.
        self.p = Pyrpl(
            config='rp_lockbox',
            hostname=self.ip_addr,
            gui=False,
            reloadserver=False,
        )
        self.rp = self.p.rp

        sc = self.rp.scope
        sc.input1 = 'in1'
        sc.input2 = 'in2'

        self.pids = [self.rp.pid0, self.rp.pid1]
        self.asgs = [self.rp.asg0, self.rp.asg1]
        self.inputs = ['in1', 'in2']
        self.outputs = ['out1', 'out2']

        for i, pid in enumerate(self.pids):
            pid.input = self.inputs[i]
            pid.output_direct = 'off'
            pid.p = 0
            pid.i = 0
            pid.ival = 0
            pid.setpoint = 0
            pid.min_voltage = -1.0
            pid.max_voltage = 1.0
            pid.pause_gains = 'pi'
            pid.paused = True
            try:
                pid.use_setpoint_sequence = False
            except AttributeError:
                pass

        # PyRPL autosaves every property change to rp_lockbox.yml and can reload
        # state in ways that fight BLACS-driven register writes. Disable autosave
        # on hardware modules we own from this worker.
        for pid in self.pids:
            try:
                pid._autosave_active = False
            except AttributeError:
                pass
        for asg in self.asgs:
            try:
                asg._autosave_active = False
            except AttributeError:
                pass

        self._asg_active = [False, False]

        self._trace_bufs = {
            'time': deque(maxlen=TRACE_BUFLEN),
            'in1': deque(maxlen=TRACE_BUFLEN),
            'in2': deque(maxlen=TRACE_BUFLEN),
            'out1': deque(maxlen=TRACE_BUFLEN),
            'out2': deque(maxlen=TRACE_BUFLEN),
        }
        self._t0 = time.monotonic()
        self._worker_trace_timing_last_log = 0.0

        try:
            ch1, ch2 = _read_scope_raw(sc, MONITOR_DECIMATION)
            LOG.info(
                'Startup ADC check: ch1 mean=%.4f V, ch2 mean=%.4f V',
                ch1.mean(), ch2.mean(),
            )
        except Exception:
            LOG.exception('Startup ADC check failed')

    # ── Voltage monitoring ───────────────────────────────────────────

    def get_trace_data(self, channel):
        """Acquire a scope trace and append mean voltages to the ring buffer.

        Called by the tab's 100 ms refresh timer.  Each call triggers two
        scope acquisitions (inputs then DAC outputs) so the output trace
        reflects real BNC voltage, not pid.ival.
        """
        t0 = time.monotonic()
        acq_error = None
        try:
            sc = self.rp.scope
            saved_in1 = sc.input1
            saved_in2 = sc.input2
            try:
                sc.input1 = 'in1'
                sc.input2 = 'in2'
                ch1, ch2 = _read_scope_raw(sc, MONITOR_DECIMATION)
                sc.input1 = 'out1'
                sc.input2 = 'out2'
                out1, out2 = _read_scope_raw(sc, MONITOR_DECIMATION)
            finally:
                sc.input1 = saved_in1
                sc.input2 = saved_in2

            t = time.monotonic() - self._t0
            self._trace_bufs['time'].append(t)
            self._trace_bufs['in1'].append(float(ch1.mean()))
            self._trace_bufs['in2'].append(float(ch2.mean()))
            self._trace_bufs['out1'].append(float(out1.mean()))
            self._trace_bufs['out2'].append(float(out2.mean()))
        except Exception as e:
            LOG.exception('get_trace_data acquisition failed')
            msg = str(e).replace('\n', ' ')
            if len(msg) > 240:
                msg = msg[:237] + '...'
            acq_error = msg
        finally:
            dt_ms = (time.monotonic() - t0) * 1000.0
            now = time.monotonic()
            last = getattr(self, '_worker_trace_timing_last_log', 0.0)
            if (
                dt_ms >= _WORKER_TIMING_TRACE_SLOW_MS
                or (now - last) >= _WORKER_TIMING_TRACE_INTERVAL_S
            ):
                LOG.info(
                    '[rp_lockbox worker timing] get_trace_data ch=%s body_ms=%.1f',
                    channel,
                    dt_ms,
                )
                self._worker_trace_timing_last_log = now

        in_key = f'in{channel + 1}'
        out_key = f'out{channel + 1}'
        pid = self.pids[channel]
        result = {
            'times': list(self._trace_bufs['time']),
            'input': list(self._trace_bufs[in_key]),
            'output': list(self._trace_bufs[out_key]),
            'setpoint': float(pid.setpoint),
        }
        if acq_error is not None:
            result['error'] = acq_error
        return result

    # ── PID parameter control ────────────────────────────────────────

    def get_pid_status(self, channel):
        pid = self.pids[channel]
        status = {
            'p': float(pid.p),
            'i': float(pid.i),
            'setpoint': float(pid.setpoint),
            'ival': float(pid.ival),
            'paused': bool(pid.paused),
            'min_voltage': float(pid.min_voltage),
            'max_voltage': float(pid.max_voltage),
            'input': str(pid.input),
            'output_direct': str(pid.output_direct),
            'pause_gains': str(pid.pause_gains),
        }
        try:
            status['use_setpoint_sequence'] = bool(pid.use_setpoint_sequence)
            status['setpoint_index'] = int(pid.setpoint_index)
            status['setpoint_in_sequence'] = float(pid.setpoint_in_sequence)
            status['sequence_wrap_flag'] = bool(pid.sequence_wrap_flag)
        except AttributeError:
            status['use_setpoint_sequence'] = False
            status['setpoint_index'] = 0
            status['setpoint_in_sequence'] = 0.0
            status['sequence_wrap_flag'] = False
        return status

    def set_pid_param(self, channel, name, value):
        """Set a single PID parameter. Returns the readback value."""
        pid = self.pids[channel]
        if name == 'p':
            pid.p = float(value)
            return float(pid.p)
        elif name == 'i':
            pid.i = float(value)
            return float(pid.i)
        elif name == 'setpoint':
            pid.setpoint = float(value)
            return float(pid.setpoint)
        elif name == 'ival':
            pid.ival = float(value)
            return float(pid.ival)
        elif name == 'min_voltage':
            pid.min_voltage = float(value)
            return float(pid.min_voltage)
        elif name == 'max_voltage':
            pid.max_voltage = float(value)
            return float(pid.max_voltage)
        elif name == 'pause_gains':
            pid.pause_gains = str(value)
            return str(pid.pause_gains)
        elif name == 'inputfilter':
            pid.inputfilter = list(value)
            return list(pid.inputfilter)
        else:
            raise ValueError(f'Unknown parameter: {name}')

    # Fixed application order for PID parameters -- used by both manual apply
    # and buffered transitions so FPGA register updates are deterministic.
    _PID_PARAM_ORDER = (
        'min_voltage', 'max_voltage', 'setpoint', 'p', 'i', 'ival', 'pause_gains',
    )

    def apply_pid_params(self, channel, params):
        """Apply several PID fields in one worker job (single BLACS queue round-trip).

        params: dict with keys among min_voltage, max_voltage, setpoint, p, i, ival,
        pause_gains. Application order is fixed for sensible FPGA updates.
        """
        order = self._PID_PARAM_ORDER
        readbacks = {}
        for key in order:
            if key not in params:
                continue
            readbacks[key] = self.set_pid_param(channel, key, params[key])
        return readbacks

    def apply_params_and_enable_pid(self, channel, params):
        """Apply panel PID fields then enable the loop in one BLACS worker job.

        Avoids an extra tab-level ``queue_work`` round-trip versus calling
        ``apply_pid_params`` and ``enable_pid`` separately.
        """
        LOG.info(
            '[rp_lockbox worker timing] apply_params_and_enable_pid ch=%s enter_wall=%.3f',
            channel,
            time.time(),
        )
        t_all = time.monotonic()
        t_apply0 = time.monotonic()
        readbacks = self.apply_pid_params(channel, params)
        apply_ms = (time.monotonic() - t_apply0) * 1000.0
        t_en0 = time.monotonic()
        enable_diag = self.enable_pid(channel)
        enable_ms = (time.monotonic() - t_en0) * 1000.0
        total_ms = (time.monotonic() - t_all) * 1000.0
        LOG.info(
            '[rp_lockbox worker timing] apply_params_and_enable_pid ch=%s '
            'apply_pid_params_ms=%.1f enable_pid_ms=%.1f total_ms=%.1f exit_wall=%.3f',
            channel,
            apply_ms,
            enable_ms,
            total_ms,
            time.time(),
        )
        return {'readbacks': readbacks, 'enable': enable_diag}

    def enable_pid(self, channel):
        pid = self.pids[channel]
        if self._asg_active[channel]:
            self.stop_asg_output(channel)

        p_val = float(pid.p)
        i_val = float(pid.i)
        sp_val = float(pid.setpoint)
        iv_val = float(pid.ival)

        pid.output_direct = self.outputs[channel]
        pid.paused = False

        # Some bitfiles may ignore P/I writes while paused; re-assert after unpausing.
        pid.p = p_val
        pid.i = i_val
        pid.setpoint = sp_val
        pid.ival = iv_val

        if bool(pid.paused):
            LOG.error(
                'enable_pid ch%d: paused still True after unpause (output_direct=%r)',
                channel,
                pid.output_direct,
            )

        diag = {
            'channel': int(channel),
            'p': float(pid.p),
            'i': float(pid.i),
            'setpoint': float(pid.setpoint),
            'ival': float(pid.ival),
            'paused': bool(pid.paused),
            'output_direct': str(pid.output_direct),
            'pause_gains': str(pid.pause_gains),
        }
        try:
            diag['current_output_signal'] = float(pid.current_output_signal)
        except AttributeError:
            diag['current_output_signal'] = None
        return diag

    def disable_pid(self, channel):
        LOG.info(
            '[rp_lockbox worker timing] disable_pid ch=%s enter_wall=%.3f',
            channel,
            time.time(),
        )
        t0 = time.monotonic()
        if self._asg_active[channel]:
            self.stop_asg_output(channel)
        pid = self.pids[channel]
        pid.paused = True
        pid.output_direct = 'off'
        dt_ms = (time.monotonic() - t0) * 1000.0
        LOG.info(
            '[rp_lockbox worker timing] disable_pid ch=%s body_ms=%.1f exit_wall=%.3f',
            channel,
            dt_ms,
            time.time(),
        )
        return pid.paused

    def reset_pid(self, channel):
        if self._asg_active[channel]:
            self.stop_asg_output(channel)
        pid = self.pids[channel]
        pid.p = 0
        pid.i = 0
        pid.ival = 0
        pid.setpoint = 0
        pid.paused = True
        pid.output_direct = 'off'
        return True

    # ── Setpoint sequence (wwlyn extensions) ─────────────────────────

    def set_setpoint_sequence(self, channel, array):
        """Load up to 16 setpoints into the FPGA sequence register."""
        pid = self.pids[channel]
        array = list(array)
        if len(array) < 16:
            array = array + [0.0] * (16 - len(array))
        elif len(array) > 16:
            array = array[:16]
        try:
            pid.set_setpoint_array(array)
            pid.use_setpoint_sequence = True
        except AttributeError:
            LOG.warning('set_setpoint_sequence: FPGA/PyRPL does not support sequences')
            return {'error': 'setpoint sequences not supported by this bitfile'}
        return array

    def disable_setpoint_sequence(self, channel):
        pid = self.pids[channel]
        try:
            pid.use_setpoint_sequence = False
        except AttributeError:
            LOG.warning('disable_setpoint_sequence: not supported by this bitfile')
        return True

    def reset_sequence_index(self, channel):
        try:
            self.pids[channel].reset_sequence_index()
        except AttributeError:
            LOG.warning('reset_sequence_index: not supported by this bitfile')
        return True

    def step_sequence(self, channel):
        try:
            self.pids[channel].manually_change_setpoint()
        except AttributeError:
            LOG.warning('step_sequence: not supported by this bitfile')
        return True

    def get_sequence_status(self, channel):
        pid = self.pids[channel]
        try:
            return {
                'use_sequence': bool(pid.use_setpoint_sequence),
                'index': int(pid.setpoint_index),
                'current_setpoint': float(pid.setpoint_in_sequence),
                'wrap_flag': bool(pid.sequence_wrap_flag),
            }
        except AttributeError:
            return {
                'use_sequence': False,
                'index': 0,
                'current_setpoint': 0.0,
                'wrap_flag': False,
            }

    # ── PSD and statistics ───────────────────────────────────────────

    def compute_psd(self, channel, decimation=1):
        """Acquire scope burst and compute Welch PSD.

        Default ``decimation=1`` uses the full 125 MS/s scope rate so the PSD
        reaches ~62.5 MHz (Nyquist). Higher decimation lowers the band limit.

        Returned ``psd`` values are clamped to be strictly positive so the
        BLACS log–log plot can render (Welch bins are often exactly zero).
        """
        try:
            from scipy.signal import welch
        except ImportError as e:
            LOG.error('compute_psd: scipy required (%s)', e)
            return {
                'freqs': [], 'psd': [], 'rms': 0.0,
                'error': 'scipy is not installed (need scipy.signal.welch)',
            }

        t_job = time.monotonic()
        try:
            scope = self.rp.scope
            saved_in1 = scope.input1
            saved_in2 = scope.input2
            try:
                scope.input1 = self.inputs[channel]
                scope.input2 = self.inputs[channel]
                ch1, _ch2 = _read_scope_raw(scope, decimation)
            finally:
                scope.input1 = saved_in1
                scope.input2 = saved_in2

            trace = ch1
            if trace.size == 0:
                LOG.warning('compute_psd: empty trace (channel=%s)', channel)
                return {
                    'freqs': [], 'psd': [], 'rms': 0.0,
                    'error': 'empty scope trace',
                }

            fs = 125e6 / decimation
            nperseg = min(1024, trace.size)
            freqs, psd_vals = welch(trace, fs=fs, nperseg=nperseg)
            rms = float(np.sqrt(np.trapz(psd_vals, freqs)))
            # Drop the DC bin (freq=0) so the log-scale plot doesn't choke
            # on log10(0) = -inf.
            freqs = freqs[1:]
            psd_vals = psd_vals[1:].astype(np.float64, copy=False)
            if freqs.size == 0 or psd_vals.size == 0:
                return {
                    'freqs': [], 'psd': [], 'rms': float(rms),
                    'error': 'PSD spectrum empty after DC removal',
                }
            # Log y cannot plot zeros or negatives; clamp for display only.
            mx = float(np.nanmax(psd_vals))
            if not np.isfinite(mx) or mx <= 0:
                floor = 1e-30
            else:
                floor = max(1e-30, mx * 1e-15)
            psd_plot = np.maximum(psd_vals, floor)
            freqs_plot = np.asarray(freqs, dtype=np.float64)
            mask = freqs_plot > 0
            if not np.all(mask):
                freqs_plot = freqs_plot[mask]
                psd_plot = psd_plot[mask]
            psd_plot = np.nan_to_num(psd_plot, nan=floor, posinf=floor, neginf=floor)
            return {
                'freqs': freqs_plot.tolist(),
                'psd': psd_plot.tolist(),
                'rms': rms,
            }
        except Exception as e:
            LOG.exception('compute_psd failed (channel=%s)', channel)
            msg = str(e).replace('\n', ' ')
            if len(msg) > 240:
                msg = msg[:237] + '...'
            return {'freqs': [], 'psd': [], 'rms': 0.0, 'error': msg}
        finally:
            dt_ms = (time.monotonic() - t_job) * 1000.0
            if dt_ms >= 500.0:
                LOG.info(
                    '[rp_lockbox worker timing] compute_psd ch=%s body_ms=%.1f',
                    channel,
                    dt_ms,
                )

    def get_stats(self, channel, decimation=64):
        """Acquire scope burst and return histogram statistics."""
        t_job = time.monotonic()
        try:
            scope = self.rp.scope
            saved_in1 = scope.input1
            saved_in2 = scope.input2
            try:
                scope.input1 = self.inputs[channel]
                scope.input2 = self.inputs[channel]
                ch1, _ch2 = _read_scope_raw(scope, decimation)
            finally:
                scope.input1 = saved_in1
                scope.input2 = saved_in2

            trace = ch1
            if trace.size == 0:
                LOG.warning('get_stats: empty trace (channel=%s)', channel)
                return {
                    'mean': 0.0,
                    'std': 0.0,
                    'hist_counts': [],
                    'hist_edges': [],
                    'error': 'empty scope trace',
                }

            mean = float(np.mean(trace))
            std = float(np.std(trace))
            counts, edges = np.histogram(trace, bins=50)
            return {
                'mean': mean,
                'std': std,
                'hist_counts': counts.tolist(),
                'hist_edges': edges.tolist(),
            }
        except Exception as e:
            LOG.exception('get_stats failed (channel=%s)', channel)
            msg = str(e).replace('\n', ' ')
            if len(msg) > 240:
                msg = msg[:237] + '...'
            return {
                'mean': 0.0,
                'std': 0.0,
                'hist_counts': [],
                'hist_edges': [],
                'error': msg,
            }
        finally:
            dt_ms = (time.monotonic() - t_job) * 1000.0
            if dt_ms >= 500.0:
                LOG.info(
                    '[rp_lockbox worker timing] get_stats ch=%s body_ms=%.1f',
                    channel,
                    dt_ms,
                )

    # ── ASG waveform output ──────────────────────────────────────────

    def set_asg_output(self, channel, waveform, frequency, amplitude, offset):
        """Output a waveform via ASG. Disables PID on this channel first."""
        pid = self.pids[channel]
        asg = self.asgs[channel]

        pid.paused = True
        pid.output_direct = 'off'

        wf_map = {'triangle': 'ramp', 'square': 'square', 'sine': 'sin', 'dc': 'dc'}
        wf = wf_map.get(waveform, waveform)
        off = float(offset)
        amp = float(amplitude)
        freq = float(frequency)

        if wf == 'dc':
            freq_hz = ASG_DC_FREQ_PLACEHOLDER_HZ
            amp_v = 1.0
        else:
            freq_hz = max(freq, ASG_MIN_FREQ_HZ)
            amp_v = max(amp, 0.0)

        asg.setup(
            waveform=wf,
            frequency=freq_hz,
            amplitude=amp_v,
            offset=off,
            trigger_source='immediately',
            output_direct=self.outputs[channel],
        )
        if hasattr(asg, 'trig'):
            asg.trig()
        if wf != 'dc':
            asg.periodic = True

        self._asg_active[channel] = True
        return True

    def stop_asg_output(self, channel):
        """Turn off ASG and reconnect PID output."""
        asg = self.asgs[channel]
        asg.output_direct = 'off'
        asg.amplitude = 0
        asg.offset = 0
        self.pids[channel].output_direct = self.outputs[channel]
        self._asg_active[channel] = False
        return True

    # ── BLACS lifecycle ──────────────────────────────────────────────

    def program_manual(self, values):
        return {}

    def transition_to_buffered(self, device_name, h5_file, initial_values, fresh):
        import h5py
        with h5py.File(h5_file, 'r') as f:
            dev_grp = f.get(f'/devices/{device_name}')
            if dev_grp is None:
                return {}
            for ch in (0, 1):
                ch_grp = dev_grp.get(f'ch{ch}')
                if ch_grp is None:
                    continue
                pid = self.pids[ch]

                def _read_val(k):
                    v = ch_grp[k][()]
                    if isinstance(v, bytes):
                        v = v.decode('utf-8')
                    return v

                # Apply scalar PID params in the same fixed order as manual apply.
                for key in self._PID_PARAM_ORDER:
                    if key not in ch_grp:
                        continue
                    self.set_pid_param(ch, key, _read_val(key))

                if 'inputfilter' in ch_grp:
                    pid.inputfilter = list(ch_grp['inputfilter'][:])

                if 'setpoint_sequence' in ch_grp:
                    arr = list(ch_grp['setpoint_sequence'][:])
                    if len(arr) < 16:
                        arr = arr + [0.0] * (16 - len(arr))
                    elif len(arr) > 16:
                        arr = arr[:16]
                    pid.set_setpoint_array(arr)
                    pid.use_setpoint_sequence = True
                    pid.reset_sequence_index()
        return {}

    def transition_to_manual(self):
        results = {}
        for ch in (0, 1):
            pid = self.pids[ch]
            results[f'ch{ch}_setpoint'] = float(pid.setpoint)
            results[f'ch{ch}_ival'] = float(pid.ival)
        return results

    def abort_buffered(self):
        for ch in range(len(self.pids)):
            if self._asg_active[ch]:
                self.stop_asg_output(ch)
        for pid in self.pids:
            pid.pause_gains = 'pi'
            pid.paused = True
            pid.output_direct = 'off'
        return True

    def abort_transition_to_buffered(self):
        return self.abort_buffered()

    def shutdown(self):
        for pid in self.pids:
            pid.pause_gains = 'pi'
            pid.paused = True
            pid.output_direct = 'off'
        for asg in self.asgs:
            asg.output_direct = 'off'
            asg.amplitude = 0
        if hasattr(self, 'p') and self.p is not None:
            try:
                self.p._clear()
            except Exception:
                LOG.debug('Pyrpl._clear() failed during shutdown', exc_info=True)
