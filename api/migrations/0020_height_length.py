from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0019_face_verification'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='height',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='length',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
    ]
