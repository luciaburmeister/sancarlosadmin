from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel


class QuestionType(models.TextChoices):
    CHOICE = "choice", _("Opción múltiple")
    YES_NO = "yes_no", _("Sí / No")
    TEXT = "text", _("Texto libre")
    SCALE = "scale", _("Escala de valoración")


class FeedbackQuestionnaire(CompanyOwnedModel):
    name = models.CharField(
        max_length=200,
        default=_("Cuestionario de feedback de presupuestos"),
        verbose_name=_("Nombre"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Activo"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Creado el"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Actualizado el"))

    class Meta:
        verbose_name = _("Cuestionario de Feedback")
        verbose_name_plural = _("Cuestionarios de Feedback")

    def __str__(self):
        return f"{self.name} ({self.company})"


class FeedbackQuestion(models.Model):
    questionnaire = models.ForeignKey(
        FeedbackQuestionnaire,
        on_delete=models.PROTECT,
        related_name="questions",
        verbose_name=_("Cuestionario"),
        null=True,
        blank=True,
    )
    text = models.CharField(max_length=500, verbose_name=_("Pregunta"))
    question_type = models.CharField(
        max_length=20,
        choices=QuestionType.choices,
        default=QuestionType.CHOICE,
        verbose_name=_("Tipo"),
    )
    # Scale endpoint labels — stored directly, blank for non-scale questions
    scale_min_label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name=_("Etiqueta mínima (1)"),
        help_text=_("Solo para preguntas de tipo Escala. Ej: 'Muy insatisfecho'"),
    )
    scale_max_label = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name=_("Etiqueta máxima (5)"),
        help_text=_("Solo para preguntas de tipo Escala. Ej: 'Muy satisfecho'"),
    )
    is_required = models.BooleanField(default=False, verbose_name=_("Obligatoria"))
    order = models.PositiveIntegerField(default=0, db_index=True, verbose_name=_("Orden"))

    class Meta:
        verbose_name = _("Pregunta")
        verbose_name_plural = _("Preguntas")
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:80]


class FeedbackQuestionOption(models.Model):
    question = models.ForeignKey(
        FeedbackQuestion,
        on_delete=models.CASCADE,
        related_name="options",
        verbose_name=_("Pregunta"),
    )
    text = models.CharField(max_length=200, verbose_name=_("Opción"))
    order = models.PositiveIntegerField(default=0, db_index=True, verbose_name=_("Orden"))

    class Meta:
        verbose_name = _("Opción")
        verbose_name_plural = _("Opciones")
        ordering = ["order", "id"]

    def __str__(self):
        return self.text


class FeedbackResponse(CompanyOwnedModel, Timestampable):
    questionnaire = models.ForeignKey(
        FeedbackQuestionnaire,
        on_delete=models.PROTECT,
        related_name="responses",
        verbose_name=_("Cuestionario"),
    )
    quote = models.ForeignKey(
        "sales.Quote",
        on_delete=models.PROTECT,
        related_name="feedback_responses",
        verbose_name=_("Presupuesto"),
        limit_choices_to=Q(status="rejected") | Q(status="sent"),
    )
    client = models.ForeignKey(
        "sales.Client",
        on_delete=models.PROTECT,
        related_name="feedback_responses",
        verbose_name=_("Cliente"),
        editable=False,
    )
    agent = models.ForeignKey(
        "company.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_responses",
        verbose_name=_("Agente"),
    )
    call_date = models.DateField(verbose_name=_("Fecha de llamada"))
    notes = models.TextField(
        blank=True,
        verbose_name=_("Observaciones"),
        help_text=_("Comentarios adicionales del agente sobre la llamada"),
    )

    class Meta:
        verbose_name = _("Respuesta de Feedback")
        verbose_name_plural = _("Respuestas de Feedback")
        ordering = ["-call_date", "-created_at"]

    def __str__(self):
        client_name = self.client.name if self.client_id else "—"
        return f"Feedback #{self.pk} — {client_name} ({self.call_date})"

    def save(self, *args, **kwargs):
        if self.quote_id:
            self.client_id = self.quote.client_id
            if not self.company_id:
                self.company_id = self.quote.company_id
        super().save(*args, **kwargs)


class FeedbackAnswer(models.Model):
    response = models.ForeignKey(
        FeedbackResponse,
        on_delete=models.CASCADE,
        related_name="answers",
        verbose_name=_("Respuesta"),
    )
    question = models.ForeignKey(
        FeedbackQuestion,
        on_delete=models.PROTECT,
        related_name="answers",
        verbose_name=_("Pregunta"),
    )
    selected_option = models.ForeignKey(
        FeedbackQuestionOption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answers",
        verbose_name=_("Opción seleccionada"),
    )
    text_value = models.TextField(
        blank=True,
        verbose_name=_("Respuesta"),
        help_text=_("Para preguntas de texto libre, sí/no o escala"),
    )

    class Meta:
        verbose_name = _("Respuesta a pregunta")
        verbose_name_plural = _("Respuestas a preguntas")
        unique_together = ("response", "question")

    def __str__(self):
        if self.selected_option_id:
            return f"{self.question.text[:40]} → {self.selected_option}"
        return f"{self.question.text[:40]} → {self.text_value[:40]}"