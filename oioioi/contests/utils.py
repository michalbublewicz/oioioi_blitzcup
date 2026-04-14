from datetime import datetime, timedelta  # pylint: disable=E0611

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext_lazy as _
from pytz import UTC

from oioioi.base.permissions import make_request_condition
from oioioi.base.utils import request_cached, request_cached_complex
from oioioi.base.utils.public_message import get_public_message
from oioioi.base.utils.query_helpers import Q_always_false
from oioioi.contests.models import (
    Contest,
    FilesMessage,
    ProblemInstance,
    ProblemScoreDisplayConfig,
    Round,
    RoundStartDelay,
    RoundTimeExtension,
    Submission,
    SubmissionReport,
    SubmissionMessage,
    SubmissionsMessage,
    SubmitMessage,
    UserResultForProblem,
)
from oioioi.participants.models import TermsAcceptedPhrase
from oioioi.programs.models import ProgramsConfig


class RoundTimes:
    def __init__(
        self,
        start,
        end,
        contest,
        show_results=None,
        show_public_results=None,
        extra_time=0,
        delay_time=0,
    ):
        self.start = start
        self.end = end
        self.show_results = show_results
        self.show_public_results = show_public_results
        self.contest = contest
        self.extra_time = extra_time
        self.delay_time = delay_time

    def is_past(self, current_datetime):
        """Returns True if the round is over for a user"""
        end = self.get_end()
        return end is not None and current_datetime > end

    def is_active(self, current_datetime):
        """Returns True if the round is still active for a user"""
        return not (self.is_past(current_datetime) or self.is_future(current_datetime))

    def is_future(self, current_datetime):
        """Returns True if the round is not started for a user"""
        start = self.get_start()
        return start is not None and current_datetime < start

    def results_visible(self, current_datetime):
        """Returns True if results are visible for a user.

        Usually show_results date decides.

        When a RoundTimeExtension is set for a given user and the round
        is still active, results publication is delayed.
        """
        if self.show_results is None:
            return False

        if self.is_active(current_datetime):
            return current_datetime >= self.show_results + timedelta(minutes=self.extra_time)

        return current_datetime >= self.show_results

    def results_date(self):
        return self.show_results

    def public_results_visible(self, current_datetime):
        """Returns True if the results of the round have already been made
        public

        If the contest's controller makes no distinction between personal
        and public results, this function returns the same as
        :meth:'results_visible'.

        Otherwise the show_public_results date is used.
        """
        if not self.contest.controller.separate_public_results():
            return self.results_visible(current_datetime)

        if self.show_public_results is None:
            return False

        return current_datetime >= self.show_public_results

    def public_results_date(self):
        if not self.contest.controller.separate_public_results():
            return self.results_date()

        return self.show_public_results

    def get_start(self):
        """Returns start of user roundtime having regard to the delay."""
        if self.start:
            return self.start + timedelta(minutes=self.delay_time)
        return self.start

    def get_end(self):
        """Returns end of user roundtime
        having regard to the extension of the rounds and delay of the start.
        """
        if self.end:
            return self.end + timedelta(minutes=self.delay_time) + timedelta(minutes=self.extra_time)
        else:
            return self.end

    def get_key_for_comparison(self):
        return (
            self.get_start() or UTC.localize(datetime.min),
            self.get_end() or UTC.localize(datetime.max),
        )


def generic_rounds_times(request=None, contest=None):
    if contest is None and not hasattr(request, "contest"):
        return {}
    contest = contest or request.contest

    cache_attribute = "_generic_rounds_times_cache"
    if request is not None:
        if not hasattr(request, cache_attribute):
            setattr(request, cache_attribute, {})
        elif contest.id in getattr(request, cache_attribute):
            return getattr(request, cache_attribute)[contest.id]

    rounds = [r for r in Round.objects.filter(contest=contest).select_related("contest")]
    rids = [r.id for r in rounds]
    if not request or not hasattr(request, "user") or request.user.is_anonymous:
        rtexts = {}
        rdelays = {}
    else:
        rtexts = {x["round_id"]: x for x in RoundTimeExtension.objects.filter(user=request.user, round__id__in=rids).values()}
        rdelays = {x["round_id"]: x for x in RoundStartDelay.objects.filter(user=request.user, round__id__in=rids).values()}

    result = {
        r: RoundTimes(
            r.start_date,
            r.end_date,
            r.contest,
            r.results_date,
            r.public_results_date,
            rtexts[r.id]["extra_time"] if r.id in rtexts else 0,
            rdelays[r.id]["delay"] if r.id in rdelays else 0,
        )
        for r in rounds
    }
    if request is not None:
        getattr(request, cache_attribute)[contest.id] = result
    return result


def rounds_times(request, contest):
    return generic_rounds_times(request, contest)


@make_request_condition
def contest_exists(request):
    return hasattr(request, "contest") and request.contest is not None


