from django.db import migrations, models

FIELDS = ('pronouns', 'height', 'length')


def _is_set(value):
    return value not in ('', None)


def personas_to_account(apps, schema_editor):
    """The account takes each value from its primary persona (else the first that has one).
    Personas that didn't show a value keep hiding it."""
    User = apps.get_model('api', 'User')
    Persona = apps.get_model('api', 'Persona')
    for user in User.objects.all():
        personas = list(Persona.objects.filter(user=user).order_by('-is_primary', 'sort_order', 'created_at'))
        for field in FIELDS:
            value = next((getattr(p, field) for p in personas if _is_set(getattr(p, field))), None)
            if value is not None:
                setattr(user, field, value)
        user.save(update_fields=list(FIELDS))
        for p in personas:
            for field in FIELDS:
                setattr(p, f'show_{field}', _is_set(getattr(p, field)))
            p.save(update_fields=[f'show_{f}' for f in FIELDS])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0033_persona_gradient'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='pronouns',
            field=models.CharField(blank=True, max_length=20),
        ),
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
        migrations.AddField(
            model_name='persona',
            name='show_pronouns',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='persona',
            name='show_height',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='persona',
            name='show_length',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(personas_to_account, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='persona',
            name='pronouns',
        ),
        migrations.RemoveField(
            model_name='persona',
            name='height',
        ),
        migrations.RemoveField(
            model_name='persona',
            name='length',
        ),
    ]
