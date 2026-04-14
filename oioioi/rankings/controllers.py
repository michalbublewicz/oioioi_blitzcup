from collections import defaultdict
from operator import itemgetter  # pylint: disable=E0611

import unicodecsv
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Max
from django.http import HttpResponse, HttpResponseBadRequest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from oioioi.base.models import PreferencesSaved
from oioioi.base.utils import ObjectWithMixins, RegisteredSubclassesBase
from oioioi.contests.controllers import ContestController, ContestControllerContext
from oioioi.contests.models import Contest, ProblemInstance, Submission, UserResultForProblem
from oioioi.contests.utils import is_contest_basicadmin, is_contest_observer
from oioioi.filetracker.utils import make_content_disposition_header
from oioioi.rankings.models import (
    ConfigurableRanking,
    ConfigurableRankingSettings,
    Ranking,
    RankingPage,
    configurable_ranking_partial_key,
    get_configurable_ranking_structure_version,
    invalidate_configurable_ranking_cache,
)
from oioioi.scoresreveal.utils import has_scores_reveal, is_revealed

CONTEST_RANKING_KEY = "c"


def _sum_scores(scores):
    scores = [score for score in scores if score is not None]
    return scores and sum(scores[1:], scores[0]) or None


def _multiply_score(score, coefficient):
    if score is None:
        return None
    return _sum_scores([score] * coefficient)


def _sum_numbers(values):
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def _multiply_number(value, coefficient):
    if value is None:
        return None
    return value * coefficient


def _score_value(score):
    if score is None:
        return None
    if hasattr(score, "value"):
        return score.value
    if hasattr(score, "points") and hasattr(score.points, "value"):
        return score.points.value
    if hasattr(score, "to_int"):
        return score.to_int()
    return score


def _format_number(value):
    if value is None:
        return ""
    if int(value) == value:
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _merge_score_maps(*score_maps):
    merged = {}
    for score_map in score_maps:
        for key, value in score_map.items():
            merged[key] = _sum_numbers([merged.get(key), value])
    return merged


def _prefixed_problem_label(source_name, problem_name):
    return _("%(source)s / %(problem)s") % {
        "source": source_name,
        "problem": problem_name,
    }


class RankingMixinForContestController:
    """ContestController mixin that sets up rankings app."""

    def ranking_controller(self):
        """Return the actual :class:`RankingController` for the contest."""
        return DefaultRankingController(self.contest)

    def update_user_results(self, user, problem_instance, *args, **kwargs):
        super().update_user_results(user, problem_instance, *args, **kwargs)
        self.ranking_controller().invalidate_pi(problem_instance)


ContestController.mix_in(RankingMixinForContestController)


