from django import forms
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from company.models import CompanyModule
from general.onboarding import MISSING, OK, WARNING, Check, Column, ImportPrompt, OnboardingStep, Section, register
from general.onboarding.forms import UnfoldFormMixin
from inventory.choices import ItemType
from inventory.models import InventoryItem, Warehouse


class WarehouseForm(UnfoldFormMixin, forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ["code", "name", "address_line1", "city", "postal_code"]

    def __init__(self, *args, company, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def clean_code(self):
        code = self.cleaned_data["code"]
        if Warehouse.objects.filter(company=self.company, code=code).exists():
            raise forms.ValidationError(_("Another warehouse already has this code."))
        return code

    def save(self, commit=True):
        self.instance.company = self.company
        return super().save(commit)


@register
class InventoryStep(OnboardingStep):
    key = "inventory"
    title = gettext_lazy("Warehouses and items")
    description = gettext_lazy(
        "Where stock is kept, and the items you buy and sell. Stock items need a warehouse, "
        "so add one before importing them."
    )
    order = 60
    module = CompanyModule.INVENTORY
    form_class = WarehouseForm
    submit_label = gettext_lazy("Add warehouse")

    def get_form_kwargs(self):
        return {"company": self.company}

    def sections(self):
        warehouses = Section(_("Warehouses"), [
            Check(f"{warehouse.code} {warehouse.name}", OK, warehouse.city)
            for warehouse in Warehouse.objects.filter(company=self.company, is_active=True).order_by("code")
        ])
        if not warehouses.checks:
            warehouses.checks.append(Check(_("Warehouses"), MISSING, _("Stock items can't be created without one.")))

        count = InventoryItem.objects.filter(company=self.company).count()
        import_url = reverse("admin:inventory_inventoryitem_import")
        items = Section(_("Items"), [
            Check(_("Items"), OK, _("%(count)s items.") % {"count": count}, import_url)
            if count else
            Check(_("Items"), WARNING, _("None yet: import them from a spreadsheet."), import_url),
        ])
        return [warehouses, items]

    def import_prompts(self):
        from inventory.resources import InventoryItemResource

        # The import reads headers in the user's language
        header = {field.attribute: str(field.column_name) for field in InventoryItemResource().get_import_fields()}
        warehouses = ", ".join(
            Warehouse.objects.filter(company=self.company, is_active=True).order_by("code").values_list("code", flat=True)
        )
        item_types = "; ".join(f"{value} ({label})" for value, label in ItemType.choices)
        return [ImportPrompt(
            title=_("Items"),
            rows=_("one row per item"),
            columns=[
                Column(header["id"], _("always empty: these are new items.")),
                Column(header["company"], _('always exactly "%(name)s".') % {"name": self.company.name}, required=True),
                Column(header["code"], _("the item's reference in the previous system, at most 20 characters.")),
                Column(header["name"], _("the item's name."), required=True),
                Column(header["description"], _("a longer description, if there is one.")),
                Column(header["barcode"], _("the EAN/UPC barcode, digits only, if there is one.")),
                Column(header["item_type"], _("one of these values, written as shown: %(types)s. Use %(default)s "
                                              "when unsure.") % {"types": item_types, "default": ItemType.STOCKABLE}),
                Column(header["default_warehouse"],
                       _("where it's stored: one of these codes: %(codes)s.") % {"codes": warehouses}
                       if warehouses else _("leave empty.")),
                Column(header["selling_price"], _("selling price per unit, before tax, in euros.")),
                Column(header["cost_price"], _("purchase or cost price per unit, before tax, in euros.")),
                Column(header["min_stock"], _("whole number of units below which to reorder, if known.")),
            ],
            rules=[_("Don't include stock quantities: stock is counted in the ERP once the items exist.")],
        )]

    def save(self, form, request):
        warehouse = form.save()
        return _("Warehouse %(name)s added.") % {"name": warehouse.name}
