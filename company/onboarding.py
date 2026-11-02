from django import forms
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from company.models import (
    BankAccount, Company, CompanyDefaultTax, CompanyModule, CompanyUser, ConsentStatus, Employee,
)
from general.onboarding import MISSING, OK, WARNING, Check, OnboardingStep, Section, register
from general.onboarding.forms import UnfoldFormMixin


def _filled(label, value, missing_status, why, url=None):
    return Check(label, OK, str(value), url) if value else Check(label, missing_status, why, url)


class CompanyDetailsForm(UnfoldFormMixin, forms.ModelForm):
    class Meta:
        model = Company
        fields = [
            "legal_name", "tax_identifier", "address", "email", "phone", "logo", "sepa_creditor_id",
            "invoice_number_format", "proforma_number_format", "debit_note_number_format",
            "credit_note_number_format",
        ]


@register
class CompanyDetailsStep(OnboardingStep):
    key = "details"
    title = gettext_lazy("Company details")
    description = gettext_lazy("What your documents print about you, and how your invoices are numbered.")
    order = 10
    form_class = CompanyDetailsForm

    def get_form_kwargs(self):
        return {"instance": self.company}

    def sections(self):
        company = self.company
        printed = _("Printed on every invoice.")
        return [
            Section(_("Identity"), [
                _filled(_("Legal name"), company.legal_name, MISSING, printed),
                _filled(_("Tax identifier"), company.tax_identifier, MISSING, printed),
                _filled(_("Address"), company.address, MISSING, printed),
                _filled(_("Logo"), company.logo, MISSING, _("Printed on every document.")),
                _filled(_("Email"), company.email, WARNING, _("Clients see it on your documents.")),
                _filled(_("Phone"), company.phone, WARNING, _("Clients see it on your documents.")),
                _filled(
                    _("SEPA creditor identifier"), company.sepa_creditor_id, WARNING,
                    _("Only needed to collect by direct debit."),
                ),
            ]),
            Section(_("Numbering"), [
                Check(_("Invoices"), OK, company.invoice_number_format or "INV-{year}-{number}"),
                Check(_("Proformas"), OK, company.proforma_number_format or "PF-{year}-{number}"),
                Check(_("Debit notes"), OK, company.debit_note_number_format or "SUP-{year}-{number}"),
                Check(_("Credit notes"), OK, company.credit_note_number_format or "R-{year}-{number}"),
            ]),
        ]

    def save(self, form, request):
        form.save()
        return _("Company details saved.")


class InviteUserForm(UnfoldFormMixin, forms.Form):
    email = forms.EmailField(label=gettext_lazy("Email"))
    first_name = forms.CharField(label=gettext_lazy("First name"), max_length=50)
    last_name = forms.CharField(label=gettext_lazy("Last name"), max_length=50)
    is_admin = forms.BooleanField(
        label=gettext_lazy("Can set up the company"), required=False,
        help_text=gettext_lazy("Lets them complete this setup and manage the company's settings."),
    )


@register
class UsersStep(OnboardingStep):
    key = "users"
    title = gettext_lazy("Users")
    description = gettext_lazy(
        "Who works in the ERP. Each person is invited by email to choose a password, "
        "and becomes an employee so they can use the mobile app."
    )
    order = 20
    form_class = InviteUserForm
    submit_label = gettext_lazy("Invite")

    def sections(self):
        links = CompanyUser.objects.filter(company=self.company, is_active=True).select_related("user")
        employee_user_ids = set(
            Employee.objects.filter(company=self.company, user__isnull=False).values_list("user_id", flat=True)
        )
        section = Section(_("People"))
        for link in links.order_by("user__first_name", "user__email"):
            user = link.user
            label = user.get_full_name() or user.email or user.username
            if user.last_login is None:
                section.checks.append(Check(label, WARNING, _("Invited; hasn't signed in yet.")))
            elif user.pk not in employee_user_ids:
                section.checks.append(Check(
                    label, WARNING, _("Not an employee here: can't use the mobile app or the production terminal."),
                ))
            else:
                section.checks.append(Check(label, OK, user.email))
        if not section.checks:
            section.checks.append(Check(_("Users"), MISSING, _("Nobody can work in the company yet.")))
        return [section]

    def save(self, form, request):
        from company.services.onboarding import invite_user

        data = form.cleaned_data
        user = invite_user(
            self.company, email=data["email"], first_name=data["first_name"], last_name=data["last_name"],
            is_admin=data["is_admin"], base_url=request.build_absolute_uri("/"),
        )
        return _("Invitation sent to %(email)s.") % {"email": user.email}


