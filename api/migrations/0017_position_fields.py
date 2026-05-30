from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0016_block_report'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='position',
            field=models.SmallIntegerField(
                blank=True, null=True,
                choices=[(0,'Bottom'),(1,'Vers Bottom'),(2,'Vers'),(3,'Vers Top'),(4,'Top')],
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='power_position',
            field=models.SmallIntegerField(
                blank=True, null=True,
                choices=[(0,'Sub'),(1,'Switch'),(2,'Dom')],
            ),
        ),
    ]
