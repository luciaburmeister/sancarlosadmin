import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import translation
from django.utils.formats import number_format
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from invoices.services.discounts import ADVANCE_DEDUCTION_PREFIX

from .creation import AdvanceInvoiceService, advance_line_signature

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
# An invoice that leaves a VAT group's goods at or below this closes the
# group: it takes whatever is left of that group's advances.
CLOSING_REMAINDER = Decimal("0.01")


@dataclass
class InvoiceGoods:
    """What an invoice bills before any advance is deducted from it.

    - ``groups``: per expediente (pk), its goods' taxable base per VAT group,
      keyed like PricedLines.groups;
    - ``taxes``: the invoice's taxes, ``(tax_type pk, %) → (tax_type, %, base)``;
      a base of None stands for the whole taxable base;
    - ``taxable_base``: the lines less the header discount.
    """

    groups: dict
    taxes: dict
    taxable_base: Decimal


@dataclass
class DeductionRow:
    """One advance line an invoice can deduct: what the default plan deducts
    (``amount``) and the bounds any other amount must stay within."""

    line: SalesInvoiceItem  # the advance line; its invoice is the advance
    advance: dict  # its advance's entry in get_invoiceable_summary()['advances']
    project_file_id: int
    signature: frozenset  # its VAT group: the (TaxType, %) pairs it carries
    pending: Decimal  # not deducted yet, on any invoice
    minimum: Decimal = ZERO
    maximum: Decimal = ZERO
    amount: Decimal = ZERO

    @property
    def fixed(self):
        """Nothing to choose: the invoice deducts exactly ``amount``."""
        return self.minimum == self.maximum

    @property
    def taxes_label(self):
        return AdvanceInvoiceService._tax_label(self.signature)


class _Room:
    """How much deductions can still take off an invoice: every tax base,
    its taxable base and its total stay ≥ 0, and an untaxed advance line
    only takes off untaxed goods (never a taxed good's base)."""

    def __init__(self, goods):
        self.taxable = goods.taxable_base
        self.taxes = dict(goods.taxes)
        self.untaxed = sum(
            (groups.get(frozenset(), ZERO) for groups in goods.groups.values()), ZERO,
        )

    def total(self, signature=frozenset(), amount=ZERO):
        """The total once ``amount`` more of ``signature``'s group is
        deducted, rounded as SalesInvoice.recalculate_totals rounds it."""
        keys = _tax_keys(signature)
        taxable = self.taxable - amount
        total = taxable
        for key, (tax_type, pct, base) in self.taxes.items():
            if base is None:
                base = taxable
            elif key in keys:
                base -= amount
            tax = AdvanceInvoiceService._tax(base, pct)
            total += tax if tax_type.behavior == "add" else -tax
        return total

    def largest(self, signature, wanted):
        """The most (up to ``wanted``) a line of ``signature``'s group can deduct."""
        keys = _tax_keys(signature)
        limits = [wanted, self.taxable]
        if not signature:
            limits.append(self.untaxed)
        for key in keys:
            if key not in self.taxes:
                return ZERO  # no base of that tax on the invoice to lower
            base = self.taxes[key][2]
            limits.append(self.taxable if base is None else base)
        ceiling = max(min(limits), ZERO)
        amount = ceiling

        total = self.total()
        if total < 0:
            return ZERO
        # The total drops by about ``slope`` per euro deducted; a retention
        # (IRPF) can outweigh the other taxes of its base.
        slope = Decimal("1") + sum(
            (pct if tax_type.behavior == "add" else -pct) / Decimal("100")
            for key, (tax_type, pct, base) in self.taxes.items()
            if key in keys or base is None
        )
        if slope > 0:
            amount = min(amount, (total / slope).quantize(CENT, rounding=ROUND_DOWN))
        while amount > 0 and self.total(signature, amount) < 0:
            amount -= CENT
        # The estimate rounds down: take the cents the total still allows
        while amount + CENT <= ceiling and self.total(signature, amount + CENT) >= 0:
            amount += CENT
        return max(amount, ZERO)

    def take(self, signature, amount):
        if amount <= 0:
            return
        self.taxable -= amount
        if not signature:
            self.untaxed -= amount
        for key in _tax_keys(signature):
            tax_type, pct, base = self.taxes[key]
            if base is not None:
                self.taxes[key] = (tax_type, pct, base - amount)


