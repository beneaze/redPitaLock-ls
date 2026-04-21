# Labscript + BLACS setup for `rp_lockbox` (Windows)

This document records what was required to get the Red Pitaya `rp_lockbox` device running under the labscript suite with BLACS, including **versions**, **install order**, **configuration**, and **errors that had to be fixed**.

---

## Versions and why they were chosen

### Python **3.9**

- **Why:** The PyRPL fork used for FPGA features (hold, setpoint sequences, extended Ki) is [wwlyn/pyrpl_change](https://github.com/wwlyn/pyrpl_change), branch `max_hold_no_iir_improvement`. That codebase targets older Python ecosystems; **Python 3.9** is a safe middle ground for `labscript-suite`, `numpy<2`, and building native wheels (e.g. `netifaces`) used by PyRPL.
- **Avoid:** Python 3.11+ may work for labscript alone but increases risk of missing or broken wheels for PyRPL dependencies.

### **numpy &lt; 2** (e.g. **1.26.x**)

- **Why:** Install **`numpy<2` before PyRPL**. Upstream PyRPL and older scientific stacks assume NumPy 1.x APIs (`np.complex`, `VisibleDeprecationWarning`, etc.). NumPy 2 removes or changes several of these; the worker includes small shims, but installing NumPy 1.x avoids most breakage.

### **labscript-suite** (pip: **3.3.0** at time of setup)

- Pulled in **BLACS 3.2.3**, **labscript 3.4.2**, **labscript-devices 3.3.0**, **labscript-utils 3.4.3**, **lyse 3.3.0**, **runmanager 3.3.0**, **runviewer 3.2.4**, etc.
- **Why this stack:** Current PyPI “labscript suite” metapackage; pin major versions in `requirements.txt` / `setup.py` if you need reproducibility.

### **h5py** (e.g. **3.11.0**)

- **Why:** A **labscript_utils** `h5_lock` / **double import denier** path can throw `KeyError` when looking up the h5py install directory on Windows if path casing (`Lib` vs `lib`) does not match the keys in the denier’s traceback map. Pinning h5py and/or applying the small patch below avoids startup failures when `h5py` was imported before `labscript_utils.h5_lock` in some orders.

### **wwlyn PyRPL** (`pyrpl` **0.9.5.0** from branch `max_hold_no_iir_improvement`)

- **Why:** Provides **hold**, **setpoint sequences**, and **extended Ki** on the FPGA; not in stock PyRPL PyPI releases in the same form.

---

## Conda environment (example)

```text
conda create -n labscript-rp python=3.9
conda activate labscript-rp
python -m pip install --upgrade pip setuptools wheel
```

**Recommended pip order:**

1. `pip install "numpy<2"`
2. `pip install labscript-suite PyQt5 scipy`
3. Clone and install wwlyn PyRPL:
   - `git clone -b max_hold_no_iir_improvement https://github.com/wwlyn/pyrpl_change.git`
   - `pip install ./pyrpl_change`
4. Optionally pin: `pip install "h5py>=3.10,<3.12"`

Then create / use your labscript profile (if not already):

```text
labscript-profile-create
```

(On an existing machine, the profile may already exist under something like `%USERPROFILE%\labscript-suite`.)

---

## Labscript profile / apparatus layout

Typical layout (paths vary; this matches one working setup):

| Item | Example path |
|------|----------------|
| Labscript suite root | `C:\Users\<user>\labscript-suite` |
| `userlib` | `...\labscript-suite\userlib` |
| `user_devices` | `...\userlib\user_devices` |
| Apparatus name in `labconfig\*.ini` | `rp_lockbox` |
| `labscriptlib\<apparatus>\connection_table.py` | `...\userlib\labscriptlib\rp_lockbox\connection_table.py` |
| Compiled connection table (HDF5) | `C:\Experiments\rp_lockbox\connection_table.h5` |

In **`labconfig\quincy.ini`** (or your active config), set:

```ini
[DEFAULT]
apparatus_name = rp_lockbox
```

Ensure **`[paths]`** resolves `connection_table_h5` and `connection_table_py` to the apparatus experiment folder and `labscriptlib\rp_lockbox\connection_table.py` respectively (defaults using `%(apparatus_name)s` usually do this once `apparatus_name` is set).

Create the experiment output folder if needed, e.g. `C:\Experiments\rp_lockbox`.

---

## Installing `rp_lockbox` into the labscript tree

### 1. Device code location

Keep the package in the git repo (e.g. `redPitaLock-ls/rp_lockbox/`) and expose it to labscript’s **`user_devices`** package.

### 2. Windows: junction (recommended without admin)

Symbolic links for directories often require elevated privileges. A **directory junction** works without admin:

```bat
mklink /J "%USERPROFILE%\labscript-suite\userlib\user_devices\rp_lockbox" "C:\path\to\redPitaLock-ls\rp_lockbox"
```

### 3. `user_devices` must be a Python package

Add an empty **`user_devices/__init__.py`** under `userlib\user_devices` if it does not exist. Otherwise `import user_devices.rp_lockbox` fails and the device registry cannot load `register_classes.py`.

Labscript adds `...\labscript-suite\userlib` to `sys.path`, so `user_devices` imports resolve correctly.

### 4. Confirm BLACS loads your `rp_lockbox` sources

After editing `blacs_tabs.py` or `blacs_workers.py`, **restart BLACS** so the main process and the **hardware worker** subprocess both reload.

Check that Python resolves `user_devices.rp_lockbox` to the tree you maintain (junction from §2, or a copy you update). In the **same conda env** you use to start BLACS (for example `labscript-rp`):

```text
python -c "import user_devices.rp_lockbox.blacs_tabs as t; print(t.__file__)"
```

If that path is not your checkout, changes in a different clone (for example `redPitaLock-ls` on disk) will not affect BLACS until you refresh `userlib\user_devices\rp_lockbox` or the junction target.

---

## Device registration (`register_classes.py`) — critical detail

BLACS looks up tabs by **labscript device class name** (the Python class), not the package folder name.

**Wrong:**

```python
register_classes('rp_lockbox', ...)  # does NOT match class RPLockbox
```

**Correct:**

```python
register_classes(
    'RPLockbox',
    BLACS_tab='user_devices.rp_lockbox.blacs_tabs.RPLockboxTab',
    runviewer_parser=None,
)
```

If this is wrong, BLACS raises:

```text
ImportError: No BLACS_tab registered for a device named RPLockbox
...
ModuleNotFoundError: No module named 'labscript_devices.RPLockbox'
```

That error means the registry never recorded a tab for **`RPLockbox`**, so it falls back to importing `labscript_devices.RPLockbox`, which does not exist.

---

## Compiling the connection table

Running `connection_table.py` directly with only `start()` / `stop()` can fail with:

```text
LabscriptError: hdf5 file for compilation not set. Please call labscript_init
```

Use **`labscript_init(path_to.h5, new=True)`** before `start()` / `stop()`, or compile from Run Manager. The repo includes **`compile_connection_table.py`** as a template; **edit paths** (`connection_table.h5` location and Red Pitaya `ip_addr`) for your PC.

After compilation, verify the HDF5 contains `/devices/rp_lockbox` (or your device instance name) and the connection table row for `RPLockbox`.

---

## Launching BLACS

From the conda env:

```text
blacs
```

Or full path, e.g.:

```text
%CONDA_PREFIX%\Scripts\blacs.exe
```

### Error: `ZMQError: Address in use (addr='tcp://*:42517')`

**Cause:** Another BLACS (or process) is already bound to the BLACS port (default **42517** in `labconfig` `[ports]`).

**Fix:** Quit the other BLACS instance or end the process holding the port, then start BLACS again.

---

## Optional: `h5_lock` / Windows path casing

If you see a **`KeyError`** referencing `h5py`’s directory inside `labscript_utils/h5_lock.py`, it can be due to **`Lib` vs `lib`** in paths on Windows. A minimal fix is to resolve the denier key with **`os.path.normcase`** when matching `os.path.dirname(h5py.__file__)` to keys in `denier.tracebacks`. This is an environment-specific patch inside `site-packages`; document it if you apply it so upgrades to `labscript_utils` do not silently overwrite it.

---

## Red Pitaya IP

The example connection table uses **`10.0.0.15`** (from the existing `redPitaLock` deploy script). Change `ip_addr` in `connection_table.py` / compile script to match your network.

---

## Quick checklist

- [ ] Conda env **Python 3.9**, **`numpy<2`** before PyRPL  
- [ ] **labscript-suite** + **PyQt5** + **scipy**  
- [ ] **wwlyn/pyrpl_change** branch **`max_hold_no_iir_improvement`** installed  
- [ ] **`user_devices/rp_lockbox`** → junction/copy to repo’s `rp_lockbox`  
- [ ] **`user_devices/__init__.py`** present  
- [ ] **`register_classes('RPLockbox', ...)`** — exact class name  
- [ ] **`labconfig`** `apparatus_name` and paths to `connection_table.h5`  
- [ ] **Compile** connection table (`labscript_init` or Run Manager)  
- [ ] **BLACS** only one instance, or free port **42517**  
- [ ] Red Pitaya **IP** correct and reachable  
- [ ] Worker has **`QCoreApplication`** (handled in `blacs_workers.py`); PyRPL numpy patches applied if needed  

---

## PyRPL numpy compatibility patches (required)

Even with `numpy < 2`, numpy 1.24+ removed several deprecated aliases (`np.complex`, `np.float`, `np.int`, `np.bool`, `np.object`, `np.str`) that PyRPL still uses throughout its codebase. These cause `AttributeError` crashes during `Pyrpl()` init (specifically in `network_analyzer.py` and other modules).

**Three patches to the installed PyRPL package** are needed (all under `site-packages/pyrpl/`):

### 1. `pyrpl/__init__.py` -- add numpy shims at the top

After `import numpy as np`, add:

```python
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    if not hasattr(np, 'complex'):
        np.complex = complex
    if not hasattr(np, 'float'):
        np.float = float
    if not hasattr(np, 'int'):
        np.int = int
    if not hasattr(np, 'bool'):
        np.bool = bool
    if not hasattr(np, 'object'):
        np.object = object
    if not hasattr(np, 'str'):
        np.str = str
```

### 2. `pyrpl/software_modules/network_analyzer.py` -- replace `np.complex` with `complex`

Replace all occurrences of `dtype=np.complex` with `dtype=complex` and `dtype=np.float` with `dtype=float`.

### 3. `pyrpl/pyrpl.py` -- don't crash on non-essential software modules

In `load_software_modules()`, change `raise e` to `continue` so that errors in non-essential modules (e.g. lockbox division-by-zero on fresh config) are logged but don't kill the entire init.

---

## BLACS worker: Qt event loop, scope acquisition, and ASG

The hardware worker runs in a **separate process**. PyRPL’s `scope.single()` completes via a nested **`QEventLoop`** (`pyrpl.async_utils`). Without a **`QCoreApplication`** in that process, scope acquisitions can fail or return empty data, which shows up as **blank PSD / histogram** plots in the BLACS tab.

**Implemented in `rp_lockbox/blacs_workers.py`:** at the start of `RPLockboxWorker.init()`, if `QCoreApplication.instance()` is `None`, create `QCoreApplication(sys.argv)` before constructing `Pyrpl(...)`.

**Live voltage traces** in `RPLockboxWorker.get_trace_data`: two back-to-back raw scope acquisitions per refresh — first **`in1` / `in2`** (input means), then **`out1` / `out2`** (DAC / BNC means). PyRPL autosave on PID/ASG modules is disabled in the worker init so BLACS register writes are not overwritten from `rp_lockbox.yml`. On **Enable PID**, the worker logs FPGA readbacks including **`current_output_signal`** when available.

**Scope traces for PSD / stats** use the same **`_read_scope_raw`** path as the rest of the worker: the FPGA trigger is armed for an immediate acquisition, the code waits for the buffer fill time, then bulk-reads the scope RAM into **1-D float arrays** (volts). This bypasses **`scope.single()`**, which can hang on some bitfiles when **`curve_ready()`** never asserts.

**Manual ASG (DC, triangle, square, sine):**

- **Never set ASG `frequency` to 0** — use a placeholder (e.g. 1 kHz) for **DC**; for other waveforms clamp to a small minimum (e.g. 0.1 Hz).
- **DC:** waveform table is all zeros; use **`amplitude=1.0`** and set the desired level with **`offset`** (volts).
- **Triangle / square / sine:** after `setup`, set **`asg.periodic = True`** so the generator repeats continuously instead of stopping after one cycle.
- Call **`asg.trig()`** when available (PyRPL pattern: `immediately` then `off`) to re-arm the trigger after `setup`.

---

## Summary table

| Component | Version / choice | Reason |
|-----------|------------------|--------|
| Python | 3.9 | Compatible with labscript + PyRPL fork + wheels |
| numpy | &lt; 2 (e.g. 1.26.4) | PyRPL / legacy API expectations |
| labscript-suite | 3.3.0 (example) | Current metapackage on PyPI at setup time |
| BLACS | 3.2.3 (pulled in) | Matches suite |
| PyRPL | wwlyn fork, branch `max_hold_no_iir_improvement` | Hold, setpoint seq, ext Ki |
| Device registration | `register_classes('RPLockbox', ...)` | Must match **class** name `RPLockbox` |

---

*Generated to document a working Windows + Miniconda + labscript-suite + `rp_lockbox` deployment. Adjust paths and IPs for your machine.*
