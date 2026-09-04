from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import include, path

urlpatterns = [
    path("", lambda request: HttpResponseRedirect("/dashboard/")),
    path("dashboard/", include("dashboard.urls")),
    path("api/v1/", include("api.urls")),
    path("django-admin/", admin.site.urls),
]
