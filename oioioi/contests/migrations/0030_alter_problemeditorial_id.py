from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("contests", "0029_problemeditorial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="problemeditorial",
            name="id",
            field=models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
    ]
