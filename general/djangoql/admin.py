"""Admin mixin enabling localized DjangoQL search on a ModelAdmin."""

from djangoql.admin import DjangoQLSearchMixin

from .schema import RegisteredDjangoQLSchema


class DjangoQLSearchAdminMixin(DjangoQLSearchMixin):
    """
    Advanced (DjangoQL) search over the fields the model declared in its
    `djangoql_fields` module. Mix in before `ModelAdmin`.

    Keep the admin's own `search_fields`: that turns the DjangoQL box into a
    toggle, so the plain search stays the default and the query language is
    there for whoever needs it.
    """

    djangoql_schema = RegisteredDjangoQLSchema
    djangoql_completion_enabled_by_default = False

    def get_search_results(self, request, queryset, search_term):
        qs, use_distinct = super().get_search_results(request, queryset, search_term)
        # Upstream always reports use_distinct=False, but schemas expose
        # to-many relations (line items, project files) and filtering across
        # one duplicates rows, so the changelist has to de-duplicate whenever
        # the DjangoQL box was the one used.
        djangoql_used = (
            not self.search_mode_toggle_enabled()
            or self.djangoql_search_enabled(request)
        )
        if search_term and djangoql_used:
            use_distinct = True
        return qs, use_distinct
