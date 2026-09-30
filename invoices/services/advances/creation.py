import logging
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

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
from invoices.services.discounts import ADVANCE_LINE_PREFIX
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
PERCENTAGE_STEP = Decimal("0.0001")  # SalesInvoice.advance_percentage keeps 4 decimals


def advance_line_signature(line):
    """An advance line's VAT group, as PricedLines.groups keys it: the
    (TaxType, %) pairs of its own taxes."""
    return frozenset(
        (tax.tax_type, tax.selected_percentage)
        for tax in line.applied_taxes.all()
        if tax.selected_percentage > 0
    )


class AdvanceInvoiceService:
    """Issues advance invoices: a share of an expediente's net taxable base,
    billed before its goods.

    The share applies to the base after the quotes' discounts and before
    taxes (the figure every expediente total uses), so the advance carries
    no discount of its own. It has one line per VAT group — the set of taxes
    a goods line carries — and each line keeps its group's taxes as per-line
    AppliedTax rows, which is what a later invoice deducts it by.

    Advances count toward the cap as soon as they exist, pro formas
    included: goods and advances together never exceed the expediente.
    """

    @classmethod
    def preview(cls, project_file, percentage=None, amount=None, summary=None):
        """What create() would issue, as JSON-ready data; writes nothing.

        ``error`` says why it cannot be issued (None when it can);
        ``max_amount``, the largest advance still allowed, comes either way.
        """
        plan = cls._plan(project_file, summary, percentage, amount)
        return {
            "error": plan["error"],
            "percentage": plan["percentage"],
            "net_base": plan["net_base"],
            "max_amount": plan["max_amount"],
            "lines": [
                {"description": line["description"], "taxes": line["tax_label"], "base": line["base"]}
                for line in plan["lines"]
            ],
            "taxes": [
                {
                    "name": tax["tax_type"].tax_name,
                    "percentage": tax["selected_percentage"],
                    "retention": tax["tax_type"].behavior != "add",
                    "base": tax["base_amount"],
                    "amount": tax["calculated_amount"],
                }
                for tax in plan["taxes"]
            ],
            "base": plan["base"],
            "tax_amount": plan["tax_amount"],
            "total": plan["total"],
        }

    @classmethod
    @transaction.atomic
    def create(cls, project_file, date_issued, due_date=None, percentage=None, amount=None,
               notes="", payment_method=None):
        """Creates a pro forma advance invoice on ``project_file``.

        Args:
            percentage: share (%) of the expediente's net taxable base
            amount: the advance's base in € instead; it is split across the
                    VAT groups and its percentage derived. Wins over
                    ``percentage`` when both are given.

        The invoice is always the expediente's company and client.

        Raises:
            ValidationError if the input is not valid or the advance exceeds
            the largest advance still allowed.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        ProjectFileInvoiceService.lock_project_files([project_file.pk])
        cls.lock_advances([project_file.pk])
        plan = cls._plan(project_file, None, percentage, amount)
        if plan["error"]:
            raise ValidationError(plan["error"])

        client = project_file.client
        invoice = SalesInvoice.objects.create(
            company=project_file.company,
            status=SalesInvoiceStatus.PROFORMA,
            document_type=SalesInvoiceDocumentType.ADVANCE,
            advance_percentage=plan["percentage"],
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=project_file,
            notes=notes,
            payment_method=resolve_sales_invoice_payment_method(
                client=client, explicit=payment_method,
            ),
        )

        # One line per VAT group, carrying its own taxes. The expediente is
        # on the invoice, so the lines leave project_file empty.
        item_ct = ContentType.objects.get_for_model(SalesInvoiceItem)
        order_of = {
            (tax["tax_type"].pk, tax["selected_percentage"]): order
            for order, tax in enumerate(plan["taxes"])
        }
        for line in plan["lines"]:
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=line["description"],
                quantity=Decimal("1"),
                unit_price=line["base"],
                source_id=f"{ADVANCE_LINE_PREFIX}{project_file.pk}",
            )
            for tax_type, pct in line["taxes"]:
                AppliedTax.objects.create(
                    content_type=item_ct,
                    object_id=item.pk,
                    tax_type=tax_type,
                    selected_percentage=pct,
                    base_amount=line["base"],
                    calculated_amount=cls._tax(line["base"], pct),
                    order=order_of[(tax_type.pk, pct)],
                )

        # Invoice-level taxes: each on the groups that carry it
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        for order, tax in enumerate(plan["taxes"]):
            AppliedTax.objects.create(
                content_type=invoice_ct,
                object_id=invoice.pk,
                tax_type=tax["tax_type"],
                selected_percentage=tax["selected_percentage"],
                base_amount=tax["base_amount"],
                order=order,
            )

        # Retentions (IRPF) are subtracted here
        invoice.recalculate_totals()

        logger.info(
            "Created advance SalesInvoice #%s (%s %%) from ProjectFile %s. "
            "Amount: %s (base: %s, tax: %s)",
            invoice.id, plan["percentage"], project_file.reference,
            invoice.total_amount, invoice.subtotal_amount, invoice.tax_amount,
        )
        return invoice

    @classmethod
    def check_cap(cls, invoice):
        """Refuse to issue an advance that no longer fits its expediente —
        goods invoiced or other advances issued since, or the expediente
        shrank. Run under the expediente's lock, before it is finalized.

        Raises:
            ValidationError if its base exceeds what is neither invoiced as
            goods nor taken by the other open advances, in total or in one
            of its VAT groups.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        summary = ProjectFileInvoiceService.get_invoiceable_summary(
            invoice.project_file, with_groups=True,
        )
        advance = next((a for a in summary["advances"] if a["id"] == invoice.pk), None)
        if advance is None:
            return
        allowed = summary["goods_remaining_net"] - (summary["advances_open_net"] - advance["pending"])
        if advance["base"] > allowed:
            raise ValidationError(cls._cap_error(
                invoice.project_file, advance["base"], max(allowed, ZERO),
            ))
        # Nor more of a VAT group than the group has left
        room = cls._room_by_group(summary, exclude_invoice_id=invoice.pk)
        for line in invoice.items.prefetch_related("applied_taxes__tax_type").order_by("pk"):
            signature = advance_line_signature(line)
            if line.line_total.amount > room.get(signature, ZERO):
                raise ValidationError(_(
                    "The %(taxes)s part of the advance (%(amount)s) exceeds what that VAT group "
                    "of %(project_file)s has left to invoice (%(max)s)."
                ) % {
                    "taxes": cls._tax_label(signature),
                    "amount": line.line_total.amount,
                    "project_file": invoice.project_file.reference or invoice.project_file.name,
                    "max": max(room.get(signature, ZERO), ZERO),
                })

    @classmethod
    def lock_advances(cls, project_file_ids):
        """Row-lock the expedientes' advance invoices, in pk order.

        Taken after the expedientes (ProjectFileInvoiceService.
        lock_project_files) and before any numbering row.
        """
        ids = sorted({pk for pk in project_file_ids if pk})
        if ids:
            list(
                SalesInvoice.objects.select_for_update()
                .filter(project_file_id__in=ids, document_type=SalesInvoiceDocumentType.ADVANCE)
                .exclude(status=SalesInvoiceStatus.CANCELLED)
                .order_by("pk").values_list("pk", flat=True)
            )

    @classmethod
    def check_no_proforma_advance(cls, project_file_ids):
        """Refuse to invoice the goods of expedientes with an advance still
        a pro forma. It cannot be deducted yet (it has no legal number), and
        finalizing it after the goods were invoiced in full would bill them
        twice. Nor while an advance's cancellation at the AEAT is in flight
        (check_no_cancelling_advance). Run under the expedientes' locks.

        Raises:
            ValidationError naming the pro forma advances.
        """
        ids = {pk for pk in project_file_ids if pk}
        if not ids:
            return
        proformas = SalesInvoice.objects.filter(
            project_file_id__in=ids,
            document_type=SalesInvoiceDocumentType.ADVANCE,
            status=SalesInvoiceStatus.PROFORMA,
        ).order_by("pk")
        numbers = [invoice.display_number for invoice in proformas]
        if numbers:
            raise ValidationError(cls.proforma_advance_error(numbers))
        cls.check_no_cancelling_advance(ids)

    @classmethod
    def check_no_cancelling_advance(cls, project_file_ids):
        """Refuse to invoice or issue the goods of expedientes with a final
        advance whose cancellation at the AEAT is queued or sent with no
        answer yet (the summary's ``cancelling``). It cannot be deducted
        meanwhile, and a rejected or failed cancellation leaves it to deduct
        again: goods invoiced in full in between would bill it twice. Run
        under the expedientes' locks.

        Raises:
            ValidationError naming those advances.
        """
        from apps.einvoicing.models import SubmissionAction, SubmissionStatus

        ids = {pk for pk in project_file_ids if pk}
        if not ids:
            return
        cancelling = SalesInvoice.objects.filter(
            project_file_id__in=ids,
            document_type=SalesInvoiceDocumentType.ADVANCE,
            status=SalesInvoiceStatus.FINAL,
            einvoicing_submissions__action=SubmissionAction.CANCEL,
            einvoicing_submissions__status__in=[
                SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED,
            ],
        ).distinct().order_by("pk")
        numbers = [invoice.display_number for invoice in cancelling]
        if numbers:
            raise ValidationError(cls.cancelling_advance_error(numbers))

    @classmethod
    def goods_block_error(cls, summary):
        """Why check_no_proforma_advance refuses the goods of the expediente
        whose invoiceable summary is ``summary``, or None when it does not."""
        proformas = [
            advance["invoice_number"] for advance in summary["advances"]
            if advance["status"] == SalesInvoiceStatus.PROFORMA
        ]
        if proformas:
            return cls.proforma_advance_error(proformas)
        cancelling = [
            advance["invoice_number"] for advance in summary["advances"] if advance["cancelling"]
        ]
        if cancelling:
            return cls.cancelling_advance_error(cancelling)
        return None

    @staticmethod
    def proforma_advance_error(numbers):
        """Why goods cannot be invoiced while advances ``numbers`` are pro formas."""
        return _(
            "Advance %(advances)s is still a pro forma: finalize or delete it before "
            "invoicing the goods of this project file."
        ) % {"advances": ", ".join(numbers)}

    @staticmethod
    def cancelling_advance_error(numbers):
        """Why goods cannot be invoiced while advances ``numbers`` are being
        cancelled at the AEAT."""
        return _(
            "The cancellation of advance %(advances)s at the AEAT is in progress: wait for it "
            "to finish before invoicing the goods of this project file."
        ) % {"advances": ", ".join(numbers)}

    @staticmethod
    def _summary(project_file, summary=None):
        """The expediente's invoiceable summary with its VAT groups (reusing
        ``summary`` when it has them)."""
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        if summary is not None and "goods_remaining_net_by_group" in summary:
            return summary
        return ProjectFileInvoiceService.get_invoiceable_summary(project_file, with_groups=True)

    @staticmethod
    def _room_by_group(summary, exclude_invoice_id=None):
        """What each VAT group can still be advanced: its goods not invoiced
        yet less what the open advances (pro formas included, bar
        ``exclude_invoice_id``) have pending in it. A with_groups summary."""
        room = dict(summary["goods_remaining_net_by_group"])
        pending = {
            entry["id"]: entry["pending"]
            for advance in summary["advances"] if advance["id"] != exclude_invoice_id
            for entry in advance["lines"] if entry["pending"]
        }
        for line in SalesInvoiceItem.objects.filter(pk__in=pending).prefetch_related(
            "applied_taxes__tax_type",
        ):
            signature = advance_line_signature(line)
            room[signature] = room.get(signature, ZERO) - pending[line.pk]
        return room

    @classmethod
    def _plan(cls, project_file, summary, percentage, amount):
        """The advance's lines and taxes, all rounded as the invoice will be.

        The base per VAT group is the expediente's (invoiceable_net_by_group:
        invoicing 100 % of it, squared with its net taxable base). A
        percentage is applied to each group and rounded half up (as
        AppliedTax.calculate rounds, so a later recalculation moves
        nothing); an amount is split across the groups by largest remainder,
        so the parts add up to it. A group whose part is more than it has
        left (its goods invoiced before the advance) would leave that part
        with nothing to deduct it from: the advance is then split across
        what the groups have left instead.
        """
        # An amount wins; either is kept at the precision the invoice stores
        if amount is not None:
            amount, percentage = amount.quantize(CENT, rounding=ROUND_HALF_UP), None
        elif percentage is not None:
            percentage = percentage.quantize(PERCENTAGE_STEP, rounding=ROUND_HALF_UP)

        summary = cls._summary(project_file, summary)
        net_base = summary["total_invoiceable_subtotal_net"]
        # Neither invoiced as goods nor taken by another advance (pro formas
        # too), in total and per VAT group
        room = {
            signature: left
            for signature, left in cls._room_by_group(summary).items()
            if left > 0
        }
        max_amount = max(
            min(
                summary["goods_remaining_net"] - summary["advances_open_net"],
                sum(room.values(), ZERO),
            ),
            ZERO,
        )
        plan = {
            "error": None,
            "percentage": None,
            "net_base": net_base,
            "max_amount": max_amount,
            "lines": [],
            "taxes": [],
            "base": Decimal("0.00"),
            "tax_amount": Decimal("0.00"),
            "total": Decimal("0.00"),
        }
        plan["error"] = cls._input_error(project_file, net_base, percentage, amount)
        if plan["error"]:
            return plan

        groups = {
            signature: base
            for signature, base in summary["invoiceable_net_by_group"].items()
            if base > 0
        }

        if amount is not None:
            bases = cls._split(amount, groups)
            plan["percentage"] = (amount / net_base * Decimal("100")).quantize(
                PERCENTAGE_STEP, rounding=ROUND_HALF_UP,
            )
        else:
            bases = {
                signature: (percentage / Decimal("100") * base).quantize(CENT, rounding=ROUND_HALF_UP)
                for signature, base in groups.items()
            }
            plan["percentage"] = percentage

        total_base = sum(bases.values(), Decimal("0.00"))
        if total_base <= 0 or plan["percentage"] <= 0:
            plan["error"] = cls._invalid_input_error(amount)
            return plan
        if plan["percentage"] > Decimal("100"):
            plan["error"] = _("Percentage cannot exceed 100%.")
            return plan
        if total_base > max_amount:
            plan["error"] = cls._cap_error(project_file, total_base, max_amount)
            return plan
        if any(base > room.get(signature, ZERO) for signature, base in bases.items()):
            # It fits in total (≤ the rooms' sum), so no share exceeds its room
            bases = cls._split(total_base, room)

        # The lines' text is stored on the invoice: in the documents'
        # language, not the operator's
        with translation.override(settings.LANGUAGE_CODE):
            labels = {
                signature: cls._tax_label(signature)
                for signature, base in bases.items() if base > 0
            }
            description = gettext(
                "Advance %(percentage)s %% of the taxable base %(base)s € — Project file %(reference)s"
            ) % {
                "percentage": cls._format_percentage(plan["percentage"]),
                "base": number_format(net_base, 2),
                "reference": project_file.reference or project_file.name,
            }
        # Main group first
        signatures = sorted(labels, key=lambda signature: (-bases[signature], labels[signature]))
        taxes = {}  # (tax_type pk, %) → invoice-level tax, in print order
        for signature in signatures:
            line = {
                "base": bases[signature],
                "taxes": cls._ordered_taxes(signature),
                "tax_label": labels[signature],
                "description": description,
            }
            if len(signatures) > 1:
                line["description"] = f"{description} ({line['tax_label']})"
            plan["lines"].append(line)
            for tax_type, pct in line["taxes"]:
                entry = taxes.setdefault((tax_type.pk, pct), {
                    "tax_type": tax_type,
                    "selected_percentage": pct,
                    "base_amount": Decimal("0.00"),
                })
                entry["base_amount"] += line["base"]

        # As SalesInvoice.recalculate_totals: each tax on its stored base,
        # retentions (IRPF) subtracted.
        tax_amount = Decimal("0.00")
        for entry in taxes.values():
            entry["calculated_amount"] = cls._tax(entry["base_amount"], entry["selected_percentage"])
            if entry["tax_type"].behavior == "add":
                tax_amount += entry["calculated_amount"]
            else:
                tax_amount -= entry["calculated_amount"]
        plan["taxes"] = list(taxes.values())
        plan["base"] = total_base
        plan["tax_amount"] = tax_amount
        plan["total"] = total_base + tax_amount
        return plan

    @classmethod
    def _input_error(cls, project_file, net_base, percentage, amount):
        """Why the request cannot even be priced, or None."""
        if not project_file.company_id:
            return _("This project file has no company.")
        if net_base <= 0:
            return _(
                "This project file has no accepted quotes, direct configurations or work entries. "
                "Cannot create an invoice."
            )
        if amount is None and percentage is None:
            return _("Enter the advance percentage or amount.")
        if (amount if amount is not None else percentage) <= 0:
            return cls._invalid_input_error(amount)
        if percentage is not None and percentage > Decimal("100"):
            return _("Percentage cannot exceed 100%.")
        return None

    @staticmethod
    def _invalid_input_error(amount):
        if amount is not None:
            return _("Please enter a valid amount.")
        return _("Please enter a valid percentage.")

    @staticmethod
    def _cap_error(project_file, amount, max_amount):
        return _(
            "The advance (%(amount)s) exceeds the largest advance still allowed on "
            "%(project_file)s (%(max)s): the taxable base neither invoiced nor advanced yet."
        ) % {
            "amount": amount,
            "project_file": project_file.reference or project_file.name,
            "max": max_amount,
        }

    @staticmethod
    def _split(amount, groups):
        """``amount`` spread over ``groups`` in proportion to their base, in
        cents that add up to it exactly (largest remainder)."""
        weight = sum(groups.values(), Decimal("0.00"))
        shares = {}
        remainders = []
        for signature, base in groups.items():
            exact = amount * base / weight
            shares[signature] = exact.quantize(CENT, rounding=ROUND_DOWN)
            remainders.append((exact - shares[signature], base, signature))
        leftover = int((amount - sum(shares.values(), Decimal("0.00"))) / CENT)
        remainders.sort(key=lambda remainder: (remainder[0], remainder[1]), reverse=True)
        for remainder in remainders[:leftover]:
            shares[remainder[2]] += CENT
        return shares

    @staticmethod
    def _tax(base, percentage):
        """A tax amount as AppliedTax.calculate rounds it."""
        return (base * (percentage / Decimal("100"))).quantize(CENT, rounding=ROUND_HALF_UP)

    @staticmethod
    def _ordered_taxes(signature):
        """A VAT group's (tax_type, %) pairs: additive taxes first, then by name and rate."""
        return sorted(
            signature,
            key=lambda tax: (tax[0].behavior != "add", tax[0].tax_name, -tax[1], tax[0].pk),
        )

    @classmethod
    def _tax_label(cls, signature):
        if not signature:
            return gettext("no taxes")
        return ", ".join(
            f"{tax_type.tax_name} {cls._format_rate(pct)} %"
            for tax_type, pct in cls._ordered_taxes(signature)
        )

    @staticmethod
    def _format_percentage(value):
        """An advance's percentage, localized: 30 → "30,00", 33.3333 → "33,3333"."""
        exponent = value.normalize().as_tuple().exponent
        return number_format(value, max(-exponent, 2))

    @staticmethod
    def _format_rate(value):
        """A tax rate, localized: 21.00 → "21", 5.20 → "5,2"."""
        exponent = value.normalize().as_tuple().exponent
        return number_format(value, max(-exponent, 0))
