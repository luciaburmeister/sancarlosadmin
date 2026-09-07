"""
Project test runner.

Two jobs:

1. **Postgres extensions for a migration-less test database.** Settings build
   the test database with ``TEST["MIGRATE"] = False`` (tables come straight
   from the models, which takes seconds instead of the minutes the ~340
   migrations need). The ``pg_trgm`` extension is normally created by
   ``banking.0006_enable_pg_trgm``, but without migrations it has to exist
   before ``migrate --run-syncdb`` creates the trigram GIN indexes on the
   invoice models. Doing it after ``setup_databases()`` returns is too late
   (syncdb has already failed), so the runner listens to ``connection_created``
   while the databases are being set up and creates the extension on the
   freshly created test database as soon as Django connects to it.

2. **Every test starts with the default language active.**

``LocaleMiddleware`` activates the language named by the URL prefix
(``i18n_patterns``) and never restores it, so a test that requests
``/en/api/...`` leaves ``en`` active in that process for every test that runs
after it: ``reverse()`` then builds ``/en/...`` URLs and translated output
comes back in English. Under ``--parallel`` which classes share a worker is
decided at run time, so those were order-dependent flakes.

Resetting the translation state in ``startTest`` (the hook both the serial and
the parallel-worker result classes go through) makes each test independent of
whatever language its predecessors left behind.
"""
import unittest

from django.db.backends.base.base import NO_DB_ALIAS
from django.db.backends.signals import connection_created
from django.test.runner import (
    DiscoverRunner,
    ParallelTestSuite,
    RemoteTestResult,
    RemoteTestRunner,
)
from django.utils import translation

# Extensions the schema needs that migrations would otherwise provide.
TEST_DB_EXTENSIONS = ("pg_trgm",)


def create_test_db_extensions(sender, connection, **kwargs):
    # Django creates/clones/drops test databases through a maintenance
    # connection to the ``postgres`` database (alias NO_DB_ALIAS); leave that
    # one alone and only touch the test database itself.
    if connection.alias == NO_DB_ALIAS or connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        for extension in TEST_DB_EXTENSIONS:
            cursor.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")


class ResetLanguageMixin:
    def startTest(self, test):
        translation.deactivate()  # back to settings.LANGUAGE_CODE
        super().startTest(test)


class ResetLanguageRemoteTestResult(ResetLanguageMixin, RemoteTestResult):
    pass


class ResetLanguageRemoteTestRunner(RemoteTestRunner):
    resultclass = ResetLanguageRemoteTestResult


class ResetLanguageParallelTestSuite(ParallelTestSuite):
    runner_class = ResetLanguageRemoteTestRunner


class ProjectTestRunner(DiscoverRunner):
    parallel_test_suite = ResetLanguageParallelTestSuite

    def setup_databases(self, **kwargs):
        # Only listen while the test databases are being built: the first
        # connection to the new test database fires the signal before
        # ``migrate --run-syncdb`` creates any table or index. Parallel
        # workers clone that database, so they inherit the extension.
        connection_created.connect(create_test_db_extensions)
        try:
            return super().setup_databases(**kwargs)
        finally:
            connection_created.disconnect(create_test_db_extensions)

    def get_resultclass(self):
        # Serial runs (and --debug-sql / --pdb, which pick their own result
        # class) get the same reset mixed in front of whatever Django chose.
        base = super().get_resultclass() or unittest.TextTestResult
        return type(f"ResetLanguage{base.__name__}", (ResetLanguageMixin, base), {})
