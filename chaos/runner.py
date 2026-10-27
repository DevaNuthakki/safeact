"""Run one scenario on one arm in one slot, and record everything.

The raw result holds, for the oracle and the report:
- the scenario (data), the arm, the slot;
- each submission's admission (accepted local id or refusal);
- the arm's final view of each accepted operation;
- the independent ledger's calls and effects for the scenario's
  resources, with wall times;
- every failpoint that fired, every fault the harness applied and
  removed, with wall times;
- the arm's own integrity check (SafeAct: projection drift);
- the machine load at the start and end (chaos.environment).
Together these are the actual interleaving of the run (§09).
"""

import threading
import time
import traceback

from chaos import environment
from chaos.arms.base import FINAL
from connectors.http_trace import correlation_hash
from deploy.stack import failpoints


POLL_SECONDS = 0.25
STABLE_SECONDS = 2.0


class SubmissionError(RuntimeError):
    def __init__(self, errors, admissions):
        super().__init__("Submission failed:\n" + "\n".join(errors))
        self.admissions = admissions


class Timeline:

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def add(self, label, **details):
        with self._lock:
            self.events.append({"at": time.time(), "event": label,
                                "monotonic_ns": time.monotonic_ns(),
                                "sequence": len(self.events), **details})


class FaultDriver:
    """Applies a scenario's fault schedule to a slot.

    A timed fault starts at_ms after the scenario starts or, with
    ``after_call: n``, at_ms after the scenario's n-th provider call (so a
    fault can follow the first request, whatever the queue wait was).
    """

    def __init__(self, slot, timeline, resources=(), arm=None):
        self.slot = slot
        self.arm = arm
        self.timeline = timeline
        self.timers = []
        self.holds = []
        self.resources = set(resources)
        self.cancelled = threading.Event()
        self.errors = []
        self.required = set()
        self.completed = set()
        self.since_ns = time.monotonic_ns()

    def perform(self, action, label, details):
        if self.cancelled.is_set():
            return
        try:
            action()
            self.timeline.add(label, **details)
        except Exception:
            error = traceback.format_exc()
            self.errors.append(error)
            self.timeline.add("fault_error", fault=label, trace=error)

    def check(self):
        if self.errors:
            raise RuntimeError("Fault driver failed:\n" + "\n".join(self.errors))

    def calls(self):
        calls, _ = self.slot.ledger.snapshot()
        return sum(1 for _, params in calls
                   if set(map(str, params.values())) & self.resources)

    def when(self, fault, offset_ms, action, label, **details):
        """Run action offset_ms after the fault's trigger."""
        after = fault.get("after_call")
        if not after:
            self.later(offset_ms / 1000, action, label, **details)
            return

        def watch():
            try:
                while not self.cancelled.is_set():
                    if self.calls() >= after:
                        if not self.cancelled.wait(offset_ms / 1000):
                            self.perform(action, label, details)
                        return
                    self.cancelled.wait(0.01)
            except Exception:
                error = traceback.format_exc()
                self.errors.append(error)
                self.timeline.add("fault_error", fault=label, trace=error)
        thread = threading.Thread(target=watch, daemon=True)
        self.timers.append(thread)
        thread.start()

    def later(self, seconds, action, label, *, after_event=None, **details):
        def run():
            if after_event is not None:
                while not self.cancelled.is_set():
                    if after_event.wait(0.01):
                        break
            if not self.cancelled.wait(max(0.0, seconds)):
                self.perform(action, label, details)
        timer = threading.Thread(target=run)
        timer.daemon = True
        self.timers.append(timer)
        timer.start()

    def pending(self):
        return any(timer.is_alive() for timer in self.timers)

    def cancel(self):
        self.cancelled.set()
        # The isolated Docker fault has a 25 s command budget plus bounded
        # Redis/DB calls. Join it before stopping its stack or removing its broker.
        join_seconds = (45.0 if "broker_kill" in self.required else
                        10.0 if self.required & {"hold_after_escalation", "hold_after_empty_lookup"} else 6.0)
        deadline = time.monotonic() + join_seconds
        for timer in self.timers:
            timer.join(max(0.0, deadline - time.monotonic()))
        if self.pending():
            self.errors.append("A fault action did not stop before cleanup")
        if self.required - self.completed:
            self.errors.append("Required fault actions did not complete: " + ", ".join(sorted(self.required - self.completed)))

    def release_holds(self):
        for held in self.holds:
            held.release()

    def wait_holds(self, timeout):
        # Called only after provider admission closed and active calls drained.
        # An unused hold cannot be consumed later; a consumed hold may still
        # have an asynchronous provider commit after its caller returned.
        deadline = time.monotonic() + timeout
        consumed = 0
        for held in self.holds:
            if held.started.is_set():
                consumed += 1
                if not held.wait_committed(max(0.0, deadline - time.monotonic())):
                    raise RuntimeError("A consumed held request did not commit before the drain deadline")
        return {"consumed_holds": consumed, "unused_holds": len(self.holds) - consumed}

    def perform_cleanup(self, action):
        try:
            action()
        except Exception:
            self.errors.append(traceback.format_exc())

    def apply(self, fault):
        from chaos.fault_contracts import validate_fault
        validate_fault(fault)
        slot, kind = self.slot, fault["kind"]
        mock = slot.mocks.get(fault.get("provider"))
        proxy = slot.proxies.get(fault.get("provider"))
        if kind == "workflow":
            from chaos.workflow_faults import configure
            configure(slot, self.arm, fault["mode"])
            self.timeline.add("workflow_fault_configured", mode=fault["mode"])
        elif kind == 'workflow_lookup':
            mock.configure_inverse_lookup_outage()
            self.timeline.add('inverse_lookup_outage_armed', provider='payments', step='compensation')
        elif kind == "provider":
            setattr(mock, fault["switch"], fault["value"])
            self.timeline.add("fault_set", switch=fault["switch"],
                              provider=fault["provider"])
        elif kind == "later":
            self.when(fault, fault["at_ms"],
                      lambda: setattr(mock, fault["switch"], fault["value"]),
                      "fault_set_later", switch=fault["switch"],
                      provider=fault["provider"])
        elif kind == "hold":
            held = mock.hold_next(respond_now=fault.get("respond_now", False))
            self.holds.append(held)
            self.timeline.add("hold_armed", provider=fault["provider"])
            self.later(fault["release_ms"] / 1000, held.release,
                       "hold_released", after_event=held.started,
                       provider=fault["provider"])
        elif kind == "hold_after_empty_lookup":
            from chaos.recovery_faults import release_after_empty_lookup
            self.required.add(kind)
            held = mock.hold_next(respond_now=fault.get('respond_now', True))
            self.holds.append(held)
            self.timeline.add("hold_armed", provider=fault["provider"])
            def observe_lookup():
                return dict(ledger=_ledger_for(slot, self.resources, 0),
                            http=self.arm.trace_observations(self.since_ns))
            def release_empty():
                if release_after_empty_lookup(held, self.cancelled, observe_lookup,
                        self.timeline.add, provider=fault["provider"], resources=self.resources,
                        minimum_seconds=fault["release_ms"] / 1000):
                    self.completed.add(kind)
            thread = threading.Thread(target=lambda: self.perform(release_empty,
                "empty_lookup_action_finished", {}), daemon=True)
            self.timers.append(thread)
            thread.start()
        elif kind == "toxic":
            def on():
                slot.toxiproxy.toxic(proxy.name, fault["toxic"],
                                     stream=fault.get("stream", "downstream"),
                                     **fault.get("attributes", {}))

            def off():
                slot.toxiproxy.clear(proxy.name)
            if fault.get("at_ms", 0) == 0:
                on()
                self.timeline.add("toxic_on", toxic=fault["toxic"],
                                  provider=fault["provider"])
            else:
                self.later(fault["at_ms"] / 1000, on, "toxic_on",
                           toxic=fault["toxic"], provider=fault["provider"])
            self.later((fault.get("at_ms", 0) + fault["duration_ms"]) / 1000,
                       off, "toxic_off", provider=fault["provider"])
        elif kind == "down":
            self.later(fault.get("at_ms", 0) / 1000,
                       lambda: slot.toxiproxy.enable(proxy.name, False),
                       "proxy_down", provider=fault["provider"])
            self.later((fault.get("at_ms", 0) + fault["duration_ms"]) / 1000,
                       lambda: slot.toxiproxy.enable(proxy.name, True),
                       "proxy_up", provider=fault["provider"])
        elif kind == "failpoint":
            armer=getattr(self.arm,'arm_failpoint',None)
            if armer is None:failpoints.arm(slot.fault_dir, fault["point"], fault["action"])
            else:armer(fault['point'],fault['action'])
            self.timeline.add("failpoint_armed", point=fault["point"],
                              action=fault["action"])
        elif kind == "worker_kill":
            armer=getattr(self.arm,'arm_failpoint',None)
            if armer is None:failpoints.arm(slot.fault_dir, fault["point"], "park")
            else:armer(fault['point'],'park')
            self.timeline.add("failpoint_armed", point=fault["point"], action="park")
            def watch():
                while not self.cancelled.is_set():
                    marker=getattr(self.arm,'parked_marker',slot.fault_dir/'parked')
                    if marker.exists():
                        self.perform(lambda: self.arm.kill_parked_worker(
                            fault["point"], self.timeline.add), "worker_fault_finished", {})
                        return
                    self.cancelled.wait(0.01)
            thread = threading.Thread(target=watch, daemon=True)
            self.timers.append(thread)
            thread.start()
        elif kind in ("broker_loss", "broker_kill"):
            self.required.add(kind)
            if kind == "broker_kill":
                self.arm.begin_broker_loss(self.timeline.add, kill=True)
            else:
                self.arm.begin_broker_loss(self.timeline.add)
            def lose():
                self.arm.finish_broker_loss(self.cancelled)
                self.completed.add(kind)
            thread = threading.Thread(target=lambda: self.perform(lose, "broker_loss_finished", {}), daemon=True)
            self.timers.append(thread)
            thread.start()
        elif kind == "broker_redelivery":
            self.required.add("broker_redelivery")
            held = mock.hold_next(respond_now=False)
            self.holds.append(held)
            self.timeline.add("hold_armed", provider=fault["provider"])
            def redeliver():
                self.arm.redeliver_during_dispatch(held, self.cancelled, self.timeline.add)
                self.completed.add("broker_redelivery")
            thread = threading.Thread(target=lambda: self.perform(redeliver,
                "broker_redelivery_action_finished", {}), daemon=True)
            self.timers.append(thread)
            thread.start()
        elif kind == "hold_after_escalation":
            self.required.add(kind)
            held = mock.hold_next(respond_now=True)
            self.holds.append(held)
            self.timeline.add("hold_armed", provider=fault["provider"])
            def release_late():
                if self.arm.release_late_commit(held, self.cancelled, self.timeline.add):
                    self.completed.add(kind)
            thread = threading.Thread(target=lambda: self.perform(release_late,
                "late_commit_action_finished", {}), daemon=True)
            self.timers.append(thread)
            thread.start()
        elif kind == "outage":
            on = lambda: slot.set_outage(fault["provider"], True)
            if not fault.get("after_call") and fault.get("at_ms", 0) == 0:
                on()  # The initial outage is active before admission can dispatch.
                self.timeline.add("outage_on", provider=fault["provider"])
            else:
                self.when(fault, fault.get("at_ms", 0), on, "outage_on",
                          provider=fault["provider"])
            self.when(fault, fault.get("at_ms", 0) + fault["duration_ms"],
                      lambda: slot.set_outage(fault["provider"], False), "outage_off",
                      provider=fault["provider"])
        elif kind == "rate_limit":
            mock.rate_limit_next = fault["count"]
            mock.retry_after = fault.get("retry_after")
            self.timeline.add("rate_limit_set", count=fault["count"],
                              provider=fault["provider"])
        elif kind == "reveal":
            def reveal():
                if fault["provider"] == "payments":
                    mock.hide_new_effects = False
                    mock.reveal_hidden_effects()
                else:
                    mock.index_lag = False
                    mock.reindex()
            self.later(fault["at_ms"] / 1000, reveal, "revealed",
                       provider=fault["provider"])
        else:
            raise ValueError(f"unknown fault kind {kind!r}")


