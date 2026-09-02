# company/views/switch_company.py

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from company.active_company import ActiveCompany
from company.models import Company


class SwitchActiveCompanyForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.none())
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # An id outside the user's switchable set fails validation.
        self.fields["company"].queryset = ActiveCompany.switchable_for(user)


class SwitchActiveCompanyView(FormView):
    """
    Toggles a company in/out of the session's active set: clicking a
    company that is not active adds it (so several companies can be worked
    on at once); clicking an active one removes it, unless it is the last
    one. Mounted inside the Company admin (see CompanyAdmin.get_urls) as
    `admin:company_switch_active`.
    """

    form_class = SwitchActiveCompanyForm
    http_method_names = ["post"]  # the switcher UI posts; no GET page needed

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        company = form.cleaned_data["company"]
        added = ActiveCompany.toggle(self.request, company)

        if added is None:
            messages.warning(
                self.request, _("Debe haber al menos una empresa activa.")
            )
        elif added:
            messages.success(
                self.request,
                _("Empresa añadida: %(name)s") % {"name": company.name},
            )
        else:
            messages.success(
                self.request,
                _("Empresa quitada: %(name)s") % {"name": company.name},
            )
        return redirect(self._safe_next(form.cleaned_data.get("next")))

    def form_invalid(self, form):
        messages.error(self.request, _("No tienes acceso a esa empresa."))
        return redirect(reverse("admin:index"))

    def _safe_next(self, next_url):
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return next_url
        return reverse("admin:index")
