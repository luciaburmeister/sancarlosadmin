"""
Reusable "send document by email" flow for admin documents tied to a client.

Shows a recipient picker built from the client's primary email + contact
emails, CCs the sender, points Reply-To at the sender, and queues a Celery
task chosen by the subclass. Used by quotes and delivery orders; new document
types only need a small subclass of BaseSendDocumentEmailView plus two admin
actions.
"""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from general.email import sender_reply_to
from unfold.views import UnfoldModelAdminViewMixin
from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget


class ClientRecipientsForm(forms.Form):
    """Recipient picker whose choices are built from a client's emails."""

    to_emails = forms.MultipleChoiceField(
        label=_("Recipients"),
        widget=UnfoldAdminCheckboxSelectMultipleWidget,
        error_messages={
            "required": _("Please select at least one recipient."),
        },
    )

    def __init__(self, *args, client=None, **kwargs):
        super().__init__(*args, **kwargs)

        choices = []
        initial = []

        if client:
            # Primary client email
            if client.email:
                label = f"{client.email} — {_('Primary')}"
                choices.append((client.email, label))
                initial.append(client.email)

            # Contact emails
            contacts = (
                client.contacts
                .exclude(email="")
                .values_list("name", "email", "position")
            )
            for name, email, position in contacts:
                suffix = f" · {position}" if position else ""
                label = f"{email} — {name}{suffix}"
                choices.append((email, label))

        self.fields["to_emails"].choices = choices
        self.fields["to_emails"].initial = initial


class BaseSendDocumentEmailView(UnfoldModelAdminViewMixin, View):
    """
    GET  → Show recipient picker (client email + contacts).
    POST → Queue the email with selected recipients, CC the sender and send
           the client's replies back to them.

    Subclasses set `model`, `pk_url_kwarg`, `title`, `permission_required`
    and implement `queue_email(obj, to_emails, cc_emails, reply_to)`.
    """

    model = None
    pk_url_kwarg = "object_id"
    template_name = "admin/core/send_document_email.html"
    select_related = ("client", "company")

    def get_object(self):
        qs = self.model.objects.select_related(*self.select_related)
        return get_object_or_404(qs, pk=self.kwargs[self.pk_url_kwarg])

    def get_client(self, obj):
        return obj.client

    def queue_email(self, obj, to_emails, cc_emails, reply_to):
        raise NotImplementedError

    def has_permission(self):
        return self.request.user.has_perms(self.get_permission_required())

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission():
            return self.handle_no_permission()
        if request.method == "POST":
            return self.post(request, *args, **kwargs)
        return self.get(request, *args, **kwargs)

    def _admin_url(self, suffix, *args):
        opts = self.model._meta
        return reverse(f"admin:{opts.app_label}_{opts.model_name}_{suffix}", args=args)

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        form = ClientRecipientsForm(client=self.get_client(obj))
        return self._render(request, obj, form)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        form = ClientRecipientsForm(request.POST, client=self.get_client(obj))

        if not form.is_valid():
            return self._render(request, obj, form)

        to_emails = form.cleaned_data["to_emails"]

        # CC the sender if they have an email, and route replies back to them
        cc_emails = []
        if request.user.email:
            cc_emails.append(request.user.email)

        self.queue_email(obj, to_emails, cc_emails, sender_reply_to(request.user))

        recipients_str = ", ".join(to_emails)
        messages.success(
            request,
            _("Email queued for %(recipients)s.") % {"recipients": recipients_str},
        )

        redirect_to = request.GET.get(
            "redirect_to", request.POST.get("redirect_to", "")
        )
        if redirect_to == "changelist":
            return redirect(self._admin_url("changelist"))
        return redirect(self._admin_url("change", obj.pk))

    def _render(self, request, obj, form):
        redirect_to = request.GET.get(
            "redirect_to", request.POST.get("redirect_to", "")
        )
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": _("Send Email — %s") % obj,
            "document": obj,
            "client": self.get_client(obj),
            "form": form,
            "sender_email": request.user.email,
            "cancel_url": self._admin_url("change", obj.pk),
            "changelist_url": self._admin_url("changelist"),
            "redirect_to": redirect_to,
            "opts": self.model._meta,
            "has_permission": True,
            "media": self.model_admin.media + form.media,
        }
        return TemplateResponse(request, self.template_name, context)
