"""lease fencing telemetry budgets

Revision ID: 0004_fencing
Revises: 0003_autonomous
Create Date: 2026-09-05 17:04:08.945184

Amendment (duplicate-safe uniqueness): the original revision created
``uq_export_job_kind`` and ``uq_storyline_job_option`` directly, which fails on
a populated 0003 database that already holds duplicate export artifacts or
storyline candidates. A later revision cannot repair that (this one runs
first), so this revision now deduplicates deterministically before adding each
constraint. Survivor rules (see ``dedupe_export_artifacts`` /
``dedupe_storyline_candidates``):

- export artifacts, per (job_id, kind): keep the newest non-empty artifact
  (created_at, then id); delete the rest.
- storyline candidates, per (job_id, option_index): keep the *selected* row if
  one exists, else the non-rejected row with the highest originality score,
  else the newest; the losers are not deleted but renumbered to free
  option_index values so no candidate a job may reference disappears.

The dedup is idempotent and logs a summary via ``print`` (visible in the
Alembic output) without any artifact or storyline content.
"""
from typing import Dict, List, Sequence, Tuple, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0004_fencing'
down_revision: Union[str, None] = '0003_autonomous'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def dedupe_export_artifacts(conn) -> Dict[str, int]:
    """Keep one artifact per (job_id, kind): newest non-empty (created_at desc, id desc)."""
    rows = conn.execute(sa.text("SELECT id, job_id, kind, size_bytes, created_at FROM exportartifact ORDER BY job_id, kind, created_at DESC, id DESC")).fetchall()
    groups: Dict[Tuple[int, str], List] = {}
    for r in rows:
        groups.setdefault((r[1], r[2]), []).append(r)
    removed = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        non_empty = [m for m in members if (m[3] or 0) > 0]
        survivor = (non_empty or members)[0]
        losers = [m[0] for m in members if m[0] != survivor[0]]
        conn.execute(sa.text("DELETE FROM exportartifact WHERE id IN (%s)" % ",".join(str(int(i)) for i in losers)))
        removed += len(losers)
    summary = {"groups_with_duplicates": sum(1 for m in groups.values() if len(m) > 1), "rows_removed": removed}
    if removed:
        print(f"[0004_fencing] exportartifact dedup: {summary}")
    return summary


def dedupe_storyline_candidates(conn) -> Dict[str, int]:
    """Keep the selected / best non-rejected / newest candidate per (job_id, option_index); renumber the others."""
    rows = conn.execute(sa.text("SELECT id, job_id, option_index, selected, rejected, originality_score, created_at FROM storylinecandidate ORDER BY job_id, option_index, id")).fetchall()
    by_job: Dict[int, List] = {}
    for r in rows:
        by_job.setdefault(r[1], []).append(r)
    renumbered = 0
    groups_dup = 0
    for job_id, members in by_job.items():
        groups: Dict[int, List] = {}
        for m in members:
            groups.setdefault(int(m[2] or 0), []).append(m)
        if all(len(g) < 2 for g in groups.values()):
            continue
        used = set(groups.keys())
        next_free = max(used) + 1 if used else 0
        for idx, group in sorted(groups.items()):
            if len(group) < 2:
                continue
            groups_dup += 1

            def rank(m):
                return (1 if m[3] else 0, 0 if m[4] else 1, float(m[5] or 0.0), str(m[6] or ""), int(m[0]))

            survivor = max(group, key=rank)
            for m in group:
                if m[0] == survivor[0]:
                    continue
                while next_free in used:
                    next_free += 1
                conn.execute(sa.text("UPDATE storylinecandidate SET option_index = :idx WHERE id = :id"), {"idx": next_free, "id": int(m[0])})
                used.add(next_free)
                renumbered += 1
    summary = {"groups_with_duplicates": groups_dup, "rows_renumbered": renumbered}
    if renumbered:
        print(f"[0004_fencing] storylinecandidate dedup: {summary}")
    return summary


def _has_column(conn, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(conn).get_columns(table))


def _has_unique(conn, table: str, name: str) -> bool:
    return any(u.get("name") == name for u in sa.inspect(conn).get_unique_constraints(table))


def _add_column_if_missing(conn, table: str, column: sa.Column) -> None:
    if _has_column(conn, table, column.name):
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(column)


def _create_index_if_missing(conn, table: str, name: str, columns: List[str]) -> None:
    if any(ix.get('name') == name for ix in sa.inspect(conn).get_indexes(table)):
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.create_index(name, columns, unique=False)


