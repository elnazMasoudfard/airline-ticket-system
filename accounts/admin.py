from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import CustomUser, WalletTransaction


class ReadOnlyAdminMixin:
    """The wallet ledger is append-only: nobody may add, edit or delete rows by hand."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class WalletTransactionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = WalletTransaction
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ('created_at', 'kind', 'amount', 'balance_before', 'balance_after', 'reference', 'description')
    readonly_fields = fields
    ordering = ('-created_at', '-id')


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'phone_number', 'wallet_balance', 'is_staff')
    # The balance is read-only here: changing it by hand would bypass the ledger.
    # Use user.deposit()/withdraw() (e.g. from the shell, kind=ADJUSTMENT) instead.
    readonly_fields = ('wallet_balance',)
    fieldsets = UserAdmin.fieldsets + (
        ('اطلاعات اختصاصی', {'fields': ('phone_number', 'wallet_balance', 'phone_verified', 'email_verified')}),
    )
    inlines = [WalletTransactionInline]


@admin.register(WalletTransaction)
class WalletTransactionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'user', 'kind', 'amount', 'balance_before', 'balance_after', 'reference')
    list_filter = ('kind', 'created_at')
    search_fields = ('user__username', 'reference', 'description')
    date_hierarchy = 'created_at'
    ordering = ('-created_at', '-id')