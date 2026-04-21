import os
import time
import threading
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


class AccumulatorThread(threading.Thread):
    """Background thread that polls sampler at ~100 Hz into ring buffers."""

    RATE_HZ = 100
    BUFLEN = 2000  # ~20 s of data

    def __init__(self, rp, stop_event):
        super().__init__(daemon=True, name='rp-accumulator')
        self.rp = rp
        self.stop = stop_event
        self.lock = threading.Lock()
        self.t0 = None
        self.bufs = {
            'time': deque(maxlen=self.BUFLEN),
            'in1': deque(maxlen=self.BUFLEN),
            'in2': deque(maxlen=self.BUFLEN),
            'out1': deque(maxlen=self.BUFLEN),
            'out2': deque(maxlen=self.BUFLEN),
        }

    def run(self):
        self.t0 = time.monotonic()
        interval = 1.0 / self.RATE_HZ
        while not self.stop.is_set():
            t = time.monotonic() - self.t0
            try:
                v_in1 = float(self.rp.sampler.in1)
                v_in2 = float(self.rp.sampler.in2)
                v_out1 = float(self.rp.sampler.out1)
                v_out2 = float(self.rp.sampler.out2)
                with self.lock:
                    self.bufs['time'].append(t)
                    self.bufs['in1'].append(v_in1)
                    self.bufs['in2'].append(v_in2)
                    self.bufs['out1'].append(v_out1)
                    self.bufs['out2'].append(v_out2)
            except Exception:
                pass
            self.stop.wait(interval)

    def get_snapshot(self, channel):
        """Return (times, input_v, output_v) lists for one channel (0 or 1)."""
        in_key = f'in{channel + 1}'
        out_key = f'out{channel + 1}'
        with self.lock:
            return (
                list(self.bufs['time']),
                list(self.bufs[in_key]),
                list(self.bufs[out_key]),
            )