class RankingController(RegisteredSubclassesBase, ObjectWithMixins):
    """Ranking system uses two types of keys: "partial key"s and "full key"s.
    Please note that full keys are abbreviated in the code as "key"s.

    A pair (request, partial_key) should allow to build a full key, while a
    partial_key can always be extracted from the full key.
    partial keys identify the rounds to display and are used everywhere
    outside controllers and rankingsd (e.g. in views and urls). However, the
    actual ranking contents can depend on many other factors, like user
    permissions. This was the reason for introduction of full keys, which
    are always sufficient to choose the right data for serialization and
    display.
    """

    modules_with_subclasses = ["controllers"]
    abstract = True
    PERMISSION_LEVELS = [
        "admin",
        "observer",
        "regular",
    ]
    PERMISSION_CHECKERS = [
        lambda request: "admin" if is_contest_basicadmin(request) else None,
        lambda request: "observer" if is_contest_observer(request) else None,
        lambda request: "regular",
    ]

    def construct_full_key(self, perm_level, partial_key):
        return perm_level + "#" + partial_key

    def get_partial_key(self, key):
        """Extracts partial key from a full key."""
        return key.split("#", 1)[1].split("|", 1)[0]

    def _key_payload(self, key):
        partial_with_payload = key.split("#", 1)[1]
        if "|" not in partial_with_payload:
            return ""
        return partial_with_payload.split("|", 1)[1]

    def replace_partial_key(self, key, new_partial):
        """Replaces partial key in a full key"""
        return self.construct_full_key(self._key_permission(key), new_partial)

    def get_full_key(self, request, partial_key):
        """Returns a full key associated with request and partial_key"""
        for checker in self.PERMISSION_CHECKERS:
            res = checker(request)
            if res is not None:
                return self.construct_full_key(res, partial_key)

    def construct_all_full_keys(self, partial_keys):
        fulls = []
        for perm in self.PERMISSION_LEVELS:
            for partial in partial_keys:
                fulls.append(self.construct_full_key(perm, partial))
        return fulls

    def _key_permission(self, key):
        """Returns a permission level associated with given full key"""
        return key.split("#")[0]

    def is_admin_key(self, key):
        """Returns true if a given full key corresponds to users with
        administrative permissions.
        """
        return self._key_permission(key) == "admin"

    def __init__(self, contest):
        self.contest = contest

    def available_rankings(self, request):
        """Returns a list of available rankings.

        Each ranking is a pair ``(key, description)``.
        """
        raise NotImplementedError

    def can_search_for_users(self):
        """Determines if in this ranking, searching for users is enabled."""
        return False

    def has_any_visible_ranking(self, request):
        return bool(self.available_rankings(request))

    def find_user_position(self, request, partial_key, user):
        """Returns user's position in the ranking.
        User should be an object of class User, not a string with username.

        If user is not in the ranking, None is returned.
        """
        raise NotImplementedError

    def get_rendered_ranking(self, request, partial_key):
        """Retrieves ranking generated by rankingsd.

        You should never override this function. It will be responsible for
        communication with rankingsd and use render_ranking for actual
        HTML generation. Feel free to override render_ranking to customize
        its logic.

        If the ranking is still being generated, or the user requested an
        invalid page, displays an appropriate message.
        """
        try:
            page_nr = int(request.GET.get("page", 1))
        except ValueError:
            return HttpResponseBadRequest("Page number must be integer")
        key = self.get_full_key(request, partial_key)
        # Let's pretend the ranking is always up-to-date during tests.
        if getattr(settings, "MOCK_RANKINGSD", False):
            data = self.get_serialized_ranking(key)
            html = self.render_ranking_page(key, data, page_nr)
            return mark_safe(html)

        ranking = Ranking.objects.get_or_create(contest=self.contest, key=key)[0]
        try:
            page = ranking.pages.get(nr=page_nr)
        except RankingPage.DoesNotExist:
            # The ranking hasn't been yet generated
            if page_nr == 1:
                return mark_safe(render_to_string("rankings/generating_ranking.html"))
            return mark_safe(render_to_string("rankings/no_page.html"))

        context = {
            "ranking_html": mark_safe(page.data),
            "is_up_to_date": ranking.is_up_to_date(),
        }
        return mark_safe(render_to_string("rankings/rendered_ranking.html", context))

    def get_serialized_ranking(self, key):
        return self.serialize_ranking(key)

    def render_ranking_page(self, key, data, page):
        return self._render_ranking_page(key, data, page)

    def build_ranking(self, key):
        """Serializes data and renders html for given key.

        Results are processed using serialize_ranking, and then as many
        pages as needed are rendered. Returns a tuple containing serialized
        data and a list of strings, that are html code of ranking pages.
        """
        data = self.get_serialized_ranking(key)
        pages = []
        num_participants = len(data["rows"])
        on_page = data["participants_on_page"]
        num_pages = (num_participants + on_page - 1) // on_page
        num_pages = max(num_pages, 1)  # Render at least a single page
        for i in range(1, num_pages + 1):
            pages.append(self.render_ranking_page(key, data, i))
        return data, pages

    def _fake_request(self, page):
        """Creates a fake request used to render ranking.

        Pagination engine requires access to request object, so it can
        extract page number from GET parameters.
        """
        fake_req = RequestFactory().get("/?page=" + str(page))
        fake_req.user = AnonymousUser()
        fake_req.contest = self.contest
        # This is required by dj-pagination
        # Normally they monkey patch this function in their middleware
        fake_req.page = lambda _: page
        return fake_req

    def _render_ranking_page(self, key, data, page):
        raise NotImplementedError

    def render_ranking_to_csv(self, request, partial_key):
        raise NotImplementedError

    def serialize_ranking(self, key):
        """Returns some data (representing ranking).
        This data will be used by :meth:`render_ranking`
        to generate the html code.
        """
        raise NotImplementedError


