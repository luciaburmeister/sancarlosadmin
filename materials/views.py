from django.contrib import messages
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from materials.models import Provider, ProviderBankAccount, Material


class MergeProvidersView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Confirmation page for merging duplicate Provider records.

    GET  → shows the selected providers with related-record counts so the
           user can pick the one to keep.
    POST → reassigns every FK reference from the discarded providers to the
           target, de-duplicates rows that would otherwise violate a unique
           constraint, and deletes the duplicates. Invoices are never
           deleted or silently dropped — an invoice_number collision leaves
           that invoice (and its provider) in place for manual resolution
           instead.
    """

    title = _("Merge Providers")
    template_name = "admin/materials/provider/merge.html"
    permission_required = ("materials.change_provider", "materials.delete_provider")

    # ── helpers ─────────────────────────────────────────────────────

    def _parse_ids(self):
        ids_str = self.request.GET.get("ids", "") or self.request.POST.get("ids", "")
        try:
            return [int(i) for i in ids_str.split(",") if i.strip()], ids_str
        except (ValueError, AttributeError):
            return [], ids_str

    @staticmethod
    def _changelist_url():
        return reverse("admin:materials_provider_changelist")

    @staticmethod
    def _related_counts(provider):
        from invoices.models import Invoice
        from purchasing.models import PurchaseOrder
        from inventory.models import InventoryItem, ItemProviderInfo
        from assets.models import Asset
        from apps.expenses.models import ExpenseTicket

        return {
            _("Invoices"): Invoice.objects.filter(provider=provider).count(),
            _("Purchase Orders"): PurchaseOrder.objects.filter(provider=provider).count(),
            _("Materials"): Material.objects.filter(provider=provider).count(),
            _("Bank Accounts"): ProviderBankAccount.objects.filter(provider=provider).count(),
            _("Item References"): ItemProviderInfo.objects.filter(provider=provider).count(),
            _("Preferred Supplier For"): InventoryItem.objects.filter(preferred_supplier=provider).count(),
            _("Assets"): Asset.objects.filter(provider=provider).count(),
            _("Expense Tickets"): ExpenseTicket.objects.filter(provider=provider).count(),
        }

    # ── GET ─────────────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ids, ids_str = self._parse_ids()

        providers_qs = Provider.objects.filter(pk__in=ids).select_related("company")

        provider_data = []
        for provider in providers_qs.order_by("name"):
            counts = self._related_counts(provider)
            provider_data.append({
                "provider": provider,
                "counts": counts,
                "total": sum(counts.values()),
            })

        # Most-referenced first (pre-selected as the keeper — usually the
        # oldest/most-used record is the "real" one).
        provider_data.sort(key=lambda x: x["total"], reverse=True)

        ctx.update({
            "title": _("Merge Providers"),
            "opts": Provider._meta,
            "providers": provider_data,
            "ids_str": ids_str,
        })
        return ctx

    def get(self, request, *args, **kwargs):
        ids, ids_str = self._parse_ids()

        if len(ids) < 2:
            messages.error(request, _("Please select at least 2 providers to merge."))
            return redirect(self._changelist_url())

        providers = list(Provider.objects.filter(pk__in=ids))
        if len(providers) < 2:
            messages.error(request, _("Some selected providers no longer exist."))
            return redirect(self._changelist_url())

        if len({p.company_id for p in providers}) > 1:
            messages.error(
                request,
                _("Selected providers belong to different companies — merging across companies isn't supported."),
            )
            return redirect(self._changelist_url())

        return super().get(request, *args, **kwargs)

    # ── POST ────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        from invoices.models import Invoice
        from purchasing.models import PurchaseOrder
        from inventory.models import InventoryItem, ItemProviderInfo
        from assets.models import Asset
        from apps.expenses.models import ExpenseTicket

        ids, ids_str = self._parse_ids()

        if len(ids) < 2:
            messages.error(request, _("Please select at least 2 providers to merge."))
            return redirect(self._changelist_url())

        target_id = request.POST.get("target_id")
        try:
            target_id = int(target_id)
        except (ValueError, TypeError):
            messages.error(request, _("Please select a provider to keep."))
            return redirect(request.get_full_path())

        if target_id not in ids:
            messages.error(request, _("Invalid target provider."))
            return redirect(request.get_full_path())

        target = Provider.objects.get(pk=target_id)
        source_ids = [i for i in ids if i != target_id]
        sources = list(Provider.objects.filter(pk__in=source_ids))

        if any(s.company_id != target.company_id for s in sources):
            messages.error(
                request,
                _("Selected providers belong to different companies — merging across companies isn't supported."),
            )
            return redirect(request.get_full_path())

        merged_names = []
        kept_with_leftovers = []

        with transaction.atomic():
            for source in sources:
                # Plain reassignment — none of these involve `provider` in a
                # unique constraint, so a straight UPDATE is safe.
                ProviderBankAccount.objects.filter(provider=source).update(provider=target)
                Material.objects.filter(provider=source).update(provider=target)
                PurchaseOrder.objects.filter(provider=source).update(provider=target)
                InventoryItem.objects.filter(preferred_supplier=source).update(preferred_supplier=target)
                Asset.objects.filter(provider=source).update(provider=target)
                ExpenseTicket.objects.filter(provider=source).update(provider=target)

                # ItemProviderInfo — unique on (item, provider_ref) is
                # provider-independent, so it can never collide from a
                # provider-only reassignment. The constraint that CAN
                # collide is (provider, provider_ref): source and target
                # may each use the same ref text for two *different* items,
                # which is a real conflict, not a duplicate — reassigning
                # would silently point one item's row at the wrong meaning.
                # Never delete these — skip and leave them on the source
                # provider for manual resolution, same policy as Invoice.
                existing_provider_refs = set(
                    ItemProviderInfo.objects.filter(provider=target).values_list("provider_ref", flat=True)
                )
                for info in ItemProviderInfo.objects.filter(provider=source):
                    if info.provider_ref in existing_provider_refs:
                        continue
                    info.provider = target
                    info.save(update_fields=["provider"])
                    existing_provider_refs.add(info.provider_ref)

                # Invoice — unique_together (company, provider, invoice_number).
                # Never delete or silently drop a real invoice — a colliding
                # number is left pointing at the source provider for manual
                # resolution instead.
                existing_invoice_numbers = set(
                    Invoice.objects.filter(company=target.company, provider=target)
                    .exclude(invoice_number__isnull=True)
                    .exclude(invoice_number="")
                    .values_list("invoice_number", flat=True)
                )
                for invoice in Invoice.objects.filter(provider=source):
                    number = invoice.invoice_number
                    if number and number in existing_invoice_numbers:
                        continue
                    invoice.provider = target
                    invoice.save(update_fields=["provider"])
                    if number:
                        existing_invoice_numbers.add(number)

                # De-duplicate multiple "default" bank accounts left after
                # reassignment — keep the first, clear the rest.
                default_accounts = list(
                    ProviderBankAccount.objects.filter(provider=target, is_default=True).order_by("pk")
                )
                for extra in default_accounts[1:]:
                    extra.is_default = False
                    extra.save(update_fields=["is_default"])

                # Only delete if nothing is left pointing at this provider.
                # Checking explicitly (rather than relying solely on
                # ProtectedError) matters because ItemProviderInfo uses
                # on_delete=CASCADE, which would otherwise silently destroy
                # the very conflicting rows we chose not to touch above.
                still_referenced = (
                    ItemProviderInfo.objects.filter(provider=source).exists()
                    or Invoice.objects.filter(provider=source).exists()
                )
                if still_referenced:
                    kept_with_leftovers.append(source.name)
                    continue

                try:
                    with transaction.atomic():
                        source.delete()
                    merged_names.append(source.name)
                except ProtectedError:
                    # Defense in depth, in case some other relation we
                    # didn't account for still points at this provider.
                    kept_with_leftovers.append(source.name)

        if merged_names:
            messages.success(
                request,
                _("Merged into \"%(target)s\": %(names)s.") % {
                    "target": target.name,
                    "names": ", ".join(merged_names),
                },
            )
        if kept_with_leftovers:
            messages.warning(
                request,
                _(
                    "Could not fully remove %(names)s — some invoice numbers or "
                    "item references collide with ones already on \"%(target)s\" "
                    "and were left in place. Resolve the conflict manually, then "
                    "merge again to finish."
                ) % {"names": ", ".join(kept_with_leftovers), "target": target.name},
            )

        return redirect(reverse("admin:materials_provider_change", args=[target.pk]))
