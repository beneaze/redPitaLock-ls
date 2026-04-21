# rp_lockbox — Red Pitaya Dual-Channel PID for labscript

Labscript-suite device integration for a Red Pitaya STEMlab 125-14 acting as a
dual-channel PID lockbox, powered by
[wwlyn's modified PyRPL fork](https://github.com/wwlyn/pyrpl_change)
(branch `max_hold_no_iir_improvement`).

## Features

- Two independent PID loops (pid0/pid1) on in1/out1 and in2/out2
- Live voltage monitoring (input + output traces at ~100 Hz)
- Power Spectral Density (Welch PSD) with integrated RMS readout
- Input histogram with mean / standard deviation display
- Manual waveform generator: DC, triangle, square, sine via ASG
- Setpoint sequences (up to 16 steps, DIO-triggered via wwlyn extension)
- Hold with preserved output, extended Ki bandwidth (FPGA-level features)

## Installation

```bash
conda create -n labscript-rp python=3.9
conda activate labscript-rp

pip install --upgrade pip setuptools wheel
pip install "numpy<2"
pip install labscript-suite PyQt5 scipy

# wwlyn's PyRPL fork (provides hold, setpoint sequences, extended Ki)
git clone -b max_hold_no_iir_improvement https://github.com/wwlyn/pyrpl_change.git
cd pyrpl_change && pip install . && cd ..

# This package
pip install -e /path/to/redPitaLock-ls

labscript-profile-create
```

## Setup

1. Copy or symlink the `rp_lockbox` folder into your labscript `user_devices`
   directory:

   ```bash
   # Find user_devices path
   python -c "import labscript_utils; print(labscript_utils.labscript_profile)"
   # Then symlink:
   # Linux/macOS:
   ln -s /path/to/redPitaLock-ls/rp_lockbox  <labscript_profile>/user_devices/rp_lockbox
   # Windows (admin cmd):
   mklink /D "<labscript_profile>\user_devices\rp_lockbox" "C:\...\redPitaLock-ls\rp_lockbox"
   ```

2. Edit `connection_table.py`, set the IP address of your Red Pitaya, and
   compile it with RunManager.

3. Launch BLACS — the device tab will appear with two channel sub-tabs.

## Connection table example

```python
from labscript import start, stop
from user_devices.rp_lockbox.labscript_devices import RPLockbox

RPLockbox('rp_lockbox', ip_addr='192.168.1.100')

start()
stop(1)
```

## Shot script example

```python
from labscript import start, stop
from user_devices.rp_lockbox.labscript_devices import RPLockbox

RPLockbox('rp_lockbox', ip_addr='192.168.1.100')

rp_lockbox.set_pid_params(0, setpoint=0.3, p=50, i=200)
rp_lockbox.set_pid_params(1, setpoint=-0.1, p=30, i=100, min_voltage=-0.5, max_voltage=0.5)
rp_lockbox.set_setpoint_sequence(0, [0.1, 0.2, 0.3, 0.4])

start()
stop(1)
```

## Architecture

```
connection_table.py          ← labscript: defines RPLockbox device
         ↓
   RPLockbox class           ← labscript_devices.py: stores params → HDF5
         ↓
   RPLockboxTab              ← blacs_tabs.py: PyQt5 GUI with 2 ChannelPanels
         ↓
   RPLockboxWorker           ← blacs_workers.py: PyRPL ↔ FPGA
         ↓
   Red Pitaya FPGA           ← via wwlyn modified PyRPL
     pid0 / pid1             (hold + setpoint seq + ext Ki)
     asg0 / asg1             (manual waveforms)
     scope / sampler          (data acquisition)
```

## Notes

- Python 3.9 is required (wwlyn's PyRPL fork compatibility).
- `numpy < 2` must be installed before PyRPL to avoid API breakage.
- Autotune is not included; the PID runs at FPGA timescales (125 MHz) and a
  Python-side autotuner would be too slow. FPGA-level autotune would require
  custom Verilog development.
