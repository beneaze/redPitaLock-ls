import os
import time
import math
import logging

os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PyQt5')

LOG = logging.getLogger(__name__)

from blacs.device_base_class import DeviceTab
from blacs.tab_base_classes import (
    define_state,
    MODE_MANUAL,
    MODE_BUFFERED,
    MODE_TRANSITION_TO_BUFFERED,
    MODE_TRANSITION_TO_MANUAL,
)

# Allow trace refresh + PSD/stats acquire while tab is manual, buffered, or transitioning.
_ALL_DEVICE_MODES = (
    MODE_MANUAL
    | MODE_BUFFERED
    | MODE_TRANSITION_TO_BUFFERED
    | MODE_TRANSITION_TO_MANUAL
)

# Continuous PSD (Welch) + input histogram for the active channel. Paused with the trace timer during PID ops.
_PSD_STATS_REFRESH_MS = 400
from qtutils.qt.QtCore import QTimer
from qtutils.qt.QtWidgets import (
    QWidget, QTabWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QSplitter, QSizePolicy,
)
from qtutils.qt.QtCore import Qt

import numpy as np
import pyqtgraph as pg


class ChannelPanel(QWidget):
    """Control + monitoring panel for one PID channel."""

    def __init__(self, channel, tab, parent=None):
        super().__init__(parent)
        self.ch = channel
        self.tab = tab
        self._build_ui()

    def _build_ui(self):
        main = QHBoxLayout(self)

        left = QVBoxLayout()
        left.addWidget(self._build_pid_group())
        left.addWidget(self._build_trace_options_group())
        left.addWidget(self._build_sequence_group())
        left.addWidget(self._build_waveform_group())
        left.addStretch()

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(self._build_trace_plots())
        right_splitter.addWidget(self._build_psd_plot())
        right_splitter.addWidget(self._build_stats_plot())
        right_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMaximumWidth(340)

        main.addWidget(left_w)
        main.addWidget(right_splitter, stretch=1)

    # ── PID Parameters ───────────────────────────────────────────────

    def _build_pid_group(self):
        grp = QGroupBox(f'PID Parameters (ch{self.ch})')
        g = QGridLayout(grp)

        fields = [
            ('Setpoint (V):', 'setpoint_edit', '0.0'),
            ('P:', 'p_edit', '0'),
            ('I:', 'i_edit', '0'),
            ('Ival:', 'ival_edit', '0.0'),
            ('Min V:', 'min_v_edit', '-1.0'),
            ('Max V:', 'max_v_edit', '1.0'),
        ]
        for row, (label, attr, default) in enumerate(fields):
            g.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(default)
            setattr(self, attr, edit)
            g.addWidget(edit, row, 1)

        row = len(fields)
        g.addWidget(QLabel('Pause gains:'), row, 0)
        self.pause_gains_combo = QComboBox()
        self.pause_gains_combo.addItems(['pi', 'p', 'i', 'off'])
        g.addWidget(self.pause_gains_combo, row, 1)

        row += 1
        self.btn_apply_pid = QPushButton('Apply PID')
        self.btn_apply_pid.setToolTip(
            'Push setpoint, P, I, Ival, limits, and pause gains to the device.',
        )
        g.addWidget(self.btn_apply_pid, row, 0, 1, 2)

        row += 1
        self.btn_enable = QPushButton('Enable PID')
        self.btn_enable.setStyleSheet('background: green; color: white;')
        self.btn_disable = QPushButton('Disable PID')
        self.btn_disable.setStyleSheet('background: red; color: white;')
        g.addWidget(self.btn_enable, row, 0)
        g.addWidget(self.btn_disable, row, 1)

        row += 1
        self.btn_reset = QPushButton('Reset PID')
        self.btn_refresh = QPushButton('Refresh')
        g.addWidget(self.btn_reset, row, 0)
        g.addWidget(self.btn_refresh, row, 1)

        return grp

    def _build_trace_options_group(self):
        """Left-column controls for trace plots (kept here so they are not clipped by splitters)."""
        grp = QGroupBox('Voltage traces')
        v = QVBoxLayout(grp)
        self.trace_show_setpoint_check = QCheckBox('Show setpoint line (input plot)')
        self.trace_show_setpoint_check.setChecked(True)
        self.trace_show_setpoint_check.setToolTip(
            'Toggle the red dashed horizontal line on the input voltage plot (PID lock reference, V).',
        )
        v.addWidget(self.trace_show_setpoint_check)
        return grp

    # ── Setpoint Sequence ────────────────────────────────────────────

    def _build_sequence_group(self):
        grp = QGroupBox('Setpoint Sequence')
        g = QGridLayout(grp)

        self.use_seq_check = QCheckBox('Use Sequence')
        g.addWidget(self.use_seq_check, 0, 0, 1, 2)

        g.addWidget(QLabel('Array (expr):'), 1, 0)
        self.seq_array_edit = QLineEdit('np.zeros(16)')
        self.seq_array_edit.setToolTip('Python expression: np.linspace(0.1,0.5,16), [0.1]*16, etc.')
        g.addWidget(self.seq_array_edit, 1, 1)

        self.seq_index_label = QLabel('Index: 0')
        self.seq_wrap_label = QLabel('Wrap: --')
        g.addWidget(self.seq_index_label, 2, 0)
        g.addWidget(self.seq_wrap_label, 2, 1)

        self.btn_seq_reset = QPushButton('Reset Index')
        self.btn_seq_step = QPushButton('Manual Step')
        g.addWidget(self.btn_seq_reset, 3, 0)
        g.addWidget(self.btn_seq_step, 3, 1)

        for w in [self.seq_array_edit, self.btn_seq_reset, self.btn_seq_step]:
            w.setEnabled(False)

        return grp

    # ── Manual Waveform ──────────────────────────────────────────────

    def _build_waveform_group(self):
        grp = QGroupBox('Manual Waveform')
        g = QGridLayout(grp)

        g.addWidget(QLabel('Type:'), 0, 0)
        self.wf_type_combo = QComboBox()
        self.wf_type_combo.addItems(['dc', 'triangle', 'square', 'sine'])
        g.addWidget(self.wf_type_combo, 0, 1)

        fields = [
            ('Freq (Hz):', 'wf_freq_edit', '1000'),
            ('Amplitude (V):', 'wf_amp_edit', '0.5'),
            ('Offset (V):', 'wf_offset_edit', '0.0'),
        ]
        for i, (label, attr, default) in enumerate(fields):
            g.addWidget(QLabel(label), i + 1, 0)
            edit = QLineEdit(default)
            setattr(self, attr, edit)
            g.addWidget(edit, i + 1, 1)

        row = len(fields) + 1
        self.btn_wf_apply = QPushButton('Apply')
        self.btn_wf_stop = QPushButton('Stop')
        g.addWidget(self.btn_wf_apply, row, 0)
        g.addWidget(self.btn_wf_stop, row, 1)

        return grp

    # ── Plots ────────────────────────────────────────────────────────

    def _build_trace_plots(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        inn = self.ch + 1
        self.trace_input_plot = pg.PlotWidget(title=f'Ch{self.ch} Input (in{inn}) vs Time')
        self.trace_input_plot.setLabel('bottom', 'Time', 's')
        self.trace_input_plot.setLabel('left', 'Voltage', 'V')
        self.trace_input_plot.showGrid(x=True, y=True)
        self.trace_input_plot.setToolTip(
            'Yellow: mean PID input from scope. Red dashed: setpoint (lock reference on PID input, V).',
        )
        self.trace_input_curve = self.trace_input_plot.plot(pen=pg.mkPen('y', width=2))
        self.trace_setpoint_line = pg.InfiniteLine(
            pos=0, angle=0, pen=pg.mkPen('r', width=1, style=Qt.DashLine),
        )
        self.trace_setpoint_line.setToolTip(
            'Setpoint: target voltage on the PID input (lock reference in V), not direct BNC output.',
        )
        self.trace_input_plot.addItem(self.trace_setpoint_line)
        in_legend = self.trace_input_plot.addLegend(offset=(10, 10))
        in_legend.addItem(self.trace_input_curve, 'Input (in)')

        self.trace_output_plot = pg.PlotWidget(title=f'Ch{self.ch} Output (out{inn}) vs Time')
        self.trace_output_plot.setLabel('bottom', 'Time', 's')
        self.trace_output_plot.setLabel('left', 'Voltage', 'V')
        self.trace_output_plot.showGrid(x=True, y=True)
        self.trace_output_plot.setToolTip(
            'Cyan: mean DAC output (out1/out2) from scope (BNC voltage).',
        )
        self.trace_output_curve = self.trace_output_plot.plot(pen=pg.mkPen('c', width=2))
        out_legend = self.trace_output_plot.addLegend(offset=(10, 10))
        out_legend.addItem(self.trace_output_curve, 'Out (BNC)')

        self.trace_show_setpoint_check.toggled.connect(self._on_trace_setpoint_visibility_toggled)

        trace_split = QSplitter(Qt.Vertical)
        trace_split.addWidget(self.trace_input_plot)
        trace_split.addWidget(self.trace_output_plot)
        trace_split.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(trace_split, stretch=1)
        return w

    def _on_trace_setpoint_visibility_toggled(self, checked):
        # Do not bind toggled directly to QGraphicsItem.setVisible: that connection
        # is unreliable across PyQt/pyqtgraph builds. Use an explicit Python callable.
        self.trace_setpoint_line.setVisible(bool(checked))

    def _build_psd_plot(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.psd_plot = pg.PlotWidget(title=f'Ch{self.ch} PSD')
        self.psd_plot.setLabel('bottom', 'Frequency', 'Hz')
        # y=False: we pass y=log10(V²/Hz) ourselves; y=True double-maps and breaks setYRange/labels.
        self.psd_plot.setLabel('left', 'log10(PSD)', 'V²/Hz')
        self.psd_plot.setLogMode(x=True, y=False)
        self.psd_plot.showGrid(x=True, y=True)
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen('g', width=2))
        self.psd_plot.getViewBox().enableAutoRange(x=True, y=False)
        self.psd_plot.getViewBox().invertY(False)
        self.psd_plot.setYRange(-7.0, 3.0, padding=0)  # 10**-7 … 10**3
        # Avoid view-dependent clipping/mangling of the plotted series.
        self.psd_curve.setDynamicRangeLimit(None)
        self.psd_curve.setClipToView(False)
        self.psd_curve.setDownsampling(ds=1, auto=False)
        self.psd_rms_label = QLabel('RMS: --')
        rms_row = QHBoxLayout()
        rms_row.addWidget(self.psd_rms_label)
        rms_row.addStretch()
        lay.addWidget(self.psd_plot)
        lay.addLayout(rms_row)
        return w

    def _build_stats_plot(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.stats_plot = pg.PlotWidget(title=f'Ch{self.ch} Input Histogram')
        self.stats_plot.setLabel('bottom', 'Voltage', 'V')
        self.stats_plot.setLabel('left', 'Count')
        self.stats_plot.showGrid(x=True, y=True)
        self.stats_bar = pg.BarGraphItem(x=[], height=[], width=0.001, brush='b')
        self.stats_plot.addItem(self.stats_bar)
        # Filled step curve: fallback if BarGraphItem does not paint on some pyqtgraph builds.
        self.stats_step_curve = pg.PlotDataItem(
            pen=None,
            brush=pg.mkBrush(30, 30, 255, 160),
            fillLevel=0,
            stepMode='center',
        )
        self.stats_plot.addItem(self.stats_step_curve)
        self.stats_step_curve.setVisible(False)
        self.stats_label = QLabel('μ=--  σ=--')
        stats_row = QHBoxLayout()
        stats_row.addWidget(self.stats_label)
        stats_row.addStretch()
        lay.addWidget(self.stats_plot)
        lay.addLayout(stats_row)
        return w


class RPLockboxTab(DeviceTab):
    device_worker_class = 'user_devices.rp_lockbox.blacs_workers.RPLockboxWorker'

    def initialise_GUI(self):
        layout = self.get_tab_layout()
        self.ch_tabs = QTabWidget()
        self.panels = []
        for ch in (0, 1):
            panel = ChannelPanel(ch, self)
            self.panels.append(panel)
            self.ch_tabs.addTab(panel, f'Channel {ch}  (in{ch+1} / out{ch+1})')
        layout.addWidget(self.ch_tabs)

        for panel in self.panels:
            self._connect_panel_signals(panel)

        self._refresh_timer = QTimer()
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()

        self._psd_stats_timer = QTimer()
        self._psd_stats_timer.setInterval(_PSD_STATS_REFRESH_MS)
        self._psd_stats_timer.timeout.connect(self._on_psd_stats_tick)
        self._psd_stats_timer.start()

    def initialise_workers(self):
        conn = self.settings['connection_table']
        dev = conn.find_by_name(self.device_name)
        ip = dev.properties.get('ip_addr')
        self.create_worker(
            'main',
            'user_devices.rp_lockbox.blacs_workers.RPLockboxWorker',
            {'ip_addr': ip},
        )
        self.primary_worker = 'main'

    def _connect_panel_signals(self, p):
        ch = p.ch

        p.btn_apply_pid.clicked.connect(lambda _, c=ch: self._apply_pid_params(c))
        p.btn_enable.clicked.connect(lambda _, c=ch: self._enable_pid(c))
        p.btn_disable.clicked.connect(lambda _, c=ch: self._disable_pid(c))
        p.btn_reset.clicked.connect(lambda _, c=ch: self._reset_pid(c))
        p.btn_refresh.clicked.connect(lambda _, c=ch: self._refresh_status(c))

        p.use_seq_check.toggled.connect(lambda checked, c=ch: self._toggle_sequence(c, checked))
        p.seq_array_edit.returnPressed.connect(lambda c=ch: self._set_sequence_array(c))
        p.btn_seq_reset.clicked.connect(lambda _, c=ch: self._reset_seq_index(c))
        p.btn_seq_step.clicked.connect(lambda _, c=ch: self._step_seq(c))

        p.btn_wf_apply.clicked.connect(lambda _, c=ch: self._apply_waveform(c))
        p.btn_wf_stop.clicked.connect(lambda _, c=ch: self._stop_waveform(c))

    # ── Timer-driven trace refresh ───────────────────────────────────

    @define_state(_ALL_DEVICE_MODES, True, True)
    def _on_refresh_tick(self):
        active_ch = self.ch_tabs.currentIndex()
        panel = self.panels[active_ch]
        result = yield self.queue_work(self.primary_worker, 'get_trace_data', active_ch)
        if result and result.get('times'):
            t = result['times']
            panel.trace_input_curve.setData(t, result['input'])
            panel.trace_output_curve.setData(t, result['output'])
            panel.trace_setpoint_line.setValue(result['setpoint'])
            panel.trace_setpoint_line.setVisible(panel.trace_show_setpoint_check.isChecked())

    @define_state(_ALL_DEVICE_MODES, True, True)
    def _on_psd_stats_tick(self):
        active_ch = self.ch_tabs.currentIndex()
        psd_result = yield self.queue_work(self.primary_worker, 'compute_psd', active_ch)
        self._apply_psd_worker_result(active_ch, psd_result)
        stats_result = yield self.queue_work(self.primary_worker, 'get_stats', active_ch)
        self._apply_stats_worker_result(active_ch, stats_result)

    def _pause_timers_for_pid_ops(self):
        """Stop trace + continuous PSD/stats to reduce worker FIFO contention during PID ops."""
        self._refresh_timer.stop()
        self._psd_stats_timer.stop()

    def _resume_timers_for_pid_ops(self):
        self._refresh_timer.start()
        self._psd_stats_timer.start()

    # ── PID parameter methods ────────────────────────────────────────

    @staticmethod
    def _parse_pid_panel_params(panel):
        """Build params dict for apply_pid_params, or None if invalid."""
        try:
            return {
                'min_voltage': float(panel.min_v_edit.text()),
                'max_voltage': float(panel.max_v_edit.text()),
                'setpoint': float(panel.setpoint_edit.text()),
                'p': float(panel.p_edit.text()),
                'i': float(panel.i_edit.text()),
                'ival': float(panel.ival_edit.text()),
                'pause_gains': panel.pause_gains_combo.currentText(),
            }
        except ValueError:
            return None

    def _apply_readbacks_to_pid_edits(self, panel, readbacks):
        """Update line edits / combo from worker apply_pid_params return dict."""
        if not readbacks:
            return
        key_to_edit = {
            'setpoint': panel.setpoint_edit,
            'p': panel.p_edit,
            'i': panel.i_edit,
            'ival': panel.ival_edit,
            'min_voltage': panel.min_v_edit,
            'max_voltage': panel.max_v_edit,
        }
        for key, edit in key_to_edit.items():
            if key not in readbacks:
                continue
            val = readbacks[key]
            if isinstance(val, (int, float)):
                edit.setText(f'{val:.6g}')
        if 'pause_gains' in readbacks:
            pg = readbacks['pause_gains']
            panel.pause_gains_combo.blockSignals(True)
            panel.pause_gains_combo.setCurrentText(str(pg))
            panel.pause_gains_combo.blockSignals(False)

    def _apply_status_dict_to_panel(self, channel, result):
        """Update channel panel widgets from get_pid_status dict."""
        p = self.panels[channel]
        p.setpoint_edit.setText(f"{result.get('setpoint', 0):.6g}")
        p.p_edit.setText(f"{result.get('p', 0):.6g}")
        p.i_edit.setText(f"{result.get('i', 0):.6g}")
        p.ival_edit.setText(f"{result.get('ival', 0):.6g}")
        p.min_v_edit.setText(f"{result.get('min_voltage', -1):.6g}")
        p.max_v_edit.setText(f"{result.get('max_voltage', 1):.6g}")
        pg_val = result.get('pause_gains', 'pi')
        p.pause_gains_combo.blockSignals(True)
        p.pause_gains_combo.setCurrentText(pg_val)
        p.pause_gains_combo.blockSignals(False)

        p.use_seq_check.blockSignals(True)
        p.use_seq_check.setChecked(result.get('use_setpoint_sequence', False))
        p.use_seq_check.blockSignals(False)
        p.seq_index_label.setText(f"Index: {result.get('setpoint_index', 0)}")
        wrap = result.get('sequence_wrap_flag', False)
        p.seq_wrap_label.setText(f"Wrap: {'YES' if wrap else '--'}")
        if wrap:
            p.seq_wrap_label.setStyleSheet('color: green; font-weight: bold;')
        else:
            p.seq_wrap_label.setStyleSheet('')

    @define_state(MODE_MANUAL, True)
    def _apply_pid_params(self, channel):
        self._pause_timers_for_pid_ops()
        try:
            panel = self.panels[channel]
            params = self._parse_pid_panel_params(panel)
            if params is None:
                return
            if params['min_voltage'] >= params['max_voltage']:
                return
            result = yield self.queue_work(
                self.primary_worker, 'apply_pid_params', channel, params,
            )
            if isinstance(result, dict):
                self._apply_readbacks_to_pid_edits(panel, result)
        finally:
            self._resume_timers_for_pid_ops()

    @define_state(MODE_MANUAL, True)
    def _enable_pid(self, channel):
        self._pause_timers_for_pid_ops()
        try:
            panel = self.panels[channel]
            params = self._parse_pid_panel_params(panel)
            if params is None:
                return
            if params['min_voltage'] >= params['max_voltage']:
                return
            LOG.info(
                '[rp_lockbox tab timing] _enable_pid ch=%s pre_queue_wall=%.3f',
                channel,
                time.time(),
            )
            result = yield self.queue_work(
                self.primary_worker, 'apply_params_and_enable_pid', channel, params,
            )
            if isinstance(result, dict):
                rb = result.get('readbacks')
                if isinstance(rb, dict):
                    self._apply_readbacks_to_pid_edits(panel, rb)
                en = result.get('enable')
                if isinstance(en, dict):
                    cos = en.get('current_output_signal')
                    cos_s = 'n/a' if cos is None else f'{cos:.6g}'
                    msg = (
                        'enable_pid ch%d: p=%.6g i=%.6g setpoint=%.6g ival=%.6g paused=%s '
                        'output_direct=%r pause_gains=%r current_output_signal=%s'
                    ) % (
                        channel,
                        en.get('p', float('nan')),
                        en.get('i', float('nan')),
                        en.get('setpoint', float('nan')),
                        en.get('ival', float('nan')),
                        en.get('paused'),
                        en.get('output_direct'),
                        en.get('pause_gains'),
                        cos_s,
                    )
                    try:
                        self.logger.info(msg)
                    except AttributeError:
                        LOG.info(msg)
        finally:
            self._resume_timers_for_pid_ops()

    @define_state(MODE_MANUAL, True)
    def _disable_pid(self, channel):
        self._pause_timers_for_pid_ops()
        try:
            LOG.info(
                '[rp_lockbox tab timing] _disable_pid ch=%s pre_queue_wall=%.3f',
                channel,
                time.time(),
            )
            yield self.queue_work(self.primary_worker, 'disable_pid', channel)
        finally:
            self._resume_timers_for_pid_ops()

    @define_state(MODE_MANUAL, True)
    def _reset_pid(self, channel):
        self._pause_timers_for_pid_ops()
        try:
            yield self.queue_work(self.primary_worker, 'reset_pid', channel)
            result = yield self.queue_work(self.primary_worker, 'get_pid_status', channel)
            if isinstance(result, dict):
                self._apply_status_dict_to_panel(channel, result)
        finally:
            self._resume_timers_for_pid_ops()

    @define_state(MODE_MANUAL, True)
    def _refresh_status(self, channel):
        self._pause_timers_for_pid_ops()
        try:
            result = yield self.queue_work(self.primary_worker, 'get_pid_status', channel)
            if not isinstance(result, dict):
                return
            self._apply_status_dict_to_panel(channel, result)
        finally:
            self._resume_timers_for_pid_ops()

    # ── Setpoint sequence ────────────────────────────────────────────

    @define_state(MODE_MANUAL, True)
    def _toggle_sequence(self, channel, checked):
        p = self.panels[channel]
        p.seq_array_edit.setEnabled(checked)
        p.btn_seq_reset.setEnabled(checked)
        p.btn_seq_step.setEnabled(checked)
        if checked:
            self._set_sequence_array(channel)
        else:
            yield self.queue_work(self.primary_worker, 'disable_setpoint_sequence', channel)

    @define_state(MODE_MANUAL, True)
    def _set_sequence_array(self, channel):
        p = self.panels[channel]
        text = p.seq_array_edit.text().strip()
        if not text:
            return
        safe_ns = {
            'np': np, 'numpy': np, 'math': __import__('math'),
            'zeros': np.zeros, 'ones': np.ones,
            'linspace': np.linspace, 'arange': np.arange,
            'array': np.array, '__builtins__': {},
        }
        try:
            result = eval(text, safe_ns)  # noqa: S307
            if hasattr(result, 'tolist'):
                arr = result.tolist()
            elif isinstance(result, (list, tuple)):
                arr = list(result)
            else:
                arr = [float(result)]
        except Exception:
            return
        if len(arr) > 16:
            return
        yield self.queue_work(self.primary_worker, 'set_setpoint_sequence', channel, arr)

    @define_state(MODE_MANUAL, True)
    def _reset_seq_index(self, channel):
        yield self.queue_work(self.primary_worker, 'reset_sequence_index', channel)
        p = self.panels[channel]
        p.seq_index_label.setText('Index: 0')
        p.seq_wrap_label.setText('Wrap: --')
        p.seq_wrap_label.setStyleSheet('')

    @define_state(MODE_MANUAL, True)
    def _step_seq(self, channel):
        yield self.queue_work(self.primary_worker, 'step_sequence', channel)
        result = yield self.queue_work(self.primary_worker, 'get_sequence_status', channel)
        if isinstance(result, dict):
            p = self.panels[channel]
            p.seq_index_label.setText(f"Index: {result.get('index', 0)}")
            wrap = result.get('wrap_flag', False)
            p.seq_wrap_label.setText(f"Wrap: {'YES' if wrap else '--'}")

    # ── Manual waveform ──────────────────────────────────────────────

    @define_state(MODE_MANUAL, True)
    def _apply_waveform(self, channel):
        p = self.panels[channel]
        try:
            wf = p.wf_type_combo.currentText()
            freq = float(p.wf_freq_edit.text())
            amp = float(p.wf_amp_edit.text())
            offset = float(p.wf_offset_edit.text())
        except ValueError:
            return
        yield self.queue_work(
            self.primary_worker, 'set_asg_output', channel, wf, freq, amp, offset,
        )

    @define_state(MODE_MANUAL, True)
    def _stop_waveform(self, channel):
        yield self.queue_work(self.primary_worker, 'stop_asg_output', channel)

    # ── PSD / Stats ──────────────────────────────────────────────────

    def _apply_psd_worker_result(self, channel, result):
        """Update PSD: ``setData`` only; ViewBox autoscaling handles the axes (``_build_psd_plot``)."""
        p = self.panels[channel]
        if not isinstance(result, dict):
            p.psd_curve.setData([], [])
            p.psd_rms_label.setText(
                'PSD failed: worker error (see BLACS worker log / tab error)',
            )
            return
        err = result.get('error')
        if err:
            p.psd_curve.setData([], [])
            short = str(err) if len(str(err)) <= 100 else str(err)[:97] + '...'
            p.psd_rms_label.setText(f'PSD failed: {short}')
            return
        freqs = result.get('freqs', [])
        psd = result.get('psd', [])
        rms = result.get('rms', 0)
        if freqs and psd:
            fx = np.asarray(freqs, dtype=np.float64)
            py = np.asarray(psd, dtype=np.float64)
            # Log mode: skip nonpositive bin values for a valid log plot.
            good = (fx > 0) & (py > 0) & np.isfinite(fx) & np.isfinite(py)
            if not good.any():
                p.psd_curve.setData([], [])
                p.psd_rms_label.setText('PSD: no data (check input / worker log)')
            else:
                f_ok = fx[good]
                p_y = py[good]
                p.psd_rms_label.setText(f'RMS: {rms:.4g} V')
                p.psd_curve.setData(f_ok, np.log10(np.maximum(p_y, 1e-300)))
                try:
                    self.logger.debug(
                        'PSD ch%d plotted: len=%d f=[%.4g, %.4g] Hz',
                        channel, len(f_ok), float(f_ok.min()), float(f_ok.max()),
                    )
                except AttributeError:
                    LOG.debug(
                        'PSD ch%d plotted: len=%d f=[%.4g, %.4g] Hz',
                        channel, len(f_ok), float(f_ok.min()), float(f_ok.max()),
                    )
        else:
            p.psd_curve.setData([], [])
            p.psd_rms_label.setText('PSD: no data (check input / worker log)')

    def _apply_stats_worker_result(self, channel, result):
        """Update histogram from ``get_stats`` return dict.

        Plot logic matches the pre-refactor ``_acquire_stats`` inline implementation.
        """
        p = self.panels[channel]

        def _clear_stats_plot():
            p.stats_bar.setOpts(x=[], height=[], width=0.001)
            p.stats_bar.setVisible(True)
            p.stats_step_curve.setData([])
            p.stats_step_curve.setVisible(False)

        if not isinstance(result, dict):
            _clear_stats_plot()
            p.stats_label.setText(
                'Stats failed: worker error (see BLACS worker log / tab error)',
            )
            return
        err = result.get('error')
        if err:
            _clear_stats_plot()
            short = str(err) if len(str(err)) <= 100 else str(err)[:97] + '...'
            p.stats_label.setText(f'Stats failed: {short}')
            return
        counts = result.get('hist_counts', [])
        edges = result.get('hist_edges', [])
        mean = result.get('mean', 0)
        std = result.get('std', 0)
        if not (counts and edges and len(edges) == len(counts) + 1):
            _clear_stats_plot()
            p.stats_label.setText('Stats: no histogram data (check input / worker log)')
            return

        counts_arr = np.asarray(counts, dtype=np.float64)
        edges_arr = np.asarray(edges, dtype=np.float64)
        centers_arr = (edges_arr[:-1] + edges_arr[1:]) * 0.5
        width = float(edges_arr[1] - edges_arr[0]) if edges_arr.size > 1 else 0.0
        bar_ok = (
            width > 0
            and math.isfinite(width)
            and centers_arr.size == counts_arr.size
            and centers_arr.size > 0
        )

        if bar_ok:
            p.stats_bar.setOpts(
                x=centers_arr,
                height=counts_arr,
                width=width * 0.9,
            )
            p.stats_bar.setVisible(True)
            p.stats_step_curve.setData([])
            p.stats_step_curve.setVisible(False)
        else:
            p.stats_bar.setOpts(x=[], height=[], width=0.001)
            p.stats_bar.setVisible(False)
            p.stats_step_curve.setData(
                centers_arr,
                counts_arr,
                stepMode='center',
                fillLevel=0,
                brush=pg.mkBrush(30, 30, 255, 160),
                pen=None,
            )
            p.stats_step_curve.setVisible(True)

        span = float(edges_arr[-1] - edges_arr[0])
        x_pad = max(span * 0.02, 1e-9)
        y_max = max(float(np.max(counts_arr)), 1.0) * 1.15
        p.stats_plot.setXRange(float(edges_arr[0]) - x_pad, float(edges_arr[-1]) + x_pad, padding=0)
        p.stats_plot.setYRange(0.0, y_max, padding=0)
        p.stats_label.setText(f'μ={mean:.4g}  σ={std:.4g}')
        try:
            self.logger.debug(
                'Stats ch%d: bins=%d bar_ok=%r sum_counts=%.4g',
                channel, len(counts_arr), bar_ok, float(np.sum(counts_arr)),
            )
        except AttributeError:
            LOG.debug(
                'Stats ch%d: bins=%d bar_ok=%r sum_counts=%.4g',
                channel, len(counts_arr), bar_ok, float(np.sum(counts_arr)),
            )