def _submit_all(arm, scenario, timeline):
    admissions = []
    errors = []
    lock = threading.Lock()

    def one(index, operation, copy, changed=False):
        timeline.add("submission_started", index=index, copy=copy,
                     changed=changed)
        try:
            answer = arm.submit(scenario, index, operation, changed=changed)
            if bool(answer.local_id) == bool(answer.refused):
                raise ValueError("Admission must contain exactly one id or refusal")
        except Exception:
            error = traceback.format_exc()
            with lock:
                errors.append(error)
            timeline.add("submission_error", index=index, copy=copy,
                         changed=changed, trace=error)
            return
        with lock:
            admissions.append({"index": index, "copy": copy,
                               "changed": changed,
                               "local_id": answer.local_id,
                               "refused": answer.refused})
            timeline.add("submitted", index=index, copy=copy,
                         changed=changed, refused=answer.refused)

    for index, operation in enumerate(scenario.operations):
        copies = [threading.Thread(target=one, args=(index, operation, c))
                  for c in range(operation.submits)]
        if operation.concurrent:
            for thread in copies:
                thread.start()
            for thread in copies:
                thread.join()
        else:
            for thread in copies:
                thread.start()
                thread.join()
        if operation.changed_resubmit:
            one(index, operation, operation.submits, changed=True)
    if errors:
        raise SubmissionError(errors, admissions)
    return admissions


