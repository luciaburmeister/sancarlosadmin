from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.http import JsonResponse
from django.conf.urls.i18n import i18n_patterns
from django.conf.urls.static import static
from django.views.generic import RedirectView
from general.password_reset import CustomPasswordResetView, CustomPasswordResetDoneView, CustomPasswordResetCompleteView, CustomPasswordResetConfirmView

def health(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("health/", health, name="health"),
    path("i18n/", include("django.conf.urls.i18n")),
    path('hijack/', include('hijack.urls')),
]

if settings.DEBUG:
    from debug_toolbar.toolbar import debug_toolbar_urls
    urlpatterns = debug_toolbar_urls() + urlpatterns + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += i18n_patterns(
    path('admin/', admin.site.urls),
    path("api/", include("api.urls")),
    path('password_reset/', CustomPasswordResetView.as_view(), name='admin_password_reset'),
    path('password_reset/done/', CustomPasswordResetDoneView.as_view(), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', CustomPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('reset/done/', CustomPasswordResetCompleteView.as_view(), name='password_reset_complete'),
)