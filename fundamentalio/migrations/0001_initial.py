import uuid

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Report',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('type', models.CharField(choices=[('quick_research', 'Quick Research'), ('deep_research', 'Deep Research')], max_length=20)),
                ('status', models.CharField(choices=[('in_process', 'In process'), ('done', 'Done'), ('error', 'Error')], default='in_process', max_length=20)),
                ('company_symbol', models.CharField(max_length=20)),
                ('exchange_code', models.CharField(blank=True, default='', max_length=10)),
                ('company_name', models.CharField(max_length=255)),
                ('read', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('markdown', models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(200000)])),
                ('usage_info', models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(50000)])),
            ],
            options={
                'verbose_name': 'report',
                'verbose_name_plural': 'reports',
            },
        ),
    ]
