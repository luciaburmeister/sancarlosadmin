"""
Quote Send Email view — recipient picker with CC support.

Replaces the previous one-click send action with an intermediate form
where the user can select which emails to send to and see that they
will be CC'd automatically.
"""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin

from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget


from core.tasks import send_quote_email_task
from sales.models import Quote


# ─────────────────────────────────────────────────────────────────────────────
# Form
# ─────────────────────────────────────────────────────────────────────────────

class QuoteSendEmailForm(forms.Form):
    """Dynamic form whose choices are built from the quote's client."""

    to_emails = forms.MultipleChoiceField(
        label=_("Recipients"),
        widget=UnfoldAdminCheckboxSelectMultipleWidget,
        error_messages={
            "required": _("Please select at least one recipient."),
        },
    )

    def __init__(self, *args, quote=None, **kwargs):
        super().__init__(*args, **kwargs)

        choices = []
        initial = []

        if quote and quote.client:
            # Primary client email
            if quote.client.email:
                label = f"{quote.client.email} — {_('Primary')}"
                choices.append((quote.client.email, label))
                initial.append(quote.client.email)

            # Contact emails
            contacts = (
                quote.client.contacts
                .exclude(email="")
                .values_list("name", "email", "position")
            )
            for name, email, position in contacts:
                suffix = f" · {position}" if position else ""
                label = f"{email} — {name}{suffix}"
                choices.append((email, label))

        self.fields["to_emails"].choices = choices
        self.fields["to_emails"].initial = initial


# ─────────────────────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────────────────────

class QuoteSendEmailView(UnfoldModelAdminViewMixin, View):
    """
    GET  → Show recipient picker (client email + contacts).
    POST → Queue the email with selected recipients + CC the sender.
    """

    title = _("Send Quote Email")
    permission_required = ("sales.change_quote",)
    template_name = "admin/sales/quote_send_email.html"

    def _get_quote(self):
        return get_object_or_404(
            Quote.objects.select_related("client", "company"),
            pk=self.kwargs["quote_id"],
        )

    def has_permission(self):
        return self.request.user.has_perms(self.get_permission_required())

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission():
            return self.handle_no_permission()
        if request.method == "POST":
            return self.post(request, *args, **kwargs)
        return self.get(request, *args, **kwargs)

    def get(self, request, quote_id, **kwargs):
        quote = self._get_quote()
        form = QuoteSendEmailForm(quote=quote)
        return self._render(request, quote, form)

    def post(self, request, quote_id, **kwargs):
        quote = self._get_quote()
        form = QuoteSendEmailForm(request.POST, quote=quote)

        if not form.is_valid():
            return self._render(request, quote, form)

        to_emails = form.cleaned_data["to_emails"]

        # CC the sender if they have an email
        cc_emails = []
        if request.user.email:
            cc_emails.append(request.user.email)

        # Queue the email
        send_quote_email_task.delay(
            quote.id,
            to_emails=to_emails,
            cc_emails=cc_emails,
        )

        recipients_str = ", ".join(to_emails)
        messages.success(
            request,
            _("Quote email queued for %(recipients)s.") % {"recipients": recipients_str},
        )

        # Redirect back to where the user came from
        redirect_to = request.GET.get(
            "redirect_to", request.POST.get("redirect_to", "")
        )
        if redirect_to == "changelist":
            return redirect(reverse("admin:sales_quote_changelist"))
        return redirect(reverse("admin:sales_quote_change", args=[quote.pk]))

    def _render(self, request, quote, form):
        redirect_to = request.GET.get(
            "redirect_to", request.POST.get("redirect_to", "")
        )
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": _("Send Quote Email — %s") % quote,
            "quote": quote,
            "form": form,
            "sender_email": request.user.email,
            "cancel_url": reverse("admin:sales_quote_change", args=[quote.pk]),
            "redirect_to": redirect_to,
            "opts": Quote._meta,
            "has_permission": True,
            "media": self.model_admin.media + form.media,
        }
        return TemplateResponse(request, self.template_name, context)
