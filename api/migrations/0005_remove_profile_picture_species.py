from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0004_user_profile_picture_profileimage'),
    ]

    operations = [
        migrations.RemoveField(model_name='user', name='profile_picture'),
        migrations.RemoveField(model_name='user', name='species'),
    ]
