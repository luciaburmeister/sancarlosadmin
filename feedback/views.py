"""
feedback/views.py
─────────────────────────────────────────────────────────────────────────────
FeedbackResponseFillView — renders one Unfold-native form per question,
saves all answers on POST.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies

from .forms import build_answer_form, AnswerFormHelper
from .models import (
    FeedbackQuestion,
    FeedbackQuestionOption,
    FeedbackResponse,
    FeedbackAnswer,
    QuestionType,
)

OPTION_TYPES = {QuestionType.CHOICE}   # types that store selected_option FK
TEXT_TYPES   = {QuestionType.YES_NO, QuestionType.SCALE, QuestionType.TEXT}  # store text_value


class FeedbackResponseFillView(UnfoldModelAdminViewMixin, TemplateView):
    title               = _("Completar respuestas de feedback")
    permission_required = ()
    template_name       = "admin/feedback/feedbackresponse/fill.html"

    def get_response_obj(self):
        if not hasattr(self, "_response_obj"):
            self._response_obj = get_object_or_404(
                FeedbackResponse,
                pk=self.kwargs["pk"],
                company__in=get_user_companies(self.request.user),
            )
        return self._response_obj

    def _ensure_answers_exist(self, response_obj):
        existing_ids = set(
            response_obj.answers.values_list("question_id", flat=True)
        )
        new_answers = [
            FeedbackAnswer(response=response_obj, question=q)
            for q in FeedbackQuestion.objects.filter(
                questionnaire=response_obj.questionnaire
            ).order_by("order", "id")
            if q.id not in existing_ids
        ]
        if new_answers:
            FeedbackAnswer.objects.bulk_create(new_answers)

    def _build_answer_data(self, response_obj, post_data=None):
        """
        Returns a list of dicts:
          {
            answer:     FeedbackAnswer,
            question:   FeedbackQuestion,
            options:    list[FeedbackQuestionOption],
            form:       forms.Form (bound if post_data, unbound otherwise),
            field_name: str,
          }
        """
        answers = (
            FeedbackAnswer.objects
            .filter(response=response_obj)
            .select_related("question", "selected_option")
            .order_by("question__order", "question__id")
        )
        result = []
        for answer in answers:
            q = answer.question
            options = (
                list(FeedbackQuestionOption.objects.filter(question=q).order_by("order"))
                if q.question_type in {qt.value for qt in OPTION_TYPES}
                else []
            )
            form, field_name = build_answer_form(answer, q, options)

            # If POST data supplied, re-build as bound form
            if post_data is not None:
                bound_form, field_name = build_answer_form(answer, q, options)
                # Manually inject POST value
                form_class = type(bound_form)
                form = form_class(data=post_data, initial={})

            result.append({
                "answer":     answer,
                "question":   q,
                "options":    options,
                "form":       form,
                "field_name": field_name,
                "helper":     AnswerFormHelper(),
            })
        return result

    # ── GET ───────────────────────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        response_obj = self.get_response_obj()
        self._ensure_answers_exist(response_obj)
        answer_data = self._build_answer_data(response_obj)

        # Compute progress
        answered = sum(
            1 for item in answer_data
            if (
                item["answer"].selected_option_id
                or item["answer"].text_value.strip()
            )
        )

        ctx.update({
            "response_obj": response_obj,
            "answer_data":  answer_data,
            "answered":     answered,
            "total":        len(answer_data),
            "back_url": reverse(
                "admin:feedback_feedbackresponse_change",
                args=[response_obj.pk],
            ),
        })
        return ctx

    # ── POST ──────────────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        response_obj = self.get_response_obj()
        self._ensure_answers_exist(response_obj)

        answer_data = self._build_answer_data(response_obj)
        has_errors  = False

        for item in answer_data:
            answer     = item["answer"]
            question   = item["question"]
            field_name = item["field_name"]
            raw_value  = request.POST.get(field_name, "").strip()

            if question.question_type in {qt.value for qt in OPTION_TYPES}:
                # CHOICE — store as selected_option FK
                try:
                    option_id = int(raw_value) if raw_value else None
                except (ValueError, TypeError):
                    option_id = None
                # Validate the option actually exists and belongs to this question
                if option_id is not None:
                    if not FeedbackQuestionOption.objects.filter(
                        pk=option_id, question=question
                    ).exists():
                        option_id = None
                answer.selected_option_id = option_id
                answer.text_value = ""
            else:
                # YES_NO, SCALE, TEXT — store as plain text
                answer.text_value = raw_value
                answer.selected_option = None

            answer.save()

        if has_errors:
            messages.error(request, _("Por favor revisa los campos obligatorios."))
            return self.render_to_response(self.get_context_data())

        messages.success(request, _("Respuestas guardadas correctamente."))
        return redirect(
            reverse(
                "admin:feedback_feedbackresponse_change",
                args=[response_obj.pk],
            )
        )