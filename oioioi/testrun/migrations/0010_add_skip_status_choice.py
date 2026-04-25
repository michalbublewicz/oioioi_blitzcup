from django.db import migrations

import oioioi.base.fields


TESTRUN_STATUS_CHOICES = [
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
        ("testrun", "0009_testrunreport_mem_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="testrunreport",
            name="status",
            field=oioioi.base.fields.EnumField(choices=TESTRUN_STATUS_CHOICES, max_length=64),
        ),
    ]
