from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import display, action
from unfold.widgets import UnfoldAdminSelect2Widget

from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies

from .forms import FeedbackQuestionForm
from .models import (
    FeedbackQuestionnaire,
    FeedbackQuestion,
    FeedbackQuestionOption,
    FeedbackResponse,
    FeedbackAnswer,
    QuestionType,
)

OPTION_TYPES = {QuestionType.CHOICE, QuestionType.SCALE}


def _sync_options(question, options_text):
    """
    Sync FeedbackQuestionOption rows without deleting existing ones —
    updates text in place to preserve PKs that FeedbackAnswer rows reference.
    """
    texts = [t.strip() for t in (options_text or "").split(",") if t.strip()]
    existing = list(question.options.order_by("order"))

    for idx, text in enumerate(texts):
        if idx < len(existing):
            # Update existing row in place — preserves PK
            opt = existing[idx]
            if opt.text != text or opt.order != idx:
                opt.text  = text
                opt.order = idx
                opt.save(update_fields=["text", "order"])
        else:
            # Create new row
            FeedbackQuestionOption.objects.create(
                question=question, text=text, order=idx
            )

    # Delete any extra rows beyond the new list length
    if len(existing) > len(texts):
        ids_to_delete = [opt.pk for opt in existing[len(texts):]]
        FeedbackQuestionOption.objects.filter(pk__in=ids_to_delete).delete()


# ─────────────────────────────────────────────────────────────────────────────
# Model 1 — Questionnaire builder
# ─────────────────────────────────────────────────────────────────────────────

class FeedbackQuestionInline(TabularInline):
    model               = FeedbackQuestion
    form                = FeedbackQuestionForm
    extra               = 1
    verbose_name        = _("Pregunta")
    verbose_name_plural = _("Preguntas del cuestionario")

    fields = ("question_type", "text", "options_text", "order", "is_required")

    conditional_fields = {
        "options_text": (
            "question_type === 'choice' || "
            "question_type === 'scale'"
        ),
    }

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .prefetch_related("options")
            .order_by("order", "id")
        )

    def get_formset(self, request, obj=None, **kwargs):
        """
        Set the default value of `order` for new blank rows to the
        current question count so new questions always pre-fill with
        the next position number.
        """
        formset = super().get_formset(request, obj, **kwargs)
        next_order = obj.questions.count() if obj and obj.pk else 0
        formset.form.base_fields["order"].initial = next_order
        return formset


@admin.register(FeedbackQuestionnaire)
class FeedbackQuestionnaireAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display        = ("name", "company", "question_count", "is_active", "updated_at")
    list_filter         = ("is_active", "company")
    search_fields       = ("name", "company__name")
    autocomplete_fields = ("company",)
    inlines             = [FeedbackQuestionInline]

    fieldsets = (
        (_("Configuración del cuestionario"), {
            "fields": ("company", "name", "is_active"),
        }),
    )

    def save_formset(self, request, form, formset, change):
        """
        Saves questions, syncs options, and re-sequences order values
        by the user-supplied order field so gaps/duplicates are resolved.
        """
        instances = formset.save(commit=False)
        for instance in instances:
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()

        # Collect all non-deleted, saved forms sorted by the submitted order value
        saved = []
        for f in formset.forms:
            if f in formset.deleted_forms:
                continue
            instance = f.instance
            if not instance.pk:
                continue
            try:
                submitted_order = int(f.cleaned_data.get("order", 0) or 0)
            except (ValueError, TypeError):
                submitted_order = 0
            saved.append((submitted_order, instance, f))

        # Sort by submitted order then re-assign clean 0-based sequence
        saved.sort(key=lambda x: x[0])
        for position, (_, instance, f) in enumerate(saved):
            instance.order = position
            instance.save(update_fields=["order"])
            if instance.question_type in {qt.value for qt in OPTION_TYPES}:
                _sync_options(instance, f.cleaned_data.get("options_text", ""))
            else:
                instance.options.all().delete()

    @display(description=_("Nº Preguntas"))
    def question_count(self, obj):
        count = obj.questions.count()
        if count == 0:
            return format_html('<span class="text-gray-400">0</span>')
        return count


