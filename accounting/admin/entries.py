from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import JournalEntry, JournalEntryLine, EntryStatus

class JournalEntryLineInline(TabularInline):
    model = JournalEntryLine
    extra = 0
    autocomplete_fields = ["account"]
    fields = ["account", "description", "debit", "credit"]
    
    # We might want to make it readonly if entry is posted?
    # But CompanyRestrictedAdmin or ModelAdmin usually handle this?
    # We implemented validation in clean(), showing error is fine.
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
             return [f.name for f in self.model._meta.fields]
        return super().get_readonly_fields(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(JournalEntry)
class JournalEntryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "date", "journal", "company", "status", "total_debit", "total_credit", "is_balanced"]
    list_filter = ["company", "journal", "status", "date"]
    search_fields = ["number", "reference", "description"]
    
    ordering = ["-date", "-number"]
    list_select_related = ["company", "journal"]
    autocomplete_fields = ["company","journal","purchase_invoice","sales_invoice"]
    date_hierarchy = "date"
    
    inlines = [JournalEntryLineInline]
    
    readonly_fields = ["posted_at", "posted_by", "number"]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.status == EntryStatus.POSTED:
            # Lock everything except maybe reference?
            return [f.name for f in self.model._meta.fields]
        return readonly

    actions = ["post_selected"]
    actions_detail = ["post_detail_action"]

    def has_post_journalentry_permission(self, request, object_id=None):
        return request.user.has_perm("accounting.change_journalentry")

    @staticmethod
    def _post(request, entry):
        """Post one entry in its own savepoint; the ledger's refusal as a message, or None."""
        from company.models import CompanyUser

        posted_by = CompanyUser.objects.filter(user=request.user, company_id=entry.company_id).first()
        try:
            with transaction.atomic():
                entry.post(user=posted_by)
        except ValidationError as e:
            return " ".join(e.messages)
        return None

    @admin.action(description=_("Post selected entries"), permissions=["post_journalentry"])
    def post_selected(self, request, queryset):
        posted, skipped, failed = [], 0, []
        for entry in queryset.select_related("company", "journal").order_by("date", "pk"):
            if entry.status != EntryStatus.DRAFT:
                skipped += 1
                continue
            error = self._post(request, entry)
            if error:
                failed.append(f"{entry.reference or entry.pk}: {error}")
            else:
                posted.append(entry.number)
        if posted:
            self.message_user(request, _("%(count)s entries posted.") % {"count": len(posted)})
        if skipped:
            self.message_user(
                request, _("%(count)s were not drafts and were left as they are.") % {"count": skipped},
                level=messages.WARNING,
            )
        for message in failed[:10]:
            self.message_user(request, message, level=messages.ERROR)
        if len(failed) > 10:
            self.message_user(
                request, _("…and %(count)s more could not be posted.") % {"count": len(failed) - 10},
                level=messages.ERROR,
            )

    @action(description=_("Post"), icon="task_alt", permissions=["post_journalentry"])
    def post_detail_action(self, request, object_id):
        entry = get_object_or_404(self.get_queryset(request), pk=object_id)
        if entry.status != EntryStatus.DRAFT:
            self.message_user(request, _("Only draft entries can be posted."), level=messages.WARNING)
        else:
            error = self._post(request, entry)
            if error:
                self.message_user(request, error, level=messages.ERROR)
            else:
                self.message_user(request, _("Journal entry %s posted.") % entry.number)
        return redirect(reverse("admin:accounting_journalentry_change", args=[object_id]))

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == EntryStatus.POSTED and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        obj.delete(allow_posted=request.user.is_superuser)

    def delete_queryset(self, request, queryset):
        if not request.user.is_superuser:
            posted = queryset.filter(status=EntryStatus.POSTED)
            if posted.exists():
                self.message_user(
                    request,
                    _("%(count)s posted entries were kept: reverse them instead.") % {"count": posted.count()},
                    level=messages.WARNING,
                )
                queryset = queryset.exclude(status=EntryStatus.POSTED)
        for obj in queryset:
            obj.delete(allow_posted=request.user.is_superuser)

    def save_model(self, request, obj, form, change):
        # We can handle posting action here perhaps? 
        # For now just standard save.
        if not obj.pk:
             # creation
             pass
        super().save_model(request, obj, form, change)
