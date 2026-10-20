from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_passkeys'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='phone_verified',
            field=models.BooleanField(default=False),
        ),
    ]
