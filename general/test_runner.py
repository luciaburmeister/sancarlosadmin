"""
Project test runner: every test starts with the default language active.

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

from django.test.runner import (
    DiscoverRunner,
    ParallelTestSuite,
    RemoteTestResult,
    RemoteTestRunner,
)
from django.utils import translation


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

    def get_resultclass(self):
        # Serial runs (and --debug-sql / --pdb, which pick their own result
        # class) get the same reset mixed in front of whatever Django chose.
        base = super().get_resultclass() or unittest.TextTestResult
        return type(f"ResetLanguage{base.__name__}", (ResetLanguageMixin, base), {})
