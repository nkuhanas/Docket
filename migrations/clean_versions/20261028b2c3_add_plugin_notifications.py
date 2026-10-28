"""retain inbox, subscriptions and independently acknowledged webhook attempts

Revision ID: 20261028b2c3
Revises: 20261028a1b2
"""

import sqlalchemy as sa
from alembic import op

revision = "20261028b2c3"
down_revision = "20261028a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("subject_ref", sa.String(40), nullable=False),
        sa.Column("summary", sa.String(512), nullable=False),
        sa.Column("deduplication_key", sa.String(255), nullable=False, unique=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_plugin_notifications_owner", "plugin_notifications", ["operator_ref", "created_at"]
    )
    op.create_table(
        "mcp_event_subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column(
            "principal_ref",
            sa.String(40),
            sa.ForeignKey("oauth_principals.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("grant_epoch", sa.Integer(), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("configuration", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'stopped', 'expired')", name="ck_mcp_sub_status"),
    )
    op.create_table(
        "mcp_event_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column(
            "notification_ref",
            sa.String(40),
            sa.ForeignKey("plugin_notifications.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subscription_ref",
            sa.String(40),
            sa.ForeignKey("mcp_event_subscriptions.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("notification_ref", "subscription_ref", name="uq_mcp_delivery_target"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'accepted', 'failed', 'stopped')",
            name="ck_mcp_delivery_status",
        ),
    )
    op.create_index("ix_mcp_delivery_due", "mcp_event_deliveries", ["status", "next_attempt_at"])
    op.create_table(
        "mcp_event_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "delivery_ref",
            sa.String(40),
            sa.ForeignKey("mcp_event_deliveries.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("response_status", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("delivery_ref", "attempt_number", name="uq_mcp_attempt_number"),
        sa.CheckConstraint(
            "status IN ('running', 'accepted', 'transient', 'failed', 'unknown')",
            name="ck_mcp_attempt_status",
        ),
    )
    with op.batch_alter_table("scheduled_notifications") as batch:
        batch.drop_constraint("ck_scheduled_notifications_status", type_="check")
        batch.create_check_constraint(
            "ck_scheduled_notifications_status",
            "status IN ('pending', 'delivering', 'delivered', 'cancelled', 'failed', 'available')",
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_plugin_notice_immutable BEFORE UPDATE OR DELETE ON plugin_notifications FOR EACH ROW EXECUTE FUNCTION docket_reject_immutable_row()"
        )
        op.execute("""CREATE FUNCTION docket_guard_event_subscription() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'MCPEventSubscription is retained'; END IF;
          IF (to_jsonb(NEW) - 'configuration' - 'nonce' - 'version' - 'status' - 'grant_epoch' - 'expires_at' - 'verified_at')
          IS DISTINCT FROM
             (to_jsonb(OLD) - 'configuration' - 'nonce' - 'version' - 'status' - 'grant_epoch' - 'expires_at' - 'verified_at')
          THEN RAISE EXCEPTION 'Subscription identity is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER trg_event_subscription_identity BEFORE UPDATE OR DELETE ON mcp_event_subscriptions FOR EACH ROW EXECUTE FUNCTION docket_guard_event_subscription()"
        )
        op.execute("""CREATE FUNCTION docket_guard_event_delivery() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'MCPEventDelivery is retained'; END IF;
          IF NEW.id IS DISTINCT FROM OLD.id OR NEW.ref_id IS DISTINCT FROM OLD.ref_id
             OR NEW.notification_ref IS DISTINCT FROM OLD.notification_ref
             OR NEW.subscription_ref IS DISTINCT FROM OLD.subscription_ref
          THEN RAISE EXCEPTION 'Delivery identity is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER trg_event_delivery_identity BEFORE UPDATE OR DELETE ON mcp_event_deliveries FOR EACH ROW EXECUTE FUNCTION docket_guard_event_delivery()"
        )
        op.execute("""CREATE FUNCTION docket_guard_event_attempt() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'MCPEventAttempt is retained'; END IF;
          IF OLD.status != 'running' OR
             (to_jsonb(NEW) - 'status' - 'finished_at' - 'response_status' - 'error_code')
             IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'finished_at' - 'response_status' - 'error_code')
          THEN RAISE EXCEPTION 'Delivery attempt history is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute(
            "CREATE TRIGGER trg_event_attempt_immutable BEFORE UPDATE OR DELETE ON mcp_event_attempts FOR EACH ROW EXECUTE FUNCTION docket_guard_event_attempt()"
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM scheduled_notifications WHERE status = 'available')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Native reminder history is retained; use forward repair or backup")
    for name in ("plugin_notifications", "mcp_event_subscriptions"):
        if op.get_bind().execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")).scalar():
            raise RuntimeError(
                "Plugin notification history is retained; use forward repair or backup"
            )
    for name in (
        "mcp_event_attempts",
        "mcp_event_deliveries",
        "mcp_event_subscriptions",
        "plugin_notifications",
    ):
        op.drop_table(name)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_guard_event_attempt()")
        op.execute("DROP FUNCTION docket_guard_event_delivery()")
        op.execute("DROP FUNCTION docket_guard_event_subscription()")
    with op.batch_alter_table("scheduled_notifications") as batch:
        batch.drop_constraint("ck_scheduled_notifications_status", type_="check")
        batch.create_check_constraint(
            "ck_scheduled_notifications_status",
            "status IN ('pending', 'delivering', 'delivered', 'cancelled', 'failed')",
        )