def _unstable(scored, close, final):
    """Operations whose state changed after the scored snapshot (never hidden)."""

    def state(view):
        return None if view is None else dict(state=view.state, outcome_class=view.outcome_class)

    changed = []
    for local_id in sorted(set(scored) | set(close or {}) | set(final or {})):
        mine = state(scored.get(local_id))
        later = dict(window_close=state((close or {}).get(local_id)),
                     final=state((final or {}).get(local_id)))
        # A snapshot that was not taken (an error path) is not a difference.
        if any(view != mine for key, view in later.items()
               if (close if key == "window_close" else final)):
            changed.append(dict(local_id=local_id, scored=mine, **later))
    return changed


def run(slot, arm, scenario):
    from chaos.fault_contracts import validate_scenario
    validate_scenario(scenario)
    timeline = Timeline()
    slot.reset()
    arm.prepare(scenario)
    fired_reader=getattr(arm,'fired_failpoints',lambda:_fired(slot))
    fired_before = fired_reader()
    load_start = environment.sample()

    driver = FaultDriver(slot, timeline,
                         [op.resource_id for op in scenario.operations], arm=arm)
    started = time.time()
    started_mono = time.monotonic()
    started_ns = time.monotonic_ns()
    ledger_offset = len(slot.ledger.events())
    timeline.add("start")
    admissions, local_ids, views, errors = [], [], {}, []
    stable_views = close_views = None
    try:
        for fault in scenario.faults:
            driver.apply(fault)
        admissions = _submit_all(arm, scenario, timeline)
        local_ids = sorted({a["local_id"] for a in admissions if a["local_id"]})
        boundary_driver = getattr(arm, "drive_boundary", None)
        if boundary_driver is not None:
            boundary_driver(scenario, local_ids, timeline.add)
        deadline = started_mono + scenario.settle_seconds
        stable_since = None
        while time.monotonic() < deadline:
            driver.check()
            sampler = getattr(slot, "sample_provider_state", None)
            if sampler is not None:
                sampler()
            views = arm.observe(local_ids)
            settled = (set(views) == set(local_ids) and all(
                v.state != "MISSING" and (v.outcome_class in FINAL
                or v.outcome_class == "escalated_unresolved")
                for v in views.values()))
            if settled and not driver.pending():
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= STABLE_SECONDS:
                    # Score the snapshot that passed the stability check; an
                    # escalated operation may still reopen for a lookup.
                    stable_views = views
                    break
            else:
                stable_since = None
            time.sleep(POLL_SECONDS)
        close_views = arm.observe(local_ids)
        views = close_views if stable_views is None else stable_views
    except Exception as error:
        admissions = getattr(error, "admissions", admissions)
        errors.append(traceback.format_exc())
        timeline.add("run_error", trace=errors[-1])
        # Keep admitted work in timed accounting even when a boundary/fault
        # driver fails. A missing observation remains an explicit second error.
        local_ids = sorted({a["local_id"] for a in admissions if a["local_id"]})
        try:
            views = arm.observe(local_ids)
        except Exception:
            errors.append(traceback.format_exc())
    finally:
        observation_finished = time.time()
        observation_finished_ns = time.monotonic_ns()
        timeline.add("observation_window_closed")
        driver.cancel()
        quiescence = _quiesce(slot, arm, driver, timeline, errors)
    errors.extend(driver.errors)
    local_ids = sorted({a["local_id"] for a in admissions if a["local_id"]})
    final_views = {}
    try:
        final_views = arm.observe(local_ids)
    except Exception:
        errors.append(traceback.format_exc())
    resources = {op.resource_id for op in scenario.operations}
    ledger = _ledger_for(slot, resources, started, offset=ledger_offset)
    http = []
    if getattr(arm, "http_trace_enabled", False):
        try:
            http = arm.trace_observations(started_ns)
            for event in ledger:
                if event["event"] == "provider_start" and not any(
                    row["event"] == "http_start"
                    and row["provider"] == event["provider"]
                    and row["correlation_sha256"] == event["correlation_sha256"]
                    and row["monotonic_ns"] <= event["monotonic_ns"] for row in http):
                    errors.append("Missing HTTP observation for a provider request")
        except Exception:
            errors.append(traceback.format_exc())
    execution = []
    if getattr(arm, "execution_trace_enabled", False):
        try:
            execution = arm.execution_observations(started_ns)
        except Exception:
            errors.append(traceback.format_exc())
    broker = []
    if getattr(arm, "broker_trace_enabled", False):
        try:
            broker = arm.broker_observations(started_ns)
            observed = {e["operation_id"] for e in broker if e["event"] == "task_started"}
            execution_ids = getattr(arm, "execution_ids", lambda ids: ids)(local_ids)
            if set(execution_ids) - observed:
                errors.append("Missing broker task-start observations for admitted operations")
        except Exception:
            errors.append(traceback.format_exc())
    database = []
    if getattr(arm, "database_trace_enabled", False):
        try:
            database = arm.database_observations(local_ids)
        except Exception:
            errors.append(traceback.format_exc())
    infrastructure={}
    observer=getattr(arm,'infrastructure_observations',None)
    if observer is not None:
        try:infrastructure=observer(started_ns)
        except Exception:errors.append(traceback.format_exc())
    try:
        integrity = arm.check_integrity()
    except Exception:
        errors.append(traceback.format_exc())
        integrity = []
    finished = time.time()
    timeline.add("end")
    return {
        "scenario_id": scenario.scenario_id,
        "family": scenario.family,
        "connector": scenario.connector,
        "arm": arm.name,
        "slot": slot.index,
        "started": started,
        "finished": finished,
        "elapsed_seconds": time.monotonic() - started_mono,
        "observation_finished": observation_finished,
        "observation_finished_monotonic_ns": observation_finished_ns,
        "quiescence": quiescence,
        "harness_errors": errors,
        "cleanup_incomplete": not quiescence["complete"],
        "admissions": admissions,
        "scored_from": "stability_check" if stable_views is not None else "window_close",
        "unstable_at_scoring": _unstable(views, close_views, final_views),
        "operations": {
            local_id: {
                "outcome_class": view.outcome_class, "state": view.state,
                "events": view.events, "resolved_at": view.resolved_at,
                "escalated_at": view.escalated_at,
                "evidence_rows": view.evidence_rows,
            } for local_id, view in views.items()
        },
        "final_operations": {local_id: {
            "outcome_class": view.outcome_class, "state": view.state,
            "events": view.events, "resolved_at": view.resolved_at,
            "escalated_at": view.escalated_at, "evidence_rows": view.evidence_rows,
        } for local_id, view in final_views.items()},
        "ledger": ledger,
        "http": http,
        "database": database,
        "execution": execution,
        "broker": broker,
        "infrastructure": infrastructure,
        "boundary": list(getattr(arm, "boundary_events", [])),
        "failpoints": fired_reader()[len(fired_before):],
        "timeline": timeline.events,
        "integrity": integrity,
        "load": {"start": load_start, "end": environment.sample()},
    }


