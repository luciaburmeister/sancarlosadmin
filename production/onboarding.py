from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from company.models import CompanyModule, CompanyScheduleSlot, WeekdayChoices
from general.onboarding import MISSING, OK, WARNING, Check, OnboardingStep, Section, register
from general.onboarding.forms import UnfoldFormMixin
from production.models import ProductionResource


class ProductionSetupForm(UnfoldFormMixin, forms.Form):
    production_stages = forms.CharField(
        label=gettext_lazy("Production stages"),
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text=gettext_lazy("One per line, in the order work goes through them."),
    )
    weekdays = forms.TypedMultipleChoiceField(
        label=gettext_lazy("Working days"),
        choices=WeekdayChoices.choices,
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=gettext_lazy("Adds the hours below to each day ticked. Add a split shift as a second set of hours."),
    )
    start_time = forms.TimeField(label=gettext_lazy("From"), required=False)
    end_time = forms.TimeField(label=gettext_lazy("To"), required=False)

    def __init__(self, *args, company, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["production_stages"].initial = "\n".join(company.production_stages)

    def clean_production_stages(self):
        stages = [line.strip() for line in self.cleaned_data["production_stages"].splitlines() if line.strip()]
        if len(set(stages)) != len(stages):
            raise ValidationError(_("Each stage can appear only once."))
        return stages

    def clean(self):
        cleaned = super().clean()
        weekdays, start, end = cleaned.get("weekdays"), cleaned.get("start_time"), cleaned.get("end_time")
        self.slots = []
        if not (weekdays or start or end):
            return cleaned
        if not (weekdays and start and end):
            raise ValidationError(_("To add working hours, tick the days and give both times."))
        for weekday in weekdays:
            slot = CompanyScheduleSlot(company=self.company, weekday=weekday, start_time=start, end_time=end)
            try:
                slot.clean()
            except ValidationError as error:
                raise ValidationError(
                    "%s: %s" % (WeekdayChoices(weekday).label, " ".join(error.messages))
                )
            self.slots.append(slot)
        return cleaned

    def save(self):
        self.company.production_stages = self.cleaned_data["production_stages"]
        self.company.save(update_fields=["production_stages"])
        CompanyScheduleSlot.objects.bulk_create(self.slots)


@register
class ProductionStep(OnboardingStep):
    key = "production"
    title = gettext_lazy("Production")
    description = gettext_lazy("The stages work goes through, and the hours it can be planned in.")
    order = 70
    module = CompanyModule.PRODUCTION
    form_class = ProductionSetupForm
    submit_label = gettext_lazy("Save")

    def get_form_kwargs(self):
        return {"company": self.company}

    def sections(self):
        company = self.company
        slots = list(CompanyScheduleSlot.objects.filter(company=company))
        resources = ProductionResource.objects.filter(company=company).count()
        resources_url = reverse("admin:production_productionresource_changelist")
        return [Section(_("Production"), [
            Check(_("Stages"), OK, " → ".join(company.production_stages))
            if company.production_stages else
            Check(_("Stages"), MISSING, _("Employees, machines and products are assigned to stages.")),
            Check(_("Working hours"), OK, ", ".join(
                f"{slot.get_weekday_display()} {slot.start_time:%H:%M}–{slot.end_time:%H:%M}" for slot in slots
            ))
            if slots else
            Check(_("Working hours"), MISSING, _("Nothing can be planned outside working hours, and there are none.")),
            Check(_("Resources"), OK, _("%(count)s resources.") % {"count": resources}, resources_url)
            if resources else
            Check(_("Resources"), WARNING, _("No lines or workstations to plan production on."), resources_url),
        ])]

    def save(self, form, request):
        form.save()
        return _("Production settings saved.")
