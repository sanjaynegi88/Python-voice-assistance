from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="dashboard/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.overview, name="overview"),
    path("customers/", views.customers, name="customers"),
    path("customers/<int:pk>/", views.customer_detail, name="customer-detail"),
    path("accounts/", views.accounts, name="accounts"),
    path("tickets/", views.tickets, name="tickets"),
    path("notifications/", views.notifications, name="notifications"),
    path("logs/", views.tool_logs, name="tool-logs"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/providers/", views.providers_view, name="providers"),
    path("settings/tunnel/", views.tunnel_view, name="tunnel"),
]
