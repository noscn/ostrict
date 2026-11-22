"""`ostrict scaffold`: draft a lint-clean semantic view from dbt metadata or a live schema."""

from __future__ import annotations

from functools import partial

from ..connectors import WarehouseConnector
from ..models.errors import ERR_PARSE, CompilerError
from .generator import ColumnDraft, RelDraft, ScaffoldResult, TableDraft, generate

__all__ = ["ColumnDraft", "RelDraft", "ScaffoldResult", "TableDraft", "generate", "scaffold_view"]


def scaffold_view(
    *,
    from_dbt: str | None = None,
    connector: WarehouseConnector | None = None,
    models: str | list[str] | None = None,
    name: str | None = None,
    dialect: str | None = None,
    sample_values: bool = False,
) -> ScaffoldResult:
    """One-call orchestration of the readers and the generator (what the CLI runs)."""
    selected = [m.strip() for m in models.split(",") if m.strip()] if isinstance(models, str) else (models or None)
    if from_dbt:
        from .dbt_reader import read_dbt

        tables, project = read_dbt(from_dbt, selected)
        origin, default_name = f"dbt project '{project}'", project
    elif connector is not None:
        from .db_reader import read_schema

        tables = read_schema(connector, selected or [])
        schemas = sorted({m.rpartition(".")[0] for m in (selected or [])} - {""})
        origin = f"{connector.dialect} information_schema"
        default_name = schemas[0].rpartition(".")[2] if schemas else "scaffolded_view"
    else:
        raise CompilerError(ERR_PARSE, "scaffold needs --from-dbt or --connector")

    sampler = None
    if sample_values:
        if connector is None:
            raise CompilerError(ERR_PARSE, "--sample-values needs --connector to read from")
        from .db_reader import sample_values as _sample

        sampler = partial(_sample, connector)

    return generate(
        tables,
        name=name or default_name,
        dialect=dialect or (connector.dialect if connector else "bigquery"),
        sampler=sampler,
        origin=origin,
    )
