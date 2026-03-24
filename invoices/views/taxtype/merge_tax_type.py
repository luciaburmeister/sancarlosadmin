from django.shortcuts import render, redirect
from django.urls import reverse
from django.views import View
from invoices.models import TaxType, AppliedTax


class MergeTaxTypeAdminView(View):
    model_admin = None

    def get(self, request):
        ids = request.GET.get("ids", "")
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        tax_types = TaxType.objects.filter(id__in=id_list)

        if tax_types.count() < 2: 
            return redirect(reverse("admin:invoices_taxtype_changelist"))

        return render(
            request,
            "admin/invoices/taxtype/merge_confirmation.html",
            {
                **self.model_admin.admin_site.each_context(request),
                "tax_types": tax_types,
                "selected_ids": id_list,
                "title": "Merge Tax Types",
                "opts": TaxType._meta,
            },
        )

    def post(self, request):
        ids = request.POST.getlist("selected_ids")
        winner_id = request.POST.get("winner_id")

        tax_types = TaxType.objects.filter(id__in=ids)
        winner = tax_types.get(id=winner_id)
        losers = tax_types.exclude(id=winner_id)  

        re_pointed = 0
        deleted_conflicts = 0
        merged_names = []

        for loser in losers:  
            for applied_tax in AppliedTax.objects.filter(tax_type=loser):
                conflict_exists = AppliedTax.objects.filter(
                    content_type=applied_tax.content_type,
                    object_id=applied_tax.object_id,
                    tax_type=winner,
                    selected_percentage=applied_tax.selected_percentage,
                ).exists()

                if conflict_exists:
                    applied_tax.delete()
                    deleted_conflicts += 1
                else:
                    applied_tax.tax_type = winner
                    applied_tax.save(update_fields=["tax_type"])
                    re_pointed += 1

            merged_names.append(loser.tax_name)
            loser.delete()

        self.model_admin.message_user(
            request,
            f"Merged {merged_names} into '{winner.tax_name}'. "
            f"Re-pointed {re_pointed} records, removed {deleted_conflicts} true duplicates."
        )
        return redirect(reverse("admin:invoices_taxtype_changelist"))