from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.mail import send_mail
from django.conf import settings
from .models import EmployeeDocument, MachineDocument
from assets.models import AssetDocument
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _  


@shared_task
def send_document_reminders():
    """
    Checks EmployeeDocument, MachineDocument, and AssetDocument for
    upcoming expirations logic and triggers an email reminder.
    """

    document_models = [
        (_("Asset Document"), AssetDocument)
        (_("Employee"), EmployeeDocument),
        (_("Machine"), MachineDocument),
    ]

    today = timezone.now().date()
    reminders_sent = 0

    for model_name, model_class in document_models:
        docs = model_class.objects.filter(
            is_active=True,
            does_not_expire=False,
            expiry_date__isnull=False,
            reminder_sent_at__isnull=True
        ).select_related('document_type', 'company')

        for doc in docs:
            notify_days = getattr(doc.document_type, 'notify_before_days', 30)
            threshold_date = today + timedelta(days=notify_days)
            
            if doc.expiry_date <= threshold_date:
                company = doc.company
                owner_name = str(doc)
                
                # Fetch recipient email (Company user, company email, or settings default)
                recipient_email = getattr(company, 'email', None)
                if not recipient_email:
                    company_user = company.user_links.first()
                    if company_user and company_user.user.email:
                        recipient_email = company_user.user.email
                    else:
                        recipient_email = settings.DEFAULT_FROM_EMAIL

                subject = f"Recordatorio de expiración de documento: {doc.document_type.name}"
                
                context = {
                    'doc_type_name': doc.document_type.name,
                    'model_name': model_name,
                    'owner_name': owner_name,
                    'expiry_date': doc.expiry_date,
                }
                
                
                
                html_message = render_to_string('company/email/document_reminder.html', context)
                plain_message = strip_tags(html_message)

                send_mail(
                        subject,
                        plain_message,
                        settings.DEFAULT_FROM_EMAIL,
                        [recipient_email],
                        html_message=html_message,
                        fail_silently=False,
                    )
                    
                    # Mark as reminded
                doc.reminder_sent_at = timezone.now()
                doc.save(update_fields=['reminder_sent_at'])
                reminders_sent += 1

    return f"Processed {reminders_sent} reminders."