class RPLockboxWorker(Worker):

    def init(self):
        from pyrpl import Pyrpl

        self.p = Pyrpl(config='rp_lockbox', hostname=self.ip_addr, gui=False)
        self.rp = self.p.rp

        self.pids = [self.rp.pid0, self.rp.pid1]
        self.asgs = [self.rp.asg0, self.rp.asg1]
        self.inputs = ['in1', 'in2']
        self.outputs = ['out1', 'out2']

        for i, pid in enumerate(self.pids):
            pid.input = self.inputs[i]
            pid.output_direct = self.outputs[i]
            pid.p = 0
            pid.i = 0
            pid.ival = 0
            pid.setpoint = 0
            pid.pause_gains = 'pi'
            pid.paused = True
            try:
                pid.use_setpoint_sequence = False
            except AttributeError:
                pass

        self._asg_active = [False, False]

        self._stop_event = threading.Event()
        self._accumulator = AccumulatorThread(self.rp, self._stop_event)
        self._accumulator.start()

    # ── Voltage monitoring ───────────────────────────────────────────

    def get_trace_data(self, channel):
        """Return accumulated voltage traces for a channel."""
        times, inp, out = self._accumulator.get_snapshot(channel)
        pid = self.pids[channel]
        return {
            'times': times,
            'input': inp,
            'output': out,
            'setpoint': float(pid.setpoint),
        }

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

    def enable_pid(self, channel):
        pid = self.pids[channel]
        if self._asg_active[channel]:
            self.stop_asg_output(channel)
        pid.output_direct = self.outputs[channel]
        pid.paused = False
        return not pid.paused

    def disable_pid(self, channel):
        pid = self.pids[channel]
        pid.paused = True
        return pid.paused

    def reset_pid(self, channel):
        pid = self.pids[channel]
        pid.p = 0
        pid.i = 0
        pid.ival = 0
        pid.setpoint = 0
        pid.paused = True
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
        pid.set_setpoint_array(array)
        pid.use_setpoint_sequence = True
        return array

    def disable_setpoint_sequence(self, channel):
        pid = self.pids[channel]
        pid.use_setpoint_sequence = False
        return True

    def reset_sequence_index(self, channel):
        self.pids[channel].reset_sequence_index()
        return True

    def step_sequence(self, channel):
        self.pids[channel].manually_change_setpoint()
        return True

    def get_sequence_status(self, channel):
        pid = self.pids[channel]
        return {
            'use_sequence': bool(pid.use_setpoint_sequence),
            'index': int(pid.setpoint_index),
            'current_setpoint': float(pid.setpoint_in_sequence),
            'wrap_flag': bool(pid.sequence_wrap_flag),
        }

    # ── PSD and statistics ───────────────────────────────────────────

    def compute_psd(self, channel, decimation=64):
        """Acquire scope burst and compute Welch PSD."""
        from scipy.signal import welch

        scope = self.rp.scope
        scope.input1 = self.inputs[channel]
        scope.decimation = decimation
        scope.trigger_source = 'immediately'
        fs = 125e6 / decimation

        trace = np.asarray(scope.single(), dtype=np.float64)
        if trace.ndim > 1:
            trace = trace[0]
        if trace.size == 0:
            return {'freqs': [], 'psd': [], 'rms': 0.0}

        nperseg = min(1024, trace.size)
        freqs, psd_vals = welch(trace, fs=fs, nperseg=nperseg)
        rms = float(np.sqrt(np.trapz(psd_vals, freqs)))
        return {
            'freqs': freqs.tolist(),
            'psd': psd_vals.tolist(),
            'rms': rms,
        }

    def get_stats(self, channel, decimation=64):
        """Acquire scope burst and return histogram statistics."""
        scope = self.rp.scope
        scope.input1 = self.inputs[channel]
        scope.decimation = decimation
        scope.trigger_source = 'immediately'

        trace = np.asarray(scope.single(), dtype=np.float64)
        if trace.ndim > 1:
            trace = trace[0]
        if trace.size == 0:
            return {'mean': 0.0, 'std': 0.0, 'hist_counts': [], 'hist_edges': []}

        mean = float(np.mean(trace))
        std = float(np.std(trace))
        counts, edges = np.histogram(trace, bins=50)
        return {
            'mean': mean,
            'std': std,
            'hist_counts': counts.tolist(),
            'hist_edges': edges.tolist(),
        }

    # ── ASG waveform output ──────────────────────────────────────────

    def set_asg_output(self, channel, waveform, frequency, amplitude, offset):
        """Output a waveform via ASG. Disables PID on this channel first."""
        pid = self.pids[channel]
        asg = self.asgs[channel]

        pid.paused = True
        pid.output_direct = 'off'

        wf_map = {'triangle': 'ramp', 'square': 'square', 'sine': 'sin', 'dc': 'dc'}
        asg.setup(
            waveform=wf_map.get(waveform, waveform),
            frequency=float(frequency) if waveform != 'dc' else 0,
            amplitude=float(amplitude) if waveform != 'dc' else 0,
            offset=float(offset),
            trigger_source='immediately',
            output_direct=self.outputs[channel],
        )
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
                for key in ch_grp:
                    val = ch_grp[key][()]
                    if isinstance(val, bytes):
                        val = val.decode('utf-8')
                    if key == 'setpoint_sequence':
                        arr = list(ch_grp[key][:])
                        if len(arr) < 16:
                            arr = arr + [0.0] * (16 - len(arr))
                        pid.set_setpoint_array(arr)
                        pid.use_setpoint_sequence = True
                        pid.reset_sequence_index()
                    elif key == 'p':
                        pid.p = float(val)
                    elif key == 'i':
                        pid.i = float(val)
                    elif key == 'setpoint':
                        pid.setpoint = float(val)
                    elif key == 'min_voltage':
                        pid.min_voltage = float(val)
                    elif key == 'max_voltage':
                        pid.max_voltage = float(val)
                    elif key == 'pause_gains':
                        pid.pause_gains = str(val)
                    elif key == 'inputfilter':
                        pid.inputfilter = list(ch_grp[key][:])
        return {}

    def transition_to_manual(self):
        results = {}
        for ch in (0, 1):
            pid = self.pids[ch]
            results[f'ch{ch}_setpoint'] = float(pid.setpoint)
            results[f'ch{ch}_ival'] = float(pid.ival)
        return results

    def abort_buffered(self):
        for pid in self.pids:
            pid.pause_gains = 'pi'
            pid.paused = True
        return True

    def abort_transition_to_buffered(self):
        return self.abort_buffered()

    def shutdown(self):
        self._stop_event.set()
        self._accumulator.join(timeout=2.0)
        for pid in self.pids:
            pid.pause_gains = 'pi'
            pid.paused = True
        for asg in self.asgs:
            asg.output_direct = 'off'
            asg.amplitude = 0
