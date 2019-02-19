from django.conf import settings
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.utils.functional import cached_property
from django.views.generic import ListView

from pretix.base.models import Order, OrderPosition
from pretix.control.forms.filter import OrderSearchFilterForm
from pretix.control.views import LargeResultSetPaginator, PaginationMixin


class OrderSearch(PaginationMixin, ListView):
    model = Order
    paginator_class = LargeResultSetPaginator
    context_object_name = 'orders'
    template_name = 'pretixcontrol/search/orders.html'

    @cached_property
    def filter_form(self):
        return OrderSearchFilterForm(data=self.request.GET, request=self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx['filter_form'] = self.filter_form

        # Only compute this annotations for this page (query optimization)
        s = OrderPosition.objects.filter(
            order=OuterRef('pk')
        ).order_by().values('order').annotate(k=Count('id')).values('k')
        annotated = {
            o['pk']: o
            for o in
            Order.objects.using(settings.DATABASE_REPLICA).filter(
                pk__in=[o.pk for o in ctx['orders']]
            ).annotate(
                pcnt=Subquery(s, output_field=IntegerField())
            ).values(
                'pk', 'pcnt',
            )
        }

        for o in ctx['orders']:
            if o.pk not in annotated:
                continue
            o.pcnt = annotated.get(o.pk)['pcnt']

        return ctx

    def get_queryset(self):
        qs = Order.objects.using(settings.DATABASE_REPLICA)

        if not self.request.user.has_active_staff_session(self.request.session.session_key):
            qs = qs.filter(
                Q(event__organizer_id__in=self.request.user.teams.filter(
                    all_events=True, can_view_orders=True).values_list('organizer', flat=True))
                | Q(event_id__in=self.request.user.teams.filter(
                    can_view_orders=True).values_list('limit_events__id', flat=True))
            )

        if self.filter_form.is_valid():
            qs = self.filter_form.filter_qs(qs)

            if self.filter_form.cleaned_data.get('query'):
                """
                We need to work around a bug in PostgreSQL's (and likely MySQL's) query plan optimizer here.
                The database lacks statistical data to predict how common our search filter is and therefore
                assumes that it is cheaper to first ORDER *all* orders in the system (since we got an index on
                datetime), then filter out with a full scan until OFFSET/LIMIT condition is fulfilled. If we
                look for something rare (such as an email address used once within hundreds of thousands of
                orders, this ends up to be pathologically slow.

                For some search queries on pretix.eu, we see search times of >30s, just due to the ORDER BY and
                LIMIT clause. Without them. the query runs in roughly 0.6s. This heuristical approach tries to
                detect these cases and rewrite the query as a nested subquery that strongly suggests sorting
                before filtering. However, since even that fails in some cases because PostgreSQL thinks it knows
                better, we literally force it by evaluating the subquery explicitly. We only do this for n<=200,
                to avoid memory leaks – and problems with maximum parameter count on SQLite. In cases where the
                search query yields lots of results, this will actually be slower since it requires two queries,
                sorry.

                Phew.
                """

                page = self.kwargs.get(self.page_kwarg) or self.request.GET.get(self.page_kwarg) or 1
                limit = self.get_paginate_by(None)
                offset = (page - 1) * limit
                resultids = list(qs.order_by().values_list('id', flat=True)[:201])
                if len(resultids) <= 200 and len(resultids) <= offset + limit:
                    qs = Order.objects.using(settings.DATABASE_REPLICA).filter(
                        id__in=resultids
                    )

        """
        We use prefetch_related here instead of select_related for a reason, even though select_related
        would be the common choice for a foreign key and doesn't require an additional database query.
        The problem is, that if our results contain the same event 25 times, select_related will create
        25 Django  objects which will all try to pull their ownsettings cache to show the event properly,
        leading to lots of unnecessary queries. Due to the way prefetch_related works differently, it
        will only create one shared Django object.
        """
        return qs.only(
            'id', 'invoice_address__name_cached', 'invoice_address__name_parts', 'code', 'event', 'email',
            'datetime', 'total', 'status', 'require_approval', 'testmode'
        ).prefetch_related(
            'event', 'event__organizer'
        ).select_related('invoice_address')
