# Plan: Mitigations for slow Enable / Disable PID

## Context (from investigation)

- All tab actions use the same **RPLockboxWorker** FIFO: `get_trace_data`, `compute_psd`, `get_stats`, and PID helpers (`rp_lockbox/blacs_workers.py`).
- **Disable** only calls `disable_pid` (two PyRPL assignments). **Enable** currently does two separate `queue_work` calls: `apply_pid_params` then `enable_pid` (`rp_lockbox/blacs_tabs.py`).
- **Disable** as delayed as **Enable** suggests the bottleneck is often **shared queue / contention**, not only eight parameters on enable.
- `_pause_timers_for_pid_ops` stops the trace refresh timer (100 ms) and the **continuous PSD + histogram timer** (~400 ms) during PID-related clicks to cut worker FIFO contention.

## Proposed changes

### 1. Pause trace timer for Enable / Disable (revised)

**Superseded:** PSD-only pause + auto PSD timer was reverted. **Current behavior:** `_enable_pid` / `_disable_pid` use the same trace-timer pause as Apply/Reset/Refresh so `get_trace_data` does not queue ahead of the PID job (traces freeze briefly during the click).

### 2. Single merged `queue_work` for Enable

Add `apply_params_and_enable_pid(self, channel, params)` in `blacs_workers.py` that calls `apply_pid_params` then `enable_pid` and returns `{'readbacks': ..., 'enable': ...}`. Update `_enable_pid` to use one `queue_work`.

**Why it helps:** Removes one BLACS worker round-trip per Enable; same PyRPL order inside one worker job.

### 3. Out of scope

- Skip redundant apply on enable (follow-up if needed).
- Instrumentation (timestamps) optional after shipping.

## Implementation checklist

| File | Change |
|------|--------|
| `rp_lockbox/blacs_workers.py` | Add `apply_params_and_enable_pid` after `apply_pid_params`. |
| `rp_lockbox/blacs_tabs.py` | Trace timer pause for PID ops; `_enable_pid` / `_disable_pid`; single `queue_work` for enable; `_apply_psd_worker_result` / `_apply_stats_worker_result` for **manual** Acquire only (no auto PSD timer). |

**Implemented** on `main`, then auto PSD timer **removed** in a follow-up.

## Risk / tradeoff

During Enable/Disable, the trace timer is paused so `get_trace_data` does not compete in the worker queue for that interval (traces briefly freeze).

## Success criteria (manual)

- Enable/Disable feel no worse; ideally faster or less stuck UI (traces keep updating).
- Apply PID / Reset / Refresh keep full timer pause behavior.
