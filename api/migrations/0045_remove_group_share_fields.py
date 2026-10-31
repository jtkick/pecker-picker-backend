from django.db import migrations


class Migration(migrations.Migration):
    """Separate from 0044: Postgres can't alter a table in the transaction that just updated its rows."""

    dependencies = [
        ('api', '0044_group_person_cards'),
    ]

    operations = [
        migrations.RemoveField(model_name='personamember', name='represent_persona'),
        migrations.RemoveField(model_name='personamember', name='share_profile'),
        migrations.RemoveField(model_name='personamember', name='share_pronouns'),
        migrations.RemoveField(model_name='personamember', name='share_height'),
        migrations.RemoveField(model_name='personamember', name='share_length'),
    ]
