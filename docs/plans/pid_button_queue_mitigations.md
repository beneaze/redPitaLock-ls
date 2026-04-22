# Plan: Mitigations for slow Enable / Disable PID

## Context (from investigation)

- All tab actions use the same **RPLockboxWorker** FIFO: `get_trace_data`, `compute_psd`, `get_stats`, and PID helpers (`rp_lockbox/blacs_workers.py`).
- **Disable** only calls `disable_pid` (two PyRPL assignments). **Enable** currently does two separate `queue_work` calls: `apply_pid_params` then `enable_pid` (`rp_lockbox/blacs_tabs.py`).
- **Disable** as delayed as **Enable** suggests the bottleneck is often **shared queue / contention**, not only eight parameters on enable.
- `_pause_timers_for_pid_ops` stops both the trace timer (100 ms) and the PSD/stats timer (200 ms) for every PID-related click, which removes live traces during Enable/Disable while not cancelling work already queued on the worker.

## Proposed changes

### 1. PSD/stats-only pause for Enable and Disable

Add `_pause_psd_stats_timer_for_pid_ops` / `_resume_psd_stats_timer_for_pid_ops` that stop/start only `_psd_stats_timer`. Use these in `_enable_pid` and `_disable_pid` instead of full pause.

**Keep** full pause for `_apply_pid_params`, `_reset_pid`, and `_refresh_status`.

**Why it helps:** PSD + stats (`compute_psd` then `get_stats` every 200 ms) competes for the same worker. Pausing only that timer during Enable/Disable reduces new heavy jobs while keeping the trace path live.

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
| `rp_lockbox/blacs_tabs.py` | PSD-only helpers; `_enable_pid` / `_disable_pid`; single `queue_work` for enable; clarify full-pause docstring. |

## Risk / tradeoff

During Enable/Disable, `get_trace_data` may still queue on the worker; intentional for live traces and usually cheaper than PSD+stats.

## Success criteria (manual)

- Enable/Disable feel no worse; ideally faster or less stuck UI (traces keep updating).
- Apply PID / Reset / Refresh keep full timer pause behavior.
