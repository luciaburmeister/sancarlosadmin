from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model

User = get_user_model()


class Company(models.Model):
    name = models.CharField(max_length=200,verbose_name=_("Commercial Name"))
    legal_name = models.CharField(max_length=200, blank=True,verbose_name=_("Legal Name"))
    is_active = models.BooleanField(default=True,verbose_name=_("Activo"))
    logo = models.ImageField(verbose_name=_("Logo"),upload_to="logos")

    def __str__(self):
        return self.name

class CompanyOwnedModel(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        null=True,
        blank=False
    )

    class Meta:
        abstract = True

class CompanyUser(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="company_links"
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="user_links"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("user", "company")

    def __str__(self):
        return f"{self.user} → {self.company}"