"""
Shadow mode integration for the CP-SAT solver.

Runs the solver after ProxLB has computed its migration plan, writes a structured
JSONL log file (one per run) for later analysis, and emits a single summary line
to the main ProxLB logger. Never touches the Proxmox API.

JSONL event types written to the run file:
  proxlb_action  — one entry per migration ProxLB planned (logged before solving)
                   fields: vm, source, target, type (vm|ct)
  constraint     — one entry per constraint recognized by the solver
  solver_run     — overall status, migration count, load gap, wall time
  plan_step      — one entry per migration in the solver's ordered plan
  pve_deferred   — VMs whose follow-up moves are delegated to PVE HA
  unbreakable_cycle — cycle that could not be resolved by the planner
  compare        — per-VM comparison between solver and ProxLB decisions
                   result: agree | differ | solver_only | proxlb_only
  infeasible     — solver found no valid placement; lists blocking VMs
  error          — unexpected exception during shadow run
  proxlb_executed — appended after Balancing() completes; dry_run=True when
                    ProxLB ran with --dry-run (no migrations were issued)
"""

from __future__ import annotations
import datetime
import json
import os
import traceback
import logging
import types
from typing import Any, IO, TYPE_CHECKING

if TYPE_CHECKING:
    from proxlb.utils.proxlb_data import ProxLbData
    from proxlb.utils.config_parser import Config
    from .models import MigrationPlan


# ---------------------------------------------------------------------------
# Normalisation helpers — accept dict or Pydantic; always return a consistent type
# ---------------------------------------------------------------------------

def _normalize_solver_cfg(cfg: Any) -> types.SimpleNamespace:
    """Convert a dict solver_cfg to a SimpleNamespace; pass Pydantic models through."""
    if isinstance(cfg, dict):
        return types.SimpleNamespace(
            mode=cfg.get("mode", "shadow"),
            log_dir=cfg.get("log_dir", "/var/log/proxlb/solver"),
            use_reservations=bool(cfg.get("use_reservations", True)),
            timeout_seconds=float(cfg.get("timeout_seconds", 30.0)),
            active_step_retries=int(cfg.get("active_step_retries", 3)),
        )
    return cfg  # type: ignore[no-any-return]  # already a Pydantic Config.Solver


def _normalize_proxlb_data(proxlb_data: Any) -> dict[str, Any]:
    """Ensure proxlb_data is a plain dict; normalise Pydantic ProxLbData if needed."""
    if isinstance(proxlb_data, dict):
        return proxlb_data
    from .adapter import _pydantic_to_dict
    return _pydantic_to_dict(proxlb_data)


def _guests_of(proxlb_data: Any) -> dict[str, Any]:
    return proxlb_data.get("guests", {}) if isinstance(proxlb_data, dict) else proxlb_data.guests  # type: ignore[no-any-return]


def _guest_get(gd: Any, key: str, default: Any = None) -> Any:
    return gd.get(key, default) if isinstance(gd, dict) else getattr(gd, key, default)


def _guest_set(gd: Any, key: str, value: Any) -> None:
    if isinstance(gd, dict):
        gd[key] = value
    else:
        setattr(gd, key, value)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write(f: IO[str], event: dict[str, Any]) -> None:
    event.setdefault("ts", _ts())
    f.write(json.dumps(event, default=str) + "\n")
    f.flush()


