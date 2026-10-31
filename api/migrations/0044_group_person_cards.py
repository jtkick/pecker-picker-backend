from django.db import migrations, models


def seed_cards(apps, schema_editor):
    """Fill each member's person card with what they shared before; owners are known by their primary profile."""
    PersonaMember = apps.get_model('api', 'PersonaMember')
    for m in PersonaMember.objects.select_related('user', 'represent_persona'):
        if m.share_profile and m.represent_persona is not None:
            m.name = m.represent_persona.display_name[:50]
        if m.share_pronouns:
            m.pronouns = m.user.pronouns
        if m.share_height:
            m.height = m.user.height
        if m.share_length:
            m.length = m.user.length
        if m.invited_as_id is None and m.role == 'owner':
            m.invited_as_id = m.represent_persona_id
        m.save(update_fields=['name', 'pronouns', 'height', 'length', 'invited_as'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0043_friend_profileview'),
    ]

    operations = [
        migrations.AddField(
            model_name='personamember',
            name='name',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='personamember',
            name='age',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='personamember',
            name='pronouns',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='personamember',
            name='height',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='personamember',
            name='length',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(seed_cards, migrations.RunPython.noop),
    ]
