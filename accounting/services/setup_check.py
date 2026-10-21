"""
What a company still needs before its invoices can be journalized.

``AccountingSetupCheck(company).run()`` returns sections of checks, each one
OK, a warning (works, but something will need attention) or missing
(journalizing fails until it's fixed). Read-only: it never changes data.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from django.contrib.contenttypes.models import ContentType
from django.db.models import Min, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from accounting.models import (
    AccountRole, BusinessUnit, CompanyAccount, CompanyChartOfAccounts, EntryStatus, FiscalYear, IncomeType,
)
from accounting.services.journal_generator import LEDGER_CURRENCY

OK, WARNING, MISSING = "ok", "warning", "missing"


@dataclass
class Check:
    label: str
    status: str
    detail: str = ""
    url: Optional[str] = None


@dataclass
class Section:
    title: str
    checks: List[Check] = field(default_factory=list)

    @property
    def status(self):
        statuses = {check.status for check in self.checks}
        return MISSING if MISSING in statuses else WARNING if WARNING in statuses else OK


class AccountingSetupCheck:
    def __init__(self, company):
        self.company = company
        self.chart = CompanyChartOfAccounts.objects.filter(company=company).select_related("template").first()

    def run(self) -> List[Section]:
        return [
            self._chart(),
            self._fiscal_year(),
            self._roles(),
            self._taxes(),
            self._business_units(),
            self._pending(),
        ]

    def is_ready(self, sections=None):
        return all(section.status != MISSING for section in (sections or self.run()))

    # -- sections -----------------------------------------------------------

    def _chart(self):
        section = Section(_("Chart of accounts"))
        if not self.chart:
            section.checks.append(Check(
                _("Template"), MISSING, _("No chart assigned: create one for this company."),
                reverse("admin:accounting_companychartofaccounts_add"),
            ))
            return section
        url = reverse("admin:accounting_companychartofaccounts_change", args=[self.chart.pk])
        installed = CompanyAccount.objects.filter(company=self.company, template_account__isnull=False).exists()
        section.checks += [
            Check(
                _("Template"), OK if installed else MISSING,
                self.chart.template.name if installed
                else _("%(template)s is assigned but not installed: press Install Chart on the company.")
                % {"template": self.chart.template.name},
                url,
            ),
            Check(
                _("Client / provider accounts"), OK,
                _("%(client)s… / %(provider)s…, %(length)s digits")
                % {"client": self.chart.client_account_root, "provider": self.chart.provider_account_root,
                   "length": self.chart.partner_account_length},
                url,
            ),
            Check(_("Rounding tolerance"), OK, str(self.chart.rounding_tolerance), url),
            Check(_("Lock date"), OK, str(self.chart.lock_date or _("none")), url),
        ]
        return section

    def _fiscal_year(self):
        today = timezone.localdate()
        year = FiscalYear.objects.filter(company=self.company, start_date__lte=today, end_date__gte=today).first()
        if not year:
            check = Check(_("Current fiscal year"), MISSING, _("None covers today: entries stay as drafts."),
                          reverse("admin:accounting_fiscalyear_add"))
        elif year.is_closed:
            check = Check(_("Current fiscal year"), MISSING, _("%(year)s is closed.") % {"year": year.name},
                          reverse("admin:accounting_fiscalyear_change", args=[year.pk]))
        else:
            check = Check(_("Current fiscal year"), OK, year.name,
                          reverse("admin:accounting_fiscalyear_change", args=[year.pk]))
        return Section(_("Fiscal year"), [check])

    def _roles(self):
        section = Section(_("Account roles"))
        holders = {
            account.role: account
            for account in CompanyAccount.objects.filter(company=self.company).exclude(role="")
        }
        sells_services = BusinessUnit.objects.filter(
            company=self.company, income_type=IncomeType.SERVICES, is_active=True,
        ).exists()
        needed = {
            AccountRole.GOODS_SALES: (MISSING, _("Every sales invoice needs it.")),
            AccountRole.SERVICES_SALES: (
                MISSING if sells_services else WARNING,
                _("A business unit sells services.") if sells_services else _("Needed once a business unit sells services."),
            ),
            AccountRole.SALES_DISCOUNTS: (WARNING, _("Needed by invoices with a discount.")),
            AccountRole.ROUNDING: (WARNING, _("Without it, cent differences leave entries as drafts.")),
        }
        accounts_url = reverse("admin:accounting_companyaccount_changelist")
        for role, (severity, why) in needed.items():
            label = str(AccountRole(role).label)
            account = holders.get(role)
            if account:
                section.checks.append(Check(
                    label, OK, str(account),
                    reverse("admin:accounting_companyaccount_change", args=[account.pk]),
                ))
            else:
                section.checks.append(Check(label, severity, why, accounts_url))
        return section

    def _taxes(self):
        """Every tax with an amount on this company's invoices, on the side it was used."""
        from invoices.models import AppliedTax, Invoice, SalesInvoice

        section = Section(_("Taxes"))
        used = {}
        for model, side in ((SalesInvoice, "sales"), (Invoice, "purchase")):
            tax_type_ids = AppliedTax.objects.filter(
                content_type=ContentType.objects.get_for_model(model),
                object_id__in=model.objects.filter(company=self.company).values("pk"),
            ).exclude(Q(calculated_amount=0) | Q(calculated_amount__isnull=True)).values_list("tax_type", flat=True)
            for tax_type_id in set(tax_type_ids):
                used.setdefault(tax_type_id, set()).add(side)
        if not used:
            section.checks.append(Check(_("Taxes in use"), OK, _("No taxed invoices yet.")))
            return section

        from invoices.models import TaxType

        for tax_type in TaxType.objects.filter(pk__in=used).select_related("sales_account", "purchase_account"):
            url = reverse("admin:invoices_taxtype_change", args=[tax_type.pk])
            for side in sorted(used[tax_type.pk], reverse=True):
                label = f"{tax_type.tax_name} · {_('sales') if side == 'sales' else _('purchases')}"
                template_acc = tax_type.sales_account if side == "sales" else tax_type.purchase_account
                if not template_acc:
                    section.checks.append(Check(label, MISSING, _("No account set on the tax type."), url))
                    continue
                account = CompanyAccount.objects.filter(
                    company=self.company, template_account=template_acc, is_postable=True, is_active=True,
                ).order_by("code").first()
                if account:
                    section.checks.append(Check(label, OK, str(account), url))
                else:
                    section.checks.append(Check(
                        label, MISSING,
                        _("Mapped to %(code)s, which this company doesn't have.") % {"code": template_acc.code},
                        url,
                    ))
        return section

    def _business_units(self):
        from configurator.models import ProductTemplate
        from inventory.models import InventoryItem

        section = Section(_("Business units"))
        default = BusinessUnit.default_for(self.company.pk)
        units_url = reverse("admin:accounting_businessunit_changelist")
        section.checks.append(
            Check(_("Default unit"), OK, str(default), reverse("admin:accounting_businessunit_change", args=[default.pk]))
            if default else
            Check(_("Default unit"), MISSING, _("Lines with no product or item behind them need one."), units_url)
        )
        for label, model, admin_name in (
            (_("Products without a unit"), ProductTemplate, "configurator_producttemplate"),
            (_("Inventory items without a unit"), InventoryItem, "inventory_inventoryitem"),
        ):
            count = model.objects.filter(company=self.company, business_unit__isnull=True).count()
            section.checks.append(Check(
                label, WARNING if count else OK,
                _("%(count)s will use the default unit.") % {"count": count} if count else _("All assigned."),
                reverse(f"admin:{admin_name}_changelist") + "?business_unit__isnull=True" if count else None,
            ))
        return section

    def _pending(self):
        """Issued invoices the ledger is missing, since the company's first sales entry."""
        from accounting.models import JournalEntry
        from invoices.models import SalesInvoice, SalesInvoiceStatus

        section = Section(_("Sales invoices"))
        since = JournalEntry.objects.filter(
            company=self.company, sales_invoice__isnull=False,
        ).aggregate(first=Min("date"))["first"]
        if not since:
            section.checks.append(Check(_("Journalized invoices"), OK, _("No sales invoice journalized yet.")))
            return section

        issued = SalesInvoice.objects.filter(
            company=self.company, status=SalesInvoiceStatus.FINAL, date_issued__gte=since,
        )
        foreign = issued.exclude(currency=LEDGER_CURRENCY).count()
        issued = issued.filter(currency=LEDGER_CURRENCY)
        without = issued.exclude(journal_entries__reversal_of__isnull=True).count()
        drafts = issued.filter(
            journal_entries__status=EntryStatus.DRAFT, journal_entries__reversal_of__isnull=True,
        ).distinct().count()
        entries_url = reverse("admin:accounting_journalentry_changelist")
        section.checks += [
            Check(
                _("Without a journal entry"), WARNING if without else OK,
                _("%(count)s since %(date)s: open each and press Generate Journal Entry.")
                % {"count": without, "date": since} if without else _("None."),
            ),
            Check(
                _("Draft entries"), WARNING if drafts else OK,
                _("%(count)s not posted.") % {"count": drafts} if drafts else _("None."),
                f"{entries_url}?status__exact={EntryStatus.DRAFT}" if drafts else None,
            ),
            Check(
                _("In another currency"), WARNING if foreign else OK,
                _("%(count)s can't be journalized: the ledger is in %(ledger)s.")
                % {"count": foreign, "ledger": LEDGER_CURRENCY} if foreign else _("None."),
            ),
        ]
        return section
