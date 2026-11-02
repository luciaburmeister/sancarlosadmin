"""
Setting up a new company: what staff create, and the people they invite.

Staff create the company's shell with `create_company_shell`: the company, its
installed chart of accounts, a default business unit, this year's fiscal year
and its first admin, who gets an email to choose a password. That admin then
completes the setup themselves from the company's onboarding page, inviting
the rest of the team with `invite_user`.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.translation import gettext as _

from company.active_company import ActiveCompany
from company.models import Company, CompanyUser, Employee

COMPANY_ADMIN_GROUP = "Company admin"

# What a customer's admin needs to set up and run their own company. Every
# model here is company-restricted in the admin, so they only reach their own.
COMPANY_ADMIN_PERMISSIONS = [
    "company.can_onboard_company",
    "company.view_company",
    "company.change_company",
    "company.view_bankaccount",
    "company.add_bankaccount",
    "company.change_bankaccount",
    # Connecting a bank account through Arcopay
    "banking.view_banktransaction",
    "company.view_employee",
    "company.add_employee",
    "company.change_employee",
    "company.view_companyemailconfig",
    "company.add_companyemailconfig",
    "company.change_companyemailconfig",
    "accounting.view_companychartofaccounts",
    "accounting.change_companychartofaccounts",
    "accounting.view_companyaccount",
    "accounting.change_companyaccount",
    "accounting.view_fiscalyear",
    "accounting.add_fiscalyear",
    "accounting.change_fiscalyear",
    "accounting.view_businessunit",
    "accounting.add_businessunit",
    "accounting.change_businessunit",
    "accounting.view_journalentry",
    "invoices.view_taxtype",
    "invoices.view_salesinvoice",
    "invoices.view_invoice",
    "sales.view_client",
    "sales.add_client",
    "sales.change_client",
    "materials.view_provider",
    "materials.add_provider",
    "materials.change_provider",
    "inventory.view_warehouse",
    "inventory.add_warehouse",
    "inventory.change_warehouse",
    "inventory.view_inventoryitem",
    "inventory.add_inventoryitem",
    "inventory.change_inventoryitem",
    "production.view_productionresource",
    "production.add_productionresource",
    "production.change_productionresource",
    "einvoicing.view_einvoicingaccount",
    "einvoicing.add_einvoicingaccount",
    "einvoicing.change_einvoicingaccount",
    "payroll.view_companypayrollconfig",
    "payroll.add_companypayrollconfig",
    "payroll.change_companypayrollconfig",
]


def company_admin_group():
    """The group every customer admin is in, with exactly `COMPANY_ADMIN_PERMISSIONS`."""
    group, _created = Group.objects.get_or_create(name=COMPANY_ADMIN_GROUP)
    permissions = []
    for dotted in COMPANY_ADMIN_PERMISSIONS:
        app_label, codename = dotted.split(".")
        permissions.append(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    group.permissions.set(permissions)
    return group


@transaction.atomic
def create_company_shell(
    *, name, legal_name, tax_identifier, logo, constituted_country, chart_template, modules,
    admin_email, admin_first_name, admin_last_name, base_url,
):
    """
    A company with what every company needs before anyone can work in it,
    and its first admin invited to complete the rest.
    """
    from accounting.models import BusinessUnit, CompanyChartOfAccounts
    from accounting.services.chart_installer import install_chart_standard

    company = Company.objects.create(
        name=name,
        legal_name=legal_name,
        tax_identifier=tax_identifier,
        logo=logo,
        constituted_country=constituted_country,
        enabled_modules=list(modules),
    )
    CompanyChartOfAccounts.objects.create(company=company, template=chart_template)
    install_chart_standard(company, chart_template)
    BusinessUnit.objects.create(company=company, name=_("General"), is_default=True)
    ensure_fiscal_year(company, timezone.localdate().year)
    invite_user(
        company, email=admin_email, first_name=admin_first_name, last_name=admin_last_name,
        is_admin=True, base_url=base_url,
    )
    # Superusers' list of companies to switch to is cached
    cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
    return company


def ensure_fiscal_year(company, year):
    """A calendar fiscal year `year`, unless one already covers part of it."""
    from accounting.models import FiscalYear

    start, end = date(year, 1, 1), date(year, 12, 31)
    covering = FiscalYear.objects.filter(company=company, start_date__lte=end, end_date__gte=start)
    if not covering.exists():
        FiscalYear.objects.create(company=company, name=str(year), start_date=start, end_date=end)


@transaction.atomic
def invite_user(company, *, email, first_name, last_name, is_admin=False, base_url):
    """
    Give `email` access to `company`, as an employee, and email them how to get in.
    `base_url` is the site's scheme and host the email links to.

    An unknown address gets a new staff user, who chooses a password from the
    email; a known one keeps their account and password and is just told
    they've been added. Someone already an employee of another company stays
    that company's employee (an account is the employee of one company only).
    """
    User = get_user_model()
    email = email.strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User(username=email, email=email, first_name=first_name, last_name=last_name, is_staff=True)
        user.set_unusable_password()
        user.save()
    elif not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])

    CompanyUser.objects.update_or_create(user=user, company=company, defaults={"is_active": True})
    cache.delete(ActiveCompany.MEMBERSHIP_CACHE_KEY.format(user_pk=user.pk))

    if not Employee.objects.filter(user=user).exists():
        Employee.objects.create(
            company=company, user=user, first_name=first_name, last_name=last_name,
            email=email, hired_at=timezone.localdate(),
        )
    if is_admin:
        user.groups.add(company_admin_group())

    if user.has_usable_password():
        path = reverse("admin:index")
    else:
        path = reverse("password_reset_confirm", kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        })
    link = base_url.rstrip("/") + path
    transaction.on_commit(lambda: _send_invite(user, company, link))
    return user


def _send_invite(user, company, link):
    from core.services.email_service import EmailService

    EmailService.send_email(
        to=[user.email],
        subject=_("You've been invited to %(company)s") % {"company": company.name},
        body_html=render_to_string("company/email/onboarding_invite.html", {
            "user": user, "company": company, "link": link,
            "set_password": not user.has_usable_password(),
        }),
        company=company,
        email_document="onboarding_invite",
    )
