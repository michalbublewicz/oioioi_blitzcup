from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from oioioi.base import admin
from oioioi.base.forms import AlwaysChangedModelForm
from oioioi.base.permissions import make_request_condition
from oioioi.blitz.models import BlitzContestConfig
from oioioi.contests.menu import contest_admin_menu_registry
from oioioi.contests.utils import is_contest_admin


class BlitzContestConfigInline(admin.TabularInline):
    model = BlitzContestConfig
    extra = 0
    max_num = 1
    form = AlwaysChangedModelForm
    category = _('Advanced')


class ContestAdminWithBlitzConfigInlineMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inlines = tuple(self.inlines) + (BlitzContestConfigInline,)


@make_request_condition
def is_blitz_contest(request):
    return bool(request.contest) and request.contest.controller_name == "oioioi.blitz.controllers.BlitzContestController"


contest_admin_menu_registry.register(
    "blitz_generate_matches",
    _("Generate matches"),
    lambda request: reverse("blitz_generate_matches", kwargs={"contest_id": request.contest.id}),
    condition=is_contest_admin & is_blitz_contest,
    order=65,
)
