from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0012_profileimage_tags'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='blur_explicit',
            field=models.BooleanField(default=False),
        ),
    ]
