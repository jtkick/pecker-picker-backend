from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0035_remove_persona_share_kinks'),
    ]

    operations = [
        migrations.AddField(
            model_name='persona',
            name='is_side',
            field=models.BooleanField(default=False),
        ),
    ]
