from django.http import HttpRequest
from django.urls import reverse_lazy
from django.utils.html import format_html
from django.utils.translation import gettext as _

from core.admin.company_access import get_user_companies
from assets.models import AssetPlanningRequestDraft


def _drafts_tab_title(request: HttpRequest) -> str:
    count = AssetPlanningRequestDraft.count_submitted_for(get_user_companies(request.user))
    if not count:
        return _("Drafts")
    return format_html(
        '{} <span class="ml-1.5 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 '
        'rounded-full bg-primary-600 text-white text-[11px] font-bold leading-none">{}</span>',
        _("Drafts"), count,
    )


def asset_planning_tabs(request: HttpRequest) -> list[dict]:
    """Tab bar switching between Asset Planning Requests and their source Drafts."""
    return [
        {
            "models": ["assets.assetplanningrequest", "assets.assetplanningrequestdraft"],
            "items": [
                {
                    "title": _("Requests"),
                    "link": reverse_lazy("admin:assets_assetplanningrequest_changelist"),
                    "permission": lambda request: request.user.has_perm("assets.view_assetplanningrequest"),
                },
                {
                    "title": _drafts_tab_title(request),
                    "link": reverse_lazy("admin:assets_assetplanningrequestdraft_changelist"),
                    "permission": lambda request: request.user.has_perm("assets.view_assetplanningrequestdraft"),
                },
            ],
        },
    ]
