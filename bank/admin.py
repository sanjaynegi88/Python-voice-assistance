"""Django admin registrations -- the raw editing surface behind the dashboard."""
from django.contrib import admin

from .models import (
    Account,
    AuthSession,
    Card,
    Customer,
    Notification,
    OtpChallenge,
    ProviderSettings,
    Ticket,
    ToolCallLog,
)


class AccountInline(admin.TabularInline):
    model = Account
    extra = 0
    fields = ("account_number", "account_type", "balance", "currency", "status")
    readonly_fields = ()
    show_change_link = True


class CardInline(admin.TabularInline):
    model = Card
    extra = 0
    fields = ("brand", "card_type", "last4", "status", "blocked_at")
    show_change_link = True


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "email", "date_of_birth")
    search_fields = ("full_name", "phone", "email")
    inlines = [AccountInline]


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_number",
        "customer",
        "account_type",
        "balance",
        "currency",
        "status",
        "failed_pin_attempts",
        "locked_until",
    )
    list_filter = ("status", "account_type", "currency")
    search_fields = ("account_number", "customer__full_name")
    readonly_fields = ("pin_hash",)
    inlines = [CardInline]
    actions = ["unlock_accounts"]

    @admin.action(description="Clear failed PIN attempts and unlock")
    def unlock_accounts(self, request, queryset):
        for account in queryset:
            account.reset_failed_attempts()
        self.message_user(request, f"Unlocked {queryset.count()} account(s).")


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ("last4", "brand", "card_type", "account", "status", "blocked_at")
    list_filter = ("status", "brand", "card_type")
    search_fields = ("last4", "account__account_number")


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_number",
        "account",
        "card",
        "category",
        "reason",
        "status",
        "created_at",
    )
    list_filter = ("status", "reason", "category")
    search_fields = ("ticket_number", "account__account_number")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "channel",
        "destination",
        "status",
        "provider",
        "ticket",
    )
    list_filter = ("channel", "status", "provider")
    search_fields = ("destination", "ticket__ticket_number")


@admin.register(AuthSession)
class AuthSessionAdmin(admin.ModelAdmin):
    list_display = ("token", "account", "level", "caller_id", "expires_at", "revoked_at")
    list_filter = ("level", "channel")
    search_fields = ("token", "account__account_number")


@admin.register(OtpChallenge)
class OtpChallengeAdmin(admin.ModelAdmin):
    list_display = ("created_at", "session", "purpose", "channel", "attempts", "consumed_at")
    list_filter = ("purpose", "channel")


@admin.register(ToolCallLog)
class ToolCallLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "tool_name",
        "ok",
        "http_status",
        "error_code",
        "duration_ms",
        "account",
    )
    list_filter = ("tool_name", "ok", "error_code")
    search_fields = ("tool_name", "error_code", "account__account_number")
    readonly_fields = [f.name for f in ToolCallLog._meta.fields]


@admin.register(ProviderSettings)
class ProviderSettingsAdmin(admin.ModelAdmin):
    """Raw access to the provider row. The dashboard form is the nicer way in."""

    list_display = ("email_provider", "sms_provider", "default_channel", "updated_at")

    def has_add_permission(self, request):
        # Singleton: it is created on first use by ProviderSettings.load().
        return not ProviderSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