def _make_run_file(log_dir: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"solver_run_{ts}.jsonl")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_shadow(proxlb_data: "dict[str, Any] | ProxLbData", solver_cfg: "dict[str, Any] | Config.Solver") -> "tuple[str | None, MigrationPlan | None]":
    """Run solver in shadow mode.

    Accepts either plain dicts (standalone/tests) or Pydantic models (ProxLB 2.0).

    Writes a structured JSONL file to solver_cfg.log_dir and logs one
    summary line to the 'proxlb' logger. Never modifies the cluster.

    Returns ``(run_file_path, migration_plan)`` where *run_file_path* is the
    path to the created JSONL file (``None`` when the directory cannot be
    created) and *migration_plan* is the :class:`MigrationPlan` produced by
    the solver (``None`` on infeasibility or error).
    """
    import logging
    log = logging.getLogger("ProxLB")

    data = _normalize_proxlb_data(proxlb_data)
    cfg  = _normalize_solver_cfg(solver_cfg)

    mode             = str(cfg.mode)
    log_dir          = cfg.log_dir
    use_reservations = cfg.use_reservations
    timeout_seconds  = cfg.timeout_seconds
    max_step_retries = cfg.active_step_retries
    balancing_cfg    = data.get("meta", {}).get("balancing", {})
    balanciness      = balancing_cfg.get("balanciness", 3)
    method           = balancing_cfg.get("method", "memory")

    log.info(
        f"[solver] starting — "
        f"mode={mode}  "
        f"log_dir={log_dir}  "
        f"use_reservations={use_reservations}  "
        f"timeout={timeout_seconds}s  "
        f"balanciness={balanciness}  "
        f"method={method}"
        + (f"  active_step_retries={max_step_retries}" if mode == "active" else "")
    )

    try:
        run_file = _make_run_file(log_dir)
    except OSError as exc:
        log.warning(f"[solver] cannot create log_dir={log_dir!r}: {exc}")
        return None, None

    _plan = None
    try:
        with open(run_file, "w") as f:
            _plan = _shadow_inner(data, cfg, f, log, run_file)
    except Exception as exc:  # noqa: BLE001
        # Safety net — _shadow_inner handles its own errors; this catches
        # unexpected failures outside that scope (e.g. file write errors).
        log.warning(f"[solver] shadow run failed: {exc}")

    return run_file, _plan


def finalize_run(run_file: str, dry_run: bool = False) -> None:
    """Append a ``proxlb_executed`` event to *run_file* after Balancing completes.

    Call this from ProxLB's main loop after ``Balancing(proxmox_api, proxlb_data)``
    to record whether migrations were actually issued or skipped (dry-run).

    Args:
        run_file: Path returned by :func:`run_shadow`.
        dry_run:  True when ProxLB was started with ``--dry-run``; no migrations
                  were issued to the Proxmox API.
    """
    try:
        with open(run_file, "a") as f:
            _write(f, {"event": "proxlb_executed", "dry_run": dry_run})
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _shadow_inner(
    proxlb_data: dict[str, Any],
    solver_cfg: types.SimpleNamespace,
    f: IO[str],
    log: logging.Logger,
    run_file: str,
) -> "MigrationPlan | None":
    try:
        # Log ProxLB's planned migrations first — independent of the solver,
        # so they are always captured even if cluster building or solving fails.
        _write_proxlb_plan(proxlb_data, f)
        _write_cluster_state(proxlb_data, solver_cfg, f)

        from .adapter import from_proxlb_data
        from .solver import solve
        from .planner import plan_migrations

        cluster = from_proxlb_data(proxlb_data, use_reservations=solver_cfg.use_reservations)

        # Log all identified constraints before solving so the full rule picture
        # is always visible, even if the solver fails or times out.
        _write_constraints(cluster, f)

        time_limit_s = solver_cfg.timeout_seconds
        solution = solve(cluster, time_limit_s=time_limit_s)

        _write(f, {
            "event": "solver_run",
            "mode": str(solver_cfg.mode),
            "status": solution.stats.status,
            "migrations": solution.stats.migration_count,
            "migration_cost_gib": solution.stats.migration_cost_gib,
            "gap": round(solution.stats.load_gap, 6),
            "wall_time_ms": round(solution.stats.wall_time_ms, 1),
            "feasible": solution.feasible,
        })

        # Single summary line to the main ProxLB log
        log.info(
            f"[solver] run={run_file} status={solution.stats.status} "
            f"migrations={solution.stats.migration_count} "
            f"gap={solution.stats.load_gap:.3f} "
            f"time={solution.stats.wall_time_ms:.0f}ms"
        )

        if not solution.feasible:
            _write(f, {"event": "infeasible", "blocking_vms": solution.blocking_vms})
            return None

        plan = plan_migrations(cluster, solution)

        for step in plan.steps:
            for m in step.migrations:
                _write(f, {
                    "event": "plan_step",
                    "step": step.step,
                    "vm": m.vm,
                    "source": m.source,
                    "target": m.target,
                    "parallel": step.parallel,
                })

        if plan.pve_deferred:
            _write(f, {"event": "pve_deferred", "vms": plan.pve_deferred})

        if not plan.path_feasible:
            _write(f, {"event": "unbreakable_cycle", "vms": plan.unbreakable_cycle})

        _write_comparison(proxlb_data, solution, f)
        return plan

    except Exception as exc:
        _write(f, {
            "event": "error",
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })
        log.warning(f"[solver] shadow run failed: {exc}")
        return None