@make_request_condition
def has_any_rounds(request_or_context):
    return Round.objects.filter(contest=request_or_context.contest).exists()


@make_request_condition
@request_cached
def has_any_active_round(request):
    controller = request.contest.controller
    for round in Round.objects.filter(contest=request.contest):
        rtimes = controller.get_round_times(request, round)
        if rtimes.is_active(request.timestamp):
            return True
    return False


def _public_results_visible(request, **kwargs):
    controller = request.contest.controller
    for round in Round.objects.filter(contest=request.contest, **kwargs):
        rtimes = controller.get_round_times(request, round)
        if not rtimes.public_results_visible(request.timestamp):
            return False
    return True


@make_request_condition
@request_cached
def all_public_results_visible(request):
    """Checks if results of all rounds of the current contest are visible to
    public.
    """
    return _public_results_visible(request)


@make_request_condition
@request_cached
def all_non_trial_public_results_visible(request):
    """Checks if results of all non-trial rounds of the current contest are
    visible to public.
    """
    return _public_results_visible(request, is_trial=False)


@make_request_condition
@request_cached
def has_any_submittable_problem(request):
    return bool(submittable_problem_instances(request))


@make_request_condition
@request_cached
def has_any_visible_problem_instance(request):
    return bool(visible_problem_instances(request))


@request_cached
def submittable_problem_instances(request):
    controller = request.contest.controller
    queryset = ProblemInstance.objects.filter(contest=request.contest).select_related("problem").prefetch_related("round").order_by("round__start_date", "order", "short_name")
    return [pi for pi in queryset if controller.can_submit(request, pi)]


@request_cached_complex
def visible_problem_instances(request, no_admin=False):
    controller = request.contest.controller
    queryset = ProblemInstance.objects.filter(contest=request.contest).select_related("problem").prefetch_related("round").order_by("round__start_date", "order", "short_name")
    return [
        pi
        for pi in queryset
        if controller.can_see_problem(
            request,
            pi,
            no_admin=no_admin,
        )
    ]


@request_cached_complex
def visible_rounds(request, no_admin=False):
    controller = request.contest.controller
    queryset = Round.objects.filter(contest=request.contest)
    return [
        r
        for r in queryset
        if controller.can_see_round(
            request,
            r,
            no_admin=no_admin,
        )
    ]


@make_request_condition
@request_cached
def are_rules_visible(request):
    return hasattr(request, "contest") and request.contest.show_contest_rules


@request_cached
def get_number_of_rounds(request):
    """Returns the number of rounds in the current contest."""
    return Round.objects.filter(contest=request.contest).count()


def get_contest_dates(request):
    """Returns the end_date of the latest round and the start_date
    of the earliest round.
    """
    rtimes = rounds_times(request, request.contest)

    ends = [rt.get_end() for rt in rtimes.values()]
    starts = [rt.get_start() for rt in rtimes.values()]

    if starts and None not in starts:
        min_start = min(starts)
    else:
        min_start = None

    if ends and None not in ends:
        max_end = max(ends)
    else:
        max_end = None

    return min_start, max_end


def get_scoring_desription(request):
    """Returns the scoring description of the current contest."""
    if hasattr(request.contest.controller, "scoring_description") and request.contest.controller.scoring_description is not None:
        return request.contest.controller.scoring_description
    else:
        return None


@request_cached
def get_problems_sumbmission_limit(request):
    """Returns the upper and lower submission limit in the current contest.
    If there is one limit for all problems, it returns a list with one element.
    If there are no problems in the contest, it returns the default limit.
    """
    controller = request.contest.controller
    queryset = ProblemInstance.objects.filter(contest=request.contest).prefetch_related("round")

    if queryset is None or not queryset.exists():
        return [Contest.objects.get(id=request.contest.id).default_submissions_limit]

    limits = set()
    for p in queryset:
        limits.add(controller.get_submissions_limit(request, p, noadmin=True))

    if len(limits) == 1:
        if None in limits:
            return None
        elif 0 in limits:
            return [_("infinity")]
        else:
            return [limits.pop()]
    elif len(limits) > 1:
        if 0 in limits:
            limits.remove(0)
            max_limit = _("infinity")
        else:
            max_limit = max(limits)

        min_limit = min(limits)

    return [min_limit, max_limit]


def get_results_visibility(request):
    """Returns the results ad ranking visibility for each round in the contest"""
    rtimes = rounds_times(request, request.contest)

    dates = []
    for r in rtimes.keys():
        results_date = rtimes[r].results_date()
        public_results_date = rtimes[r].public_results_date()

        if results_date is None or results_date <= request.timestamp:
            results = _("immediately")
        else:
            results = _("after %(date)s") % {"date": results_date.strftime("%Y-%m-%d %H:%M:%S")}

        if public_results_date is None or public_results_date <= request.timestamp:
            ranking = _("immediately")
        else:
            ranking = _("after %(date)s") % {"date": public_results_date.strftime("%Y-%m-%d %H:%M:%S")}

        dates.append({"name": r.name, "results": results, "ranking": ranking})

    return dates