class DefaultRankingController(RankingController):
    description = _("Default ranking")

    def _is_configurable_ranking_partial(self, partial_key):
        return partial_key.startswith("cr") and partial_key[2:].isdigit()

    def _is_configurable_ranking_key(self, key):
        return self._is_configurable_ranking_partial(self.get_partial_key(key))

    def _configurable_rankings(self):
        if not self.contest.controller.supports_configurable_round_rankings():
            return []
        contest_cache_name = "_configurable_rankings_cache"
        version_cache_name = "_configurable_rankings_cache_version"
        cache_version = get_configurable_ranking_structure_version(self.contest.id)
        if (
            not hasattr(self.contest, contest_cache_name)
            or getattr(self.contest, version_cache_name, None) != cache_version
        ):
            setattr(
                self.contest,
                contest_cache_name,
                list(
                    ConfigurableRanking.objects.filter(contest=self.contest)
                    .prefetch_related(
                        "round_configs",
                        "round_configs__round",
                        "round_configs__sub_ranking",
                    )
                    .order_by("order", "name", "id")
                ),
            )
            setattr(self.contest, version_cache_name, cache_version)
        return getattr(self.contest, contest_cache_name)

    def _default_rankings_enabled(self):
        if not self.contest.controller.supports_configurable_round_rankings():
            return True
        contest_cache_name = "_default_rankings_enabled_cache"
        version_cache_name = "_default_rankings_enabled_cache_version"
        cache_version = get_configurable_ranking_structure_version(self.contest.id)
        if (
            not hasattr(self.contest, contest_cache_name)
            or getattr(self.contest, version_cache_name, None) != cache_version
        ):
            setattr(
                self.contest,
                contest_cache_name,
                not ConfigurableRankingSettings.objects.filter(
                    contest=self.contest,
                    show_default_rankings=False,
                ).exists(),
            )
            setattr(self.contest, version_cache_name, cache_version)
        return getattr(self.contest, contest_cache_name)

    def _get_configurable_ranking(self, partial_key):
        if not self._is_configurable_ranking_partial(partial_key):
            return None
        ranking_id = int(partial_key[2:])
        for ranking in self._configurable_rankings():
            if ranking.id == ranking_id:
                return ranking
        return None

    def _referenced_configurable_ranking_ids(self):
        return {
            config.sub_ranking_id
            for ranking in self._configurable_rankings()
            for config in ranking.round_configs.all()
            if config.sub_ranking_id is not None
        }

    def _config_rounds(self, round_config, seen=None):
        seen = set() if seen is None else set(seen)
        if round_config.source_type == "sub_ranking":
            if not round_config.sub_ranking_id or round_config.sub_ranking_id in seen:
                return []
            child_seen = seen | {round_config.sub_ranking_id}
            rounds = []
            for child_config in round_config.sub_ranking.round_configs.all():
                rounds.extend(self._config_rounds(child_config, child_seen))
            unique_rounds = {}
            for round in rounds:
                unique_rounds[round.id] = round
            return list(unique_rounds.values())
        if round_config.round_id:
            return [round_config.round]
        return []

    def _is_round_config_visible(self, round_config, timestamp, seen=None):
        visibility = round_config.column_visibility
        if visibility == "never":
            return False

        rounds = self._config_rounds(round_config, seen)
        if not rounds:
            return False

        round_times = [round.contest.controller.get_round_times(None, round) for round in rounds]
        if visibility == "after_start":
            return any(not times.is_future(timestamp) for times in round_times)
        if visibility == "after_end":
            return all(times.is_past(timestamp) for times in round_times)
        return all(times.public_results_visible(timestamp) for times in round_times)

    def _visible_round_configs(self, ranking, timestamp, seen=None):
        return [config for config in ranking.round_configs.all() if self._is_round_config_visible(config, timestamp, seen)]

    def _regular_visibility_state(self, ranking, timestamp, seen=None):
        seen = set() if seen is None else set(seen)
        if ranking.id in seen:
            return {}
        ranking_seen = seen | {ranking.id}
        state = {}
        visible_ids = []
        for config in self._visible_round_configs(ranking, timestamp, ranking_seen):
            visible_ids.append(config.id)
            if config.source_type == "sub_ranking" and config.sub_ranking_id:
                state.update(self._regular_visibility_state(config.sub_ranking, timestamp, ranking_seen))
        state[ranking.id] = visible_ids
        return state

    def _regular_visibility_signature(self, ranking, timestamp, seen=None):
        state = self._regular_visibility_state(ranking, timestamp, seen)
        parts = []
        for ranking_id in sorted(state):
            parts.append(f"r{ranking_id}:{','.join(str(config_id) for config_id in state[ranking_id])}")
        return ";".join(parts)

    def _visibility_state_for_key(self, key):
        payload = self._key_payload(key)
        if not payload:
            return {}
        state = {}
        for item in payload.split(";"):
            if not item:
                continue
            ranking_token, separator, config_token = item.partition(":")
            if separator != ":" or not ranking_token.startswith("r"):
                continue
            try:
                ranking_id = int(ranking_token[1:])
            except ValueError:
                continue
            visible_ids = set()
            for value in config_token.split(","):
                if value:
                    visible_ids.add(int(value))
            state[ranking_id] = visible_ids
        return state

    def _dependent_configurable_ranking_ids(self, ranking_ids):
        pending = list(ranking_ids)
        seen = set(ranking_ids)
        while pending:
            ranking_id = pending.pop()
            parent_ids = ConfigurableRanking.objects.filter(
                contest=self.contest,
                round_configs__sub_ranking_id=ranking_id,
            ).values_list("id", flat=True)
            for parent_id in parent_ids:
                if parent_id not in seen:
                    seen.add(parent_id)
                    pending.append(parent_id)
        return seen

    def _visible_round_config_ids_for_key(self, key, ranking):
        if self._key_permission(key) != "regular":
            return {config.id for config in ranking.round_configs.all()}
        return self._visibility_state_for_key(key).get(ranking.id, set())

    def _append_configurable_rankings(self, rankings, request):
        if not self.contest.controller.supports_configurable_round_rankings():
            return rankings

        can_see_all = is_contest_basicadmin(request) or is_contest_observer(request)
        referenced_ids = self._referenced_configurable_ranking_ids()
        for ranking in self._configurable_rankings():
            if not can_see_all and ranking.id in referenced_ids:
                continue
            if not ranking.round_configs.exists():
                continue
            if can_see_all or self._visible_round_configs(ranking, request.timestamp):
                rankings.append((ranking.partial_key, ranking.name))
        return rankings

    def get_full_key(self, request, partial_key):
        key = super().get_full_key(request, partial_key)
        if key is None or not self._is_configurable_ranking_partial(partial_key):
            return key

        if self._key_permission(key) != "regular":
            return key

        ranking = self._get_configurable_ranking(partial_key)
        if ranking is None:
            return key

        return f"{key}|{self._regular_visibility_signature(ranking, request.timestamp)}"

    def _iter_rounds(self, can_see_all, timestamp, partial_key, request=None):
        ccontroller = self.contest.controller
        queryset = self.contest.round_set.all()
        if partial_key != CONTEST_RANKING_KEY:
            queryset = queryset.filter(id=partial_key)
        for round in queryset:
            times = ccontroller.get_round_times(request, round)
            if can_see_all or times.public_results_visible(timestamp):
                yield round

    def _rounds_for_ranking(self, request, partial_key=CONTEST_RANKING_KEY):
        can_see_all = is_contest_basicadmin(request) or is_contest_observer(request)
        return self._iter_rounds(can_see_all, request.timestamp, partial_key, request)

    def _rounds_for_key(self, key):
        can_see_all = self._key_permission(key) in {"admin", "observer"}
        partial_key = self.get_partial_key(key)
        return self._iter_rounds(can_see_all, timezone.now(), partial_key)

    def has_any_visible_ranking(self, request):
        if self._default_rankings_enabled():
            for _ in self._rounds_for_ranking(request):
                return True
        return bool(self._append_configurable_rankings([], request))

    def available_rankings(self, request):
        if self._default_rankings_enabled():
            rankings = [(CONTEST_RANKING_KEY, _("Contest"))]
            for round in self._rounds_for_ranking(request):
                rankings.append((str(round.id), round.name))
            if len(rankings) == 1:
                # No rounds have visible results
                rankings = []
            elif len(rankings) == 2:
                # Only a single round => call this "contest ranking".
                rankings = rankings[:1]
        else:
            rankings = []
        return self._append_configurable_rankings(rankings, request)

    # Rankings with different partial key logic need must override this
    # or invalidate_pi accordingly. As a last resort, the all rankings
    # for the given contest may be invalidated.
    def partial_keys_for_probleminstance(self, pi):
        partial_keys = [CONTEST_RANKING_KEY, str(pi.round_id)]
        if self.contest.controller.supports_configurable_round_rankings():
            direct_ranking_ids = ConfigurableRanking.objects.filter(
                contest_id=pi.contest_id,
                round_configs__round_id=pi.round_id,
            ).values_list("id", flat=True).distinct()
            ranking_ids = self._dependent_configurable_ranking_ids(direct_ranking_ids)
            partial_keys.extend(configurable_ranking_partial_key(ranking_id) for ranking_id in ranking_ids)
        return partial_keys

    def keys_for_probleminstance(self, pi):
        return self.construct_all_full_keys(self.partial_keys_for_probleminstance(pi))

    def invalidate_pi(self, pi):
        base_partial_keys = [key for key in self.partial_keys_for_probleminstance(pi) if not self._is_configurable_ranking_partial(key)]
        Ranking.invalidate_queryset(
            Ranking.objects.filter(
                contest_id=pi.contest_id,
                key__in=self.construct_all_full_keys(base_partial_keys),
            )
        )
        direct_ranking_ids = ConfigurableRanking.objects.filter(
            contest_id=pi.contest_id,
            round_configs__round_id=pi.round_id,
        ).values_list("id", flat=True).distinct()
        for ranking_id in self._dependent_configurable_ranking_ids(direct_ranking_ids):
            invalidate_configurable_ranking_cache(pi.contest_id, ranking_id)

    def can_search_for_users(self):
        return True

    def find_user_position(self, request, partial_key, user):
        key = self.get_full_key(request, partial_key)
        if getattr(settings, "MOCK_RANKINGSD", False):
            rows = self.get_serialized_ranking(key)["rows"]
        else:
            try:
                ranking = Ranking.objects.get(contest=self.contest, key=key)
            except Ranking.DoesNotExist:
                return None
            serialized = ranking.serialized or {}
            rows = serialized.get("rows")
            if not rows:  # Ranking isn't ready yet
                return None

        for i, row in enumerate(rows):
            if row["user"] == user:
                return i + 1
        # User not found
        return None

    def _render_ranking_page(self, key, data, page):
        request = self._fake_request(page)
        data["is_admin"] = self.is_admin_key(key)
        return render_to_string("rankings/default_ranking.html", context=data, request=request)

    def render_ranking_page(self, key, data, page):
        if self._is_configurable_ranking_key(key):
            request = self._fake_request(page)
            data["is_admin"] = self.is_admin_key(key)
            return render_to_string("rankings/configurable_ranking.html", context=data, request=request)
        return super().render_ranking_page(key, data, page)

    def _get_csv_header(self, key, data):
        if self._is_configurable_ranking_key(key):
            header = [_("No."), _("Login"), _("First name"), _("Last name")]
            if data["show_medal"]:
                header.append(_("Medal"))
            if data["show_sum"]:
                header.append(_("Sum"))
            if data["show_percentage"]:
                header.append(_("Percentage"))
            if data["show_difference"]:
                header.append(_("Difference"))
            header.extend(config.source_name for config in data["round_configs"])
            header.extend(column["label"] for column in data.get("problem_columns", []))
            return header

        header = [_("No."), _("Login"), _("First name"), _("Last name")]
        for pi, _statement_visible in data["problem_instances"]:
            header.append(pi.get_short_name_display())
        header.append(_("Sum"))
        return header

    def _get_csv_row(self, key, row):
        if self._is_configurable_ranking_key(key):
            line = [
                row["place"],
                row["user"].username,
                row["user"].first_name,
                row["user"].last_name,
            ]
            if row.get("show_medal"):
                line.append(row.get("medal_label", ""))
            if row.get("show_sum"):
                line.append(row.get("total_display", ""))
            if row.get("show_percentage"):
                line.append(row.get("percentage_display", ""))
            if row.get("show_difference"):
                line.append(row.get("difference_display", ""))
            line.extend(row.get("round_score_displays", []))
            line.extend(row.get("problem_score_displays", []))
            return line

        line = [
            row["place"],
            row["user"].username,
            row["user"].first_name,
            row["user"].last_name,
        ]
        line += [r.score if r and r.score is not None else "" for r in row["results"]]
        line.append(row["sum"])
        return line

    def render_ranking_to_csv(self, request, partial_key):
        key = self.get_full_key(request, partial_key)
        data = self.get_serialized_ranking(key)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = make_content_disposition_header("attachment", "{}-{}-{}.csv".format(_("ranking"), self.contest.id, key))
        writer = unicodecsv.writer(response)

        writer.writerow(list(map(force_str, self._get_csv_header(key, data))))
        for row in data["rows"]:
            writer.writerow(list(map(force_str, self._get_csv_row(key, row))))

        return response

    def get_serialized_ranking(self, key):
        if self._is_configurable_ranking_key(key):
            return self._serialize_configurable_ranking(key)
        return super().get_serialized_ranking(key)

    def filter_users_for_ranking(self, key, queryset):
        return queryset.filter(is_superuser=False)

    def _filter_pis_for_ranking(self, partial_key, queryset):
        return queryset

    def _allow_zero_score(self):
        return True

    def _get_users_results(self, pis, results, rounds, users):
        by_user = defaultdict(dict)
        for r in results:
            by_user[r.user_id][r.problem_instance_id] = r
        users = users.filter(id__in=list(by_user.keys()))
        data = []
        all_rounds_trial = all(r.is_trial for r in rounds)
        for user in users.order_by("last_name", "first_name", "username"):
            by_user_row = by_user[user.id]
            user_results = []
            user_data = {"user": user, "results": user_results, "sum": None}

            for pi in pis:
                result = by_user_row.get(pi.id)
                if result and hasattr(result, "submission_report") and hasattr(result.submission_report, "submission_id"):
                    submission_id = result.submission_report.submission_id
                    kwargs = {
                        "contest_id": self.contest.id,
                        "submission_id": submission_id,
                    }
                    result.url = reverse("submission", kwargs=kwargs)

                user_results.append(result)
                if result and result.score and (not pi.round.is_trial or all_rounds_trial):
                    if user_data["sum"] is None:
                        user_data["sum"] = result.score
                    else:
                        user_data["sum"] += result.score
            if user_data["sum"] is not None:
                # This rare corner case with sum being None may happen if all
                # user's submissions do not have scores (for example the
                # problems do not support scoring, or all the evaluations
                # failed with System Errors).
                if self._allow_zero_score() or user_data["sum"].to_int() != 0:
                    data.append(user_data)
        return data

    def _assign_places(self, data, extractor):
        """Assigns places to the serialized ranking ``data``.

        Extractor should return values by which users should be ordered in
        the ranking. Users with the same place should have same value
        returned.
        """
        data.sort(key=extractor, reverse=True)
        prev_sum = None
        place = None
        for i, row in enumerate(data, 1):
            if extractor(row) != prev_sum:
                place = i
                prev_sum = extractor(row)
            row["place"] = place

    def _is_problem_statement_visible(self, key, pi, timestamp):
        if self.is_admin_key(key):
            return True
        ccontroller = self.contest.controller
        context = ContestControllerContext(self.contest, timezone.now(), False)
        return ccontroller.can_see_problem(context, pi) and ccontroller.can_see_statement(context, pi)

    def _get_pis_with_visibility(self, key, pis):
        now = timezone.now()
        return [(pi, self._is_problem_statement_visible(key, pi, now)) for pi in pis]

    def _choose_submission_for_mode(self, submissions, score_mode):
        if not submissions:
            return None

        last_submission = submissions[-1]
        best_submission = submissions[0]
        for submission in submissions[1:]:
            if submission.score > best_submission.score or (
                submission.score == best_submission.score and (submission.date, submission.id) > (best_submission.date, best_submission.id)
            ):
                best_submission = submission

        if score_mode == "best":
            return best_submission
        if score_mode == "last":
            return last_submission
        if score_mode == "last_revealed" and has_scores_reveal(last_submission.problem_instance):
            best_revealed = None
            for submission in submissions:
                if not is_revealed(submission):
                    continue
                if best_revealed is None or submission.score > best_revealed.score or (
                    submission.score == best_revealed.score and (submission.date, submission.id) > (best_revealed.date, best_revealed.id)
                ):
                    best_revealed = submission
            if best_revealed is not None and best_revealed.score > last_submission.score:
                return best_revealed
        return last_submission

    def _submissions_during_round(self, problem_instance, submissions):
        if not submissions:
            return []
        round = problem_instance.round
        if round.end_date is None:
            return list(submissions)
        return [submission for submission in submissions if submission.date <= round.end_date]

    def _max_score_for_problem_instance(self, problem_instance):
        grouped_scores = (
            problem_instance.test_set.filter(kind="NORMAL")
            .values("group")
            .annotate(group_max_score=Max("max_score"))
            .values_list("group_max_score", flat=True)
        )
        return sum(score or 0 for score in grouped_scores)

    def _assign_medals(self, rows, medal_scheme):
        if medal_scheme == "none" or not rows:
            return

        medal_labels = {
            "gold": _("Gold"),
            "silver": _("Silver"),
            "bronze": _("Bronze"),
        }

        if medal_scheme == "og":
            cutoffs = {"gold": 1, "silver": 2, "bronze": 3}
        else:
            participants = len(rows)
            gold_cutoff = min(participants, max(1, participants // 12))
            silver_cutoff = min(participants, max(gold_cutoff + 1, participants // 6))
            bronze_cutoff = min(participants, max(silver_cutoff + 1, participants // 4))
            cutoffs = {
                "gold": gold_cutoff,
                "silver": silver_cutoff,
                "bronze": bronze_cutoff,
            }

        for row in rows:
            if row["place"] <= cutoffs["gold"]:
                medal = "gold"
            elif row["place"] <= cutoffs["silver"]:
                medal = "silver"
            elif row["place"] <= cutoffs["bronze"]:
                medal = "bronze"
            else:
                medal = None
            row["medal"] = medal
            row["medal_label"] = medal_labels.get(medal, "")

    def _calculate_configurable_ranking_data(self, ranking, permission, timestamp=None, cache=None, visible_state=None):
        timestamp = timezone.now() if timestamp is None else timestamp
        cache = {} if cache is None else cache
        visible_state_key = None
        if visible_state is not None:
            visible_state_key = tuple(
                (ranking_id, tuple(sorted(config_ids)))
                for ranking_id, config_ids in sorted(visible_state.items())
            )
        cache_key = (ranking.id, permission, timestamp, visible_state_key)
        if cache_key in cache:
            return cache[cache_key]

        all_configs = list(ranking.round_configs.all())
        if permission == "regular":
            if visible_state is None:
                visible_configs = [config for config in all_configs if self._is_round_config_visible(config, timestamp, {ranking.id})]
            else:
                allowed_ids = visible_state.get(ranking.id, set())
                visible_configs = [config for config in all_configs if config.id in allowed_ids]
        else:
            visible_configs = all_configs

        round_ids = [config.round_id for config in all_configs if config.round_id]
        problem_instances = list(
            ProblemInstance.objects.filter(round_id__in=round_ids)
            .select_related("problem", "round")
            .order_by("round_id", "order", "short_name", "id")
            .distinct()
        )
        problem_instances_by_round = defaultdict(list)
        for problem_instance in problem_instances:
            problem_instances_by_round[problem_instance.round_id].append(problem_instance)

        child_data_by_config = {}
        relevant_user_ids = set()
        for config in all_configs:
            if config.source_type == "sub_ranking" and config.sub_ranking_id:
                child_data = self._calculate_configurable_ranking_data(
                    config.sub_ranking,
                    permission,
                    timestamp,
                    cache,
                    visible_state,
                )
                child_data_by_config[config.id] = child_data
                relevant_user_ids.update(child_data["rows_by_user_id"].keys())

        full_key = self.construct_full_key(permission, ranking.partial_key)
        users = self.filter_users_for_ranking(full_key, User.objects.all()).distinct()
        submissions = (
            Submission.objects.filter(
                problem_instance__in=problem_instances,
                user__in=users,
                kind="NORMAL",
                score__isnull=False,
                user__isnull=False,
            )
            .select_related("user", "problem_instance", "problem_instance__round", "problem_instance__contest")
            .order_by("user_id", "problem_instance_id", "date", "id")
        )

        submissions_by_user = defaultdict(lambda: defaultdict(list))
        for submission in submissions:
            submissions_by_user[submission.user_id][submission.problem_instance_id].append(submission)
            relevant_user_ids.add(submission.user_id)

        max_contest_total = 0
        max_all_time_total = 0
        problem_columns = []
        for config in visible_configs:
            if config.source_type == "sub_ranking" and config.sub_ranking_id:
                child_data = child_data_by_config[config.id]
                max_contest_total += max(config.coefficient, 0) * child_data["max_contest_total"]
                max_all_time_total += max(config.all_time_coefficient, 0) * child_data["max_all_time_total"]
                for child_column in child_data["problem_columns"]:
                    problem_columns.append(
                        {
                            "key": (config.id,) + tuple(child_column["key"]),
                            "label": _prefixed_problem_label(config.source_name, child_column["label"]),
                        }
                    )
                continue

            source_max = sum(
                self._max_score_for_problem_instance(problem_instance)
                for problem_instance in problem_instances_by_round[config.round_id]
            )
            max_contest_total += max(config.coefficient, 0) * source_max
            max_all_time_total += max(config.all_time_coefficient, 0) * source_max
            for problem_instance in problem_instances_by_round[config.round_id]:
                problem_columns.append(
                    {
                        "key": (config.id, problem_instance.id),
                        "label": _prefixed_problem_label(config.source_name, problem_instance.get_short_name_display()),
                    }
                )

        rows_by_user_id = {}
        for user in users.filter(id__in=relevant_user_ids).order_by("last_name", "first_name", "username"):
            source_scores = {}
            contest_contributions = []
            all_time_contributions = []
            contest_problem_contributions = {}
            all_time_problem_contributions = {}
            user_problem_submissions = submissions_by_user[user.id]

            for config in visible_configs:
                if config.source_type == "sub_ranking" and config.sub_ranking_id:
                    child_row = child_data_by_config[config.id]["rows_by_user_id"].get(user.id)
                    contest_base = child_row["contest_total"] if child_row is not None else None
                    all_time_base = child_row["all_time_total"] if child_row is not None else None
                    config_contest_problem_scores = {}
                    config_all_time_problem_scores = {}
                    if child_row is not None:
                        config_contest_problem_scores = {
                            (config.id,) + tuple(key): _multiply_number(value, config.coefficient)
                            for key, value in child_row["contest_problem_scores"].items()
                        }
                        config_all_time_problem_scores = {
                            (config.id,) + tuple(key): _multiply_number(value, config.all_time_coefficient)
                            for key, value in child_row["all_time_problem_scores"].items()
                        }
                else:
                    contest_problem_values = []
                    all_time_problem_values = []
                    config_contest_problem_scores = {}
                    config_all_time_problem_scores = {}
                    for problem_instance in problem_instances_by_round[config.round_id]:
                        pi_submissions = list(user_problem_submissions.get(problem_instance.id, []))
                        if config.ignore_submissions_after is not None:
                            pi_submissions = [submission for submission in pi_submissions if submission.date <= config.ignore_submissions_after]
                        contest_submission = self._choose_submission_for_mode(
                            self._submissions_during_round(problem_instance, pi_submissions),
                            config.score_mode,
                        )
                        all_time_submission = self._choose_submission_for_mode(pi_submissions, config.all_time_score_mode)
                        contest_problem_score = _score_value(contest_submission.score) if contest_submission is not None else None
                        all_time_problem_score = _score_value(all_time_submission.score) if all_time_submission is not None else None
                        contest_problem_values.append(contest_problem_score)
                        all_time_problem_values.append(all_time_problem_score)
                        config_contest_problem_scores[(config.id, problem_instance.id)] = _multiply_number(contest_problem_score, config.coefficient)
                        config_all_time_problem_scores[(config.id, problem_instance.id)] = _multiply_number(
                            all_time_problem_score,
                            config.all_time_coefficient,
                        )
                    contest_base = _sum_numbers(contest_problem_values)
                    all_time_base = _sum_numbers(all_time_problem_values)

                contest_contribution = _multiply_number(contest_base, config.coefficient)
                all_time_contribution = _multiply_number(all_time_base, config.all_time_coefficient)
                contest_contributions.append(contest_contribution)
                all_time_contributions.append(all_time_contribution)
                source_scores[config.id] = _sum_numbers([contest_contribution, all_time_contribution])
                contest_problem_contributions = _merge_score_maps(contest_problem_contributions, config_contest_problem_scores)
                all_time_problem_contributions = _merge_score_maps(all_time_problem_contributions, config_all_time_problem_scores)

            contest_total = _sum_numbers(contest_contributions)
            all_time_total = _sum_numbers(all_time_contributions)
            total = _sum_numbers([contest_total, all_time_total])
            if total is None:
                continue
            if not self._allow_zero_score() and total == 0:
                continue

            rows_by_user_id[user.id] = {
                "user": user,
                "contest_total": contest_total,
                "all_time_total": all_time_total,
                "total": total,
                "source_scores": source_scores,
                "contest_problem_scores": contest_problem_contributions,
                "all_time_problem_scores": all_time_problem_contributions,
                "problem_scores": _merge_score_maps(contest_problem_contributions, all_time_problem_contributions),
            }

        data = {
            "visible_configs": visible_configs,
            "problem_columns": problem_columns,
            "rows_by_user_id": rows_by_user_id,
            "max_contest_total": max_contest_total,
            "max_all_time_total": max_all_time_total,
            "max_total": max_contest_total + max_all_time_total,
        }
        cache[cache_key] = data
        return data

    def _serialize_configurable_ranking(self, key):
        ranking = self._get_configurable_ranking(self.get_partial_key(key))
        if ranking is None:
            return {
                "rows": [],
                "participants_on_page": getattr(settings, "PARTICIPANTS_ON_PAGE", 100),
                "round_configs": [],
                "problem_columns": [],
                "show_sum": True,
                "show_percentage": False,
                "show_difference": False,
                "show_medal": False,
                "max_total": 0,
            }

        permission = self._key_permission(key)
        ranking_data = self._calculate_configurable_ranking_data(
            ranking,
            permission,
            timezone.now(),
            visible_state=self._visibility_state_for_key(key),
        )
        visible_configs = ranking_data["visible_configs"]
        problem_columns = ranking_data["problem_columns"]

        rows = []
        for row in ranking_data["rows_by_user_id"].values():
            round_scores = [row["source_scores"].get(config.id) for config in visible_configs]
            problem_scores = [row["problem_scores"].get(tuple(column["key"])) for column in problem_columns]
            total = _sum_numbers(round_scores)
            if total is None:
                continue
            rows.append(
                {
                    "user": row["user"],
                    "round_scores": round_scores,
                    "round_score_displays": [_format_number(score) for score in round_scores],
                    "problem_scores": problem_scores,
                    "problem_score_displays": [_format_number(score) for score in problem_scores],
                    "contest_total": row["contest_total"],
                    "all_time_total": row["all_time_total"],
                    "total": total,
                    "total_display": _format_number(total),
                    "show_sum": ranking.show_sum,
                    "show_percentage": ranking.show_percentage,
                    "show_difference": ranking.show_difference,
                    "show_medal": ranking.medal_scheme != "none",
                }
            )

        self._assign_places(rows, itemgetter("total"))
        leader_total = rows[0]["total"] if rows else None
        for row in rows:
            total_value = row["total"]
            if ranking.show_percentage and ranking_data["max_total"]:
                percentage = 100.0 * total_value / ranking_data["max_total"]
                row["percentage_value"] = percentage
                row["percentage_display"] = f"{_format_number(percentage)}%"
            else:
                row["percentage_value"] = None
                row["percentage_display"] = ""
            if ranking.show_difference and leader_total is not None:
                difference = leader_total - total_value
                row["difference_value"] = difference
                row["difference_display"] = _format_number(difference)
            else:
                row["difference_value"] = None
                row["difference_display"] = ""

        self._assign_medals(rows, ranking.medal_scheme)
        return {
            "rows": rows,
            "round_configs": visible_configs,
            "problem_columns": problem_columns,
            "participants_on_page": getattr(settings, "PARTICIPANTS_ON_PAGE", 100),
            "show_sum": ranking.show_sum,
            "show_percentage": ranking.show_percentage,
            "show_difference": ranking.show_difference,
            "show_medal": ranking.medal_scheme != "none",
            "max_total": ranking_data["max_total"],
            "ranking_name": ranking.name,
        }

    def serialize_ranking(self, key):
        partial_key = self.get_partial_key(key)
        rounds = list(self._rounds_for_key(key))
        pis = list(
            self._filter_pis_for_ranking(partial_key, ProblemInstance.objects.filter(round__in=rounds)).select_related("problem").prefetch_related("round")
        )
        users = self.filter_users_for_ranking(key, User.objects.all()).distinct()
        results = (
            UserResultForProblem.objects.filter(problem_instance__in=pis, user__in=users)
            .prefetch_related("problem_instance__round")
            .select_related("submission_report", "problem_instance", "problem_instance__contest")
        )

        data = self._get_users_results(pis, results, rounds, users)
        self._assign_places(data, itemgetter("sum"))
        return {
            "rows": data,
            "problem_instances": self._get_pis_with_visibility(key, pis),
            "participants_on_page": getattr(settings, "PARTICIPANTS_ON_PAGE", 100),
        }


def update_rankings_with_user_callback(sender, user, **kwargs):
    contests = Contest.objects.filter(probleminstance__submission__user=user)
    for contest in contests:
        Ranking.invalidate_contest(contest)


PreferencesSaved.connect(update_rankings_with_user_callback)
