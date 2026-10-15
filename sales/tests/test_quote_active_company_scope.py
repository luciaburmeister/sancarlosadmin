"""
Quotes are scoped to the active company, superusers included.

A superuser is allowed into every company, but the switcher still decides
which of them they are working in right now — so the quote autocomplete, the
quote pickers of the configuration wizards and the quote dashboard all show
only the active company's quotes.

    python manage.py test sales.tests.test_quote_active_company_scope
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from configurator.forms.client_selection_form import ClientSelectionForm
from configurator.views.configuration.autocomplete_views import QuoteAutocompleteView
from sales.components.quote_dashboard_charts import _get_company_filtered_quotes
from sales.tests.factories import make_client, make_company, make_quote
from sales.views.quote_dashboard_view import QuoteDashboardView

User = get_user_model()


class QuoteActiveCompanyScopeTests(TestCase):
    def setUp(self):
        cache.clear()  # LocMem cache persists across tests in-process
        self.alpha = make_company(name="Alpha")
        self.beta = make_company(name="Beta")
        self.quote_alpha = make_quote(
            company=self.alpha, client=make_client(company=self.alpha, name="A client"), project_name="Alpha job",
        )
        self.quote_beta = make_quote(
            company=self.beta, client=make_client(company=self.beta, name="B client"), project_name="Beta job",
        )
        self.superuser = User.objects.create_superuser(
            username="root", email="root@example.com", password="x"
        )
        self.addCleanup(ActiveCompany.clear_current)

    def _request(self, company):
        ActiveCompany.set_current_ids([company.pk])
        request = RequestFactory().get("/")
        request.user = self.superuser
        return request

    def test_autocomplete_lists_only_the_active_company(self):
        view = QuoteAutocompleteView()
        view.request = self._request(self.alpha)

        self.assertEqual(list(view.get_queryset()), [self.quote_alpha])

    def test_configuration_wizard_picker_offers_only_the_active_company(self):
        request = self._request(self.beta)
        request.method = "POST"  # a GET starts empty for the autocomplete
        form = ClientSelectionForm(request=request)

        self.assertEqual(list(form.fields["quote"].queryset), [self.quote_beta])

    def test_dashboard_counts_only_the_active_company(self):
        view = QuoteDashboardView()
        view.request = self._request(self.alpha)

        self.assertEqual(list(view._get_quotes()), [self.quote_alpha])

    def test_dashboard_charts_count_only_the_active_company(self):
        quotes = _get_company_filtered_quotes(self._request(self.beta))

        self.assertEqual(list(quotes), [self.quote_beta])

    def test_changelist_renders_only_the_active_company(self):
        # The only test that renders the quote changelist, so it also guards
        # the list columns, filters and the expandable details section.
        self.client.force_login(self.superuser)
        self.client.post(
            reverse("admin:company_switch_active_apply"),
            {"companies": [self.alpha.pk]},
        )

        response = self.client.get(reverse("admin:sales_quote_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["cl"].queryset), [self.quote_alpha])
