from core.admin.company_access import get_user_companies


class _CompanyImportMixin:

    def get_import_form_kwargs(self, request):
        kwargs = super().get_import_form_kwargs(request)
        kwargs["companies"] = get_user_companies(request.user)
        kwargs["request"] = request
        return kwargs

    def get_confirm_form_initial(self, request, import_form):
        initial = super().get_confirm_form_initial(request, import_form)
        if import_form and hasattr(import_form, "cleaned_data"):
            company = import_form.cleaned_data.get("company")
            if company:
                initial["company"] = str(company.pk)
        return initial

    def get_import_resource_kwargs(self, request, **kwargs):
        from company.models import Company as CompanyModel
        result = super().get_import_resource_kwargs(request, **kwargs)
        form = kwargs.get("form")
        company = None
        if form and hasattr(form, "cleaned_data"):
            raw = form.cleaned_data.get("company")
            if raw:
                if hasattr(raw, "pk"):
                    company = raw
                else:
                    try:
                        company = CompanyModel.objects.get(pk=raw)
                    except CompanyModel.DoesNotExist:
                        pass
        if company:
            result["default_company"] = company
        else:
            companies = get_user_companies(request.user)
            if companies.count() == 1:
                result["default_company"] = companies.first()
        return result