class DefaultTaxForm(UnfoldFormMixin, forms.ModelForm):
    class Meta:
        model = CompanyDefaultTax
        fields = ["tax_type", "selected_percentage"]

    def __init__(self, *args, company, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["tax_type"].queryset = self.fields["tax_type"].queryset.filter(is_active=True)

    def clean_tax_type(self):
        tax_type = self.cleaned_data["tax_type"]
        if CompanyDefaultTax.objects.filter(company=self.company, tax_type=tax_type).exists():
            raise forms.ValidationError(_("This tax is already one of the defaults."))
        return tax_type

    def save(self, commit=True):
        self.instance.company = self.company
        return super().save(commit)


@register
class TaxesStep(OnboardingStep):
    key = "taxes"
    title = gettext_lazy("Default taxes")
    description = gettext_lazy("Applied to new items and configurations when nothing else says which tax they pay.")
    order = 40
    form_class = DefaultTaxForm
    submit_label = gettext_lazy("Add")

    def get_form_kwargs(self):
        return {"company": self.company}

    def sections(self):
        section = Section(_("Default taxes"), [
            Check(str(default.tax_type), OK, f"{default.selected_percentage}%")
            for default in CompanyDefaultTax.objects.filter(company=self.company).select_related("tax_type")
        ])
        if not section.checks:
            section.checks.append(Check(_("Default taxes"), MISSING, _("New items would be created without tax.")))
        return [section]

    def save(self, form, request):
        default = form.save()
        return _("%(tax)s added.") % {"tax": default.tax_type}


class BankAccountForm(UnfoldFormMixin, forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["bank_name", "holder_name", "iban", "swift_bic", "is_default"]

    def __init__(self, *args, company, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        self.instance.company = self.company
        return super().save(commit)


@register
class BankAccountsStep(OnboardingStep):
    key = "bank"
    title = gettext_lazy("Bank accounts")
    description = gettext_lazy(
        "Connect your bank so its accounts and movements come in on their own, and payments are matched "
        "to invoices. The default account is printed on your documents."
    )
    order = 50
    form_class = BankAccountForm
    submit_label = gettext_lazy("Add")
    fallback_form_title = gettext_lazy("Your bank isn't listed? Enter an account by hand")

    def get_form_kwargs(self):
        return {"company": self.company}

    def connect_url(self, account=None):
        """The bank connection, locked to this company, coming back to this step when done."""
        params = {
            "company_id": self.company.pk,
            "next": reverse("admin:company_company_onboarding_step", args=[self.company.pk, self.key]),
        }
        if account is not None:
            params["bank_account_id"] = account.pk
        return f"{reverse('admin:banking_connect_bank')}?{urlencode(params)}"

    def actions(self):
        return [(_("Connect your bank"), self.connect_url(), "account_balance")]

    def sections(self):
        accounts = list(BankAccount.objects.filter(company=self.company).order_by("-is_default", "bank_name"))
        section = Section(_("Bank accounts"))
        for account in accounts:
            label = " · ".join(filter(None, [account.bank_name, _("default") if account.is_default else ""]))
            if not account.nuek_token:
                section.checks.append(Check(
                    label, WARNING, _("Entered by hand: its movements aren't imported. Connect it to import them."),
                    self.connect_url(),
                ))
            elif account.consent_status == ConsentStatus.INVALID:
                section.checks.append(Check(
                    label, WARNING, _("The bank connection has expired: reconnect it to keep importing movements."),
                    self.connect_url(account),
                ))
            else:
                section.checks.append(Check(label, OK, " · ".join(filter(None, [account.iban, _("connected")]))))
        if not accounts:
            section.checks.append(Check(
                _("Bank accounts"), MISSING,
                _("Documents can't tell clients where to pay, and payments can't be booked."), self.connect_url(),
            ))
        elif not any(account.is_default for account in accounts):
            section.checks.append(Check(
                _("Default account"), WARNING, _("None is the default: documents print no account."),
                reverse("admin:company_bankaccount_changelist"),
            ))
        return [section]

    def save(self, form, request):
        account = form.save()
        return _("%(bank)s added.") % {"bank": account.bank_name}


@register
class OtherSettingsStep(OnboardingStep):
    key = "other"
    title = gettext_lazy("Other settings")
    description = gettext_lazy("Not needed to start; worth a look once the company is running.")
    order = 100
    required = False

    def sections(self):
        from company.models import CompanyEmailConfig
        from invoices.models import PaymentReminderSettings

        company = self.company
        change_url = reverse("admin:company_company_change", args=[company.pk])
        inbox = CompanyEmailConfig.objects.filter(company=company).first()
        checks = [
            Check(_("Invoice inbox"), OK, inbox.email_address,
                  reverse("admin:company_companyemailconfig_change", args=[inbox.pk]))
            if inbox else
            Check(_("Invoice inbox"), WARNING, _("Provider invoices emailed to a mailbox are read automatically."),
                  reverse("admin:company_companyemailconfig_add")),
            Check(_("Payment reminders"), OK, "", reverse("admin:invoices_paymentremindersettings_changelist"))
            if PaymentReminderSettings.objects.filter(company=company).exists() else
            Check(_("Payment reminders"), WARNING, _("Clients aren't reminded of overdue invoices."),
                  reverse("admin:invoices_paymentremindersettings_changelist")),
            _filled(_("Pending invoices digest"), ", ".join(company.invoice_digest_emails), WARNING,
                    _("Nobody receives the daily summary of invoices to pay and collect."), change_url),
        ]
        if CompanyModule.PAYROLL in company.enabled_modules:
            from payroll.models import CompanyPayrollConfig

            checks.append(
                Check(_("Payroll"), OK, "", reverse("admin:payroll_companypayrollconfig_changelist"))
                if CompanyPayrollConfig.objects.filter(company=company).exists() else
                Check(_("Payroll"), WARNING, _("Payroll isn't configured."),
                      reverse("admin:payroll_companypayrollconfig_add"))
            )
        return [Section(_("Other settings"), checks)]
