# rp_lockbox worker timing logs (BLACS diagnosis)

When investigating **slow Enable/Disable PID** or **PSD/histogram** issues, enable INFO logging for `rp_lockbox.blacs_workers` and `rp_lockbox.blacs_tabs` in BLACS (or watch the hardware worker console).

## Log lines

| Prefix | Meaning |
|--------|---------|
| `[rp_lockbox tab timing]` | Tab thread: timestamp **immediately before** `yield queue_work(...)` (wall clock, seconds). |
| `[rp_lockbox worker timing] ... enter_wall=` | Worker **started** this job (wall clock). |
| `[rp_lockbox worker timing] get_trace_data ... body_ms=` | Time spent **inside** `get_trace_data` (throttled: at most once per 5 s per channel, or if slower than 400 ms). |
| `[rp_lockbox worker timing] disable_pid ... body_ms=` | Time inside `disable_pid` (should be tiny if PyRPL is healthy). |
| `[rp_lockbox worker timing] apply_params_and_enable_pid ...` | Split: `apply_pid_params_ms`, `enable_pid_ms`, `total_ms`, plus `enter_wall` / `exit_wall`. |
| `[rp_lockbox worker timing] compute_psd` / `get_stats` ... `body_ms=` | Logged only if that job took **≥ 500 ms** (slow scope / Welch / histogram path). |

## One-session analysis: queue wait vs worker body

1. Click **Disable** (or **Enable**) once.
2. Find the pair of lines for that action:
   - `[rp_lockbox tab timing] _disable_pid ... pre_queue_wall=...` (or `_enable_pid`)
   - `[rp_lockbox worker timing] disable_pid ... enter_wall=...` (or `apply_params_and_enable_pid ... enter_wall=...`)

**Approximate queue + BLACS scheduling delay (seconds)**  
`enter_wall - pre_queue_wall`  
If this is large but **`body_ms`** for `disable_pid` is small, the worker spent most of the wait **not** inside `disable_pid` — typically **other jobs ahead in the FIFO** (often `get_trace_data` from the 100 ms trace timer).

3. For **Enable**, compare **`apply_pid_params_ms`** and **`enable_pid_ms`** to **`total_ms`**. If both parts are large, PyRPL property I/O dominates. If **`total_ms`** is small but the UI still felt slow, the delay was mostly **before** `enter_wall` (queue / tab / mode gating).

## PSD / histogram and scope

- `get_trace_data`, `compute_psd`, and `get_stats` each set `scope.input1` / `input2` and restore them in a **`finally`** block in [`blacs_workers.py`](../rp_lockbox/blacs_workers.py). If plots are empty or errors appear in the worker log, check `compute_psd` / `get_stats` exception messages and any `error` key returned to the tab.
- Tab-side PSD/stats plotting is centralized in `_apply_psd_worker_result` / `_apply_stats_worker_result` in [`blacs_tabs.py`](../rp_lockbox/blacs_tabs.py); behavior is intended to match the older inline `_acquire_psd` / `_acquire_stats` logic.

## Tuning log noise

- `get_trace_data` timing is **throttled** to avoid flooding at 10 Hz.
- To disable detailed worker logs later, raise the log level for `rp_lockbox.blacs_workers` above INFO.