def aggregate_statuses(statuses):
    """Returns the decisive status while treating SKIP as neutral when possible."""

    statuses = [s for s in statuses if s is not None]
    failures = [s for s in statuses if s not in ('OK', 'SKIP')]
    if failures:
        return failures[0]
    if any(s == 'OK' for s in statuses):
        return 'OK'
    if statuses and all(s == 'SKIP' for s in statuses):
        return 'SKIP'
    return 'OK'


def used_controllers():
    """Returns list of dotted paths to contest controller classes in use
    by contests on this instance.
    """
    return Contest.objects.values_list("controller_name", flat=True).distinct()


@request_cached
def visible_contests_queryset_old(request):
    """Returns Q for filtering contests visible to the logged in user."""
    if request.GET.get("living", "safely") == "dangerously":
        visible_query = Contest.objects.none()
        for controller_name in used_controllers():
            controller_class = import_string(controller_name)
            # HACK: we pass None contest just to call visible_contests_query.
            # This is a workaround for mixins not taking classmethods very well.
            controller = controller_class(None)
            subquery = Contest.objects.filter(controller_name=controller_name).filter(controller.registration_controller().visible_contests_query(request))
            visible_query = visible_query.union(subquery, all=False)
        return visible_query
    visible_query = Q_always_false()
    for controller_name in used_controllers():
        controller_class = import_string(controller_name)
        # HACK: we pass None contest just to call visible_contests_query.
        # This is a workaround for mixins not taking classmethods very well.
        controller = controller_class(None)
        visible_query |= Q(controller_name=controller_name) & controller.registration_controller().visible_contests_query(request)
    return visible_query


def visible_contests_query(request):
    """Returns materialized set of contests visible to the logged in user."""
    if request.GET.get("living", "safely") == "dangerously":
        visible_query = Contest.objects.none()
        for controller_name in used_controllers():
            controller_class = import_string(controller_name)
            # HACK: we pass None contest just to call visible_contests_query.
            # This is a workaround for mixins not taking classmethods very well.
            controller = controller_class(None)
            subquery = Contest.objects.filter(controller_name=controller_name).filter(controller.registration_controller().visible_contests_query(request))
            visible_query = visible_query.union(subquery, all=False)
        return visible_query
    visible_query = Q_always_false()
    for controller_name in used_controllers():
        controller_class = import_string(controller_name)
        # HACK: we pass None contest just to call visible_contests_query.
        # This is a workaround for mixins not taking classmethods very well.
        controller = controller_class(None)
        visible_query |= Q(controller_name=controller_name) & controller.registration_controller().visible_contests_query(request)
    return Contest.objects.filter(visible_query).distinct()


def visible_contests_as_django_queryset(request):
    """Returns query set of contests visible to the logged in user."""
    if request.GET.get("living", "safely") == "dangerously":
        visible_query = Contest.objects.none()
        for controller_name in used_controllers():
            controller_class = import_string(controller_name)
            # HACK: we pass None contest just to call visible_contests_query.
            # This is a workaround for mixins not taking classmethods very well.
            controller = controller_class(None)
            subquery = Contest.objects.filter(controller_name=controller_name).filter(controller.registration_controller().visible_contests_query(request))
            visible_query = visible_query.union(subquery, all=False)
        return visible_query
    visible_query = Q_always_false()
    for controller_name in used_controllers():
        controller_class = import_string(controller_name)
        # HACK: we pass None contest just to call visible_contests_query.
        # This is a workaround for mixins not taking classmethods very well.
        controller = controller_class(None)
        visible_query |= Q(controller_name=controller_name) & controller.registration_controller().visible_contests_query(request)
    return Contest.objects.filter(visible_query).distinct()


@request_cached
def visible_contests(request):
    contests = visible_contests_as_django_queryset(request)
    return set(contests)


@request_cached_complex
def visible_contests_queryset(request, filter_value=None):
    contests = visible_contests_as_django_queryset(request)
    if filter_value is not None:
        contests = contests.filter(Q(name__icontains=filter_value) | Q(id__icontains=filter_value) | Q(school_year=filter_value))
    return set(contests)


@request_cached_complex
def visible_filtered_contests_as_django_queryset(request, filter_value=None):
    """TODO: remove code duplication visible_contests_queryset/visible_contests_query"""
    contests = visible_contests_as_django_queryset(request)
    if filter_value is not None:
        contests = contests.filter(Q(name__icontains=filter_value) | Q(id__icontains=filter_value) | Q(school_year=filter_value))
    return contests


# why is there no `can_admin_contest_query`?
@request_cached
def administered_contests(request):
    """Returns a list of contests for which the logged
    user has contest_admin permission for.
    """
    return [contest for contest in visible_contests(request) if can_admin_contest(request.user, contest)]


