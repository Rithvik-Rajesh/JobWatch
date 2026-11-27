"""worker: procrastinate schema and worker heartbeats

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12 00:10:00
"""

from collections.abc import Sequence
from importlib import resources

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def split_sql(script: str) -> list[str]:
    """Split a SQL script into single statements (asyncpg runs one statement per call).

    Understands `--` comments, '...' strings and $tag$...$tag$ bodies, so semicolons inside PL/pgSQL
    function bodies do not split."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if script.startswith("--", i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            buf.append(script[i:end])
            i = end
        elif ch == "'":
            j = i + 1
            while j < n:
                if script[j] == "'":
                    if script.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            buf.append(script[i : j + 1])
            i = j + 1
        elif ch == "$":
            j = i + 1
            while j < n and (script[j].isalnum() or script[j] == "_"):
                j += 1
            if j < n and script[j] == "$":
                tag = script[i : j + 1]
                end = script.find(tag, j + 1)
                if end == -1:
                    raise ValueError(f"unterminated dollar-quoted body starting with {tag}")
                buf.append(script[i : end + len(tag)])
                i = end + len(tag)
            else:
                buf.append(ch)
                i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail and any(not line.strip().startswith("--") for line in tail.splitlines() if line.strip()):
        statements.append(tail)
    return statements


DROP_PROCRASTINATE = """
do $$
declare r record;
begin
    for r in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = current_schema() and p.proname like 'procrastinate\\_%' loop
        execute 'drop function if exists ' || r.sig || ' cascade';
    end loop;
end $$
"""


def upgrade() -> None:
    # Procrastinate ships its schema as one multi-statement SQL file; we apply it statement by statement
    # (pinned by the installed procrastinate version at the time the migration runs).
    schema = (resources.files("procrastinate.sql") / "schema.sql").read_text(encoding="utf-8")
    bind = op.get_bind()
    for statement in split_sql(schema):
        bind.exec_driver_sql(statement)

    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_worker_heartbeats")),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    for table in (
        "procrastinate_events",
        "procrastinate_periodic_defers",
        "procrastinate_jobs",
        "procrastinate_workers",
    ):
        op.execute(f"drop table if exists {table} cascade")
    op.execute(DROP_PROCRASTINATE)
    for type_name in ("procrastinate_job_to_defer_v1", "procrastinate_job_event_type", "procrastinate_job_status"):
        op.execute(f"drop type if exists {type_name} cascade")
