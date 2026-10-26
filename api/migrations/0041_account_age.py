from django.db import migrations, models


def personas_to_account(apps, schema_editor):
    """The account takes its age from its primary persona (else the first that has one).
    Personas that didn't show an age keep hiding it."""
    User = apps.get_model('api', 'User')
    Persona = apps.get_model('api', 'Persona')
    for user in User.objects.all():
        personas = list(Persona.objects.filter(user=user).order_by('-is_primary', 'sort_order', 'created_at'))
        user.age = next((p.age for p in personas if p.age is not None), None)
        user.save(update_fields=['age'])
        for p in personas:
            p.show_age = p.age is not None
            p.save(update_fields=['show_age'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0040_lifeline'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='age',
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='persona',
            name='show_age',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(personas_to_account, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='persona',
            name='age',
        ),
    ]