@make_request_condition
@request_cached
def is_contest_owner(request):
    """Checks if the user is the owner of the current contest.
    This permission level allows full access to all contest functionality
    and additionally permits managing contest permissions for a given contest
    with the exception of contest ownerships.
    """
    return request.user.has_perm("contests.contest_owner", request.contest)


@make_request_condition
@request_cached
def is_contest_admin(request):
    """Checks if the user is the contest admin of the current contest.
    This permission level allows full access to all contest functionality.
    """
    return request.user.has_perm("contests.contest_admin", request.contest)


def can_admin_contest(user, contest):
    """Checks if the user should be allowed on the admin pages of the contest."""
    return user.has_perm("contests.contest_basicadmin", contest)


@make_request_condition
@request_cached
def is_contest_basicadmin(request):
    """Checks if the user is a basic admin of the current contest.
    This permission level allows edit access to basic contest functionality.
    It is also implied by having full admin privileges (is_contest_admin).
    """
    return can_admin_contest(request.user, request.contest)


@make_request_condition
@request_cached
def is_contest_observer(request):
    """Checks if the current user can observe the current contest."""
    return request.user.has_perm("contests.contest_observer", request.contest)


@make_request_condition
@request_cached
def can_see_personal_data(request):
    """Checks if the current user has permission to see personal data."""
    return request.user.has_perm("contests.personal_data", request.contest)


@make_request_condition
@request_cached
def can_enter_contest(request):
    rcontroller = request.contest.controller.registration_controller()
    return rcontroller.can_enter_contest(request)


def get_submission_or_error(request, submission_id, submission_class=Submission):
    """Returns the submission if it exists and user has rights to see it."""
    submission = get_object_or_404(submission_class, id=submission_id)
    if hasattr(request, "user") and request.user.is_superuser:
        return submission
    pi = submission.problem_instance
    if pi.contest:
        if not request.contest or request.contest.id != pi.contest.id:
            raise PermissionDenied
        if is_contest_basicadmin(request) or is_contest_observer(request):
            return submission
    elif request.contest:
        raise PermissionDenied
    queryset = Submission.objects.filter(id=submission.id)
    if not pi.controller.filter_my_visible_submissions(request, queryset):
        raise PermissionDenied
    return submission


@request_cached
def last_break_between_rounds(request_or_context):
    """Returns the end_date of the latest past round and the start_date
    of the closest future round.

    Assumes that none of the rounds is active.
    """
    if isinstance(request_or_context, HttpRequest):
        rtimes = rounds_times(request_or_context, request_or_context.contest)
    else:
        rtimes = generic_rounds_times(None, request_or_context.contest)
    ends = [rt.get_end() for rt in rtimes.values() if rt.is_past(request_or_context.timestamp)]
    starts = [rt.get_start() for rt in rtimes.values() if rt.is_future(request_or_context.timestamp)]

    max_end = max(ends) if ends else None
    min_start = min(starts) if starts else None

    return max_end, min_start


def best_round_to_display(request, allow_past_rounds=False):
    timestamp = getattr(request, "timestamp", None)
    contest = getattr(request, "contest", None)

    next_rtimes = None
    current_rtimes = None
    past_rtimes = None

    if timestamp and contest:
        rtimes = {round: contest.controller.get_round_times(request, round) for round in Round.objects.filter(contest=contest)}
        next_rtimes = [(r, rt) for r, rt in rtimes.items() if rt.is_future(timestamp)]
        next_rtimes.sort(key=lambda r_rt: r_rt[1].get_start())
        current_rtimes = [(r, rt) for r, rt in rtimes if rt.is_active(timestamp) and rt.get_end()]
        current_rtimes.sort(key=lambda r_rt1: r_rt1[1].get_end())
        past_rtimes = [(r, rt) for r, rt in rtimes.items() if rt.is_past(timestamp)]
        past_rtimes.sort(key=lambda r_rt2: r_rt2[1].get_end())

    if current_rtimes:
        return current_rtimes[0][0]
    elif next_rtimes:
        return next_rtimes[0][0]
    elif past_rtimes and allow_past_rounds:
        return past_rtimes[-1][0]
    else:
        return None


@make_request_condition
def has_any_contest(request):
    # holy shit.
    contests = [contest for contest in administered_contests(request)]
    return len(contests) > 0


def get_files_message(request):
    return get_public_message(
        request,
        FilesMessage,
        "files_message",
    )


def get_submissions_message(request):
    return get_public_message(
        request,
        SubmissionsMessage,
        "submissions_message",
    )


def get_submit_message(request):
    return get_public_message(
        request,
        SubmitMessage,
        "submit_message",
    )


def get_submission_message(request):
    return get_public_message(
        request,
        SubmissionMessage,
        "submission_message",
    )


