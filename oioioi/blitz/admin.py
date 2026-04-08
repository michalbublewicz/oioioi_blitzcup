from django.utils.translation import gettext_lazy as _

from oioioi.base import admin
from oioioi.base.forms import AlwaysChangedModelForm
from oioioi.blitz.models import BlitzContestConfig


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
