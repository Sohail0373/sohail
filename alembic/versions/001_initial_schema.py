"""Initial schema — stores table

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stores",
        sa.Column("id",                   sa.Integer(),          nullable=False),
        sa.Column("shop_domain",          sa.String(255),        nullable=False),
        sa.Column("access_token",         sa.String(255),        nullable=False),
        sa.Column("shop_name",            sa.String(255),        nullable=True),
        sa.Column("shop_currency",        sa.String(10),         nullable=True),
        sa.Column("shop_url",             sa.String(512),        nullable=True),
        sa.Column("installed_at",         sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("last_feed_generated",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("product_count",        sa.Integer(),          nullable=True),
        sa.Column("is_active",            sa.Boolean(),          nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stores_id",          "stores", ["id"],          unique=False)
    op.create_index("ix_stores_shop_domain", "stores", ["shop_domain"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_stores_shop_domain", table_name="stores")
    op.drop_index("ix_stores_id",          table_name="stores")
    op.drop_table("stores")