@make_request_condition
@request_cached
def is_contest_archived(request):
    return hasattr(request, "contest") and (request.contest is not None) and request.contest.is_archived


def get_inline_for_contest(inline, contest):
    """Returns inline without add, change or delete permissions,
    with all fields in readonly for archived contests.
    For unarchived contests returns the inline itself.
    """
    if not contest or not contest.is_archived:
        return inline

    class ArchivedInlineWrapper(inline):
        extra = 0
        max_num = 0
        can_delete = False
        editable_fields = []
        exclude = []

        def has_add_permission(self, request, obj=None):
            return False

        def has_change_permission(self, request, obj=None):
            return False

        def has_delete_permission(self, request, obj=None):
            return False

        def has_view_permission(self, request, obj=None):
            return True

    return ArchivedInlineWrapper


# The whole section below requires refactoring,
# may include refactoring the models of `Contest`, `ProgramsConfig` and `TermsAcceptedPhrase`


def _ensure_mutable_post(request):
    if hasattr(request.POST, "_mutable") and not request.POST._mutable:
        request.POST = request.POST.copy()
    return request.POST


def _set_single_inline_post_binding(request, prefix, object_id, contest_id):
    post_data = _ensure_mutable_post(request)
    post_data[f"{prefix}-TOTAL_FORMS"] = "1"
    post_data[f"{prefix}-INITIAL_FORMS"] = "1"
    post_data.setdefault(f"{prefix}-MIN_NUM_FORMS", "0")
    post_data.setdefault(f"{prefix}-MAX_NUM_FORMS", "1")
    post_data[f"{prefix}-0-id"] = str(object_id)
    post_data[f"{prefix}-0-contest"] = str(contest_id)


def extract_programs_config_post_data(request):
    execution_mode = request.POST.get("programs_config-0-execution_mode", "AUTO") or "AUTO"
    raw_subtask_parallel_limit = request.POST.get("programs_config-0-subtask_parallel_limit", None)

    if raw_subtask_parallel_limit in (None, ""):
        subtask_parallel_limit = None
    else:
        try:
            subtask_parallel_limit = int(raw_subtask_parallel_limit)
        except (TypeError, ValueError):
            subtask_parallel_limit = None
        else:
            if subtask_parallel_limit <= 0:
                subtask_parallel_limit = None

    return {
        "execution_mode": execution_mode,
        "subtask_parallel_limit": subtask_parallel_limit,
    }


def create_programs_config(request, adding):
    """Creates ProgramsConfig for a given contest if needed.

    Args:
        request: The HTTP request object.
        adding (bool): If True, the contest is being added; otherwise, it is being modified.
    """
    requested_contest_id = request.POST.get("id", None)
    programs_config_data = extract_programs_config_post_data(request)
    execution_mode = programs_config_data["execution_mode"]
    subtask_parallel_limit = programs_config_data["subtask_parallel_limit"]

    if execution_mode != "AUTO" or subtask_parallel_limit is not None:
        if adding and requested_contest_id:
            ProgramsConfig.objects.create(contest_id=requested_contest_id, **programs_config_data)
        elif request.contest and request.contest.id:
            programs_config = ProgramsConfig.objects.filter(contest_id=request.contest.id).first()
            if programs_config is None:
                programs_config = ProgramsConfig.objects.create(contest_id=request.contest.id, **programs_config_data)
            _set_single_inline_post_binding(request, "programs_config", programs_config.pk, request.contest.id)


def extract_terms_accepted_phrase_text(request):
    return request.POST.get("terms_accepted_phrase-0-text", None)


def extract_problem_score_display_mode(request):
    return request.POST.get("problemscoredisplayconfig-0-score_mode", None)


def extract_configurable_ranking_settings_data(request):
    raw_value = request.POST.get("configurablerankingsettings-0-show_default_rankings", None)
    if raw_value is None:
        return None
    return {
        "show_default_rankings": bool(raw_value),
    }


def _parse_split_datetime(request, prefix):
    date_value = request.POST.get(f"{prefix}_0", "")
    time_value = request.POST.get(f"{prefix}_1", "")
    if not date_value or not time_value:
        return None
    parsed = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M:%S")
    return timezone.make_aware(parsed, UTC)


def _create_single_contest_inline(request, adding, prefix, model_class, defaults):
    requested_contest_id = request.POST.get("id", None)
    if adding and requested_contest_id:
        instance = model_class.objects.create(contest_id=requested_contest_id, **defaults)
        _set_single_inline_post_binding(request, prefix, instance.pk, requested_contest_id)
    elif request.contest and request.contest.id:
        instance = model_class.objects.filter(contest_id=request.contest.id).first()
        if instance is None:
            instance = model_class.objects.create(contest_id=request.contest.id, **defaults)
        else:
            for key, value in defaults.items():
                setattr(instance, key, value)
            instance.save(update_fields=list(defaults.keys()))
        _set_single_inline_post_binding(request, prefix, instance.pk, request.contest.id)


