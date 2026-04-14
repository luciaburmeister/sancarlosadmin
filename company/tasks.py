from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.mail import send_mail
from django.conf import settings
from django.db import models, transaction
from django.db.models import Exists, OuterRef
from .models import EmployeeDocument, MachineDocument, CompanyDocument
from assets.models import AssetDocument
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

@shared_task(name="debug_env")
def debug_env():
    import os
    return {k: v for k, v in os.environ.items() if "EMAIL" in k}


@shared_task(name="company.tasks.send_document_reminders")
def send_document_reminders():
    """
    Checks EmployeeDocument, MachineDocument, AssetDocument, and CompanyDocument for
    upcoming expirations logic and triggers an email reminder.
    Reminders are grouped by company to send a single summary email.
    Only sends for the latest version of a document type for each owner.
    """

    document_models = [
        (_("Asset Document"), AssetDocument, 'asset', 'assets', 'asset'),
        (_("Employee Document"), EmployeeDocument, 'employee', 'company', 'employee'),
        (_("Machine Document"), MachineDocument, 'machine', 'company', 'machine'),
        (_("Company Document"), CompanyDocument, 'company', 'company', 'company'),
    ]

    today = timezone.now().date()
    all_maturing_docs = []

    for model_label, model_class, owner_field, app_label, owner_model_name in document_models:
        # Subquery to check if there is a document with the same owner and document type
        # that has a later expiry date, or the same expiry date but a higher ID (more recent entry).
        newer_docs = model_class.objects.filter(
            **{owner_field: OuterRef(owner_field)},
            document_type=OuterRef('document_type') 
        ).filter(
            models.Q(expiry_date__gt=OuterRef('expiry_date')) |
            models.Q(expiry_date=OuterRef('expiry_date'), id__gt=OuterRef('id'))
        )

        docs = model_class.objects.annotate(
            has_newer=Exists(newer_docs)
        ).filter(
            has_newer=False,
            is_active=True,
            does_not_expire=False,
            expiry_date__isnull=False,
            reminder_sent_at__isnull=True
        ).select_related('document_type', 'company', owner_field)

        for doc in docs:
            notify_days = getattr(doc.document_type, 'notify_before_days', 30)
            threshold_date = today + timedelta(days=notify_days)
            
            if doc.expiry_date <= threshold_date:
                owner_obj = getattr(doc, owner_field)
                
                # Generate admin link
                admin_path = f"/admin/{app_label}/{owner_model_name}/{owner_obj.id}/change/"
                admin_url = f"{settings.BACKEND_URL.rstrip('/')}{admin_path}"
                
                all_maturing_docs.append({
                    'doc_instance': doc,
                    'company': doc.company,
                    'doc_type_name': doc.document_type.name,
                    'model_label': model_label,
                    'owner_name': str(owner_obj),
                    'expiry_date': doc.expiry_date,
                    'admin_url': admin_url
                })

    # Group by company
    company_groups = {}
    for item in all_maturing_docs:
        company = item['company']
        if company.id not in company_groups:
            company_groups[company.id] = {'company': company, 'items': []}
        company_groups[company.id]['items'].append(item)

    emails_sent = 0
    errors = []
    
    for group in company_groups.values():
        company = group['company']
        items = group['items']
        
        # Fetch recipient emails
        recipients = company.document_reminder_emails
        if not recipients:
            if company.email:
                recipients = [company.email]
            else:
                company_user = company.user_links.first()
                if company_user and company_user.user.email:
                    recipients = [company_user.user.email]
                else:
                    recipients = [settings.DEFAULT_FROM_EMAIL]

        subject = f"Recordatorio de expiración de documentos: {company.name}"
        
        context = {
            'company_name': company.name,
            'document_list': items,
        }
        
        try:
            with transaction.atomic():
                html_message = render_to_string('company/email/document_reminder.html', context)
                plain_message = strip_tags(html_message)

                send_mail(
                    subject,
                    plain_message,
                    settings.DEFAULT_FROM_EMAIL,
                    recipients,
                    html_message=html_message,
                    fail_silently=False,
                )
                
                # Mark all documents in this email as reminded
                for item in items:
                    doc = item['doc_instance']
                    doc.reminder_sent_at = timezone.now()
                    doc.save(update_fields=['reminder_sent_at'])
            
            emails_sent += 1
        except Exception as e:
            errors.append(f"Error processing company {company.id} ({company.name}): {str(e)}")

    if errors:
        raise Exception(f"Document reminder task completed with errors: {'; '.join(errors)}")

    return f"Processed {emails_sent} company emails for {len(all_maturing_docs)} documents."
