from django.apps import AppConfig


class DownloaderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "downloader"

    def ready(self):
        from .instagram import cleanup_old_jobs

        cleanup_old_jobs()
