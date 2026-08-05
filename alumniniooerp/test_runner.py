from django.test.runner import DiscoverRunner


class ConsoleEmailTestRunner(DiscoverRunner):
    """Prints outgoing emails to the terminal during test runs.

    Django's own setup_test_environment() forces EMAIL_BACKEND to locmem
    (silently collected in mail.outbox) regardless of settings — override
    it back to the console backend after that runs so emails are visible.
    """

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        from django.conf import settings

        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
