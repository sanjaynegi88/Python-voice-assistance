"""
Form for the editable provider credentials.

Secrets are write-only: the field renders empty with a "leave blank to keep"
hint, and a blank submission preserves whatever is already stored. That way the
page never puts an API key back into an HTTP response.
"""
from django import forms

from bank.models import ProviderSettings


class ProviderSettingsForm(forms.ModelForm):
    class Meta:
        model = ProviderSettings
        fields = [
            "email_provider",
            "sms_provider",
            "default_channel",
            "brevo_api_key",
            "brevo_sender_email",
            "brevo_sender_name",
            "brevo_sms_sender",
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_password",
            "smtp_use_tls",
            "smtp_from_email",
            "twilio_account_sid",
            "twilio_auth_token",
            "twilio_from_number",
        ]
        widgets = {
            "brevo_api_key": forms.PasswordInput(render_value=False),
            "smtp_password": forms.PasswordInput(render_value=False),
            "twilio_auth_token": forms.PasswordInput(render_value=False),
        }
        labels = {
            "email_provider": "Email provider",
            "sms_provider": "SMS provider",
            "default_channel": "Default channel",
            "brevo_api_key": "API key",
            "brevo_sender_email": "Sender email",
            "brevo_sender_name": "Sender name",
            "brevo_sms_sender": "SMS sender ID",
            "smtp_host": "Host",
            "smtp_port": "Port",
            "smtp_user": "Username",
            "smtp_password": "Password",
            "smtp_use_tls": "Use TLS",
            "smtp_from_email": "From address",
            "twilio_account_sid": "Account SID",
            "twilio_auth_token": "Auth token",
            "twilio_from_number": "From number",
        }
        help_texts = {
            "brevo_sender_email": "Must be a verified sender in your Brevo account.",
            "brevo_sms_sender": "Up to 11 characters, letters and digits.",
            "twilio_from_number": "E.164, e.g. +14155550123.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ProviderSettings.SECRET_FIELDS:
            field = self.fields[name]
            field.required = False
            stored = getattr(self.instance, name, "")
            field.widget.attrs["placeholder"] = (
                "•••••••• stored — leave blank to keep"
                if stored
                else "not set"
            )
            field.widget.attrs["autocomplete"] = "new-password"

    def clean(self):
        cleaned = super().clean()

        # A blank secret means "unchanged", not "delete".
        for name in ProviderSettings.SECRET_FIELDS:
            if not cleaned.get(name):
                cleaned[name] = getattr(self.instance, name, "")

        self._require_for(
            cleaned,
            active=cleaned.get("email_provider") == "brevo"
            or cleaned.get("sms_provider") == "brevo",
            fields=["brevo_api_key"],
            message="Brevo is selected but no API key is stored.",
        )
        self._require_for(
            cleaned,
            active=cleaned.get("email_provider") == "brevo",
            fields=["brevo_sender_email"],
            message="Brevo email needs a verified sender address.",
        )
        self._require_for(
            cleaned,
            active=cleaned.get("email_provider") == "smtp",
            fields=["smtp_host", "smtp_from_email"],
            message="SMTP needs a host and a from address.",
        )
        self._require_for(
            cleaned,
            active=cleaned.get("sms_provider") == "twilio",
            fields=["twilio_account_sid", "twilio_auth_token", "twilio_from_number"],
            message="Twilio needs an account SID, auth token and from number.",
        )
        return cleaned

    def _require_for(self, cleaned, *, active, fields, message):
        if not active:
            return
        for name in fields:
            if not cleaned.get(name):
                self.add_error(name, message)
