from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/health", views.health, name="health"),
    path("api/download", views.api_download, name="download"),
    path("api/media/<str:job_id>/<str:filename>", views.media, name="media"),
]
