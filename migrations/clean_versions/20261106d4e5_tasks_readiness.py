"""retain resumable Tasks discovery, scoped validation and policy routing snapshots"""

import sqlalchemy as sa
from alembic import op

revision = "20261106d4e5"
down_revision = "20261106c3d4"
branch_labels = None
depends_on = None


def _resolution_table(per_field: bool) -> None:
    columns = [
        sa.Column(
            "conflict_ref",
            sa.String(40),
            sa.ForeignKey("task_sync_conflicts.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "changeset_ref",
            sa.String(40),
            sa.ForeignKey("change_sets.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("selection", sa.String(16), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if per_field:
        columns.append(sa.Column("field_name", sa.String(32), nullable=False))
        columns.append(sa.Column("canonical_version_after", sa.Integer()))
    key = ["conflict_ref", "field_name"] if per_field else ["conflict_ref"]
    op.create_table(
        "task_conflict_resolutions",
        *columns,
        sa.PrimaryKeyConstraint(
            *key, name="pk_task_conflict_fields" if per_field else "pk_task_conflict_single"
        ),
    )


def _retain_resolutions() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER retain_task_conflict_resolutions BEFORE UPDATE OR DELETE ON task_conflict_resolutions FOR EACH ROW EXECUTE FUNCTION retain_google_tasks_evidence()"
        )


def _binding_guard(with_snapshot: bool) -> None:
    retained = ",to_jsonb(r.provider_snapshot)" if with_snapshot else ""
    current = ",to_jsonb(NEW.provider_snapshot)" if with_snapshot else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION require_task_binding_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM task_binding_revisions r
            WHERE r.binding_ref = NEW.ref_id AND r.revision = NEW.version
              AND (r.task_list_ref,r.provider_task_id,r.generation,r.active,r.detached,
                   r.canonical_version,r.provider_etag,r.provider_updated_at,r.observed_at,r.observation_ref,
                   to_jsonb(r.baseline),to_jsonb(r.limitations){retained})
                IS NOT DISTINCT FROM
                  (NEW.task_list_ref,NEW.provider_task_id,NEW.generation,NEW.active,NEW.detached,
                   NEW.canonical_version,NEW.provider_etag,NEW.provider_updated_at,NEW.observed_at,NEW.observation_ref,
                   to_jsonb(NEW.baseline),to_jsonb(NEW.limitations){current})) THEN
            RAISE EXCEPTION 'a task binding head requires its exact retained revision'; END IF;
          RETURN NEW;
        END $$;
    """)


def upgrade() -> None:
    # A former whole-conflict decision means exactly its original fields. Expand
    # that immutable meaning once; no compatibility lookup survives the migration.
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM task_conflict_resolutions r JOIN task_sync_conflicts c ON c.ref_id = r.conflict_ref WHERE json_array_length(c.fields) = 0"
        )
    ):
        raise RuntimeError("cannot expand a resolution without its original fields")
    op.rename_table("task_conflict_resolutions", "task_conflict_resolutions_before_fields")
    _resolution_table(True)
    values = (
        "CROSS JOIN LATERAL json_array_elements_text(c.fields) AS field(value)"
        if op.get_bind().dialect.name == "postgresql"
        else "JOIN json_each(c.fields) AS field"
    )
    op.execute(
        f"INSERT INTO task_conflict_resolutions (conflict_ref,field_name,changeset_ref,selection,resolved_at) "
        "SELECT r.conflict_ref,field.value,r.changeset_ref,r.selection,r.resolved_at "
        "FROM task_conflict_resolutions_before_fields r "
        f"JOIN task_sync_conflicts c ON c.ref_id = r.conflict_ref {values}"
    )
    op.drop_table("task_conflict_resolutions_before_fields")
    _retain_resolutions()
    op.create_table(
        "tasks_account_states",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("requested", sa.Boolean(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_token", sa.String(length=4096), nullable=True),
        sa.Column("generation", sa.Uuid(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "tasks_method_validations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("adapter_version", sa.Integer(), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("google_task_lists", sa.Column("discovery_generation", sa.Uuid()))
    op.add_column("task_policy_effects", sa.Column("expected_binding_version", sa.Integer()))
    op.add_column(
        "google_task_observations", sa.Column("read_started_at", sa.DateTime(timezone=True))
    )
    op.add_column("google_task_observations", sa.Column("read_after_binding_version", sa.Integer()))
    for table in ("task_provider_bindings", "task_binding_revisions"):
        op.add_column(
            table, sa.Column("provider_snapshot", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("task_provider_bindings") as batch:
        batch.create_check_constraint("ck_task_binding_detached_inactive", "NOT (active AND detached)")
    with op.batch_alter_table("task_list_policy_revisions") as batch:
        batch.add_column(sa.Column("is_default", sa.Boolean()))
        batch.add_column(
            sa.Column(
                "reminder_calendar_ref",
                sa.String(40),
                sa.ForeignKey(
                    "calendar_lanes.ref_id",
                    name="fk_task_policy_reminder_calendar",
                    ondelete="RESTRICT",
                ),
            )
        )
    if op.get_bind().dialect.name == "postgresql":
        _binding_guard(True)
        op.execute(
            "CREATE TRIGGER retain_tasks_method_validation BEFORE UPDATE OR DELETE ON tasks_method_validations FOR EACH ROW EXECUTE FUNCTION retain_google_tasks_evidence()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM google_task_observations WHERE read_started_at IS NOT NULL OR read_after_binding_version IS NOT NULL"
    )):
        raise RuntimeError("cannot discard retained observation read fences; use forward repair")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM task_conflict_resolutions")):
        raise RuntimeError("cannot discard retained field resolutions; use forward repair")
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM task_policy_effects WHERE expected_binding_version IS NOT NULL"
        )
    ):
        raise RuntimeError("cannot discard retained task effect preconditions; use forward repair")
    if any(
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
        for table in ("task_provider_bindings", "task_binding_revisions")
    ):
        raise RuntimeError("cannot discard retained provider snapshots; use forward repair")
    if any(
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
        for table in ("tasks_account_states", "tasks_method_validations")
    ):
        raise RuntimeError("cannot discard retained Tasks readiness; use forward repair")
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM task_list_policy_revisions WHERE is_default IS NOT NULL OR reminder_calendar_ref IS NOT NULL"
        )
    ):
        raise RuntimeError("cannot discard retained task policy routing; use forward repair")
    with op.batch_alter_table("task_list_policy_revisions") as batch:
        batch.drop_constraint("fk_task_policy_reminder_calendar", type_="foreignkey")
        batch.drop_column("reminder_calendar_ref")
        batch.drop_column("is_default")
    with op.batch_alter_table("google_task_lists") as batch:
        batch.drop_column("discovery_generation")
    with op.batch_alter_table("google_task_observations") as batch:
        batch.drop_column("read_started_at")
        batch.drop_column("read_after_binding_version")
    with op.batch_alter_table("task_policy_effects") as batch:
        batch.drop_column("expected_binding_version")
    if op.get_bind().dialect.name == "postgresql":
        _binding_guard(False)
    with op.batch_alter_table("task_provider_bindings") as batch:
        batch.drop_constraint("ck_task_binding_detached_inactive", type_="check")
    for table in ("task_provider_bindings", "task_binding_revisions"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("provider_snapshot")
    op.drop_table("tasks_method_validations")
    op.drop_table("tasks_account_states")
    op.drop_table("task_conflict_resolutions")
    _resolution_table(False)
    _retain_resolutions()
