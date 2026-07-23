import json

from django.contrib import messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from formtools.wizard.views import SessionWizardView
from unfold.views import UnfoldModelAdminViewMixin
from management.models import ProjectFile


from assets.forms import (
    AssetWizardBasicsForm,
    AssetWizardDetailsForm,
    DATA_FIELD_PREFIX,
    IDENTIFIER_FIELD_PREFIX,
    extract_prefixed_dict,
)
from assets.models import Asset, AssetIdentifier


class AssetCreateWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    """
    Two-step asset creation: basics (name/type/client/company) first, then —
    with the type known — its identifiers and type-specific data fields.
    Works inside the related-field "+" popup (e.g. from a project file): the
    final step renders Django's popup response so the opener selects the new
    asset.
    """

    form_list = [
        ("basics", AssetWizardBasicsForm),
        ("details", AssetWizardDetailsForm),
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ("assets.add_asset",)
    title = _("New Asset")

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        # The wizard form posts to "." without the query string, so stash the
        # popup / expediente state in the wizard storage.
        for key, param in (
            ("popup", "_popup"),
            ("to_field", "_to_field"),
            ("project_file_id", "project_file_id"),
        ):
            if request.GET.get(param):
                self.storage.extra_data[key] = request.GET[param]
        return response

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == "basics":
            project_file_id = (
                self.storage.extra_data.get("project_file_id")
                or self.request.GET.get("project_file_id")
            )
            if project_file_id:
                project_file = ProjectFile.objects.select_related(
                    "client", "company"
                ).filter(pk=project_file_id).first()
                if project_file:
                    initial.setdefault("client", project_file.client)
                    initial.setdefault("company", project_file.company)
        return initial

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        if step == "basics":
            kwargs["request"] = self.request
        elif step == "details":
            basics = self.get_cleaned_data_for_step("basics") or {}
            kwargs["asset_type"] = basics.get("asset_type")
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from configurator.forms import ConfigurationFormHelper
        context["helper"] = ConfigurationFormHelper(self.get_form(step=self.steps.current))
        # Popup pages must not render the sidebar/header chrome. On the first
        # GET the storage isn't stashed yet, so also look at the query string.
        context["is_popup"] = bool(
            self.request.GET.get("_popup") or self.storage.extra_data.get("popup")
        )
        # Cancel returns to the expediente this wizard was launched from (in
        # popup mode the template closes the window instead).
        context["cancel_url"] = (
            reverse("admin:management_projectfile_change", args=[project_file_id])
            if (
                project_file_id := (
                    self.storage.extra_data.get("project_file_id")
                    or self.request.GET.get("project_file_id")
                )
            )
            else reverse("admin:assets_asset_changelist")
        )
        return context

    def done(self, form_list, **kwargs):
        basics_form, details_form = form_list

        data = extract_prefixed_dict(details_form.cleaned_data, DATA_FIELD_PREFIX)
        identifiers = {
            name[len(IDENTIFIER_FIELD_PREFIX):]: value
            for name, value in details_form.cleaned_data.items()
            if name.startswith(IDENTIFIER_FIELD_PREFIX) and value
        }

        asset = Asset.objects.create(
            name=basics_form.cleaned_data["name"],
            asset_type=basics_form.cleaned_data["asset_type"],
            client=basics_form.cleaned_data.get("client"),
            company=basics_form.cleaned_data["company"],
            data=data,
        )
        for label, value in identifiers.items():
            AssetIdentifier.objects.get_or_create(
                asset=asset, label=label, defaults={"value": value}
            )

        if self.storage.extra_data.get("popup"):
            to_field = self.storage.extra_data.get("to_field")
            attr = str(to_field) if to_field else asset._meta.pk.attname
            value = asset.serializable_value(attr)
            return TemplateResponse(
                self.request,
                "admin/popup_response.html",
                {
                    "popup_response_data": json.dumps(
                        {"value": str(value), "obj": str(asset)}
                    ),
                },
            )

        # Launched from an expediente: link the new vehicle and go back to it.
        project_file_id = (
            self.storage.extra_data.get("project_file_id")
            or self.request.GET.get("project_file_id")
        )
        if project_file_id:
            from management.models import ProjectFile
            project_file = ProjectFile.objects.select_related(
                "client", "company"
            ).filter(pk=project_file_id).first()
            if project_file:
                project_file.asset = asset
                project_file.save(update_fields=["asset"])
                messages.success(
                    self.request,
                    _('Asset "%(asset)s" created and linked to %(reference)s.')
                    % {"asset": asset, "reference": project_file.reference},
                )
                return redirect(
                    reverse(
                        "admin:management_projectfile_change", args=[project_file.pk]
                    )
                )

        return redirect(reverse("admin:assets_asset_change", args=[asset.pk]))
