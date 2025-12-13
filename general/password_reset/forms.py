from django import forms
from django.contrib.auth.forms import (
    PasswordResetForm as DjangoPasswordResetForm,
    SetPasswordForm as DjangoSetPasswordForm,
)


class PasswordResetForm(DjangoPasswordResetForm):
    """Custom Password Reset Form with clean Bootstrap styling."""

    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "placeholder": "Enter your email address",
                "class": "form-control",
            }
        ),
        label="Email Address",
    )


class SetPasswordForm(DjangoSetPasswordForm):
    """Custom Set Password Form with clean Bootstrap styling."""

    new_password1 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "placeholder": "Enter new password",
                "class": "form-control",
            }
        ),
        label="New Password",
    )
    new_password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "placeholder": "Confirm new password",
                "class": "form-control",
            }
        ),
        label="Confirm Password",
    )