def create_terms_accepted_phrase(request, adding):
    """Creates TermsAcceptedPhrase for a given contest if needed.

    Args:
        request: The HTTP request object.
        adding (bool): If True, the contest is being added; otherwise, it is being modified.
    """

    requested_contest_id = request.POST.get("id", None)
    text = extract_terms_accepted_phrase_text(request)

    if text:
        if adding and requested_contest_id:
            TermsAcceptedPhrase.objects.create(contest_id=requested_contest_id, text=text)
        elif request.contest and request.contest.id:
            terms_accepted_phrase = TermsAcceptedPhrase.objects.filter(contest_id=request.contest.id).first()
            if terms_accepted_phrase is None:
                terms_accepted_phrase = TermsAcceptedPhrase.objects.create(contest_id=request.contest.id, text=text)
            _set_single_inline_post_binding(request, "terms_accepted_phrase", terms_accepted_phrase.pk, request.contest.id)


def create_problem_score_display_config(request, adding):
    score_mode = extract_problem_score_display_mode(request)
    if score_mode is None:
        return
    _create_single_contest_inline(
        request,
        adding,
        "problemscoredisplayconfig",
        ProblemScoreDisplayConfig,
        {"score_mode": score_mode or "last"},
    )


def create_configurable_ranking_settings(request, adding):
    settings_data = extract_configurable_ranking_settings_data(request)
    if settings_data is None:
        return
    from oioioi.rankings.models import ConfigurableRankingSettings

    _create_single_contest_inline(
        request,
        adding,
        "configurablerankingsettings",
        ConfigurableRankingSettings,
        settings_data,
    )


def _warn_on_not_exclusive_rounds_from_post(request):
    total_forms = int(request.POST.get("exclusivenessconfig_set-TOTAL_FORMS", "0") or "0")
    if total_forms <= 0:
        return

    from oioioi.contestexcl.models import ExclusivenessConfig

    ex_confs = []
    for index in range(total_forms):
        if request.POST.get(f"exclusivenessconfig_set-{index}-DELETE"):
            continue
        start_date = _parse_split_datetime(request, f"exclusivenessconfig_set-{index}-start_date")
        if start_date is None:
            continue
        end_date = _parse_split_datetime(request, f"exclusivenessconfig_set-{index}-end_date")
        enabled = bool(request.POST.get(f"exclusivenessconfig_set-{index}-enabled"))
        if not enabled:
            continue
        ex_conf = ExclusivenessConfig(
            contest=request.contest,
            start_date=start_date,
            end_date=end_date,
            enabled=enabled,
        )
        ex_confs.append(ex_conf)

    if not ex_confs or not request.contest:
        return

    round_start = _parse_split_datetime(request, "round_set-0-start_date")
    round_end = _parse_split_datetime(request, "round_set-0-end_date")
    round_name = request.POST.get("round_set-0-name", "")
    if round_start is None:
        return

    ex_confs.sort(key=lambda ex_conf: ex_conf.start_date)
    round_not_excl_dates = []
    round_excl_end_date = round_start
    for ex_conf in ex_confs:
        if ex_conf.start_date > round_excl_end_date:
            round_not_excl_dates.append((round_excl_end_date, ex_conf.start_date))
            round_excl_end_date = ex_conf.start_date
        if ex_conf.end_date:
            round_excl_end_date = max(round_excl_end_date, ex_conf.end_date)
        else:
            break
        if round_end and round_excl_end_date >= round_end:
            break
    else:
        round_not_excl_dates.append((round_excl_end_date, round_end))

    if not round_not_excl_dates:
        return

    first_future_date = round_not_excl_dates[0]
    for date in round_not_excl_dates:
        if not date[1] or date[1] >= timezone.now():
            first_future_date = date
            break

    if not first_future_date[1]:
        msg = _(
            'Exclusiveness configs usually cover entire rounds, but currently round "%s" is not exclusive from %s! '
            "Please verify that your exclusiveness configs are correct."
        ) % (round_name, first_future_date[0])
    else:
        msg = _(
            'Exclusiveness configs usually cover entire rounds, but currently round "%s" is not exclusive from %s to %s! '
            "Please verify that your exclusiveness configs are correct."
        ) % (round_name, first_future_date[0], first_future_date[1])
    messages.warning(request, msg)


def create_contest_attributes(request, adding):
    """Called to create certain attributes of contest object after modifying it that would not be created automatically.
    Creates attributes are ProgramsConfig and TermsAcceptedPhrase

    Args:
        request: The HTTP request object.
        adding (bool): If True, the contest is being added; otherwise, it is being modified.
    """
    if request.method != "POST":
        return
    create_programs_config(request, adding)
    create_terms_accepted_phrase(request, adding)
    create_problem_score_display_config(request, adding)
    create_configurable_ranking_settings(request, adding)
    _warn_on_not_exclusive_rounds_from_post(request)