def _write_proxlb_plan(proxlb_data: dict[str, Any], f: IO[str]) -> None:
    """Emit one ``proxlb_action`` event per migration ProxLB has planned.

    These are derived directly from ``proxlb_data`` before the solver runs,
    so they capture ProxLB's independent decision for later comparison.
    Guests whose ``node_target`` equals ``node_current`` (no move needed) are
    silently skipped.
    """
    for name in sorted(proxlb_data.get("guests", {})):
        gd = proxlb_data["guests"][name]
        if gd.get("node_target") and gd["node_target"] != gd.get("node_current"):
            _write(f, {
                "event": "proxlb_action",
                "vm": name,
                "source": gd.get("node_current"),
                "target": gd["node_target"],
                "type": gd.get("type", "vm"),
            })


def _write_cluster_state(proxlb_data: dict[str, Any], solver_cfg: types.SimpleNamespace, f: IO[str]) -> None:
    """Emit a ``cluster_state`` snapshot used for before/after load reporting.

    Captures per-node memory and CPU totals plus the current VM placement so
    the reporter can compute the projected load after applying the solver plan.
    """
    balancing_cfg = proxlb_data.get("meta", {}).get("balancing", {})
    method = balancing_cfg.get("method", "memory")

    nodes: dict[str, Any] = {}
    for name, nd in proxlb_data.get("nodes", {}).items():
        def _nd_val(key: str, subkey: str | None = None, default: Any = 0, _nd: dict[str, Any] = nd) -> Any:
            val = _nd.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        mem_used  = _nd_val("memory_used", default=_nd_val("memory", "used"))
        mem_free  = _nd_val("memory_free", default=_nd_val("memory", "free"))
        raw_total = (mem_used + mem_free) if (mem_used + mem_free) > 0 else _nd_val("memory_total", default=_nd_val("memory", "total"))
        nodes[name] = {
            "cpu_total":    _nd_val("cpu_total", default=_nd_val("cpu", "total")),
            "memory_total": raw_total,
            "memory_used":  mem_used,
        }

    guests: dict[str, Any] = {}
    for name, gd in proxlb_data.get("guests", {}).items():
        def _gd_val(key: str, subkey: str | None = None, default: Any = 0, _gd: dict[str, Any] = gd) -> Any:
            val = _gd.get(key)
            if subkey and isinstance(val, dict):
                return val.get(subkey, default)
            return val if val is not None else default

        entry: dict[str, Any] = {
            "node":   _gd_val("node_current", default=gd.get("node_current")),
            "memory": _gd_val("memory_total", default=_gd_val("memory", "total")),
            "cpu":    _gd_val("cpu_total", default=_gd_val("cpu", "total", default=1)),
        }
        mem_used = _gd_val("memory_used", default=_gd_val("memory", "used"))
        if mem_used > 0:
            entry["memory_used"] = mem_used
        cpu_used = _gd_val("cpu_used", default=_gd_val("cpu", "used"))
        if cpu_used > 0:
            entry["cpu_usage"] = cpu_used
        guests[name] = entry

    _write(f, {
        "event":  "cluster_state",
        "method": method,
        "nodes":  nodes,
        "guests": guests,
    })


def _write_constraints(cluster: Any, f: IO[str]) -> None:
    """Emit one JSONL event per identified constraint so the rule picture is
    always visible in the log, regardless of whether solving succeeds.

    Event fields by type:
      affinity / anti_affinity:
        name    — rule/tag/pool identifier
        origin  — "pve" (native HA rule) | "plb" (tag or pool)
        vms     — list of VM names in the group
        hard    — True = hard constraint (solver must satisfy)

      pin:
        vm      — VM name
        nodes   — allowed node names (union of all pin sources)
        origins — list of {origin, source} dicts showing where each pin came from

      ignore:
        vm      — VM name that is excluded from rebalancing
    """
    for rule in cluster.constraints.affinity:
        _write(f, {
            "event": "constraint",
            "type": "affinity",
            "name": rule["name"],
            "origin": rule["origin"],
            "vms": rule["vms"],
            "hard": rule.get("hard", True),
        })

    for rule in cluster.constraints.anti_affinity:
        _write(f, {
            "event": "constraint",
            "type": "anti_affinity",
            "name": rule["name"],
            "origin": rule["origin"],
            "vms": rule["vms"],
            "hard": rule.get("hard", True),
        })

    for rule in cluster.constraints.pin:
        _write(f, {
            "event": "constraint",
            "type": "pin",
            "vm": rule["vm"],
            "nodes": rule["nodes"],
            "origins": rule.get("origins", []),
        })

    for vm_name in cluster.constraints.ignore:
        _write(f, {
            "event": "constraint",
            "type": "ignore",
            "vm": vm_name,
        })


