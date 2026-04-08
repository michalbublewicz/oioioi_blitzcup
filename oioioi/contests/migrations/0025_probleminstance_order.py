from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('contests', '0024_roundstartdelay'),
    ]

    operations = [
        migrations.AddField(
            model_name='probleminstance',
            name='order',
            field=models.IntegerField(default=0, verbose_name='order'),
        ),
    ]