def _tax_keys(signature):
    return {(tax_type.pk, pct) for tax_type, pct in signature}


_signature = advance_line_signature


class AdvanceDeductionService:
    """Keeps what an expediente's invoices bill within what it has to bill,
    and takes its advances off the invoices that bill its goods.

    The checks work on net (taxable) goods figures: advances are not goods
    and their deductions give the goods back, so tax rounding, retentions
    (IRPF) and advances never block a legitimate invoice.

    A deduction is a negative line pointing at the advance line it deducts
    (``advance_line``). It lowers the invoice's taxes of that line's VAT
    group by its amount, and is never part of a discount's base.

    Default plan, per expediente and VAT group: an invoice deducts the same
    share of the group's pending advances as the share of the group's goods
    left to invoice that it bills; the invoice that finishes a group's goods
    (or all of the expediente's) takes whatever is left of them. Any other
    amount must stay between
    - a minimum: what keeps the advances still open within the goods left
      after the invoice (on the closing invoice, everything pending), and
    - what is pending,
    and all of it within what the invoice can take: no tax base, taxable
    base or total below zero, oldest advance first. Only FINAL advances with
    no cancellation at the AEAT, of the invoice's company, client and
    currency, can be deducted: an invoice of other parties billing the goods
    of an expediente with such advances pending is refused.
    """

    # Share of the invoiceable base the goods may exceed it by: per-line
    # rounding across several partial invoices.
    TOLERANCE = Decimal("0.001")

    @classmethod
    def check(cls, summary, goods_net):
        """Refuse an invoice whose goods take an expediente past its base.

        Args:
            summary: get_invoiceable_summary() of the expediente, taken
                     before the invoice was written (under its row lock)
            goods_net: what the invoice bills of that expediente's goods:
                       its lines less their discount, without deductions

        Raises:
            ValidationError if the goods exceed goods_remaining_net by more
            than the tolerance.
        """
        invoiceable = summary['total_invoiceable_subtotal_net']
        over = goods_net - summary['goods_remaining_net']
        if over > invoiceable * cls.TOLERANCE:
            raise ValidationError(
                _("This invoice would exceed the invoiceable amount of %(project_file)s. "
                  "Invoiceable (taxable base): %(invoiceable)s, "
                  "already invoiced: %(invoiced)s, this invoice: %(amount)s, "
                  "over by: %(over)s") % {
                    'project_file': summary['project_file'].reference,
                    'invoiceable': invoiceable,
                    'invoiced': invoiceable - summary['goods_remaining_net'],
                    'amount': goods_net.quantize(Decimal("0.01")),
                    'over': over.quantize(Decimal("0.01")),
                }
            )

    @classmethod
    def preview_plan(cls, project_file, items_data, company, client):
        """The default plan (DeductionRows) of an invoice of ``items_data``
        on ``project_file`` that is not created yet; writes nothing.

        apply() recomputes it on the invoice once created: this is what the
        invoice wizard offers, not what apply() trusts.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        if summary['advances_deductible_net'] <= 0:
            return []
        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file, with_groups=True)
        priced = ProjectFileInvoiceService.price_lines(summary, items_data)
        # As the invoice will store it: lines to the cent
        lines_total = sum(
            (ProjectFileInvoiceService.line_total(item).quantize(CENT, rounding=ROUND_HALF_UP)
             for item in items_data),
            ZERO,
        )
        goods_net = lines_total - priced.resolution.discount_amount
        goods = InvoiceGoods(
            groups={
                project_file.pk: ProjectFileInvoiceService._reconcile_groups(priced.groups, goods_net),
            },
            taxes={
                (entry["tax_type"].pk, entry["selected_percentage"]): (
                    entry["tax_type"], entry["selected_percentage"], entry["base_amount"],
                )
                for entry in priced.tax_agg.values()
            },
            taxable_base=goods_net,
        )
        return cls._plan(
            goods, {project_file.pk: summary},
            company.pk, client.pk, SalesInvoice._meta.get_field("currency").default,
        )

    @classmethod
    @transaction.atomic
    def apply(cls, invoice, amounts=None):
        """Deducts, on the pro forma ``invoice``, the advances of the
        expedientes it bills. Runs once its goods lines, discount and taxes
        exist.

        Each deduction is a line of ``-amount`` pointing at the advance line
        (``advance_ded_<line pk>``); it carries the expediente only on a bulk
        invoice, whose lines say which expediente they bill. The invoice's
        taxes of the line's VAT group are lowered by the amount.

        Args:
            amounts: {advance line pk: amount}. None deducts the default
                     plan; {} nothing. Checked against the plan computed
                     here, under the locks: posted ids are never trusted.

        Returns:
            The deduction lines created.

        Raises:
            ValidationError if the invoice cannot deduct advances, an amount
            is not one the plan allows, or the invoice would go below zero.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        refused = _("Advances can only be deducted on a pro forma invoice that is not an advance itself.")
        if (
            invoice.status != SalesInvoiceStatus.PROFORMA
            or invoice.document_type != SalesInvoiceDocumentType.INVOICE
        ):
            raise ValidationError(refused)
        project_file_ids = ProjectFileInvoiceService.project_file_ids_of(invoice)
        # Expedientes, then their advances (the invoicing paths take both
        # before their numbering row)
        ProjectFileInvoiceService.lock_project_files(project_file_ids)
        AdvanceInvoiceService.lock_advances(project_file_ids)
        # It may have been issued while this request waited for the locks
        if cls._lock_status(invoice) != SalesInvoiceStatus.PROFORMA:
            raise ValidationError(refused)
        if invoice.items.filter(advance_line__isnull=False).exists():
            raise ValidationError(_("This invoice already deducts advances: remove them first."))

        rows = cls.default_plan(invoice, project_file_ids)
        chosen = cls._chosen(rows, amounts)

        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        invoice_taxes = {
            (tax.tax_type_id, tax.selected_percentage): tax
            for tax in AppliedTax.objects.filter(content_type=invoice_ct, object_id=invoice.pk)
        }
        created = []
        for row in rows:
            amount = chosen[row.line.pk]
            if amount <= 0:
                continue
            created.append(SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=cls._description(row),
                quantity=Decimal("1"),
                unit_price=-amount,
                source_id=f"{ADVANCE_DEDUCTION_PREFIX}{row.line.pk}",
                advance_line=row.line,
                project_file_id=None if invoice.project_file_id else row.project_file_id,
            ))
            # Its group's taxes are due on that much less
            for tax_type, pct in row.signature:
                tax = invoice_taxes[(tax_type.pk, pct)]
                if tax.base_amount is not None:
                    tax.base_amount = tax.base_amount.amount - amount
                    tax.save(update_fields=["base_amount"])
        if not created:
            return created

        invoice.recalculate_totals()
        cls.check_totals(invoice)
        logger.info(
            "SalesInvoice #%s deducts %s of advances (%s). Total: %s",
            invoice.pk,
            sum((-line.line_total.amount for line in created), ZERO),
            ", ".join(row.advance["invoice_number"] for row in rows if chosen[row.line.pk] > 0),
            invoice.total_amount,
        )
        return created

    @classmethod
    @transaction.atomic
    def remove(cls, invoice, lines=None):
        """Takes the advance deductions (all, or those of ``lines``) off the
        pro forma ``invoice``: their amounts go back onto the tax bases they
        lowered, and the advances are pending again.

        Returns:
            The amount put back.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        refused = _("Advance deductions can only be changed on a pro forma invoice.")
        if invoice.status != SalesInvoiceStatus.PROFORMA:
            raise ValidationError(refused)
        project_file_ids = ProjectFileInvoiceService.project_file_ids_of(invoice)
        ProjectFileInvoiceService.lock_project_files(project_file_ids)
        AdvanceInvoiceService.lock_advances(project_file_ids)
        # It may have been issued while this request waited for the locks
        if cls._lock_status(invoice) != SalesInvoiceStatus.PROFORMA:
            raise ValidationError(refused)

        deductions = invoice.items.filter(advance_line__isnull=False).prefetch_related(
            "advance_line__applied_taxes__tax_type",
        )
        if lines is not None:
            deductions = deductions.filter(pk__in=[line.pk for line in lines])
        deductions = list(deductions)
        if not deductions:
            return ZERO

        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        invoice_taxes = {
            (tax.tax_type_id, tax.selected_percentage): tax
            for tax in AppliedTax.objects.filter(content_type=invoice_ct, object_id=invoice.pk)
        }
        restored = ZERO
        for line in deductions:
            amount = -line.line_total.amount
            for tax_type, pct in _signature(line.advance_line):
                tax = invoice_taxes.get((tax_type.pk, pct))
                if tax is not None and tax.base_amount is not None:
                    tax.base_amount = tax.base_amount.amount + amount
                    tax.save(update_fields=["base_amount"])
            restored += amount
        SalesInvoiceItem.objects.filter(pk__in=[line.pk for line in deductions]).delete()
        invoice.recalculate_totals()
        return restored

    @staticmethod
    def _lock_status(invoice):
        """Row-lock the invoice (after its expedientes and their advances)
        and reload its status onto ``invoice``. Returns that status."""
        invoice.status = (
            SalesInvoice.objects.select_for_update()
            .values_list("status", flat=True).get(pk=invoice.pk)
        )
        return invoice.status

    @classmethod
    def check_for_finalize(cls, invoice, project_file_ids=None):
        """Refuse to issue a pro forma goods ``invoice`` whose advance
        deductions no longer hold. Run under the locks of its expedientes
        and their advances, on fresh totals.

        - no advance of the expedientes it bills is being cancelled at the
          AEAT (AdvanceInvoiceService.check_no_cancelling_advance): until
          the answer, whether it must be deducted is not known;
        - every advance it deducts can still be deducted on it (FINAL, no
          cancellation at the AEAT, its company, client and currency), and
          every deduction — or advance left out — is within its bounds: not
          beyond what is pending, and all of it on a closing invoice;
        - each tax base its deductions lowered is its goods' base at that
          rate less those deductions;
        - no tax base, taxable base or total below zero.

        An invoice that deducts nothing, of expedientes with nothing to
        deduct, is not checked: a manual credit note is issued as before.

        Raises:
            ValidationError naming what no longer holds.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
        from management.models import ProjectFile

        if project_file_ids is None:
            project_file_ids = ProjectFileInvoiceService.project_file_ids_of(invoice)
        lines = list(
            invoice.items.filter(advance_line__isnull=False)
            .select_related("advance_line__invoice")
            .prefetch_related("advance_line__applied_taxes__tax_type")
            .order_by("pk")
        )
        billed = cls._billed_project_file_ids(invoice, project_file_ids)
        if not lines and not ProjectFile.objects.filter(
            pk__in=billed, advance_pending_amount__gt=0,
        ).exists():
            return
        AdvanceInvoiceService.check_no_cancelling_advance(billed)

        summaries = {}
        for project_file in ProjectFile.objects.filter(pk__in=billed).order_by("pk"):
            summary = ProjectFileInvoiceService.get_invoiceable_summary(
                project_file, exclude_invoice=invoice,
            )
            if summary["advances_deductible_net"] > 0:
                summary = ProjectFileInvoiceService.get_invoiceable_summary(
                    project_file, with_groups=True, exclude_invoice=invoice,
                )
            elif not lines:
                continue
            summaries[project_file.pk] = summary

        deducted = {}  # advance line pk → what this invoice deducts of it
        for line in lines:
            deducted[line.advance_line_id] = (
                deducted.get(line.advance_line_id, ZERO) - line.line_total.amount
            )
        rows = []
        if summaries:
            goods = cls._invoice_goods(invoice, summaries)
            # Its client or currency may have been edited since it was priced
            cls._check_parties(invoice, goods, summaries)
            # Its taxes as they were before its deductions lowered them
            for line in lines:
                for key in _tax_keys(_signature(line.advance_line)):
                    if key in goods.taxes and goods.taxes[key][2] is not None:
                        tax_type, pct, base = goods.taxes[key]
                        goods.taxes[key] = (tax_type, pct, base - line.line_total.amount)
            rows = cls._plan(
                goods, summaries, invoice.company_id, invoice.client_id, invoice.currency,
            )

        offered = {row.line.pk for row in rows}
        for line in lines:
            if line.advance_line_id not in offered:
                raise ValidationError(
                    _("Advance %(advance)s can no longer be deducted on %(invoice)s: "
                      "remove the advances and reapply them.") % {
                        "advance": line.advance_line.invoice.display_number,
                        "invoice": invoice.display_number,
                    }
                )
        for row in rows:
            amount = deducted.get(row.line.pk, ZERO)
            if not row.minimum <= amount <= row.maximum:
                raise ValidationError(
                    _("%(invoice)s must deduct between %(minimum)s and %(maximum)s of advance "
                      "%(advance)s and deducts %(amount)s: reapply the advances.") % {
                        "invoice": invoice.display_number,
                        "minimum": row.minimum,
                        "maximum": row.maximum,
                        "advance": row.advance["invoice_number"],
                        "amount": amount,
                    }
                )
        if lines:
            cls._check_tax_bases(invoice, lines, summaries)
            cls.check_totals(invoice)

    @classmethod
    def _check_tax_bases(cls, invoice, lines, summaries):
        """Refuse an invoice whose deductions did not lower its taxes as
        they should: each tax base they lowered must be the goods' base at
        that rate — re-priced against the expedientes' quotes — less the
        deductions at that rate.

        Re-pricing a line is exact to the cent it was rounded to; lines that
        cannot be re-priced (manual ones) and a discount the quotes do not
        explain are allowed for.
        """
        from invoices.services.discounts import source_kind
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        goods_lines = list(invoice.items.filter(advance_line__isnull=True))
        expected = {}  # (tax_type pk, %) → the goods' base at that rate
        priced_discount = ZERO
        unpriced = {line.pk: abs(line.line_total.amount) for line in goods_lines}
        for project_file_id, summary in summaries.items():
            own = [
                line for line in goods_lines
                if invoice.project_file_id == project_file_id
                or line.project_file_id == project_file_id
            ]
            priced = ProjectFileInvoiceService.price_lines(
                summary, ProjectFileInvoiceService.repriceable_items(own),
            )
            priced_discount += priced.resolution.discount_amount
            for entry in priced.tax_agg.values():
                key = (entry["tax_type"].pk, entry["selected_percentage"])
                expected[key] = expected.get(key, ZERO) + entry["base_amount"]
            for line in own:
                if source_kind(line.source_id):
                    unpriced.pop(line.pk, None)
        discount = invoice.discount_amount.amount if invoice.discount_amount else ZERO
        tolerance = (
            CENT * max(len(goods_lines), 1)
            + sum(unpriced.values(), ZERO)
            + abs(priced_discount - discount)
        )

        lowered = {}  # (TaxType, %) → what the deductions took off that rate's base
        for line in lines:
            for tax in _signature(line.advance_line):
                lowered[tax] = lowered.get(tax, ZERO) - line.line_total.amount
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        bases = {
            (tax.tax_type_id, tax.selected_percentage): tax.base_amount
            for tax in AppliedTax.objects.filter(content_type=invoice_ct, object_id=invoice.pk)
        }
        for (tax_type, pct), amount in lowered.items():
            base = bases.get((tax_type.pk, pct))
            base = base.amount if base is not None else None
            want = expected.get((tax_type.pk, pct), ZERO) - amount
            if base is None or abs(base - want) > tolerance:
                raise ValidationError(
                    _("The %(tax)s base of %(invoice)s (%(base)s) is not its goods' base at that "
                      "rate less its advance deductions (%(expected)s): remove the advances, "
                      "correct the taxes and reapply them.") % {
                        "tax": AdvanceInvoiceService._tax_label(frozenset({(tax_type, pct)})),
                        "invoice": invoice.display_number,
                        "base": base if base is not None else "—",
                        "expected": want.quantize(CENT),
                    }
                )

    @classmethod
    def check_totals(cls, invoice):
        """Refuse an invoice its advance deductions take below zero: a tax
        base, its taxable base or its total."""
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        negative_base = AppliedTax.objects.filter(
            content_type=invoice_ct, object_id=invoice.pk, base_amount__lt=0,
        ).exists()
        if negative_base or invoice.taxable_base.amount < 0 or invoice.total_amount.amount < 0:
            raise ValidationError(
                _("The advance deductions would take %(invoice)s below zero: "
                  "a tax base, its taxable base or its total.") % {"invoice": invoice.display_number}
            )

    @staticmethod
    def deduction_lines(invoice):
        """The invoice's advance deduction lines, with their advances."""
        return list(
            invoice.items.filter(advance_line__isnull=False)
            .select_related("advance_line__invoice").order_by("pk")
        )

    @staticmethod
    def describe(lines):
        """``{"amount", "advances"}`` of deduction ``lines``, for messages:
        the amount deducted and the advances' numbers."""
        numbers = []
        for line in lines:
            number = line.advance_line.invoice.display_number
            if number not in numbers:
                numbers.append(number)
        return {
            "amount": number_format(sum((-line.line_total.amount for line in lines), ZERO), 2),
            "advances": ", ".join(numbers),
        }

    @classmethod
    def default_plan(cls, invoice, project_file_ids=None):
        """The default plan (DeductionRows) of an existing invoice that
        deducts nothing yet, against its expedientes as they stand without
        it; writes nothing."""
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
        from management.models import ProjectFile

        if project_file_ids is None:
            project_file_ids = ProjectFileInvoiceService.project_file_ids_of(invoice)
        billed = cls._billed_project_file_ids(invoice, project_file_ids)
        # Nothing pending on any of them (their cached figure, as
        # check_for_finalize reads it): no summary to price
        if not ProjectFile.objects.filter(pk__in=billed, advance_pending_amount__gt=0).exists():
            return []
        summaries = {}
        for project_file in ProjectFile.objects.filter(pk__in=billed).order_by("pk"):
            summary = ProjectFileInvoiceService.get_invoiceable_summary(
                project_file, exclude_invoice=invoice,
            )
            # The per-group figures are only worth pricing with something to deduct
            if summary["advances_deductible_net"] > 0:
                summaries[project_file.pk] = ProjectFileInvoiceService.get_invoiceable_summary(
                    project_file, with_groups=True, exclude_invoice=invoice,
                )
        if not summaries:
            return []
        goods = cls._invoice_goods(invoice, summaries)
        cls._check_parties(invoice, goods, summaries)
        return cls._plan(
            goods, summaries, invoice.company_id, invoice.client_id, invoice.currency,
        )

    @classmethod
    def _check_parties(cls, invoice, goods, summaries):
        """Refuse an invoice billing the goods of an expediente whose FINAL
        advances still pending are of another company, client or currency:
        it cannot deduct them, and would bill those goods twice.

        Raises:
            ValidationError naming the advances.
        """
        for project_file_id, summary in summaries.items():
            if sum(goods.groups.get(project_file_id, {}).values(), ZERO) <= 0:
                continue  # a credit note is issued as before
            line_ids = [
                entry["id"]
                for advance in summary["advances"] if advance["deductible"]
                for entry in advance["lines"] if entry["pending"] > 0
            ]
            if not line_ids:
                continue
            foreign = [
                advance.display_number
                for advance in SalesInvoice.objects.filter(items__pk__in=line_ids)
                .exclude(
                    company_id=invoice.company_id,
                    client_id=invoice.client_id,
                    currency=invoice.currency,
                ).distinct().order_by("pk")
            ]
            if foreign:
                raise ValidationError(
                    _("Advance %(advances)s of %(project_file)s was issued by another company, "
                      "to another client or in another currency: invoice its goods with the "
                      "advance's company, client and currency, or cancel the advance first.") % {
                        "advances": ", ".join(foreign),
                        "project_file": summary["project_file"].reference,
                    }
                )

    @staticmethod
    def _billed_project_file_ids(invoice, project_file_ids):
        """Of ``project_file_ids``, the expedientes whose goods the invoice
        bills: a bulk invoice also links those it skipped, their goods
        already invoiced."""
        billed = set(
            invoice.items.filter(advance_line__isnull=True, project_file__isnull=False)
            .values_list("project_file_id", flat=True)
        )
        if invoice.project_file_id:
            billed.add(invoice.project_file_id)
        return set(project_file_ids) & billed

    @classmethod
    def _invoice_goods(cls, invoice, summaries):
        """InvoiceGoods of an existing invoice that deducts nothing yet."""
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        lines = list(invoice.items.filter(advance_line__isnull=True).order_by("pk"))
        lines_total = sum((line.line_total.amount for line in lines), ZERO)
        discount = invoice.discount_amount.amount if invoice.discount_amount else ZERO
        groups = {}
        for project_file_id, summary in summaries.items():
            if invoice.project_file_id == project_file_id:
                own, goods_net = lines, lines_total - discount
            else:
                # A bulk invoice: this expediente's lines, counted as the
                # summary counts them
                own = [line for line in lines if line.project_file_id == project_file_id]
                goods_net = sum((line.line_total.amount for line in own), ZERO)
            groups[project_file_id] = ProjectFileInvoiceService.goods_net_by_group(
                summary, own, goods_net,
            )
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        taxes = {
            (tax.tax_type_id, tax.selected_percentage): (
                tax.tax_type,
                tax.selected_percentage,
                tax.base_amount.amount if tax.base_amount is not None else None,
            )
            for tax in AppliedTax.objects.filter(
                content_type=invoice_ct, object_id=invoice.pk,
            ).select_related("tax_type")
        }
        return InvoiceGoods(groups, taxes, lines_total - discount)

    @classmethod
    def _plan(cls, goods, summaries, company_id, client_id, currency):
        """The default plan: DeductionRows, oldest advance first, of the
        advance lines the invoice can deduct (see the class docstring)."""
        rows = cls._eligible_rows(summaries, company_id, client_id, currency)

        by_group = {}
        for row in rows:
            by_group.setdefault((row.project_file_id, row.signature), []).append(row)
        for (project_file_id, signature), group in by_group.items():
            summary = summaries[project_file_id]
            own = goods.groups.get(project_file_id, {})
            # An invoice that finishes the expediente's goods closes all of
            # its groups, whatever cents their own figures drifted by
            closes_all = (
                summary["goods_remaining_net"] - sum(own.values(), ZERO) <= CLOSING_REMAINDER
            )
            left = summary["goods_remaining_net_by_group"].get(signature, ZERO)
            billed = own.get(signature, ZERO)
            pending = sum((row.pending for row in group), ZERO)
            if closes_all or left - billed <= CLOSING_REMAINDER:
                minimum = target = pending
            else:
                # What the open advances need to still fit the goods left
                minimum = min(pending, max(ZERO, billed - (left - pending)))
                share = billed / left if left > 0 else Decimal("1")
                share = min(max(share, ZERO), Decimal("1"))
                target = max(minimum, (pending * share).quantize(CENT, rounding=ROUND_HALF_UP))
            minimums = cls._split(minimum, {row.line.pk: row.pending for row in group})
            extras = cls._split(
                target - minimum,
                {row.line.pk: row.pending - minimums[row.line.pk] for row in group},
            )
            for row in group:
                row.minimum = minimums[row.line.pk]
                row.amount = min(row.pending, row.minimum + extras[row.line.pk])

        # What the invoice can take, oldest advance first: a cap only binds
        # when the expediente shrank, and what it leaves stays pending.
        rows.sort(key=lambda row: (row.line.invoice_id, row.line.pk))
        room = _Room(goods)
        for row in rows:
            row.maximum = room.largest(row.signature, row.pending)
            row.minimum = min(row.minimum, row.maximum)
            row.amount = min(row.amount, row.maximum)
            room.take(row.signature, row.amount)
        return [row for row in rows if row.maximum > 0]

    @classmethod
    def _eligible_rows(cls, summaries, company_id, client_id, currency):
        """A DeductionRow (bounds not set) per advance line with something
        pending that an invoice of these parties can deduct."""
        wanted = []  # (project file pk, advance entry, line entry)
        for project_file_id, summary in summaries.items():
            for advance in summary["advances"]:
                if not advance["deductible"]:
                    continue
                for entry in advance["lines"]:
                    if entry["pending"] > 0:
                        wanted.append((project_file_id, advance, entry))
        if not wanted:
            return []

        lines = (
            SalesInvoiceItem.objects.select_related("invoice")
            .prefetch_related("applied_taxes__tax_type")
            .in_bulk([entry["id"] for _pf, _advance, entry in wanted])
        )
        rows = []
        for project_file_id, advance, entry in wanted:
            line = lines[entry["id"]]
            parties = (line.invoice.company_id, line.invoice.client_id, line.invoice.currency)
            if parties != (company_id, client_id, currency):
                continue
            rows.append(DeductionRow(
                line=line,
                advance=advance,
                project_file_id=project_file_id,
                signature=_signature(line),
                pending=entry["pending"],
            ))
        return rows

    @classmethod
    def _chosen(cls, rows, amounts):
        """{advance line pk: amount} to deduct: the plan's, or ``amounts``
        checked against it. Every line must be one the plan offers and every
        amount within its bounds; a line left out deducts nothing."""
        if amounts is None:
            return {row.line.pk: row.amount for row in rows}

        by_pk = {row.line.pk: row for row in rows}
        chosen = {}
        for pk, amount in amounts.items():
            row = by_pk.get(cls._pk(pk))
            if row is None:
                raise ValidationError(
                    _("Advance line %(line)s is not an advance this invoice can deduct.") % {"line": pk}
                )
            try:
                chosen[row.line.pk] = Decimal(str(amount or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
            except InvalidOperation:
                raise ValidationError(_("Please enter a valid amount."))
        for row in rows:
            amount = chosen.setdefault(row.line.pk, ZERO)
            if not row.minimum <= amount <= row.maximum:
                raise ValidationError(
                    _("The deduction of advance %(advance)s must be between %(minimum)s and "
                      "%(maximum)s; %(amount)s was entered.") % {
                        "advance": row.advance["invoice_number"],
                        "minimum": row.minimum,
                        "maximum": row.maximum,
                        "amount": amount,
                    }
                )
        return chosen

    @staticmethod
    def _pk(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _split(amount, weights):
        """``amount`` spread over ``weights`` in cents that add up to it."""
        if amount <= 0 or sum(weights.values(), ZERO) <= 0:
            return {key: ZERO for key in weights}
        return AdvanceInvoiceService._split(amount, weights)

    @staticmethod
    def _description(row):
        """The deduction line's text, stored on the invoice: in the
        documents' language, not the operator's."""
        advance = row.advance
        values = {
            "number": advance["invoice_number"],
            "date": advance["date_issued"].strftime("%d/%m/%Y"),
        }
        with translation.override(settings.LANGUAGE_CODE):
            if advance["percentage"] is not None:
                values["percentage"] = AdvanceInvoiceService._format_percentage(advance["percentage"])
                description = gettext(
                    "Less advance on invoice %(number)s of %(date)s (%(percentage)s %%)"
                ) % values
            else:
                description = gettext("Less advance on invoice %(number)s of %(date)s") % values
            # Which of the advance's lines, when it has several
            if len(advance["lines"]) > 1:
                description = f"{description} ({row.taxes_label})"
        return description
