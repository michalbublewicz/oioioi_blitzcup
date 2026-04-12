from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _

from oioioi.base.permissions import enforce_condition
from oioioi.blitz.admin import is_blitz_contest
from oioioi.blitz.forms import GenerateMatchesForm
from oioioi.blitz.services import generate_match_contests
from oioioi.contests.utils import can_enter_contest, contest_exists, is_contest_admin


@enforce_condition(contest_exists & can_enter_contest)
def blitz_status_view(request):
    controller = request.contest.controller
    return JsonResponse(controller.serialize_live_status(request))


@enforce_condition(contest_exists)
def generate_matches_view(request):
    if not is_blitz_contest(request):
        raise Http404
    if not is_contest_admin(request):
        raise PermissionDenied

    if request.method == "POST":
        form = GenerateMatchesForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                created_contests = generate_match_contests(
                    source_contest=request.contest,
                    level_label=form.cleaned_data["level_label"],
                    contest_id_prefix=form.cleaned_data["contest_id_prefix"],
                    workbook_file=form.cleaned_data["xlsx_file"],
                )
            except ValidationError as e:
                for message in e.messages:
                    form.add_error(None, message)
            else:
                messages.success(
                    request,
                    _("Created %(count)d match contests.") % {"count": len(created_contests)},
                )
                return redirect(
                    "oioioiadmin:contests_contest_change",
                    contest_id=request.contest.id,
                    object_id=request.contest.id,
                )
    else:
        form = GenerateMatchesForm()

    return TemplateResponse(
        request,
        "blitz/generate_matches.html",
        {
            "form": form,
            "source_contest": request.contest,
        },
    )
