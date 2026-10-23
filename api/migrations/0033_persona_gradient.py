from django.core.validators import MaxValueValidator
from django.db import migrations, models

# The client's old single-color palette, each mapped to a gradient starting at that color.
OLD_PALETTE = [
    (0x7C4DFF, 0x448AFF),
    (0x00897B, 0x26C6DA),
    (0xE65100, 0xFFB300),
    (0xC2185B, 0xFF6E40),
    (0x1E88E5, 0x7C4DFF),
    (0x6D4C41, 0xD7A86E),
    (0x43A047, 0xC0CA33),
    (0x546E7A, 0xB0BEC5),
]


def palette_to_gradient(apps, schema_editor):
    Persona = apps.get_model('api', 'Persona')
    for persona in Persona.objects.all():
        persona.color_start, persona.color_end = OLD_PALETTE[persona.color % len(OLD_PALETTE)]
        persona.save(update_fields=['color_start', 'color_end'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0032_message_attachments'),
    ]

    operations = [
        migrations.AddField(
            model_name='persona',
            name='color_start',
            field=models.PositiveIntegerField(default=0xE53935, validators=[MaxValueValidator(0xFFFFFF)]),
        ),
        migrations.AddField(
            model_name='persona',
            name='color_end',
            field=models.PositiveIntegerField(default=0xF9A825, validators=[MaxValueValidator(0xFFFFFF)]),
        ),
        migrations.RunPython(palette_to_gradient, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='persona',
            name='color',
        ),
    ]
