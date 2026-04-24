from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("programs", "0022_programsconfig_subtask_parallel_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="testreport",
            name="mem_used",
            field=models.IntegerField(blank=True, default=0),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="testreport",
            name="test_mem_limit",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
