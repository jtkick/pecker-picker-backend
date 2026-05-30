from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0020_height_length'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='kinks',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