def get_problem_statements(request, controller, problem_instances):
    problem_instances = list(problem_instances)
    results_map = get_problem_display_results_map(request, controller, problem_instances)

    # Problem statements in order
    # 1) problem instance
    # 2) statement_visible
    # 3) round end time
    # 4) user result
    # 5) number of submissions left
    # 6) submissions_limit
    # 7) can_submit
    # Sorted by (start_date, end_date, round name, problem name)
    return sorted(
        [
            (
                pi,
                controller.can_see_statement(request, pi),
                controller.get_round_times(request, pi.round),
                results_map.get(pi.id),
                pi.controller.get_submissions_left(request, pi),
                pi.controller.get_submissions_limit(request, pi),
                controller.can_submit(request, pi) and not is_contest_archived(request),
            )
            for pi in problem_instances
        ],
        key=lambda p: (p[2].get_key_for_comparison(), p[0].round.name, getattr(p[0], 'order', 0), p[0].short_name),
    )


def get_problem_display_results_map(request, controller, problem_instances):
    if request.user.is_anonymous:
        return {}

    problem_instances = list(problem_instances)
    if not problem_instances:
        return {}

    contest_supports_custom_problem_scores = {
        contest.id: contest.controller.supports_configurable_round_rankings()
        for contest in Contest.objects.filter(id__in={problem_instance.contest_id for problem_instance in problem_instances})
    }
    contest_score_modes = {
        contest_id: "last"
        for contest_id in {problem_instance.contest_id for problem_instance in problem_instances}
    }
    contest_score_modes.update(
        ProblemScoreDisplayConfig.objects.filter(contest_id__in=contest_score_modes)
        .values_list("contest_id", "score_mode")
    )

    results_map = {}
    stored_problem_instances = [
        problem_instance
        for problem_instance in problem_instances
        if not contest_supports_custom_problem_scores.get(problem_instance.contest_id, False)
    ]
    if stored_problem_instances:
        stored_results = (
            UserResultForProblem.objects.filter(problem_instance__in=stored_problem_instances, user=request.user)
            .select_related("problem_instance", "submission_report", "submission_report__submission")
            .prefetch_related("submission_report__scorereport_set")
        )
        for result in stored_results:
            if result.score is None:
                continue
            if (
                result.submission_report is None
                or result.submission_report.submission is None
                or not controller.can_see_submission_score(request, result.submission_report.submission)
            ):
                continue
            results_map[result.problem_instance_id] = result

    selectable_problem_instances = [
        problem_instance
        for problem_instance in problem_instances
        if contest_supports_custom_problem_scores.get(problem_instance.contest_id, False)
    ]
    if not selectable_problem_instances:
        return results_map

    submissions = list(
        Submission.objects.filter(
            user=request.user,
            problem_instance__in=selectable_problem_instances,
            kind="NORMAL",
            score__isnull=False,
        )
        .select_related(
            "problem_instance",
            "problem_instance__contest",
            "problem_instance__problem",
            "problem_instance__round",
        )
        .order_by("problem_instance_id", "date", "id")
    )

    submissions_by_problem = {}
    for submission in submissions:
        submissions_by_problem.setdefault(submission.problem_instance_id, []).append(submission)

    active_reports = {
        report.submission_id: report
        for report in SubmissionReport.objects.filter(
            submission__in=submissions,
            status="ACTIVE",
            kind="NORMAL",
        )
        .prefetch_related("scorereport_set")
    }

    for problem_instance in selectable_problem_instances:
        visible_submissions = [
            submission
            for submission in submissions_by_problem.get(problem_instance.id, [])
            if controller.can_see_submission_score(request, submission)
        ]
        if not visible_submissions:
            continue

        score_mode = contest_score_modes[problem_instance.contest_id]
        chosen_submission = visible_submissions[-1]
        if score_mode == "best":
            chosen_submission = visible_submissions[0]
            for submission in visible_submissions[1:]:
                if submission.score > chosen_submission.score or (
                    submission.score == chosen_submission.score
                    and (submission.date, submission.id) > (chosen_submission.date, chosen_submission.id)
                ):
                    chosen_submission = submission
        if chosen_submission.score is None:
            continue

        results_map[problem_instance.id] = UserResultForProblem(
            user=request.user,
            problem_instance=problem_instance,
            score=chosen_submission.score,
            status=chosen_submission.status,
            submission_report=active_reports.get(chosen_submission.id),
        )

    return results_map


