from django.db import migrations

import oioioi.base.fields


PROGRAM_STATUS_CHOICES = [
    ("?", "Pending"),
    ("OK", "OK"),
    ("ERR", "Error"),
    ("CE", "Compilation failed"),
    ("RE", "Runtime error"),
    ("WA", "Wrong answer"),
    ("TLE", "Time limit exceeded"),
    ("MLE", "Memory limit exceeded"),
    ("OLE", "Output limit exceeded"),
    ("SE", "System error"),
    ("RV", "Rule violation"),
    ("SKIP", "Skipped"),
    ("INI_OK", "Initial tests: OK"),
    ("INI_ERR", "Initial tests: failed"),
    ("TESTRUN_OK", "No error"),
    ("MSE", "Outgoing message size limit exceeded"),
    ("MCE", "Outgoing message count limit exceeded"),
    ("IGN", "Ignored"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("programs", "0023_testreport_mem_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="compilationreport",
            name="status",
            field=oioioi.base.fields.EnumField(choices=PROGRAM_STATUS_CHOICES, max_length=64),
        ),
        migrations.AlterField(
            model_name="groupreport",
            name="status",
            field=oioioi.base.fields.EnumField(choices=PROGRAM_STATUS_CHOICES, max_length=64),
        ),
        migrations.AlterField(
            model_name="testreport",
            name="status",
            field=oioioi.base.fields.EnumField(choices=PROGRAM_STATUS_CHOICES, max_length=64),
        ),
    ]