def _write_comparison(proxlb_data: Any, solution: Any, f: IO[str]) -> None:
    """Compare solver plan with ProxLB decisions, write one JSONL line per VM."""
    solver_plan = {m.vm: m.target for m in solution.migrations}
    guests = _guests_of(proxlb_data)
    proxlb_plan = {
        name: _guest_get(gd, "node_target")
        for name, gd in guests.items()
        if _guest_get(gd, "node_target") and _guest_get(gd, "node_target") != _guest_get(gd, "node_current")
    }
    all_vms = set(solver_plan) | set(proxlb_plan)
    for vm in sorted(all_vms):
        sv, pv = solver_plan.get(vm), proxlb_plan.get(vm)
        if sv == pv:
            _write(f, {"event": "compare", "vm": vm, "result": "agree", "target": sv})
        elif sv and pv:
            _write(f, {"event": "compare", "vm": vm, "result": "differ",
                       "solver_target": sv, "proxlb_target": pv})
        elif sv:
            _write(f, {"event": "compare", "vm": vm, "result": "solver_only",
                       "solver_target": sv})
        else:
            _write(f, {"event": "compare", "vm": vm, "result": "proxlb_only",
                       "proxlb_target": pv})


# ---------------------------------------------------------------------------
# Active mode — feedback loop execution
# ---------------------------------------------------------------------------

def _append_run_event(run_file: str | None, event: dict[str, Any]) -> None:
    """Append one event dict to the run JSONL file; silently swallows errors."""
    if not run_file:
        return
    try:
        with open(run_file, "a") as f:
            _write(f, event)
    except OSError:
        pass


def _log_step_retry(
    run_file: str | None,
    failed_step: int,
    step_retry: int,
    pinned_vms: set[str],
) -> None:
    """Append an ``active_step_retry`` event when a step triggers a re-solve.

    Fields:
      step        — the step number that failed and triggered this retry
      step_retry  — monotonically increasing counter across all re-solves
      pinned_vms  — all VMs pinned to their current node so far
    """
    _append_run_event(run_file, {
        "event": "active_step_retry",
        "step": failed_step,
        "step_retry": step_retry,
        "pinned_vms": sorted(pinned_vms),
    })


def _log_resolve(run_file: str | None, solution: Any, step_retry: int) -> None:
    """Append an ``active_resolve`` event with the re-solve outcome.

    Fields:
      step_retry  — which retry number triggered this re-solve
      status      — CP-SAT status string (e.g. "OPTIMAL", "INFEASIBLE")
      migrations  — number of migrations in the new plan (0 if infeasible)
      feasible    — whether the re-solve found a valid placement
    """
    _append_run_event(run_file, {
        "event": "active_resolve",
        "step_retry": step_retry,
        "status": solution.stats.status,
        "migrations": solution.stats.migration_count,
        "feasible": solution.feasible,
    })


def _verify_step(proxmox_api: Any, step_migrations: dict[str, str]) -> dict[str, str | None]:
    """Query actual guest locations via the PVE cluster API.

    Returns a dict mapping ``guest_name → actual_node`` (``None`` when the guest
    cannot be found or the API call fails, which is treated as failure).
    """
    try:
        # Query all resources to cover both VMs and Containers
        resources = proxmox_api.cluster.resources.get()
    except Exception:
        # API failure — treat every guest as unverified (None → counted as failed).
        return {vm: None for vm in step_migrations}

    # Map name to node for all guests.
    # If 'type' is present, we filter for vm/ct.
    # If 'type' is absent, we include it anyway (compatibility with mocks/older API).
    actual_nodes = {}
    for r in resources:
        name = r.get("name")
        node = r.get("node")
        g_type = r.get("type")
        if name and node:
            if g_type is None or g_type in ("vm", "ct", "qemu", "lxc"):
                actual_nodes[name] = node

    return {vm: actual_nodes.get(vm) for vm in step_migrations}


