from rest_framework.filters import OrderingFilter


class StableOrderingFilter(OrderingFilter):
    """``?ordering=`` that keeps the list's own order as the tie-breaker.

    DRF's filter replaces the queryset's order with the client's fields, so
    ``?ordering=first_name`` returns two students with the same first name
    in whatever order the database picks. With LIMIT/OFFSET that order may
    differ between pages, repeating or skipping rows. The default order, and
    then the primary key, are appended after the client's fields.
    """

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        named = {field.lstrip("-") for field in ordering}
        default = list(queryset.query.order_by) or list(queryset.model._meta.ordering)
        tail = [f for f in default if isinstance(f, str) and f.lstrip("-") not in named]
        if not named & {"pk", "id"}:
            tail.append("pk")
        return queryset.order_by(*ordering, *tail)
