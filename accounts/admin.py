from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'phone_number', 'wallet_balance', 'is_staff')
    fieldsets = UserAdmin.fieldsets + (
        ('اطلاعات اختصاصی', {'fields': ('phone_number', 'wallet_balance', 'phone_verified', 'email_verified')}),
    )