from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0006_conversation_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='share_images',
            field=models.BooleanField(default=False),
        ),
    ]
