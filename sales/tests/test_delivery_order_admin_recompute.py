"""
Editing a delivery order's lines in the admin re-derives its carried quote
discount and its tax bases; editing only the applied-tax inline leaves the
taxes the user just entered alone.

    python manage.py test sales.tests.test_delivery_order_admin_recompute
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.forms.widgets import MultiWidget
from django.test import TestCase
from django.urls import reverse

from invoices.tests.factories import (
    ExpedienteMixin,
    discounted_quote,
    do_line,
    financed_quote,
)
from sales.models import DeliveryOrderItem


class _AdminEditBase(ExpedienteMixin, TestCase):
    project_file_name = "PF Admin Edit"

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw",
        )
        self.client.force_login(self.superuser)

    def _financed_do(self):
        """1000 gross, 10% standard + 5% financing, 21% IVA → total 1028.50."""
        quote = financed_quote(project_file=self.project_file)
        config = self._make_config(quote, "1000.00", total=Decimal("1210.00"))
        do = self._create_do([do_line(config, unit_price="1000.00")])
        return do, quote

    def _change_url(self, do):
        return reverse("admin:sales_deliveryorder_change", args=[do.pk])

    def _post_data(self, do):
        """Round-trips the rendered change form back into POST data, so the
        submission carries every inline the admin actually renders.

        Returns ``(data, row_prefixes)``, where ``row_prefixes[(model, pk)]``
        is the field-name prefix of that object's inline row — pks repeat
        across inlines, so a row can only be addressed by model *and* pk.
        """
        response = self.client.get(self._change_url(do))
        self.assertEqual(response.status_code, 200)

        data = {}
        row_prefixes = {}

        def put(key, value):
            if value is None or value is False:
                return
            if value is True:
                data[key] = "on"
            elif isinstance(value, (list, tuple)):
                data[key] = [str(v) for v in value if v is not None]
            else:
                data[key] = str(value)

        def collect(form):
            for name, field in form.fields.items():
                if field.disabled:
                    continue
                bound = form[name]
                value = bound.value()
                widget = field.widget
                # MoneyField and friends render one input per subwidget
                # (amount + currency), each suffixed with its index.
                if isinstance(widget, MultiWidget):
                    for index, sub in enumerate(widget.decompress(value)):
                        put(f"{bound.html_name}_{index}", sub)
                else:
                    put(bound.html_name, value)

        collect(response.context["adminform"].form)

        for inline_formset in response.context["inline_admin_formsets"]:
            formset = inline_formset.formset
            mgmt = formset.management_form
            for name in mgmt.fields:
                data[mgmt[name].html_name] = str(mgmt[name].value() or 0)
            for form in formset.forms:
                collect(form)
                if form.instance.pk:
                    row_prefixes[(formset.model, form.instance.pk)] = form.prefix

        return data, row_prefixes

    def _row(self, row_prefixes, obj):
        """The POST field-name prefix of this object's inline row."""
        prefix = row_prefixes.get((type(obj), obj.pk))
        self.assertIsNotNone(prefix, f"{obj!r} is not rendered in the change form")
        return prefix

    def _assert_saved(self, response):
        """Asserts the admin accepted the POST, reporting validation errors."""
        if response.status_code != 302:
            problems = []
            context = getattr(response, "context", None) or {}
            adminform = context.get("adminform")
            if adminform is not None:
                problems.append(f"form: {adminform.form.errors.as_data()}")
            for inline in context.get("inline_admin_formsets", []):
                if inline.formset.errors or inline.formset.non_form_errors():
                    problems.append(
                        f"{inline.formset.prefix}: {inline.formset.errors} "
                        f"{inline.formset.non_form_errors()}"
                    )
            self.fail(f"admin rejected the POST ({response.status_code}): " + " | ".join(problems))

    def _goods_line(self, do):
        return do.items.get()


class AdminLineEditRecomputeTests(_AdminEditBase):

    def test_halving_a_line_in_admin_rescales_discount_and_tax_base(self):
        do, quote = self._financed_do()
        self.assertEqual(do.total_amount.amount, Decimal("1028.50"))

        data, rows = self._post_data(do)
        data[f"{self._row(rows, self._goods_line(do))}-unit_price_0"] = "500.00"

        response = self.client.post(self._change_url(do), data)
        self._assert_saved(response)

        do.refresh_from_db()
        self.assertEqual(self._goods_line(do).line_total.amount, Decimal("500.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("75.00"))
        self.assertEqual(do.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(do.applied_taxes.get().base_amount.amount, Decimal("425.00"))
        self.assertEqual(do.total_amount.amount, Decimal("514.25"))

    def test_saving_without_touching_lines_keeps_manual_tax_edits(self):
        do, quote = self._financed_do()
        data, rows = self._post_data(do)

        # Only the applied-tax inline changes: drop IVA to 10%
        tax = do.applied_taxes.get()
        data[f"{self._row(rows, tax)}-selected_percentage"] = "10"

        response = self.client.post(self._change_url(do), data)
        self._assert_saved(response)

        do.refresh_from_db()
        applied = do.applied_taxes.get()
        self.assertEqual(applied.selected_percentage, Decimal("10.00"))
        self.assertEqual(applied.base_amount.amount, Decimal("850.00"))
        self.assertEqual(applied.calculated_amount.amount, Decimal("85.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(do.total_amount.amount, Decimal("935.00"))


class AdminLegacyDeliveryOrderTests(_AdminEditBase):
    """A legacy delivery order is priced net of the quote discount; editing it
    must not start carrying that discount a second time."""

    def _legacy_do(self):
        from sales.models import DeliveryOrder, DeliveryOrderStatus

        quote = discounted_quote(project_file=self.project_file, kind="financing")
        config = self._make_config(
            quote, "1000.00", total=Decimal("1210.00"), tax_pct=None,
        )
        do = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.DRAFT,
            subtotal_amount=Decimal("900.00"),
        )
        DeliveryOrderItem.objects.create(
            delivery_order=do, description="Config (net)",
            quantity=Decimal("1"), unit_price=Decimal("900.00"),
            source_content_type=self.config_ct, source_object_id=config.pk,
        )
        return do

    def test_editing_a_legacy_do_does_not_add_a_discount(self):
        do = self._legacy_do()
        data, rows = self._post_data(do)
        data[f"{self._row(rows, do.items.get())}-unit_price_0"] = "450.00"

        response = self.client.post(self._change_url(do), data)
        self._assert_saved(response)

        do.refresh_from_db()
        self.assertEqual(do.items.count(), 1)
        self.assertEqual(do.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(do.subtotal_amount.amount, Decimal("450.00"))
        self.assertEqual(do.total_amount.amount, Decimal("450.00"))
