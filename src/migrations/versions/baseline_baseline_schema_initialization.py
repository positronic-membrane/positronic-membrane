"""baseline_schema_initialization

Revision ID: baseline
Revises: None
Create Date: 2026-06-19 20:22:52.304410

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