@admin.register(FeedbackQuestion)
class FeedbackQuestionAdmin(ModelAdmin):
    search_fields = ("text",)

    def get_model_perms(self, request):
        return {}


@admin.register(FeedbackQuestionOption)
class FeedbackQuestionOptionAdmin(ModelAdmin):
    search_fields = ("text",)

    def get_model_perms(self, request):
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Model 2 — Feedback Response
# ─────────────────────────────────────────────────────────────────────────────

class FeedbackAnswerInline(StackedInline):
    """
    Read-only inline showing all answers inside the FeedbackResponse change form.
    The agent can review every question and its saved answer without leaving the page.
    """
    model               = FeedbackAnswer
    extra               = 0
    can_delete          = False
    verbose_name        = _("Respuesta")
    verbose_name_plural = _("Respuestas del cuestionario")

    fields         = ("display_question", "display_answer")
    readonly_fields = ("display_question", "display_answer")

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("question", "selected_option")
            .order_by("question__order", "question__id")
        )

    @display(description=_("Pregunta"))
    def display_question(self, obj):
        badge_colors = {
            "choice":   ("bg-blue-100",   "text-blue-700"),
            "yes_no":   ("bg-green-100",  "text-green-700"),
            "text":     ("bg-gray-100",   "text-gray-600"),
            "scale":    ("bg-yellow-100", "text-yellow-700"),
        }
        bg, fg = badge_colors.get(obj.question.question_type, ("bg-gray-100", "text-gray-600"))
        return format_html(
            '<div class="flex items-center gap-2">'
            '<span class="font-medium text-sm">{}</span>'
            '<span class="text-xs font-semibold px-2 py-0.5 rounded-full {} {}">{}</span>'
            '</div>',
            obj.question.text,
            bg, fg,
            obj.question.get_question_type_display(),
        )

    @display(description=_("Respuesta"))
    def display_answer(self, obj):
        if obj.selected_option_id:
            value = obj.selected_option.text
        elif obj.text_value:
            value = obj.text_value
        else:
            return format_html(
                '<span class="text-gray-400 text-sm italic">{}</span>',
                _("Sin respuesta")
            )
        return format_html(
            '<span class="font-semibold text-sm">{}</span>', value
        )


