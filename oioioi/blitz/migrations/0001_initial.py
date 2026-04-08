from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('contests', '0024_roundstartdelay'),
    ]

    operations = [
        migrations.CreateModel(
            name='BlitzContestConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('intermission_seconds', models.PositiveIntegerField(default=60, help_text='How long the break after a solved problem lasts.', verbose_name='intermission (seconds)')),
                ('contest', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='blitz_config', to='contests.contest', verbose_name='contest')),
            ],
            options={
                'verbose_name': 'blitz contest configuration',
                'verbose_name_plural': 'blitz contest configurations',
            },
        ),
        migrations.CreateModel(
            name='BlitzProblemState',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('closed_at', models.DateTimeField(blank=True, null=True, verbose_name='closed at')),
                ('problem_instance', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='blitz_state', to='contests.probleminstance', verbose_name='problem instance')),
                ('solved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='blitz_solved_problems', to=settings.AUTH_USER_MODEL, verbose_name='solved by')),
                ('winning_submission', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='blitz_wins', to='contests.submission', verbose_name='winning submission')),
            ],
            options={
                'verbose_name': 'blitz problem state',
                'verbose_name_plural': 'blitz problem states',
            },
        ),
    ]
