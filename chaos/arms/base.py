"""What every arm (SafeAct, B0-B3) provides to the harness.

An arm admits and runs operations in its own way, against the same mock
providers through the same proxies, and reports its *own view* of each
operation. The oracle compares that view with the independent ledger;
an arm's report is never taken as evidence of an effect.

Outcome classes (blueprint §09, reported separately):
  confirmed_success     the arm says it is done, with evidence
  confirmed_failure     the arm says it was not applied, for good
  verified_compensation the arm undid an applied effect and verified it
  escalated_unresolved  the arm handed it to a person, outcome unknown
  pending               still in progress when the scenario ended
  cancelled             withdrawn before any attempt
"""

from dataclasses import dataclass, field


OUTCOME_CLASSES = (
    "confirmed_success", "confirmed_failure", "verified_compensation",
    "escalated_unresolved", "pending", "cancelled",
)
FINAL = frozenset({"confirmed_success", "confirmed_failure",
                   "verified_compensation", "cancelled"})


@dataclass(frozen=True)
class Admission:
    """One submission's answer: an accepted local id, or a refusal."""

    local_id: str | None
    refused: str | None = None


@dataclass
class OperationView:
    """The arm's own account of one accepted operation."""

    local_id: str
    outcome_class: str
    state: str
    events: list = field(default_factory=list)   # (wall time, label)
    resolved_at: float | None = None
    escalated_at: float | None = None
    evidence_rows: int = 0


class Arm:
    name = "arm"
    # Families an arm cannot be asked about (no such feature): reported N/A.
    not_applicable = frozenset()

    def setup(self, slot, workdir):
        raise NotImplementedError

    def teardown(self):
        raise NotImplementedError

    def prepare(self, scenario):
        """Before a scenario: make sure every process is alive."""

    def quiesce(self):
        """Stop all execution; keep the database available for final observation."""
        raise NotImplementedError

    def submit(self, scenario, index, operation, *, changed=False):
        raise NotImplementedError

    def observe(self, local_ids):
        """{local_id: OperationView} for the given accepted operations."""
        raise NotImplementedError

    def check_integrity(self):
        """Arm-internal invariants (for SafeAct: projection drift)."""
        return []