def _execute_single_step(
    proxmox_api: Any,
    proxlb_data: Any,
    step: Any,
    guests: Any,
    run_file: str | None,
    step_retry: int = 0,
) -> set[str]:
    """Execute one MigrationStep via ProxLB Balancing; return failed VM names.

    Steps:
      1. Freezes all guests (node_target = node_current), then sets only the
         targets for VMs in this step.
      2. Calls ``Balancing()`` to trigger the actual migrations.  If Balancing
         raises, every VM in the step is recorded as failed immediately
         (no verification attempted — cluster state after a partial failure
         cannot be trusted).
      3. Verifies actual VM placement via the PVE cluster API.
      4. Updates ``node_current`` for VMs that reached their expected node.
      5. Appends an ``active_step_result`` event for every VM with full detail.

    Returns the set of VM names that did *not* reach their expected node.
    """
    from proxlb.models.balancing import Balancing as _Balancing  # late import

    # Freeze every guest in place: set node_target = node_current so that
    # Balancing() skips them (it only migrates when node_current != node_target).
    # We then override only the VMs belonging to this step.
    for gd in guests.values():
        _guest_set(gd, "node_target", _guest_get(gd, "node_current"))

    step_migrations: dict[str, str] = {}
    for m in step.migrations:
        if m.vm in guests:
            _guest_set(guests[m.vm], "node_target", m.target)
            step_migrations[m.vm] = m.target

    if not step_migrations:
        return set()

    try:
        _Balancing.balance(proxmox_api, proxlb_data)
    except Exception as exc:
        # Balancing raised — record every VM in this step as failed.
        # Skip verification: the cluster state after a partial/failed
        # Balancing call is unknown and should not be trusted.
        for vm_name, expected in step_migrations.items():
            _append_run_event(run_file, {
                "event": "active_step_result",
                "step_retry": step_retry,
                "step": step.step,
                "vm": vm_name,
                "expected": expected,
                "actual": None,
                "success": False,
                "error": str(exc),
            })
        return set(step_migrations)

    actual_map = _verify_step(proxmox_api, step_migrations)
    newly_failed: set[str] = set()

    for vm_name, expected in step_migrations.items():
        actual = actual_map.get(vm_name)
        success = actual == expected

        if success and vm_name in guests:
            _guest_set(guests[vm_name], "node_current", expected)

        _append_run_event(run_file, {
            "event": "active_step_result",
            "step_retry": step_retry,
            "step": step.step,
            "vm": vm_name,
            "expected": expected,
            "actual": actual,
            "success": success,
        })

        if not success:
            newly_failed.add(vm_name)

    return newly_failed


