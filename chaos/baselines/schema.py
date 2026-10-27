"""B3-owned ordinary workflow and human-review records."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


def extend(t):
    def table(name,*columns):
        return sa.Table("baseline_"+name,t.metadata,*columns)
    def uid(name,**kw):return sa.Column(name,UUID(as_uuid=True),**kw)
    def stamp(name,**kw):return sa.Column(name,sa.DateTime(timezone=True),**kw)
    def job():return sa.Column("operation_id",UUID(as_uuid=True),sa.ForeignKey(t.jobs.c.operation_id),nullable=False)
    t.queue_results=table('queue_results',sa.Column('operation_id',UUID(as_uuid=True),sa.ForeignKey(t.jobs.c.operation_id),primary_key=True),
        sa.Column('attempt_id',UUID(as_uuid=True),sa.ForeignKey(t.attempts.c.attempt_id),nullable=False),
        sa.Column('reference',sa.Text,nullable=False),stamp('created_at',nullable=False,server_default=sa.func.now()))
    t.workflows = table("workflows",uid("workflow_id",primary_key=True),
        sa.Column("tenant_id",sa.String(128),nullable=False),
        sa.Column("request_key",sa.String(255),nullable=False),
        sa.Column("requester_id",sa.String(128),nullable=False),
        sa.Column("request",JSONB,nullable=False),
        stamp("created_at",nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id","request_key",name="baseline_workflow_identity"))
    t.steps = table("steps",
        sa.Column("workflow_id",UUID(as_uuid=True),sa.ForeignKey(t.workflows.c.workflow_id),primary_key=True),
        sa.Column("step",sa.String(16),primary_key=True),job(),
        sa.Column("compensates_operation_id",UUID(as_uuid=True),sa.ForeignKey(t.jobs.c.operation_id)),
        sa.UniqueConstraint("operation_id",name="baseline_step_operation"),
        sa.CheckConstraint("step IN ('charge','order','compensation')",name="baseline_step_kind"),
        sa.CheckConstraint("(step = 'compensation') = (compensates_operation_id IS NOT NULL)",name="baseline_inverse_link"))
    t.workflow_audit = table("workflow_audit",
        sa.Column("audit_id",sa.BigInteger,primary_key=True,autoincrement=True),
        sa.Column("workflow_id",UUID(as_uuid=True),sa.ForeignKey(t.workflows.c.workflow_id),nullable=False),
        sa.Column("kind",sa.String(32),nullable=False),sa.Column("details",JSONB,nullable=False),
        sa.Column("commit_xid",sa.String(32),nullable=False,server_default=sa.text("pg_current_xact_id()::text")),
        stamp("created_at",nullable=False,server_default=sa.func.now()))
    t.humans = table("humans",sa.Column("actor_id",sa.String(128),primary_key=True),
        sa.Column("tenant_id",sa.String(128),nullable=False),sa.Column("username",sa.String(128),nullable=False,unique=True),
        sa.Column("password_hash",sa.Text,nullable=False),sa.Column("roles",JSONB,nullable=False),
        sa.Column("enabled",sa.Boolean,nullable=False,server_default=sa.true()),
        sa.CheckConstraint("password_hash ~ '^[$]2a[$]12[$][./A-Za-z0-9]{53}$'",name="baseline_password_cost"))
    t.sessions = table("sessions",sa.Column("token_sha256",sa.String(64),primary_key=True),
        sa.Column("actor_id",sa.String(128),sa.ForeignKey(t.humans.c.actor_id),nullable=False),
        stamp("expires_at",nullable=False),sa.Column("revoked",sa.Boolean,nullable=False,server_default=sa.false()))
    t.policies = table("review_policies",sa.Column("connector_id",sa.String(64),primary_key=True),
        sa.Column("contract_version",sa.String(64),primary_key=True),
        sa.Column("requires_retry_review",sa.Boolean,nullable=False),sa.Column("revision",sa.Integer,nullable=False))
    t.proposals = table("proposals",uid("proposal_id",primary_key=True),job(),
        sa.Column("proposer_id",sa.String(128),sa.ForeignKey(t.humans.c.actor_id),nullable=False),
        sa.Column("action",sa.String(32),nullable=False),sa.Column("binding_sha256",sa.String(64),nullable=False),
        sa.Column("basis",JSONB,nullable=False),stamp("expires_at",nullable=False),
        stamp("created_at",nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("action IN ('PROTECTED_RETRY','REVERIFY')",name="baseline_review_action"))
    t.decisions = table("decisions",
        sa.Column("proposal_id",UUID(as_uuid=True),sa.ForeignKey(t.proposals.c.proposal_id),primary_key=True),
        sa.Column("reviewer_id",sa.String(128),sa.ForeignKey(t.humans.c.actor_id),nullable=False),
        sa.Column("decision_key",sa.String(128),nullable=False),sa.Column("approved",sa.Boolean,nullable=False),
        stamp("created_at",nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint("reviewer_id","decision_key",name="baseline_decision_identity"))
    t.authorizations = table("authorizations",
        sa.Column("proposal_id",UUID(as_uuid=True),sa.ForeignKey(t.decisions.c.proposal_id),primary_key=True),job(),
        sa.Column("action",sa.String(32),nullable=False),sa.Column("basis_sha256",sa.String(64),nullable=False),
        sa.Column("next_ordinal",sa.Integer,nullable=False),stamp("expires_at",nullable=False))
    t.authorization_uses = table("authorization_uses",
        sa.Column("proposal_id",UUID(as_uuid=True),sa.ForeignKey(t.authorizations.c.proposal_id),primary_key=True),
        sa.Column("attempt_id",UUID(as_uuid=True),sa.ForeignKey(t.attempts.c.attempt_id)),uid("lookup_token"),
        stamp("created_at",nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("(attempt_id IS NOT NULL) <> (lookup_token IS NOT NULL)",name="baseline_authorization_use"))
