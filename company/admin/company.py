from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.forms import ModelForm
from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import action
import html
import re


from company.models import Company, CompanyDefaultTax, CompanyScheduleSlot, CompanyUser, CompanyEmailConfig, BankAccount, CompanyDocument, CompanyDocumentType, ConfigurationList
from django.utils.translation import gettext_lazy as _
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from core.admin.company_access import get_user_companies
from unfold.admin import ModelAdmin, StackedInline,TabularInline
from core.admin.company_admin import CompanyRestrictedAdmin
from company.forms import CompanyDocumentForm

from unfold.contrib.forms.widgets import ArrayWidget, WysiwygWidget
from unfold.widgets import (
    UnfoldAdminFileFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTimeWidget,
)
from django.contrib.postgres.fields import ArrayField

@admin.register(BankAccount)
class BankAccountAdmin(ModelAdmin):
    list_display = ("bank_name", "account_number", "iban", "nuek_token", "is_default", "company")
    search_fields = ("bank_name", "account_number", "iban")
    autocomplete_fields = ("company",)
    actions_detail = ["sync_transactions_action"]

    @action(
        description=_("Sync Transactions"),
        url_path="sync-transactions",
        icon="sync",
    )
    def sync_transactions_action(self, request, object_id: int):
        from banking.tasks import sync_bank_account_task

        account = get_object_or_404(BankAccount, pk=object_id)
        if not account.nuek_token:
            messages.error(
                request,
                _("This bank account is not connected to Arcopay (no token). Please connect it first."),
            )
        else:
            sync_bank_account_task.delay(account.pk)
            messages.success(
                request,
                _("Transaction sync queued for %(bank)s. It will run in the background.")
                % {"bank": account.bank_name or account.iban},
            )
        return redirect(request.META.get("HTTP_REFERER"))

class BankAccountInline(StackedInline):
    model = BankAccount
    extra = 1
    fields = ("bank_name", "holder_name", "account_number", "iban", "swift_bic", "routing_number", "is_default")
    tab = True
    tab_title = _("Bank Accounts")


class CompanyDefaultTaxInline(TabularInline):
    model = CompanyDefaultTax
    extra = 0
    autocomplete_fields = ["tax_type"]
    verbose_name = _("Default Tax")
    verbose_name_plural = _("Default Taxes")