def upgrade() -> None:
    # Every step is guarded so a database left half-migrated by the original (failing) 0004 completes cleanly.
    conn = op.get_bind()
    # SQLite batch mode leaves a temp table behind when a rebuild fails; remove it before retrying.
    for leftover in [t for t in sa.inspect(conn).get_table_names() if t.startswith('_alembic_tmp_')]:
        op.drop_table(leftover)
    if not sa.inspect(conn).has_table('modelinvocationattempt'):
        op.create_table('modelinvocationattempt',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('invocation_id', sa.Integer(), nullable=True),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('provider', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('model_name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('llm_config_id', sa.Integer(), nullable=True),
    sa.Column('fallback', sa.Boolean(), nullable=False),
    sa.Column('role', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('stage', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('error_category', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('provider_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('provider_request_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('retry_after_seconds', sa.Float(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('timeout_seconds', sa.Float(), nullable=True),
    sa.Column('response_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('diagnostic', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.PrimaryKeyConstraint('id')
        )
    for col in ('invocation_id', 'job_id', 'role', 'status'):
        _create_index_if_missing(conn, 'modelinvocationattempt', f'ix_modelinvocationattempt_{col}', [col])

    if not sa.inspect(conn).has_table('recoveryaction'):
        op.create_table('recoveryaction',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=False),
    sa.Column('stage', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('stage_attempt', sa.Integer(), nullable=False),
    sa.Column('failure_category', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('parameters_before', sa.JSON(), nullable=True),
    sa.Column('parameters_after', sa.JSON(), nullable=True),
    sa.Column('input_artifact', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('output_artifact', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('original_model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('selected_model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('downstream_invalidations', sa.JSON(), nullable=True),
    sa.Column('validation', sa.JSON(), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
        )
    for col in ('action', 'job_id', 'stage'):
        _create_index_if_missing(conn, 'recoveryaction', f'ix_recoveryaction_{col}', [col])

    for column in (
        sa.Column('lease_generation', sa.Integer(), server_default='0', nullable=False),
        sa.Column('budget', sa.JSON(), nullable=True),
        sa.Column('reserved_calls', sa.Integer(), server_default='0', nullable=False),
        sa.Column('reserved_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('repair_calls', sa.Integer(), server_default='0', nullable=False),
        sa.Column('quality_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('quality_summary', sa.JSON(), nullable=True),
    ):
        _add_column_if_missing(conn, 'autonomousnoveljob', column)
    _create_index_if_missing(conn, 'autonomousnoveljob', 'ix_autonomousnoveljob_quality_status', ['quality_status'])

    dedupe_export_artifacts(conn)
    if not _has_unique(conn, 'exportartifact', 'uq_export_job_kind'):
        with op.batch_alter_table('exportartifact', schema=None) as batch_op:
            batch_op.create_unique_constraint('uq_export_job_kind', ['job_id', 'kind'])

    for column in (
        sa.Column('prompt_hash', sa.String(), server_default='', nullable=False),
        sa.Column('total_attempts', sa.Integer(), server_default='1', nullable=False),
        sa.Column('selected_attempt', sa.Integer(), nullable=True),
        sa.Column('fallback_used', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    ):
        _add_column_if_missing(conn, 'modelinvocation', column)

    dedupe_storyline_candidates(conn)
    if not _has_unique(conn, 'storylinecandidate', 'uq_storyline_job_option'):
        with op.batch_alter_table('storylinecandidate', schema=None) as batch_op:
            batch_op.create_unique_constraint('uq_storyline_job_option', ['job_id', 'option_index'])

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('storylinecandidate', schema=None) as batch_op:
        batch_op.drop_constraint('uq_storyline_job_option', type_='unique')

    with op.batch_alter_table('modelinvocation', schema=None) as batch_op:
        batch_op.drop_column('finished_at')
        batch_op.drop_column('started_at')
        batch_op.drop_column('fallback_used')
        batch_op.drop_column('selected_attempt')
        batch_op.drop_column('total_attempts')
        batch_op.drop_column('prompt_hash')

    with op.batch_alter_table('exportartifact', schema=None) as batch_op:
        batch_op.drop_constraint('uq_export_job_kind', type_='unique')

    with op.batch_alter_table('autonomousnoveljob', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_autonomousnoveljob_quality_status'))
        batch_op.drop_column('quality_summary')
        batch_op.drop_column('quality_status')
        batch_op.drop_column('repair_calls')
        batch_op.drop_column('reserved_tokens')
        batch_op.drop_column('reserved_calls')
        batch_op.drop_column('budget')
        batch_op.drop_column('lease_generation')

    with op.batch_alter_table('recoveryaction', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recoveryaction_stage'))
        batch_op.drop_index(batch_op.f('ix_recoveryaction_job_id'))
        batch_op.drop_index(batch_op.f('ix_recoveryaction_action'))

    op.drop_table('recoveryaction')
    with op.batch_alter_table('modelinvocationattempt', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_modelinvocationattempt_status'))
        batch_op.drop_index(batch_op.f('ix_modelinvocationattempt_role'))
        batch_op.drop_index(batch_op.f('ix_modelinvocationattempt_job_id'))
        batch_op.drop_index(batch_op.f('ix_modelinvocationattempt_invocation_id'))

    op.drop_table('modelinvocationattempt')
    # ### end Alembic commands ###
