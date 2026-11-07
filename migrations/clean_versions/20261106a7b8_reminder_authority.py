"""retain exact reminder authority and genuine Calendar observation effects"""

import sqlalchemy as sa
from alembic import op

revision = "20261106a7b8"
down_revision = "20261106f6a7"
branch_labels = None
depends_on = None

SOURCE_CHECK = (
    "(policy_ref IS NOT NULL AND observation_ref IS NOT NULL "
    "AND reminder_generation_ref IS NULL AND reminder_observation IS NULL) OR "
    "(policy_ref IS NULL AND observation_ref IS NULL "
    "AND reminder_generation_ref IS NOT NULL AND reminder_observation IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "task_reminder_authorities",
        sa.Column(
            "generation_ref",
            sa.String(40),
            sa.ForeignKey("task_reminder_generations.ref_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("principal_ref", sa.String(128), nullable=False),
        sa.Column(
            "authorized_request_ref",
            sa.String(40),
            sa.ForeignKey("authenticated_requests.ref_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("grant_epoch", sa.Integer()),
    )
    with op.batch_alter_table("task_policy_effects") as batch:
        batch.alter_column("policy_ref", existing_type=sa.String(40), nullable=True)
        batch.alter_column("observation_ref", existing_type=sa.String(40), nullable=True)
        batch.add_column(
            sa.Column(
                "reminder_generation_ref",
                sa.String(40),
                sa.ForeignKey(
                    "task_reminder_generations.ref_id",
                    ondelete="RESTRICT",
                    name="fk_task_effect_reminder_generation",
                ),
            )
        )
        batch.add_column(sa.Column("reminder_observation", sa.JSON(none_as_null=True)))
        batch.create_check_constraint("ck_task_policy_effect_source", SOURCE_CHECK)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER retain_task_reminder_authorities BEFORE UPDATE OR DELETE "
            "ON task_reminder_authorities FOR EACH ROW "
            "EXECUTE FUNCTION retain_google_tasks_evidence()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM task_reminder_authorities")):
        raise RuntimeError("retained reminder authority requires forward repair")
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM task_policy_effects WHERE reminder_generation_ref IS NOT NULL"
        )
    ):
        raise RuntimeError("retained Calendar reminder evidence requires forward repair")
    with op.batch_alter_table("task_policy_effects") as batch:
        batch.drop_constraint("ck_task_policy_effect_source", type_="check")
        batch.drop_constraint("fk_task_effect_reminder_generation", type_="foreignkey")
        batch.drop_column("reminder_observation")
        batch.drop_column("reminder_generation_ref")
        batch.alter_column("policy_ref", existing_type=sa.String(40), nullable=False)
        batch.alter_column("observation_ref", existing_type=sa.String(40), nullable=False)
    op.drop_table("task_reminder_authorities")
