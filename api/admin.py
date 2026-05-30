from django.contrib import admin
from .models import Kink


@admin.register(Kink)
class KinkAdmin(admin.ModelAdmin):
    list_display = ['name', 'parent', 'is_active']
    list_editable = ['is_active']
    list_select_related = ['parent']
    list_filter = ['is_active', 'parent']
    search_fields = ['name']
    ordering = ['parent__name', 'name']
    raw_id_fields = ['parent']