class CompanyDocumentInline(StackedInline):
    model = CompanyDocument
    form = CompanyDocumentForm
    extra = 0
    tab = True
    tab_title = _("Documents")
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type",  "file"),
                ("does_not_expire","is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )

@admin.register(CompanyDocumentType)
class CompanyDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

class CompanyScheduleSlotForm(ModelForm):
    class Meta:
        model = CompanyScheduleSlot
        fields = "__all__"
        widgets = {
            "weekday": UnfoldAdminSelectWidget,
            "start_time": UnfoldAdminTimeWidget,
            "end_time": UnfoldAdminTimeWidget,
        }

class CompanyScheduleSlotInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()

        slots_by_day = {}

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue

            if form.cleaned_data.get("DELETE"):
                continue

            weekday = form.cleaned_data.get("weekday")
            start_time = form.cleaned_data.get("start_time")
            end_time = form.cleaned_data.get("end_time")

            if weekday is None or start_time is None or end_time is None:
                continue

            if end_time <= start_time:
                raise ValidationError(_("End time must be after start time."))

            slots_by_day.setdefault(weekday, []).append((start_time, end_time))

        for weekday, slots in slots_by_day.items():
            slots.sort(key=lambda x: x[0])

            for i in range(1, len(slots)):
                prev_start, prev_end = slots[i - 1]
                curr_start, curr_end = slots[i]

                if curr_start < prev_end:
                    raise ValidationError(
                        _("There are overlapping slots on the same weekday.")
                    )

class CompanyScheduleSlotInline(admin.TabularInline):
    model = CompanyScheduleSlot
    form = CompanyScheduleSlotForm
    formset = CompanyScheduleSlotInlineFormSet
    extra = 0
    tab = True
    fields = ("weekday", "start_time", "end_time")
    ordering = ("weekday", "start_time")


class CompanyAdminForm(ModelForm):
    terms_md_file = forms.FileField(
        required=False,
        widget=UnfoldAdminFileFieldWidget(attrs={"accept": ".md,.markdown"}),
        label=_("Importar archivo Markdown (.md)"),
        help_text=_(
            "Si seleccionas un archivo .md, su contenido reemplazará el texto "
            "actual al guardar."
        ),
    )

    class Meta:
        model = Company
        fields = "__all__"

    def _markdown_to_html(self, markdown_text: str) -> str:
        text = (markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        out: list[str] = []
        in_ul = False

        def inline(value: str) -> str:
            escaped = html.escape(value)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
            return escaped

        for raw in lines:
            line = raw.strip()
            if not line:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                continue

            if line.startswith("### "):
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append(f"<h3>{inline(line[4:])}</h3>")
                continue
            if line.startswith("## "):
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append(f"<h2>{inline(line[3:])}</h2>")
                continue
            if line.startswith("# "):
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append(f"<h1>{inline(line[2:])}</h1>")
                continue

            if line.startswith("- ") or line.startswith("* "):
                if not in_ul:
                    out.append("<ul>")
                    in_ul = True
                out.append(f"<li>{inline(line[2:])}</li>")
                continue

            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<p>{inline(line)}</p>")

        if in_ul:
            out.append("</ul>")

        return "\n".join(out)

    def save(self, commit=True):
        instance = super().save(commit=False)
        uploaded = self.cleaned_data.get("terms_md_file")
        if uploaded:
            name = (uploaded.name or "").lower()
            if not (name.endswith(".md") or name.endswith(".markdown")):
                raise ValidationError(
                    {"terms_md_file": _("Solo se permiten archivos .md o .markdown")}
                )
            try:
                markdown_text = uploaded.read().decode("utf-8")
                instance.terms_text = self._markdown_to_html(markdown_text)
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    {"terms_md_file": _("El archivo debe estar codificado en UTF-8.")}
                ) from exc
        if commit:
            instance.save()
            self.save_m2m()
        return instance

@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    form = CompanyAdminForm
    readonly_fields = ("terms_import_hint",)
    inlines = [BankAccountInline, CompanyDocumentInline, CompanyScheduleSlotInline, CompanyDefaultTaxInline]
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("name",)

    fieldsets = (
        (_("Base Info"), {
            "classes": ["tab"],
            "fields": (
                ("name",
                "legal_name"),
                ("logo",
                "constituted_country"),
                "address",
                ("email",
                "phone"),
                "is_active"

            )
        }),
        (_("Application Configurations"), {
            "fields": (

                ("production_stages",
                "asset_identifier_labels"),
                "allow_shared_production_stage_activities",
                "auto_apply_client_taxes",
                ("document_reminder_emails"),
            ),
            "classes": ["tab"]
        }),
        (_("Taxes"), {
            "fields": (
                "tax_identifier",
                "industry_identifier",
                "business_activity_description"
            ),
            "classes": ["tab"]
        }),
        (_("Términos y Condiciones"), {
            "fields": ("terms_import_hint", "terms_md_file", "terms_text"),
            "classes": ["tab"],
            "description": "Texto que el cliente firmará digitalmente al registrarse en la app. Déjalo vacío para no requerir firma.",
        }),
    )

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "terms_text":
            kwargs["widget"] = WysiwygWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description=_("Importación de términos"))
    def terms_import_hint(self, obj):
        return format_html(
            """
            <div style="padding:12px 14px;border-radius:10px;border:1px solid #99F6E4;background:#F0FDFA;">
              <div style="font-weight:700;margin-bottom:4px;color:#115E59;">Carga rápida desde Markdown</div>
              <div style="font-size:12px;color:#134E4A;">
                Sube un archivo <code>.md</code> o <code>.markdown</code> para reemplazar automáticamente
                el texto de términos al guardar la compañía.
              </div>
            </div>
            """
        )

    actions_row = ["install_chart_action"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(id__in=get_user_companies(request.user))

    @action(
        description=_("Install Chart"),
        url_path="install-chart",
        icon="playlist_add"
    )
    def install_chart_action(self, request, object_id: int):
        from accounting.services.chart_installer import install_chart_standard
        
        company = get_object_or_404(Company, pk=object_id)
        
        if not hasattr(company, 'chart'):
            messages.error(request, _("Company has no Chart of Accounts template assigned."))
            return redirect(request.META.get("HTTP_REFERER"))
            
        try:
            count = install_chart_standard(company, company.chart.template)
            messages.success(request, _(f"Successfully installed {count} accounts from template."))
        except Exception as e:
            messages.error(request, _(f"Error installing chart: {str(e)}"))
            
        return redirect(request.META.get("HTTP_REFERER"))


@admin.register(CompanyUser)
class CompanyUserAdmin(ModelAdmin):
    list_display = ("user", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = (
        "user__username",
        "user__email",
        "company__name",
    )

    autocomplete_fields = ("user", "company")

    ordering = ("company", "user")

@admin.register(CompanyEmailConfig)
class CompanyEmailConfigAdmin(ModelAdmin):
    list_display = ("email_address", "company", "imap_server", "last_checked")
    list_filter = ("company", "use_ssl")
    search_fields = ("email_address", "imap_server", "imap_user")
    autocomplete_fields = ("company",)
    actions_row = ["test_connection_action", "pull_emails_action"]

    @action(
        description=_("Test Connection"),
        url_path="test-connection",
        icon="cable",
    )
    def test_connection_action(self, request, object_id: int):
        import imaplib

        config = get_object_or_404(CompanyEmailConfig, pk=object_id)
        try:
            if config.use_ssl:
                mail = imaplib.IMAP4_SSL(config.imap_server, config.imap_port)
            else:
                mail = imaplib.IMAP4(config.imap_server, config.imap_port)

            mail.login(config.imap_user, config.imap_password)
            status, mailbox_list = mail.list()
            mailbox_count = len(mailbox_list) if mailbox_list else 0
            mail.logout()

            messages.success(
                request,
                _("Connection successful — %(email)s is reachable (%(count)d mailboxes found).")
                % {"email": config.email_address, "count": mailbox_count},
            )
        except imaplib.IMAP4.error as e:
            messages.error(
                request,
                _("IMAP authentication failed for %(email)s: %(error)s")
                % {"email": config.email_address, "error": str(e)},
            )
        except OSError as e:
            messages.error(
                request,
                _("Could not connect to %(server)s:%(port)s — %(error)s")
                % {"server": config.imap_server, "port": config.imap_port, "error": str(e)},
            )
        except Exception as e:
            messages.error(
                request,
                _("Unexpected error testing %(email)s: %(error)s")
                % {"email": config.email_address, "error": str(e)},
            )

        return redirect(request.META.get("HTTP_REFERER"))

    @action(
        description=_("pull emails"),
        url_path="pull-emails",
        icon="mail"
    )
    def pull_emails_action(self, request, object_id: int):
        from invoices.tasks import fetch_email_invoices_task
        try:
            fetch_email_invoices_task.delay(object_id)
            messages.success(request, _("Email fetching task has been triggered in the background."))
        except Exception as e:
            messages.error(request, _(f"Error triggering task: {str(e)}"))
            
        return redirect(request.META.get("HTTP_REFERER"))


@admin.register(ConfigurationList)
class ConfigurationListAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("key", "get_values_preview", "company")
    search_fields = ("key",)
    list_filter = ("company",)
    autocomplete_fields = ("company",)
    fieldsets = (
        (None, {
            "fields": ("key", "company", "values")
        }),
    )

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    @admin.display(description=_("Values"))
    def get_values_preview(self, obj):
        if obj.values:
            preview = ", ".join(obj.values[:5])
            if len(obj.values) > 5:
                preview += f" ... (+{len(obj.values) - 5} more)"
            return preview
        return "—"