@admin.register(FeedbackResponse)
class FeedbackResponseAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "response_link", "quote_link", "display_client", "questionnaire",
        "agent", "call_date",
    )
    list_filter         = ("company", "call_date", "agent", "questionnaire")
    search_fields       = ("client__name", "quote__project_name",
                           "agent__first_name", "agent__last_name")
    autocomplete_fields = ("questionnaire", "agent", "quote")
    date_hierarchy      = "call_date"
    readonly_fields     = ("display_client", "fill_answers_link")
    actions_row         = ["go_to_fill"]
    inlines             = [FeedbackAnswerInline]

    # Remove add/delete/change icons from FK fields — keep only view
    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)
        companies = get_user_companies(request.user)
        if "questionnaire" in form.base_fields:
            form.base_fields["questionnaire"].queryset = (
                FeedbackQuestionnaire.objects.filter(
                    company__in=companies, is_active=True,
                ).select_related("company")
            )
        if "agent" in form.base_fields:
            from company.models import Employee
            form.base_fields["agent"].queryset = Employee.objects.filter(
                company__in=companies, is_active=True,
            ).order_by("last_name", "first_name")
        return form

    # Disable add/delete shortcuts on FK widgets
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name in ("questionnaire", "quote", "agent"):
            if hasattr(field, "widget"):
                field.widget.can_add_related    = False
                field.widget.can_change_related = False
                field.widget.can_delete_related = False
        if db_field.name == "company":
            field.widget = UnfoldAdminSelect2Widget()
            field.widget.can_add_related    = False
            field.widget.can_change_related = False
            field.widget.can_delete_related = False
        return field

    fieldsets = (
        (_("Identificación"), {
            "fields": (("company", "questionnaire"),),
        }),
        (_("Presupuesto y cliente"), {
            "fields": (("quote", "display_client"),),
        }),
        (_("Datos de la llamada"), {
            "fields": (("agent", "call_date"), "notes"),
        }),
        (_("Respuestas"), {
            "fields": ("fill_answers_link",),
        }),
    )

    def save_model(self, request, obj, form, change):
        if obj.quote_id and not obj.company_id:
            obj.company_id = obj.quote.company_id
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        self._ensure_answers_exist(form.instance)

    def response_add(self, request, obj, post_url_continue=None):
        self._ensure_answers_exist(obj)
        return redirect(
            reverse("admin:feedback_feedbackresponse_fill", args=[obj.pk])
        )

    def _ensure_answers_exist(self, obj):
        if not obj.questionnaire_id:
            return
        existing_ids = set(obj.answers.values_list("question_id", flat=True))
        new_answers = [
            FeedbackAnswer(response=obj, question=q)
            for q in FeedbackQuestion.objects.filter(
                questionnaire=obj.questionnaire
            ).order_by("order", "id")
            if q.id not in existing_ids
        ]
        if new_answers:
            FeedbackAnswer.objects.bulk_create(new_answers)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial["call_date"] = timezone.localdate()
        return initial

    def get_urls(self):
        from .views import FeedbackResponseFillView
        urls = super().get_urls()
        return [
            path(
                "<int:pk>/fill/",
                self.admin_site.admin_view(
                    FeedbackResponseFillView.as_view(model_admin=self)
                ),
                name="feedback_feedbackresponse_fill",
            ),
        ] + urls

    @action(description=_("Completar respuestas"), icon="edit_note")
    def go_to_fill(self, request, object_id: int):
        return redirect(
            reverse("admin:feedback_feedbackresponse_fill", args=[object_id])
        )

    @display(description=_("Feedback"))
    def response_link(self, obj):
        url = reverse("admin:feedback_feedbackresponse_change", args=[obj.pk])
        client_name = (
            obj.quote.client.name.upper()
            if obj.quote_id and obj.quote.client else "—"
        )
        date_str = obj.call_date.strftime("%Y-%m-%d") if obj.call_date else "—"
        label = f"{client_name} ({date_str})"
        return format_html('<a href="{}" class="font-medium">{}</a>', url, label)

    @display(description=_("ID"))
    def display_id(self, obj):
        return f"#{obj.pk}"

    @display(description=_("Cliente"), label=True)
    def display_client(self, obj):
        if obj.quote_id and obj.quote.client:
            return obj.quote.client.name
        return "—"

    @display(description=_("Presupuesto"))
    def quote_link(self, obj):
        if obj.quote_id:
            url = reverse("admin:sales_quote_change", args=[obj.quote_id])
            return format_html('<a href="{}">{}</a>', url, obj.quote)
        return "—"

    @display(description=_("Progreso"))
    def answer_progress(self, obj):
        count = obj.answers.exclude(
            selected_option__isnull=True, text_value=""
        ).count()
        total = obj.questionnaire.questions.count() if obj.questionnaire_id else 0
        if total == 0:
            return "—"
        color = "success" if count == total else "warning"
        return format_html(
            '<span class="text-{}-600 font-semibold">{}/{}</span>',
            color, count, total,
        )

    @display(description=_("Respuestas"))
    def fill_answers_link(self, obj):
        if not obj.pk:
            return format_html(
                '<span class="text-gray-400 text-sm">{}</span>',
                _("Guarda primero para poder completar las respuestas.")
            )
        url = reverse("admin:feedback_feedbackresponse_fill", args=[obj.pk])
        answered = obj.answers.exclude(
            selected_option__isnull=True, text_value=""
        ).count()
        total = obj.questionnaire.questions.count() if obj.questionnaire_id else 0
        progress = f"{answered}/{total}" if total else ""
        return format_html(
            '<a href="{}" class="flex items-center gap-2 font-medium '
            'text-primary-600 hover:text-primary-700">'
            '<span class="material-symbols-outlined" style="font-size:1.1rem;">edit_note</span>'
            '{}{}</a>',
            url,
            _("Completar respuestas"),
            format_html(
                ' <span class="ml-1 text-xs font-semibold px-2 py-0.5 rounded-full '
                'bg-gray-100 text-gray-600">{}</span>', progress
            ) if progress else "",
        )