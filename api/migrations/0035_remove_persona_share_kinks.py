from django.db import migrations


def clear_unshared_kinks(apps, schema_editor):
    """Kinks are now always public, so drop the ones that were hidden rather than expose them."""
    Persona = apps.get_model('api', 'Persona')
    Persona.objects.filter(share_kinks=False).exclude(kinks=[]).update(kinks=[])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0034_account_body_fields'),
    ]

    operations = [
        migrations.RunPython(clear_unshared_kinks, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='persona',
            name='share_kinks',
        ),
    ]
