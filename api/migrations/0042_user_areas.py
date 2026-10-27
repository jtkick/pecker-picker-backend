from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0041_account_age'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='area_neighbourhood',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='area_locality',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='area_county',
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
