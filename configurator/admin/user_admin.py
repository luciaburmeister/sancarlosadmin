from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.models import User, Group

from unfold.admin import TabularInline
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from unfold.admin import ModelAdmin
from apps.public_api.admin import UserAPIKeyInline
from company.models import CompanyUser


admin.site.unregister(User)
admin.site.unregister(Group)

class CompanyUserInline(TabularInline):
    model = CompanyUser
    extra = 0
    autocomplete_fields = ["company"]
    verbose_name = "Company access"
    verbose_name_plural = "Company access"


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    inlines = [CompanyUserInline, UserAPIKeyInline]


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass