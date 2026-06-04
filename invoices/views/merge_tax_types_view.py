from django.contrib.auth.decorators import permission_required
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from invoices.models import AppliedTax, TaxType


class MergeTaxTypesView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Confirmation page for merging duplicate TaxType records.

    GET  → shows the selected tax types with AppliedTax counts so the user
           can pick the one to keep.
    POST → reassigns all AppliedTax records from discarded types to the
           target, handles unique-constraint conflicts, and deletes the
           duplicates.
    """

    title = _("Merge Tax Types")
    template_name = "admin/invoices/taxtype/merge.html"
    permission_required = ("tax_type.edit")

    # ── helpers ─────────────────────────────────────────────────────

    def _parse_ids(self):
        """Return a list of int PKs from the ``ids`` query/post param."""
        ids_str = self.request.GET.get("ids", "") or self.request.POST.get("ids", "")
        try:
            return [int(i) for i in ids_str.split(",") if i.strip()], ids_str
        except (ValueError, AttributeError):
            return [], ids_str

    @staticmethod
    def _changelist_url():
        return reverse("admin:invoices_taxtype_changelist")

    # ── GET ─────────────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ids, ids_str = self._parse_ids()

        tax_types_qs = TaxType.objects.filter(pk__in=ids)

        tax_type_data = []
        for tax in tax_types_qs.order_by("-is_active", "tax_name"):
            applied_count = AppliedTax.objects.filter(tax_type=tax).count()
            tax_type_data.append({
                "tax": tax,
                "applied_count": applied_count,
            })

        # Most-used first (pre-selected in the radio)
        tax_type_data.sort(key=lambda x: x["applied_count"], reverse=True)

        ctx.update({
            "title": _("Merge Tax Types"),
            "opts": TaxType._meta,
            "tax_types": tax_type_data,
            "ids_str": ids_str,
        })
        return ctx

    def get(self, request, *args, **kwargs):
        ids, ids_str = self._parse_ids()

        if len(ids) < 2:
            messages.error(request, _("Please select at least 2 tax types to merge."))
            return redirect(self._changelist_url())

        if TaxType.objects.filter(pk__in=ids).count() < 2:
            messages.error(request, _("Some selected tax types no longer exist."))
            return redirect(self._changelist_url())

        return super().get(request, *args, **kwargs)

    # ── POST ────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        ids, ids_str = self._parse_ids()

        if len(ids) < 2:
            messages.error(request, _("Please select at least 2 tax types to merge."))
            return redirect(self._changelist_url())

        target_id = request.POST.get("target_id")
        try:
            target_id = int(target_id)
        except (ValueError, TypeError):
            messages.error(request, _("Please select a target tax type."))
            return redirect(request.get_full_path())

        if target_id not in ids:
            messages.error(request, _("Invalid target tax type."))
            return redirect(request.get_full_path())

        target = TaxType.objects.get(pk=target_id)
        source_ids = [i for i in ids if i != target_id]

        with transaction.atomic():
            source_applied = AppliedTax.objects.filter(tax_type_id__in=source_ids)

            # Unique constraint: (content_type, object_id, tax_type, selected_percentage)
            existing_target_keys = set(
                AppliedTax.objects.filter(tax_type=target).values_list(
                    "content_type_id", "object_id", "selected_percentage"
                )
            )

            to_delete_pks = []
            to_update_pks = []

            for at in source_applied.iterator():
                key = (at.content_type_id, at.object_id, at.selected_percentage)
                if key in existing_target_keys:
                    to_delete_pks.append(at.pk)
                else:
                    to_update_pks.append(at.pk)
                    existing_target_keys.add(key)

            deleted_count = AppliedTax.objects.filter(pk__in=to_delete_pks).delete()[0]

            updated_count = AppliedTax.objects.filter(pk__in=to_update_pks).update(
                tax_type=target
            )

            source_names = list(
                TaxType.objects.filter(pk__in=source_ids).values_list("tax_name", flat=True)
            )
            TaxType.objects.filter(pk__in=source_ids).delete()

        messages.success(
            request,
            _(
                "Merged into \"%(target)s\": %(updated)d records reassigned, "
                "%(deleted)d duplicates removed, %(sources)d tax types deleted (%(names)s)."
            ) % {
                "target": target.tax_name,
                "updated": updated_count,
                "deleted": deleted_count,
                "sources": len(source_ids),
                "names": ", ".join(source_names),
            },
        )
        return redirect(self._changelist_url())
