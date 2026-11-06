"""retain Google Tasks policies, observations and reminder generations"""

import sqlalchemy as sa
from alembic import op

revision = "20261106c3d4"
down_revision = "20261106b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_task_lists",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider_list_id", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("policy_mode", sa.String(length=16), nullable=False),
        sa.Column("policy_ref", sa.String(length=40), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("reminder_calendar_ref", sa.String(length=40), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "policy_mode IN ('disabled','observe','two_way')", name="ck_google_task_list_policy"
        ),
        sa.CheckConstraint("version >= 1", name="ck_google_task_list_version"),
        sa.CheckConstraint(
            "NOT is_default OR policy_mode = 'two_way'", name="ck_google_task_list_default_policy"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reminder_calendar_ref"], ["calendar_lanes.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("account_id", "provider_list_id", name="uq_google_task_list_identity"),
    )
    op.create_index(
        "uq_google_task_list_default",
        "google_task_lists",
        ["operator_ref"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )
    op.create_table(
        "task_list_policy_revisions",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("task_list_ref", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operator_ref", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("propagate_google_deletion", sa.Boolean(), nullable=False),
        sa.Column("authorized_request_ref", sa.String(length=40), nullable=False),
        sa.Column("principal_ref", sa.String(length=255), nullable=False),
        sa.Column("grant_epoch", sa.Integer(), nullable=True),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('disabled','observe','two_way')", name="ck_task_list_policy_mode"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_task_list_policy_revision"),
        sa.CheckConstraint(
            "propagate_google_deletion = (mode = 'two_way')",
            name="ck_task_list_policy_fixed_deletion",
        ),
        sa.ForeignKeyConstraint(
            ["authorized_request_ref"], ["authenticated_requests.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_list_ref"], ["google_task_lists.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("task_list_ref", "revision", name="uq_task_list_policy_revision"),
    )
    op.create_table(
        "google_task_observations",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("task_list_ref", sa.String(length=40), nullable=False),
        sa.Column("provider_task_id", sa.String(length=1024), nullable=False),
        sa.Column("provider_etag", sa.String(length=512), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inventory_generation", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_list_ref"], ["google_task_lists.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint(
            "task_list_ref",
            "provider_task_id",
            "source_hash",
            name="uq_google_task_observation_version",
        ),
    )
    op.create_index(
        "ix_google_task_observation_identity",
        "google_task_observations",
        ["task_list_ref", "provider_task_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "task_provider_bindings",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("task_ref", sa.String(length=40), nullable=False),
        sa.Column("task_list_ref", sa.String(length=40), nullable=False),
        sa.Column("provider_task_id", sa.String(length=1024), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("detached", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("canonical_version", sa.Integer(), nullable=False),
        sa.Column("provider_etag", sa.String(length=512), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("observation_ref", sa.String(length=40), nullable=True),
        sa.Column("correlation", sa.String(length=128), nullable=False),
        sa.Column("marker_present", sa.Boolean(), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation >= 1 AND version >= 1", name="ck_task_provider_binding_version"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["observation_ref"], ["google_task_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_list_ref"], ["google_task_lists.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_ref"], ["tasks.ref_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("correlation"),
        sa.UniqueConstraint(
            "task_list_ref", "provider_task_id", name="uq_task_provider_binding_identity"
        ),
    )
    op.create_index(
        "uq_task_provider_binding_active_task",
        "task_provider_bindings",
        ["task_ref"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "task_binding_revisions",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("binding_ref", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("task_list_ref", sa.String(length=40), nullable=False),
        sa.Column("provider_task_id", sa.String(length=1024), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("detached", sa.Boolean(), nullable=False),
        sa.Column("canonical_version", sa.Integer(), nullable=False),
        sa.Column("provider_etag", sa.String(length=512), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("observation_ref", sa.String(length=40), nullable=True),
        sa.Column("basis_refs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_ref"], ["task_provider_bindings.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["observation_ref"], ["google_task_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_list_ref"], ["google_task_lists.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("binding_ref", "revision", name="uq_task_binding_revision"),
    )
    op.create_table(
        "task_sync_conflicts",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("binding_ref", sa.String(length=40), nullable=False),
        sa.Column("observation_ref", sa.String(length=40), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("local_values", sa.JSON(), nullable=False),
        sa.Column("google_values", sa.JSON(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_ref"], ["task_provider_bindings.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["observation_ref"], ["google_task_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("binding_ref", "source_hash", name="uq_task_sync_conflict_observation"),
    )
    op.create_table(
        "task_conflict_resolutions",
        sa.Column("conflict_ref", sa.String(length=40), nullable=False),
        sa.Column("changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("selection", sa.String(length=16), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conflict_ref"], ["task_sync_conflicts.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("conflict_ref"),
    )
    op.create_table(
        "task_sync_states",
        sa.Column("task_list_ref", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_token", sa.String(length=4096), nullable=True),
        sa.Column("generation", sa.Uuid(), nullable=True),
        sa.Column("scan_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_updated_min", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_policy_ref", sa.String(length=40), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("requested", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scan_policy_ref"], ["task_list_policy_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_list_ref"], ["google_task_lists.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("task_list_ref"),
    )
    op.create_table(
        "task_policy_effects",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("policy_ref", sa.String(length=40), nullable=False),
        sa.Column("observation_ref", sa.String(length=40), nullable=False),
        sa.Column("effect_hash", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("planner_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["observation_ref"], ["google_task_observations.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["policy_ref"], ["task_list_policy_revisions.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
    )
    op.create_table(
        "task_reminder_generations",
        sa.Column("ref_id", sa.String(length=40), nullable=False),
        sa.Column("task_ref", sa.String(length=40), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("temporal_binding_ref", sa.String(length=40), nullable=False),
        sa.Column("projection_ref", sa.String(length=40), nullable=False),
        sa.Column("plan_ref", sa.String(length=40), nullable=False),
        sa.Column("created_by_changeset_ref", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_task_reminder_generation"),
        sa.ForeignKeyConstraint(
            ["created_by_changeset_ref"], ["change_sets.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["plan_ref"], ["reminder_plans.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["projection_ref"], ["temporal_calendar_projections.ref_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["task_ref"], ["tasks.ref_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["temporal_binding_ref"], ["temporal_bindings.ref_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("ref_id"),
        sa.UniqueConstraint("plan_ref"),
        sa.UniqueConstraint("projection_ref"),
        sa.UniqueConstraint("task_ref", "generation", name="uq_task_reminder_generation"),
        sa.UniqueConstraint("temporal_binding_ref"),
    )

    with op.batch_alter_table("google_task_lists") as batch:
        batch.create_foreign_key(
            "fk_google_task_list_current_policy",
            "task_list_policy_revisions",
            ["policy_ref"],
            ["ref_id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(
            sa.Column(
                "task_list_ref",
                sa.String(40),
                sa.ForeignKey(
                    "google_task_lists.ref_id",
                    name="fk_tasks_google_task_list",
                    ondelete="RESTRICT",
                ),
            )
        )
        batch.add_column(sa.Column("scheduled_date", sa.Date()))
        batch.add_column(
            sa.Column(
                "parent_task_ref",
                sa.String(40),
                sa.ForeignKey("tasks.ref_id", name="fk_tasks_parent_task", ondelete="RESTRICT"),
            )
        )
        batch.add_column(sa.Column("remind_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("reminder_timezone", sa.String(128)))
        batch.add_column(
            sa.Column("reminder_generation", sa.Integer(), nullable=False, server_default="0")
        )
        batch.create_check_constraint("ck_tasks_reminder_generation", "reminder_generation >= 0")
        batch.create_check_constraint(
            "ck_tasks_reminder_zone", "(remind_at IS NULL) = (reminder_timezone IS NULL)"
        )
    with op.batch_alter_table("intent_sessions") as batch:
        batch.drop_constraint("ck_intent_sessions_authority_root", type_="check")
        batch.add_column(
            sa.Column(
                "source_policy_effect_ref",
                sa.String(40),
                sa.ForeignKey(
                    "task_policy_effects.ref_id",
                    name="fk_intent_task_policy_effect",
                    ondelete="RESTRICT",
                ),
            )
        )
        batch.create_unique_constraint("uq_intent_task_policy_effect", ["source_policy_effect_ref"])
        batch.create_check_constraint(
            "ck_intent_sessions_authority_root",
            "source_utterance_ref IS NOT NULL OR source_request_ref IS NOT NULL "
            "OR source_policy_effect_ref IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_intent_sessions_distinct_policy_root",
            "source_policy_effect_ref IS NULL OR "
            "(source_request_ref IS NULL AND source_utterance_ref IS NULL)",
        )
    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.add_column(sa.Column("origin_kind", sa.String(32)))
        batch.add_column(
            sa.Column(
                "origin_owner_ref",
                sa.String(40),
                sa.ForeignKey(
                    "tasks.ref_id", name="fk_calendar_cache_origin_task", ondelete="RESTRICT"
                ),
            )
        )
        batch.add_column(
            sa.Column(
                "origin_projection_ref",
                sa.String(40),
                sa.ForeignKey(
                    "temporal_calendar_projections.ref_id",
                    name="fk_calendar_cache_origin_projection",
                    ondelete="RESTRICT",
                ),
            )
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION retain_google_tasks_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Google Tasks evidence is immutable'; END $$;
            CREATE FUNCTION retain_task_policy_intent_root() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF OLD.source_policy_effect_ref IS DISTINCT FROM NEW.source_policy_effect_ref THEN
                RAISE EXCEPTION 'task policy admission is an immutable service root'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER retain_task_policy_intent_root BEFORE UPDATE ON intent_sessions
              FOR EACH ROW EXECUTE FUNCTION retain_task_policy_intent_root();
            CREATE FUNCTION guard_task_binding_head() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (OLD.ref_id,OLD.task_ref,OLD.correlation,OLD.created_by_changeset_ref,OLD.created_at)
                  IS DISTINCT FROM (NEW.ref_id,NEW.task_ref,NEW.correlation,NEW.created_by_changeset_ref,NEW.created_at)
                  OR NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'task binding identity is retained and updates are versioned'; END IF;
              IF OLD.task_list_ref IS DISTINCT FROM NEW.task_list_ref OR
                (OLD.provider_task_id IS NOT NULL AND OLD.provider_task_id IS DISTINCT FROM NEW.provider_task_id) THEN
                IF NEW.generation <> OLD.generation + 1 THEN
                  RAISE EXCEPTION 'a task location change requires a new generation'; END IF;
              ELSIF NEW.generation <> OLD.generation THEN
                RAISE EXCEPTION 'a task binding generation changes with its location';
              END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER guard_task_binding_head BEFORE UPDATE ON task_provider_bindings
              FOR EACH ROW EXECUTE FUNCTION guard_task_binding_head();
            CREATE FUNCTION require_task_binding_revision() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM task_binding_revisions r
                WHERE r.binding_ref = NEW.ref_id AND r.revision = NEW.version
                  AND (r.task_list_ref,r.provider_task_id,r.generation,r.active,r.detached,
                       r.canonical_version,r.provider_etag,r.provider_updated_at,r.observed_at,r.observation_ref,to_jsonb(r.baseline),to_jsonb(r.limitations))
                    IS NOT DISTINCT FROM
                      (NEW.task_list_ref,NEW.provider_task_id,NEW.generation,NEW.active,NEW.detached,
                       NEW.canonical_version,NEW.provider_etag,NEW.provider_updated_at,NEW.observed_at,NEW.observation_ref,to_jsonb(NEW.baseline),to_jsonb(NEW.limitations))) THEN
                RAISE EXCEPTION 'a task binding head requires its exact retained revision'; END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER require_task_binding_revision AFTER INSERT OR UPDATE ON task_provider_bindings
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_task_binding_revision();
        """)
        for table in (
            "task_list_policy_revisions",
            "google_task_observations",
            "task_binding_revisions",
            "task_sync_conflicts",
            "task_conflict_resolutions",
            "task_policy_effects",
            "task_reminder_generations",
        ):
            op.execute(
                f"CREATE TRIGGER retain_{table} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION retain_google_tasks_evidence()"
            )


def downgrade() -> None:
    tables = (
        "task_reminder_generations",
        "task_policy_effects",
        "task_conflict_resolutions",
        "task_sync_conflicts",
        "task_binding_revisions",
        "task_sync_states",
        "task_provider_bindings",
        "google_task_observations",
        "task_list_policy_revisions",
        "google_task_lists",
    )
    if any(op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")) for table in tables):
        raise RuntimeError(
            "cannot discard retained Tasks synchronization evidence; use forward repair"
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER retain_task_policy_intent_root ON intent_sessions")
        op.execute("DROP FUNCTION retain_task_policy_intent_root()")
    with op.batch_alter_table("calendar_event_cache") as batch:
        batch.drop_constraint("fk_calendar_cache_origin_projection", type_="foreignkey")
        batch.drop_constraint("fk_calendar_cache_origin_task", type_="foreignkey")
        for column in ("origin_projection_ref", "origin_owner_ref", "origin_kind"):
            batch.drop_column(column)
    with op.batch_alter_table("intent_sessions") as batch:
        batch.drop_constraint("ck_intent_sessions_authority_root", type_="check")
        batch.drop_constraint("ck_intent_sessions_distinct_policy_root", type_="check")
        batch.drop_constraint("fk_intent_task_policy_effect", type_="foreignkey")
        batch.drop_constraint("uq_intent_task_policy_effect", type_="unique")
        batch.drop_column("source_policy_effect_ref")
        batch.create_check_constraint(
            "ck_intent_sessions_authority_root",
            "source_utterance_ref IS NOT NULL OR source_request_ref IS NOT NULL",
        )
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_google_task_list", type_="foreignkey")
        batch.drop_constraint("fk_tasks_parent_task", type_="foreignkey")
        batch.drop_constraint("ck_tasks_reminder_generation", type_="check")
        batch.drop_constraint("ck_tasks_reminder_zone", type_="check")
        for column in (
            "task_list_ref",
            "scheduled_date",
            "parent_task_ref",
            "remind_at",
            "reminder_timezone",
            "reminder_generation",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("google_task_lists") as batch:
        batch.drop_constraint("fk_google_task_list_current_policy", type_="foreignkey")
    for table in tables:
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION retain_google_tasks_evidence()")
        op.execute("DROP FUNCTION guard_task_binding_head()")
        op.execute("DROP FUNCTION require_task_binding_revision()")