def _quiesce(slot, arm, driver, timeline, errors):
    result = {"complete": not driver.pending()}
    deadline = None
    for label, action in (
        ("execution_stopped", lambda: arm.quiesce()),
        ("provider_admission_closed", lambda: slot.close_provider_admission()),
        ("cleanup_holds_released", driver.release_holds),
        ("provider_calls_drained", lambda: slot.wait_provider_idle(
            max(0.0, deadline - time.monotonic()))),
        ("held_commits_drained", lambda: driver.wait_holds(
            max(0.0, deadline - time.monotonic()))),
    ):
        try:
            details = action()
            result[label] = details if details is not None else True
            timeline.add(label, details=details)
        except Exception:
            result["complete"] = False
            errors.append(traceback.format_exc())
            timeline.add("quiescence_error", stage=label, trace=errors[-1])
        if label == "cleanup_holds_released":
            deadline = time.monotonic() + 5.0
    timeline.add("quiescence_finished", complete=result["complete"])
    return result


def _fired(slot):
    path = slot.fault_dir / "fired"
    if not path.exists():
        return []
    return [line.split() for line in path.read_text().splitlines() if line]


def _ledger_for(slot, resources, since, *, offset=0):
    calls, _ = slot.ledger.snapshot()
    candidates_by_correlation = {}
    for correlation, params in calls:
        # Named provider resource fields also retain unexpected targets.
        candidates = [params[name] for name in
                      ("charge_id", "case_id", "customer_id") if name in params]
        candidates += [value for value in params.values()
                       if isinstance(value, str) and value in resources]
        if candidates:
            candidates_by_correlation.setdefault(correlation_hash(correlation), set()).update(
                map(str, candidates))
    events = []
    for event in slot.ledger.events()[offset:]:
        candidates = candidates_by_correlation.get(event.get("correlation_sha256"), set())
        resource = next(iter(candidates)) if len(candidates) == 1 else None
        # Unknown targets are evidence for orphan detection, never discarded.
        events.append(dict(event, resource=resource))
    return events
