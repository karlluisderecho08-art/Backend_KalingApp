from django.apps import AppConfig


class MilkbankConfig(AppConfig):
    name = 'milkbank'

    def ready(self):
        # Connects the post_delete hook that removes a serology photo's
        # stored file along with its row.
        from . import signals  # noqa: F401
