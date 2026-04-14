from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from oioioi.base import admin
from oioioi.base.permissions import make_request_condition
from oioioi.contests.admin import ContestAdmin, contest_site
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.utils import is_contest_admin
from oioioi.rankings.models import (
    ConfigurableRanking,
    ConfigurableRankingRound,
    ConfigurableRankingSettings,
)


class ConfigurableRankingRoundInline(admin.TabularInline):
    model = ConfigurableRankingRound
    fk_name = "ranking"
    extra = 1
    fields = (
        "source_type",
        "round",
        "sub_ranking",
        "order",
        "coefficient",
        "score_mode",
        "all_time_coefficient",
        "all_time_score_mode",
        "column_visibility",
        "ignore_submissions_after",
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "round" and request.contest is not None:
            kwargs["queryset"] = request.contest.round_set.all().order_by("start_date", "name")
        if db_field.name == "sub_ranking" and request.contest is not None:
            queryset = ConfigurableRanking.objects.filter(contest=request.contest).order_by("order", "name", "id")
            object_id = request.resolver_match.kwargs.get("object_id")
            if object_id is not None:
                queryset = queryset.exclude(pk=object_id)
            kwargs["queryset"] = queryset
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ConfigurableRankingSettingsInline(admin.TabularInline):
    model = ConfigurableRankingSettings
    extra = 1
    max_num = 1
    category = _("Advanced")

    def _is_supported(self, request):
        return bool(request.contest) and request.contest.controller.supports_configurable_round_rankings()

    def has_add_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request)

    def has_change_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request)


class ConfigurableRankingSettingsAdminMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inlines = tuple(self.inlines) + (ConfigurableRankingSettingsInline,)


ContestAdmin.mix_in(ConfigurableRankingSettingsAdminMixin)


class ConfigurableRankingAdmin(admin.ModelAdmin):
    fields = ("name", "order", "medal_scheme", "show_sum", "show_percentage", "show_difference")
    inlines = (ConfigurableRankingRoundInline,)
    list_display = ("name", "order", "medal_scheme", "configured_sources")
    ordering = ("order", "name", "id")

    def _is_supported(self, request):
        return bool(request.contest) and request.contest.controller.supports_configurable_round_rankings()

    def has_module_permission(self, request):
        return self._is_supported(request) and is_contest_admin(request)

    def has_view_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request)

    def has_add_permission(self, request):
        return self._is_supported(request) and is_contest_admin(request) and not request.contest.is_archived

    def has_change_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_supported(request) and is_contest_admin(request) and not request.contest.is_archived

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.contest is None:
            return qs.none()
        return qs.filter(contest=request.contest).prefetch_related("round_configs")

    def save_model(self, request, obj, form, change):
        obj.contest = request.contest
        super().save_model(request, obj, form, change)

    def configured_sources(self, obj):
        return obj.round_configs.count()

    configured_sources.short_description = _("sources")


contest_site.contest_register(ConfigurableRanking, ConfigurableRankingAdmin)


@make_request_condition
def supports_configurable_rankings(request):
    return bool(request.contest) and request.contest.controller.supports_configurable_round_rankings()


contest_admin_menu_registry.register(
    "configurable_rankings",
    _("Rankings"),
    lambda request: reverse("oioioiadmin:rankings_configurableranking_changelist"),
    condition=is_contest_admin & supports_configurable_rankings,
    order=45,
)
