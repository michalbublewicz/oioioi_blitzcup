from django.http import JsonResponse

from oioioi.contests.utils import can_enter_contest, contest_exists
from oioioi.base.permissions import enforce_condition


@enforce_condition(contest_exists & can_enter_contest)
def blitz_status_view(request):
    controller = request.contest.controller
    return JsonResponse(controller.serialize_live_status(request))