def process_instances_to_limits(raw_instances):
    instances_to_limits = {}

    for instance in raw_instances:
        if instance["min_time"] is not None:
            instances_to_limits[instance["id"]] = {
                "default": (instance["min_time"], instance["max_time"], instance["min_memory"], instance["max_memory"]),
                "cpp": (
                    min(filter(None, [instance["cpp_min_time"], instance["cpp_min_time_non_overridden"]])),
                    max(filter(None, [instance["cpp_max_time"], instance["cpp_max_time_non_overridden"]])),
                    min(filter(None, [instance["cpp_min_memory"], instance["cpp_min_memory_non_overridden"]])),
                    max(filter(None, [instance["cpp_max_memory"], instance["cpp_max_memory_non_overridden"]])),
                ),
                "py": (
                    min(filter(None, [instance["py_min_time"], instance["py_min_time_non_overridden"]])),
                    max(filter(None, [instance["py_max_time"], instance["py_max_time_non_overridden"]])),
                    min(filter(None, [instance["py_min_memory"], instance["py_min_memory_non_overridden"]])),
                    max(filter(None, [instance["py_max_memory"], instance["py_max_memory_non_overridden"]])),
                ),
            }

    return instances_to_limits


def stringify_problems_limits(raw_limits):
    """Stringifies the time and memory limits for a given set of problem instances.

    This function processes a dictionary of problem instances (raw_limits), where each problem instance
    contains limits for default, C++, and Python. The function then formats these limits into
    human-readable strings based on the following logic:
        - If both C++ and Python limits are the same as the default, only the default limits are shown.
        - Else if both limits for C++ or Python differ from the default limits, those limits are formatted separately.
        - Else if one of language's limits differ, the default and the differing language limits are shown.

    Args:
        raw_limits (dict): A dictionary of problem instances, where each key is the problem instance ID and
        each value is another dictionary containing the following keys:
        - 'default': A tuple (min_time, max_time, min_memory, max_memory) for the default limits.
        - 'cpp': A tuple (min_time, max_time, min_memory, max_memory) for C++ language.
        - 'py': A tuple (min_time, max_time, min_memory, max_memory) for Python language.

    Returns:
        dict: A dictionary of formatted limits, where each key is the problem instance ID and each value is
              a tuple with the following format:
              - For default-only limits: (('', time_limit, memory_limit),)
              - For limits with both languages: (('C++:', cpp_time, cpp_memory), ('Python:', py_time, py_memory))
              - For mixed limits (one language differs): (('Default:', time_limit, memory_limit), language_limits)
    """

    def KiB_to_MiB(KiBs):
        return (KiBs) // 1024

    def ms_to_seconds(ms: int) -> str:
        seconds: int = ms // 1000
        ms %= 1000
        if ms == 0:
            return str(seconds)
        return f"{seconds}.{str(ms).rjust(3, '0').rstrip('0')}"

    def format_limits(pi_limits):
        lower_ms = pi_limits[0]
        higher_ms = pi_limits[1]

        time_lower = ms_to_seconds(lower_ms)
        time_higher = ms_to_seconds(higher_ms)

        time_limit = f"{time_lower} s" if lower_ms == higher_ms else f"{time_lower}-{time_higher} s"

        if pi_limits[2] < 1024:  # lower memory limit is smaller than 1MiB, display KiB
            unit = "KiB"
            memory_lower = pi_limits[2]
            memory_higher = pi_limits[3]
        else:
            unit = "MiB"
            memory_lower = KiB_to_MiB(pi_limits[2])
            memory_higher = KiB_to_MiB(pi_limits[3])

        memory_limit = f"{memory_lower} {unit}" if memory_lower == memory_higher else f"{memory_lower}-{memory_higher} {unit}"

        return time_limit, memory_limit

    stringified = {}

    for pi_pk, pi_limits in raw_limits.items():
        if all(pi_limits[lang] == pi_limits["default"] for lang in ["cpp", "py"]):  # language limits same as default
            time_limit, memory_limit = format_limits(pi_limits["default"])
            stringified[pi_pk] = (("", time_limit, memory_limit),)

        elif all(pi_limits[lang] != pi_limits["default"] for lang in ["cpp", "py"]):  # both languages differ
            cpp_time, cpp_memory = format_limits(pi_limits["cpp"])
            py_time, py_memory = format_limits(pi_limits["py"])
            stringified[pi_pk] = (("C++:", cpp_time, cpp_memory), ("Python:", py_time, py_memory))

        else:  # one of languages differ
            if pi_limits["cpp"] != pi_limits["default"]:
                language_limits = ("C++:", *format_limits(pi_limits["cpp"]))
            else:
                language_limits = ("Python:", *format_limits(pi_limits["py"]))

            stringified[pi_pk] = ((_("Default") + ":", *format_limits(pi_limits["default"])), language_limits)

    return stringified


def filter_last_submissions(queryset):
    """Filters the given Submission queryset to keep only the last submission per user and problem_instance."""
    last_subquery = (
        Submission.objects.filter(
            user=OuterRef("user"),
            problem_instance=OuterRef("problem_instance"),
        )
        .order_by("-date")
        .values("id")[:1]
    )
    return queryset.filter(id=Subquery(last_subquery))
