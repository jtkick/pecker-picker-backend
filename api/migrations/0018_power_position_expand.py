from django.db import migrations, models


def remap_power_position(apps, schema_editor):
    # Old: 0=Sub, 1=Switch, 2=Dom
    # New: 0=Sub, 1=Sub Switch, 2=Switch, 3=Dom Switch, 4=Dom
    # Remap in reverse order so 2→4 doesn't collide with the old 2.
    User = apps.get_model('api', 'User')
    User.objects.filter(power_position=2).update(power_position=4)
    User.objects.filter(power_position=1).update(power_position=2)


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0017_position_fields'),
    ]

    operations = [
        migrations.RunPython(remap_power_position, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='power_position',
            field=models.SmallIntegerField(
                blank=True, null=True,
                choices=[(0,'Sub'),(1,'Sub Switch'),(2,'Switch'),(3,'Dom Switch'),(4,'Dom')],
            ),
        ),
    ]
