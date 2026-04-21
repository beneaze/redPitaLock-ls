import os
import math

os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PyQt5')

from blacs.device_base_class import DeviceTab
from blacs.tab_base_classes import define_state, MODE_MANUAL

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
        left.addWidget(self._build_sequence_group())
        left.addWidget(self._build_waveform_group())
        left.addStretch()

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(self._build_trace_plot())
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

    def _build_trace_plot(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.trace_plot = pg.PlotWidget(title=f'Ch{self.ch} Voltage vs Time')
        self.trace_plot.setLabel('bottom', 'Time', 's')
        self.trace_plot.setLabel('left', 'Voltage', 'V')
        self.trace_plot.showGrid(x=True, y=True)
        self.trace_input_curve = self.trace_plot.plot(pen=pg.mkPen('y', width=2), name='Input')
        self.trace_output_curve = self.trace_plot.plot(pen=pg.mkPen('c', width=2), name='Output')
        self.trace_setpoint_line = pg.InfiniteLine(
            pos=0, angle=0, pen=pg.mkPen('r', width=1, style=Qt.DashLine),
        )
        self.trace_plot.addItem(self.trace_setpoint_line)
        legend = self.trace_plot.addLegend(offset=(10, 10))
        legend.addItem(self.trace_input_curve, 'Input')
        legend.addItem(self.trace_output_curve, 'Output')
        lay.addWidget(self.trace_plot)
        return w

    def _build_psd_plot(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.psd_plot = pg.PlotWidget(title=f'Ch{self.ch} PSD')
        self.psd_plot.setLabel('bottom', 'Frequency', 'Hz')
        self.psd_plot.setLabel('left', 'PSD', 'V²/Hz')
        self.psd_plot.setLogMode(x=True, y=True)
        self.psd_plot.showGrid(x=True, y=True)
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen('g', width=2))
        self.psd_rms_label = QLabel('RMS: --')
        btn_row = QHBoxLayout()
        self.btn_psd = QPushButton('Acquire PSD')
        btn_row.addWidget(self.btn_psd)
        btn_row.addWidget(self.psd_rms_label)
        btn_row.addStretch()
        lay.addWidget(self.psd_plot)
        lay.addLayout(btn_row)
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
        self.stats_label = QLabel('μ=--  σ=--')
        btn_row = QHBoxLayout()
        self.btn_stats = QPushButton('Acquire Stats')
        btn_row.addWidget(self.btn_stats)
        btn_row.addWidget(self.stats_label)
        btn_row.addStretch()
        lay.addWidget(self.stats_plot)
        lay.addLayout(btn_row)
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

        p.setpoint_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'setpoint'))
        p.p_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'p'))
        p.i_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'i'))
        p.ival_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'ival'))
        p.min_v_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'min_voltage'))
        p.max_v_edit.returnPressed.connect(lambda c=ch: self._set_param(c, 'max_voltage'))
        p.pause_gains_combo.currentTextChanged.connect(lambda v, c=ch: self._set_pause_gains(c, v))

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

        p.btn_psd.clicked.connect(lambda _, c=ch: self._acquire_psd(c))
        p.btn_stats.clicked.connect(lambda _, c=ch: self._acquire_stats(c))

    # ── Timer-driven trace refresh ───────────────────────────────────

    @define_state(MODE_MANUAL, True)
    def _on_refresh_tick(self):
        active_ch = self.ch_tabs.currentIndex()
        panel = self.panels[active_ch]
        result = yield self.queue_work(self.primary_worker, 'get_trace_data', active_ch)
        if result and result.get('times'):
            t = result['times']
            panel.trace_input_curve.setData(t, result['input'])
            panel.trace_output_curve.setData(t, result['output'])
            panel.trace_setpoint_line.setValue(result['setpoint'])

    # ── PID parameter methods ────────────────────────────────────────

    @define_state(MODE_MANUAL, True)
    def _set_param(self, channel, name):
        panel = self.panels[channel]
        edit_map = {
            'setpoint': panel.setpoint_edit,
            'p': panel.p_edit,
            'i': panel.i_edit,
            'ival': panel.ival_edit,
            'min_voltage': panel.min_v_edit,
            'max_voltage': panel.max_v_edit,
        }
        try:
            val = float(edit_map[name].text())
        except ValueError:
            return
        result = yield self.queue_work(self.primary_worker, 'set_pid_param', channel, name, val)
        if isinstance(result, (int, float)):
            edit_map[name].setText(f'{result:.6g}')

    @define_state(MODE_MANUAL, True)
    def _set_pause_gains(self, channel, value):
        yield self.queue_work(self.primary_worker, 'set_pid_param', channel, 'pause_gains', value)

    @define_state(MODE_MANUAL, True)
    def _enable_pid(self, channel):
        yield self.queue_work(self.primary_worker, 'enable_pid', channel)

    @define_state(MODE_MANUAL, True)
    def _disable_pid(self, channel):
        yield self.queue_work(self.primary_worker, 'disable_pid', channel)

    @define_state(MODE_MANUAL, True)
    def _reset_pid(self, channel):
        yield self.queue_work(self.primary_worker, 'reset_pid', channel)
        self._refresh_status(channel)

    @define_state(MODE_MANUAL, True)
    def _refresh_status(self, channel):
        result = yield self.queue_work(self.primary_worker, 'get_pid_status', channel)
        if not isinstance(result, dict):
            return
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

    @define_state(MODE_MANUAL, True)
    def _acquire_psd(self, channel):
        result = yield self.queue_work(self.primary_worker, 'compute_psd', channel)
        if not isinstance(result, dict):
            return
        freqs = result.get('freqs', [])
        psd = result.get('psd', [])
        rms = result.get('rms', 0)
        p = self.panels[channel]
        if freqs and psd:
            p.psd_curve.setData(freqs, psd)
        p.psd_rms_label.setText(f'RMS: {rms:.4g} V')

    @define_state(MODE_MANUAL, True)
    def _acquire_stats(self, channel):
        result = yield self.queue_work(self.primary_worker, 'get_stats', channel)
        if not isinstance(result, dict):
            return
        p = self.panels[channel]
        counts = result.get('hist_counts', [])
        edges = result.get('hist_edges', [])
        mean = result.get('mean', 0)
        std = result.get('std', 0)
        if counts and edges:
            centers = [(edges[i] + edges[i + 1]) / 2 for i in range(len(counts))]
            width = edges[1] - edges[0] if len(edges) > 1 else 0.001
            p.stats_bar.setOpts(x=centers, height=counts, width=width * 0.9)
        p.stats_label.setText(f'μ={mean:.4g}  σ={std:.4g}')
