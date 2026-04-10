from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):
    dependencies = [
        ('programs', '0021_testreport_result_percentage_denominator_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='programsconfig',
            name='subtask_parallel_limit',
            field=models.PositiveIntegerField(
                blank=True,
                default=None,
                help_text=_('Maximum number of tests from the same group run in parallel. Leave blank to keep current behavior.'),
                null=True,
                verbose_name=_('subtask parallel worker limit'),
            ),
        ),
    ]
