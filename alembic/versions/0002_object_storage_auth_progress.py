"""object storage, auth and progress

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19 21:50:01.887757

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Added nullable, backfilled, then constrained. Adding it NOT NULL in one
    # step fails on any table that already has rows, which is every deployed
    # database. Backfilled from the run's own id: unique by construction, and
    # correct in the only sense that matters, since nothing will ever resubmit
    # a historical run.
    op.add_column("runs", sa.Column("external_id", sa.String(length=128), nullable=True))
    op.execute("UPDATE runs SET external_id = id WHERE external_id IS NULL")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.alter_column("external_id", existing_type=sa.String(length=128), nullable=False)
    op.create_index("ix_runs_external_id", "runs", ["external_id"], unique=True)

    # Every column below is nullable, or carries a server_default so the
    # ALTER succeeds on a table that already has rows. No backfill needed.
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("submitted_by", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("input_key", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("extra_input_keys", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(sa.Column("output_bucket", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("artifacts_uploaded", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("upload_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("upload_error", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("next_upload_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_runs_owner_id"), ["owner_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_runs_next_upload_attempt_at"), ["next_upload_attempt_at"], unique=False
        )

    with op.batch_alter_table("run_stages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        # Drives the ETA median: "how long does `variant` usually take".
        batch_op.create_index("ix_run_stages_name_status", ["name", "status"], unique=False)

    with op.batch_alter_table("run_artifacts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("s3_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_artifacts", schema=None) as batch_op:
        batch_op.drop_column("uploaded_at")
        batch_op.drop_column("s3_key")

    with op.batch_alter_table("run_stages", schema=None) as batch_op:
        batch_op.drop_index("ix_run_stages_name_status")
        batch_op.drop_column("last_heartbeat_at")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_next_upload_attempt_at"))
        batch_op.drop_index(batch_op.f("ix_runs_owner_id"))
        batch_op.drop_column("next_upload_attempt_at")
        batch_op.drop_column("upload_error")
        batch_op.drop_column("upload_attempts")
        batch_op.drop_column("artifacts_uploaded")
        batch_op.drop_column("output_bucket")
        batch_op.drop_column("extra_input_keys")
        batch_op.drop_column("input_key")
        batch_op.drop_column("submitted_by")
        batch_op.drop_column("owner_id")

    op.drop_index("ix_runs_external_id", table_name="runs")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("external_id")
