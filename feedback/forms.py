from django import forms
from django.utils.translation import gettext_lazy as _

from crispy_forms.helper import FormHelper
from unfold.layout import Submit

from unfold.widgets import (
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminIntegerFieldWidget,
)

from .models import FeedbackQuestion, FeedbackQuestionOption, QuestionType


# ─────────────────────────────────────────────────────────────────────────────
# Questionnaire builder form (used in FeedbackQuestionInline)
# ─────────────────────────────────────────────────────────────────────────────

class FeedbackQuestionForm(forms.ModelForm):
    options_text = forms.CharField(
        required=False,
        label=_("Opciones (separadas por comas)"),
        widget=UnfoldAdminTextInputWidget(),
    )

    class Meta:
        model  = FeedbackQuestion
        fields = ["question_type", "text", "is_required", "order"]
        widgets = {
            "question_type": UnfoldAdminSelect2Widget(),
            "text":          UnfoldAdminTextInputWidget(),
            "order":         UnfoldAdminIntegerFieldWidget(),
        }
        labels = {
            "question_type": _("Tipo"),
            "text":          _("Pregunta"),
            "is_required":   _("Obligatoria"),
            "order":         _("Orden"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            texts = list(
                self.instance.options
                .order_by("order")
                .values_list("text", flat=True)
            )
            if texts:
                joined = ", ".join(texts)
                self.initial["options_text"]        = joined
                self.fields["options_text"].initial = joined


# ─────────────────────────────────────────────────────────────────────────────
# Fill form — one form per answer, rendered with Unfold widgets
# ─────────────────────────────────────────────────────────────────────────────

def build_answer_form(answer, question, options):
    """
    Dynamically build a single-field Form for one FeedbackAnswer.

    The empty placeholder ("—") has value "" and is disabled via the
    Select2Widget's placeholder mechanism — it won't appear as a selectable
    option when the dropdown opens, only as the unselected state label.
    """
    field_name = f"answer_{answer.pk}"
    q_type     = question.question_type
    required   = question.is_required

    if q_type in (QuestionType.CHOICE,):
        choices = [("", "—")] + [(str(opt.pk), opt.text) for opt in options]
        field = forms.ChoiceField(
            choices=choices,
            required=required,
            label=question.text,
            widget=UnfoldAdminSelect2Widget(attrs={"data-placeholder": "—"}),
        )

    elif q_type == QuestionType.YES_NO:
        # Empty value is the placeholder — not a selectable answer
        choices = [("", "—"), ("yes", _("Sí")), ("no", _("No"))]
        field = forms.ChoiceField(
            choices=choices,
            required=required,
            label=question.text,
            widget=UnfoldAdminSelect2Widget(attrs={"data-placeholder": "—"}),
        )

    elif q_type == QuestionType.SCALE:
        if options:
            choices = [("", "—")] + [(opt.text, opt.text) for opt in options]
        else:
            choices = [("", "—")] + [(str(i), str(i)) for i in range(1, 6)]
        field = forms.ChoiceField(
            choices=choices,
            required=required,
            label=question.text,
            widget=UnfoldAdminSelect2Widget(attrs={"data-placeholder": "—"}),
        )

    else:  # TEXT
        field = forms.CharField(
            required=required,
            label=question.text,
            widget=UnfoldAdminTextareaWidget(
                attrs={
                    "rows": 3,
                    "placeholder": _("Escribe aquí la respuesta del cliente…"),
                }
            ),
        )

    # Build a one-field Form class dynamically
    form_class = type(
        f"AnswerForm_{answer.pk}",
        (forms.Form,),
        {field_name: field},
    )

    # Pre-populate with existing saved value
    # Only CHOICE stores a FK (selected_option_id); everything else uses text_value
    if q_type == QuestionType.CHOICE:
        initial = {field_name: str(answer.selected_option_id) if answer.selected_option_id else ""}
    else:
        initial = {field_name: answer.text_value}

    return form_class(initial=initial, prefix=""), field_name


class AnswerFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag         = False
        self.form_show_labels = True
        self.template_pack    = "unfold_crispy"