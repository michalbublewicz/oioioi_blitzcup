from django.db import migrations


def _table_columns(connection, table_name):
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table_name)
    return {column.name: column for column in description}


def _table_constraints(connection, table_name):
    with connection.cursor() as cursor:
        return connection.introspection.get_constraints(cursor, table_name)


def repair_configurable_ranking_schema(apps, schema_editor):
    connection = schema_editor.connection
    existing_tables = set(connection.introspection.table_names())

    ConfigurableRankingSettings = apps.get_model("rankings", "ConfigurableRankingSettings")
    ConfigurableRankingRound = apps.get_model("rankings", "ConfigurableRankingRound")

    settings_table = ConfigurableRankingSettings._meta.db_table
    if settings_table not in existing_tables:
        schema_editor.create_model(ConfigurableRankingSettings)
    else:
        settings_columns = _table_columns(connection, settings_table)
        for field_name in ("contest", "show_default_rankings"):
            field = ConfigurableRankingSettings._meta.get_field(field_name)
            if field.column not in settings_columns:
                schema_editor.add_field(ConfigurableRankingSettings, field)

    round_table = ConfigurableRankingRound._meta.db_table
    round_columns = _table_columns(connection, round_table)
    for field_name in (
        "source_type",
        "sub_ranking",
        "all_time_coefficient",
        "all_time_score_mode",
    ):
        field = ConfigurableRankingRound._meta.get_field(field_name)
        if field.column not in round_columns:
            schema_editor.add_field(ConfigurableRankingRound, field)

    round_field = ConfigurableRankingRound._meta.get_field("round")
    round_columns = _table_columns(connection, round_table)
    round_column = round_columns.get(round_field.column)
    if round_column is not None and not round_column.null_ok:
        schema_editor.execute(
            "ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL".format(
                table=schema_editor.quote_name(round_table),
                column=schema_editor.quote_name(round_field.column),
            )
        )

    constraints = _table_constraints(connection, round_table)
    for constraint in ConfigurableRankingRound._meta.constraints:
        if constraint.name not in constraints:
            schema_editor.add_constraint(ConfigurableRankingRound, constraint)


class Migration(migrations.Migration):
    dependencies = [
        ("rankings", "0006_configurablerankingsettings_and_sources"),
    ]

    operations = [
        migrations.RunPython(repair_configurable_ranking_schema, migrations.RunPython.noop),
    ]