def execute_solver_plan(
    proxmox_api: Any,
    proxlb_data: Any,
    initial_plan: "MigrationPlan",
    solver_cfg: Any,
    run_file: str | None = None,
) -> None:
    """Execute the solver's plan via ProxLB Balancing, with per-step re-solve.

    Feedback loop — for each step in the plan:
      1. Execute the step via ``_execute_single_step()`` (Balancing + verify).
      2. If the step succeeds, advance to the next step.
      3. If the step fails (any VM missed its target or Balancing raised):
         a. Pin all failed VMs to their current node.
         b. Re-solve from the current cluster state (``node_current`` already
            updated for previously succeeded steps).
         c. Replace *plan* with the new plan and restart from step 0.
         d. Repeat until all steps succeed, the re-solve is infeasible, or
            ``active_step_retries`` re-solves have been exhausted.

    At the end an ``active_complete`` event is appended summarising the total
    number of re-solves and which VMs were permanently pinned.

    After the loop, PVE-deferred / unbreakable-cycle / pinned VMs have their
    original ProxLB ``node_target`` restored so PVE HA can handle them.
    """
    import logging
    from proxlb.models.balancing import Balancing as _Balancing  # late import

    log = logging.getLogger("ProxLB")

    cfg = _normalize_solver_cfg(solver_cfg)

    max_step_retries = cfg.active_step_retries
    use_res          = cfg.use_reservations
    time_limit       = cfg.timeout_seconds

    plan   = initial_plan
    pinned: set[str] = set()
    guests = _guests_of(proxlb_data)
    orig_targets = {n: _guest_get(gd, "node_target") for n, gd in guests.items()}

    n_steps = len(plan.steps)
    n_vms   = sum(len(s.migrations) for s in plan.steps)
    log.info(
        f"[solver] active: executing plan — {n_steps} step(s), {n_vms} VM(s), "
        f"max_step_retries={max_step_retries}"
    )

    step_retry = 0   # counts re-solves, not plan iterations
    step_idx   = 0   # position in current plan.steps list

    while step_idx < len(plan.steps):
        step = plan.steps[step_idx]
        vms_in_step = [m.vm for m in step.migrations]
        log.info(
            f"[solver] active: step {step.step} "
            f"(retry={step_retry}) — "
            f"{', '.join(f'{m.vm} {m.source}→{m.target}' for m in step.migrations)}"
        )

        failed = _execute_single_step(
            proxmox_api, proxlb_data, step, guests, run_file, step_retry
        )

        if not failed:
            succeeded = set(vms_in_step) - failed
            log.info(
                f"[solver] active: step {step.step} succeeded "
                f"({len(vms_in_step)} VM(s) migrated)"
            )
            step_idx += 1
            continue

        # ── Step had failures ────────────────────────────────────────────────
        log.warning(
            f"[solver] active: step {step.step} failed — "
            f"{sorted(failed)} did not reach expected node"
        )
        pinned |= failed
        step_retry += 1

        if step_retry > max_step_retries:
            log.warning(
                f"[solver] active: max_step_retries={max_step_retries} exhausted "
                f"after step {step.step}; {len(pinned)} VM(s) remain unplaced: "
                f"{sorted(pinned)}"
            )
            break

        # Re-solve from the current cluster state.
        # node_current has already been updated for all steps that succeeded,
        # so the new plan starts from the real post-migration placement.
        log.info(
            f"[solver] active: re-solving (step_retry={step_retry}) with "
            f"{len(pinned)} VM(s) pinned: {sorted(pinned)}"
        )
        _log_step_retry(run_file, step.step, step_retry, pinned)

        from .adapter import from_proxlb_data
        from .solver import solve
        from .planner import plan_migrations as _plan_migs

        cluster  = from_proxlb_data(proxlb_data, use_reservations=use_res,
                                    pin_vms=pinned)
        solution = solve(cluster, time_limit_s=time_limit)
        _log_resolve(run_file, solution, step_retry)

        if not solution.feasible:
            log.warning(
                f"[solver] active: re-solve step_retry={step_retry} infeasible; "
                "giving up"
            )
            break

        plan     = _plan_migs(cluster, solution)
        step_idx = 0  # restart from the beginning of the new plan
        log.info(
            f"[solver] active: new plan after re-solve — "
            f"{len(plan.steps)} step(s), "
            f"{sum(len(s.migrations) for s in plan.steps)} VM(s)"
        )

    # ── Summary event ────────────────────────────────────────────────────────
    _append_run_event(run_file, {
        "event": "active_complete",
        "step_retries": step_retry,
        "pinned_vms": sorted(pinned),
    })
    if pinned:
        log.warning(
            f"[solver] active: complete — {step_retry} re-solve(s), "
            f"{len(pinned)} VM(s) permanently skipped: {sorted(pinned)}"
        )
    else:
        log.info(
            f"[solver] active: complete — {step_retry} re-solve(s), "
            f"all VMs placed successfully"
        )

    # ── Final pass ───────────────────────────────────────────────────────────
    # Restore original ProxLB node_target for VMs the solver could not place
    # (PVE-deferred, unbreakable cycles, persistently failed migrations).
    # PVE HA will handle them via one final Balancing() call.
    remainder = set(plan.pve_deferred) | set(plan.unbreakable_cycle) | pinned
    for vm_name in remainder:
        if vm_name in guests:
            orig = orig_targets.get(vm_name, _guest_get(guests[vm_name], "node_current"))
            _guest_set(guests[vm_name], "node_target", orig)
    if any(
        _guest_get(guests[n], "node_target") != _guest_get(guests[n], "node_current")
        for n in remainder if n in guests
    ):
        log.info(f"[solver] active: handing {len(remainder)} remainder VM(s) to ProxLB Balancing")
        try:
            _Balancing.balance(proxmox_api, proxlb_data)
        except Exception as exc:
            log.warning(f"[solver] active: remainder Balancing() failed: {exc}")
