"""retain explicitly approved remote plugin principals

Revision ID: 20261028a1b2
Revises: 20261007c3d4
"""

import sqlalchemy as sa
from alembic import op

revision = "20261028a1b2"
down_revision = "20261007c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_principals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ref_id", sa.String(40), nullable=False, unique=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("operator_ref", sa.String(255), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("valid_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_oauth_principal_owner"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION docket_guard_oauth_identity() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'OAuthPrincipal is retained'; END IF;
          IF (to_jsonb(NEW) - 'scopes' - 'enabled' - 'valid_after') IS DISTINCT FROM
             (to_jsonb(OLD) - 'scopes' - 'enabled' - 'valid_after') THEN
            RAISE EXCEPTION 'OAuthPrincipal identity is immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute("CREATE TRIGGER trg_oauth_identity BEFORE UPDATE OR DELETE ON oauth_principals FOR EACH ROW EXECUTE FUNCTION docket_guard_oauth_identity()")


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM oauth_principals)")).scalar():
        raise RuntimeError("OAuth principal history is retained; use forward repair or backup")
    op.drop_table("oauth_principals")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION docket_guard_oauth_identity()")
