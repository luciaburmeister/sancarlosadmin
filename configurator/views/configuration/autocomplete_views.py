from unfold.views import BaseAutocompleteView
from sales.models import Client, Quote
from management.models import ProjectFile
from assets.models import Asset
from core.admin.company_access import get_user_companies
from django.db.models import Q

class ClientAutocompleteView(BaseAutocompleteView):
    model = Client

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(user))
            
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(name__icontains=term)
            
        return qs

class AssetAutocompleteView(BaseAutocompleteView):
    model = Asset

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(user))
            
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(
                Q(name__icontains=term) | 
                Q(identifiers__value__icontains=term)
            ).distinct()
            
        return qs
class QuoteAutocompleteView(BaseAutocompleteView):
    model = Quote

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(user))
            
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(
                Q(project_name__icontains=term) | 
                Q(client__name__icontains=term) |
                Q(id__icontains=term)
            ).distinct()
            
        return qs.order_by("-id")

class ProjectFileAutocompleteView(BaseAutocompleteView):
    model = ProjectFile

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        
        if not user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(user))
            
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(
                Q(name__icontains=term) | 
                Q(reference__icontains=term) |
                Q(client__name__icontains=term)
            ).distinct()
            
        return qs.order_by("-created_at")
