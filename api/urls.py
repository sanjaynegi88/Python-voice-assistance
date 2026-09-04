from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("health", views.health, name="health"),
    path("tools/schema", views.tool_schema, name="tool-schema"),
    path("tools/authenticate", views.authenticate_caller, name="authenticate"),
    path("tools/get_balance", views.get_account_balance, name="get-balance"),
    path("tools/list_cards", views.list_cards, name="list-cards"),
    path("tools/send_otp", views.send_otp, name="send-otp"),
    path("tools/verify_otp", views.verify_otp, name="verify-otp"),
    path("tools/block_card", views.block_card, name="block-card"),
    path("tools/send_confirmation", views.send_confirmation, name="send-confirmation"),
]
