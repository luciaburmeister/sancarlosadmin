"""
A company's setup: the steps it still has to go through, each on its own page.

The steps come from the `general.onboarding` registry. Each step page shows the
step's checks and, when it has one, a form that saves straight away, so the
setup can be left and resumed at any point. Staff create the company and its
first admin (`NewCompanyView`); that admin does the rest.
"""
from functools import cached_property

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View
from django.views.generic import FormView, TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.active_company import ActiveCompany
from company.forms import NewCompanyForm
from general.onboarding import CompanyReadiness


class CompanyOnboardingMixin(UnfoldModelAdminViewMixin):
    permission_required = ("company.can_onboard_company",)

    @cached_property
    def company(self):
        # Any company the user belongs to, whether or not it's switched on in the header
        return get_object_or_404(ActiveCompany.allowed_for(self.request.user), pk=self.kwargs["pk"])

    @cached_property
    def readiness(self):
        return CompanyReadiness(self.company)

    def overview_url(self):
        return reverse("admin:company_company_onboarding", args=[self.company.pk])

    def step_url(self, step):
        return reverse("admin:company_company_onboarding_step", args=[self.company.pk, step.key])


class OnboardingOverviewView(CompanyOnboardingMixin, TemplateView):
    title = gettext_lazy("Company setup")
    template_name = "company/onboarding/overview.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "company": self.company,
            "steps": [(step, self.step_url(step)) for step in self.readiness.steps],
            "ready": self.readiness.is_ready(),
            "finish_url": reverse("admin:company_company_onboarding_finish", args=[self.company.pk]),
        })
        return context


class OnboardingStepView(CompanyOnboardingMixin, TemplateView):
    title = gettext_lazy("Company setup")
    template_name = "company/onboarding/step.html"

    @cached_property
    def step(self):
        step = self.readiness.step(self.kwargs["step"])
        if step is None:
            raise Http404
        return step

    def post(self, request, *args, **kwargs):
        form = self.step.get_form(request.POST, request.FILES)
        if form is None:
            raise Http404
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        message = self.step.save(form, request)
        if message:
            messages.success(request, message)
        return redirect(self.step_url(self.step))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        steps = self.readiness.steps
        index = steps.index(self.step)
        context.update({
            "title": self.step.title,
            "company": self.company,
            "step": self.step,
            "form": kwargs.get("form") or self.step.get_form(),
            "actions": self.step.actions(),
            "import_prompts": self.step.import_prompts(),
            "sections": self.step.results,
            "overview_url": self.overview_url(),
            "previous_url": self.step_url(steps[index - 1]) if index else None,
            "next_url": self.step_url(steps[index + 1]) if index + 1 < len(steps) else None,
            "position": index + 1,
            "total": len(steps),
        })
        return context


class OnboardingFinishView(CompanyOnboardingMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        if not self.readiness.is_ready():
            messages.error(request, _("Some required steps still have missing items."))
            return redirect(self.overview_url())
        if self.company.onboarding_completed_at is None:
            self.company.onboarding_completed_at = timezone.now()
            self.company.save(update_fields=["onboarding_completed_at"])
        messages.success(request, _("%(company)s is set up.") % {"company": self.company})
        return redirect("admin:index")


class OnboardingStartView(UnfoldModelAdminViewMixin, View):
    """Where the sidebar link lands: the first of the user's companies still being set up."""

    permission_required = ("company.can_onboard_company",)

    def get(self, request, *args, **kwargs):
        pending = ActiveCompany.allowed_for(request.user).filter(onboarding_completed_at__isnull=True)
        company = pending.order_by("name").first()
        if company is None:
            messages.info(request, _("All your companies are set up."))
            return redirect("admin:company_company_changelist")
        return redirect("admin:company_company_onboarding", company.pk)


class NewCompanyView(UnfoldModelAdminViewMixin, FormView):
    """Staff only: a new company with its essentials, and its first admin invited to set up the rest."""

    title = gettext_lazy("New company")
    form_class = NewCompanyForm
    template_name = "company/onboarding/new.html"

    def has_permission(self):
        return self.request.user.is_superuser

    def form_valid(self, form):
        from company.services.onboarding import create_company_shell

        data = form.cleaned_data
        company = create_company_shell(
            name=data["name"], legal_name=data["legal_name"], tax_identifier=data["tax_identifier"],
            logo=data["logo"], constituted_country=data["constituted_country"],
            chart_template=data["chart_template"], modules=data["modules"],
            admin_email=data["admin_email"], admin_first_name=data["admin_first_name"],
            admin_last_name=data["admin_last_name"], base_url=self.request.build_absolute_uri("/"),
        )
        messages.success(self.request, _("%(company)s created and %(email)s invited to set it up.") % {
            "company": company, "email": data["admin_email"],
        })
        return redirect("admin:company_company_onboarding", company.pk)
