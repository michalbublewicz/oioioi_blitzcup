from datetime import datetime, timedelta, timezone as dt_timezone
from operator import itemgetter

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.template.loader import render_to_string

from oioioi.acm.controllers import ACMContestController
from oioioi.base.utils import get_user_display_name
from oioioi.blitz.admin import ContestAdminWithBlitzConfigInlineMixin
from oioioi.blitz.models import BlitzContestConfig, BlitzProblemState
from oioioi.contests.models import ProblemInstance, Submission, SubmissionReport, UserResultForProblem
from oioioi.contests.scores import IntegerScore
from oioioi.rankings.controllers import CONTEST_RANKING_KEY, DefaultRankingController


class BlitzContestController(ACMContestController):
    description = _('Blitz cup contest')
    scoring_description = _(
        'Nie chce mi sie XD '
    )
    problems_list_template = 'blitz/problems_list.html'
    html_statement_template = 'blitz/html_statement.html'

    def mixins_for_admin(self):
        return super().mixins_for_admin() + (ContestAdminWithBlitzConfigInlineMixin,)

    def adjust_contest(self):
        BlitzContestConfig.objects.get_or_create(contest=self.contest)
        for pi in ProblemInstance.objects.filter(contest=self.contest):
            BlitzProblemState.objects.get_or_create(problem_instance=pi)

    def ranking_controller(self):
        return BlitzRankingController(self.contest)

    def get_intermission_seconds(self):
        config, _created = BlitzContestConfig.objects.get_or_create(contest=self.contest)
        return config.intermission_seconds

    def order_problem_instances(self, problem_instances):
        return sorted(
            problem_instances,
            key=lambda pi: (
                pi.round.start_date if pi.round else datetime(2999, 1, 1, tzinfo=dt_timezone.utc),
                getattr(pi, 'order', 0),
                pi.short_name,
                pi.id,
            ),
        )

    def get_ordered_problem_instances(self):
        queryset = ProblemInstance.objects.filter(contest=self.contest).select_related('problem', 'round')
        return self.order_problem_instances(queryset)

    def get_problem_position(self, problem_instance, ordered_problem_instances=None):
        ordered = ordered_problem_instances or self.get_ordered_problem_instances()
        for index, pi in enumerate(ordered, start=1):
            if pi.id == problem_instance.id:
                return index
        return None

    def get_problem_points(self, problem_instance, ordered_problem_instances=None):
        position = self.get_problem_position(problem_instance, ordered_problem_instances)
        if position is None:
            return 0
        if position == 1:
            return 2
        if position == 2:
            return 3
        return 2 * (position - 1)

    def _contest_start(self, ordered_problem_instances):
        if not ordered_problem_instances:
            return None
        first_round = ordered_problem_instances[0].round
        return first_round.start_date if first_round else None

    def _contest_end(self, ordered_problem_instances):
        end_dates = [pi.round.end_date for pi in ordered_problem_instances if pi.round and pi.round.end_date]
        return max(end_dates) if end_dates else None

    def _get_locked_state_map(self, ordered_problem_instances):
        state_map = {}
        for pi in ordered_problem_instances:
            state, _created = BlitzProblemState.objects.select_for_update().get_or_create(problem_instance=pi)
            state_map[pi.id] = state
        return state_map

    def _get_state_map(self, ordered_problem_instances):
        states = BlitzProblemState.objects.filter(problem_instance__in=ordered_problem_instances).select_related('solved_by', 'winning_submission')
        state_map = {state.problem_instance_id: state for state in states}
        for pi in ordered_problem_instances:
            state_map.setdefault(pi.id, BlitzProblemState(problem_instance=pi))
        return state_map

    def _find_winning_submission(self, problem_instance, open_time, contest_end):
        if open_time is None:
            return None, None

        reports = (
            SubmissionReport.objects.filter(
                submission__problem_instance=problem_instance,
                submission__kind='NORMAL',
                submission__status='OK',
                submission__user__isnull=False,
                status='ACTIVE',
                creation_date__gte=open_time,
            )
            .select_related('submission', 'submission__user')
            .order_by('creation_date', 'submission_id')
        )
        if contest_end is not None:
            reports = reports.filter(creation_date__lte=contest_end)

        report = reports.first()
        if report is not None:
            return report.submission, report.creation_date

        submissions = Submission.objects.filter(
            problem_instance=problem_instance,
            kind='NORMAL',
            status='OK',
            user__isnull=False,
            date__gte=open_time,
        ).order_by('date', 'id')
        if contest_end is not None:
            submissions = submissions.filter(date__lte=contest_end)
        submission = submissions.first()
        return submission, submission.date if submission is not None else None

    def reconcile_problem_states(self):
        ordered = self.get_ordered_problem_instances()
        state_map = self._get_locked_state_map(ordered)
        contest_start = self._contest_start(ordered)
        contest_end = self._contest_end(ordered)
        previous_closed_at = None
        changed = []

        for index, pi in enumerate(ordered):
            state = state_map[pi.id]
            if index == 0:
                open_time = contest_start
            elif previous_closed_at is None:
                open_time = None
            else:
                open_time = previous_closed_at + timedelta(seconds=self.get_intermission_seconds())

            if contest_end is not None and open_time is not None and open_time > contest_end:
                open_time = None

            winning_submission, verdict_time = self._find_winning_submission(pi, open_time, contest_end)
            solved_by = winning_submission.user if winning_submission else None
            closed_at = verdict_time if winning_submission else None

            if (
                state.winning_submission_id != (winning_submission.id if winning_submission else None)
                or state.solved_by_id != (solved_by.id if solved_by else None)
                or state.closed_at != closed_at
            ):
                changed.append(
                    {
                        'problem_instance': pi,
                        'old_user_id': state.solved_by_id,
                        'new_user_id': solved_by.id if solved_by else None,
                        'old_submission_id': state.winning_submission_id,
                        'new_submission_id': winning_submission.id if winning_submission else None,
                    }
                )
                state.solved_by = solved_by
                state.winning_submission = winning_submission
                state.closed_at = closed_at
                state.save(update_fields=['solved_by', 'winning_submission', 'closed_at'])
            previous_closed_at = closed_at

        return changed

    def get_blitz_status(self, request_or_context):
        context = self.make_context(request_or_context)
        ordered = self.get_ordered_problem_instances()
        status = {
            'phase': 'empty',
            'ordered_problem_instances': ordered,
            'current_problem': None,
            'next_problem': None,
            'completed_problem': None,
            'intermission_until': None,
            'contest_start': self._contest_start(ordered),
            'contest_end': self._contest_end(ordered),
        }
        if not ordered:
            return status

        now = context.timestamp
        if status['contest_start'] and now < status['contest_start']:
            status['phase'] = 'before_start'
            status['current_problem'] = ordered[0]
            return status

        state_map = self._get_state_map(ordered)
        contest_end = status['contest_end']
        intermission = timedelta(seconds=self.get_intermission_seconds())

        for index, pi in enumerate(ordered):
            state = state_map[pi.id]
            if state.closed_at:
                if index + 1 >= len(ordered):
                    status['phase'] = 'finished'
                    status['completed_problem'] = pi
                    return status
                intermission_until = state.closed_at + intermission
                if contest_end is not None and state.closed_at > contest_end:
                    status['phase'] = 'finished'
                    status['completed_problem'] = pi
                    return status
                if now < intermission_until:
                    status['phase'] = 'intermission'
                    status['completed_problem'] = pi
                    status['next_problem'] = ordered[index + 1]
                    status['intermission_until'] = intermission_until
                    return status
                continue

            if contest_end is not None and now > contest_end:
                status['phase'] = 'finished'
                return status

            status['phase'] = 'problem_open'
            status['current_problem'] = pi
            status['next_problem'] = ordered[index + 1] if index + 1 < len(ordered) else None
            return status

        status['phase'] = 'finished'
        return status

    def serialize_live_status(self, request):
        status = self.get_blitz_status(request)
        ordered = status['ordered_problem_instances']
        state_map = self._get_state_map(ordered)
        latest_completed = None
        completed_states = [state for state in state_map.values() if state.closed_at and state.solved_by_id and state.winning_submission_id]
        if completed_states:
            latest_completed = max(completed_states, key=lambda state: (state.closed_at, state.problem_instance_id))

        event = None
        if latest_completed is not None:
            event = {
                'event_id': f'{latest_completed.problem_instance_id}:{latest_completed.winning_submission_id}:{int(latest_completed.closed_at.timestamp())}',
                'solver': get_user_display_name(latest_completed.solved_by),
                'short_name': latest_completed.problem_instance.get_short_name_display(),
                'problem_name': latest_completed.problem_instance.problem.name,
                'points': self.get_problem_points(latest_completed.problem_instance, ordered),
                'closed_at': timezone.localtime(latest_completed.closed_at).isoformat(),
                'details': str(self._live_event_details(latest_completed.problem_instance, ordered)),
            }

        return {
            'phase': status['phase'],
            'current_problem': self._serialize_problem(status['current_problem'], ordered),
            'next_problem': self._serialize_problem(status['next_problem'], ordered),
            'completed_problem': self._serialize_problem(status['completed_problem'], ordered),
            'intermission_until': timezone.localtime(status['intermission_until']).isoformat() if status['intermission_until'] else None,
            'latest_event': event,
        }

    def _live_event_details(self, problem_instance, ordered_problem_instances):
        position = self.get_problem_position(problem_instance, ordered_problem_instances)
        if position is not None and position < len(ordered_problem_instances):
            return _('Następne zadanie po przerwie.')
        return _('Koniec.')

    def _serialize_problem(self, problem_instance, ordered_problem_instances):
        if problem_instance is None:
            return None
        return {
            'id': problem_instance.id,
            'short_name': problem_instance.get_short_name_display(),
            'name': problem_instance.problem.name,
            'order': self.get_problem_position(problem_instance, ordered_problem_instances),
            'points': self.get_problem_points(problem_instance, ordered_problem_instances),
        }

    def default_can_see_statement(self, request_or_context, problem_instance):
        status = self.get_blitz_status(request_or_context)
        current_problem = status['current_problem']
        return status['phase'] == 'problem_open' and current_problem is not None and current_problem.id == problem_instance.id

    def can_submit(self, request, problem_instance, check_round_times=True):
        if not super().can_submit(request, problem_instance, check_round_times):
            return False
        status = self.get_blitz_status(request)
        current_problem = status['current_problem']
        return status['phase'] == 'problem_open' and current_problem is not None and current_problem.id == problem_instance.id

    def update_user_result_for_problem(self, result):
        state, _created = BlitzProblemState.objects.get_or_create(problem_instance=result.problem_instance)
        if state.solved_by_id == result.user_id and state.winning_submission_id:
            result.score = IntegerScore(self.get_problem_points(result.problem_instance))
            result.status = 'OK'
            result.submission_report = SubmissionReport.objects.filter(submission=state.winning_submission, status='ACTIVE').order_by('-id').first()
        else:
            result.score = None
            result.status = None
            result.submission_report = None

    def get_notification_message_submission_judged(self, submission):
        state, _created = BlitzProblemState.objects.get_or_create(problem_instance=submission.problem_instance)
        if state.winning_submission_id == submission.id:
            return _('Twoje zgłoszenie do %(short_name)s było pierwsze!')
        if submission.score is not None and getattr(submission.score, 'accepted', False):
            return _('Poprawne rozwiązanie do %(short_name)s , ale ktoś cię wyprzedził.')
        return _('Zgłoszenie do %(short_name)s jest złe.')

    def submission_judged(self, submission, rejudged=False):
        super().submission_judged(submission, rejudged)
        if submission.problem_instance.contest_id != self.contest.id:
            return

        with transaction.atomic():
            changed = self.reconcile_problem_states()

        affected = set()
        for item in changed:
            pi = item['problem_instance']
            if item['old_user_id']:
                affected.add((item['old_user_id'], pi.id))
            if item['new_user_id']:
                affected.add((item['new_user_id'], pi.id))

        for user_id, problem_instance_id in affected:
            user = User.objects.get(id=user_id)
            pi = ProblemInstance.objects.get(id=problem_instance_id)
            self.update_user_results(user, pi)

    def get_problems_list_context(self, request, problems_statements):
        status = self.get_blitz_status(request)
        ordered = status['ordered_problem_instances']
        state_map = self._get_state_map(ordered)
        rows = []
        for pi, statement_visible, _round_time, _problem_limits, result, submissions_left, submissions_limit, can_submit, last_submission in problems_statements:
            state = state_map.get(pi.id)
            if state and state.solved_by_id:
                state_label = _('Solved')
                state_class = 'badge-success'
            elif status['current_problem'] and status['current_problem'].id == pi.id:
                state_label = _('Open now')
                state_class = 'badge-danger'
            elif status['next_problem'] and status['next_problem'].id == pi.id:
                state_label = _('')
                state_class = 'badge-warning'
            else:
                state_label = _('')
                state_class = 'badge-secondary'
            rows.append(
                {
                    'pi': pi,
                    'statement_visible': statement_visible,
                    'result': result,
                    'submissions_left': submissions_left,
                    'submissions_limit': submissions_limit,
                    'can_submit': can_submit,
                    'last_submission': last_submission,
                    'order': self.get_problem_position(pi, ordered),
                    'points': self.get_problem_points(pi, ordered),
                    'winner': get_user_display_name(state.solved_by) if state and state.solved_by_id else None,
                    'state_label': state_label,
                    'state_class': state_class,
                }
            )
        return {
            'blitz_rows': rows,
            'blitz_status': status,
            'blitz_status_endpoint': reverse('blitz_status', kwargs={'contest_id': self.contest.id}),
            'blitz_initial_payload': self.serialize_live_status(request),
        }

    def get_statement_extra_context(self, request, problem_instance, rendered_html_statement=False):
        return {
            'blitz_status_endpoint': reverse('blitz_status', kwargs={'contest_id': self.contest.id}),
            'blitz_initial_payload': self.serialize_live_status(request),
        }


class BlitzRankingController(DefaultRankingController):
    description = _('Blitz ranking')

    def _iter_rounds(self, can_see_all, timestamp, partial_key, request=None):
        ccontroller = self.contest.controller
        queryset = self.contest.round_set.all()
        if partial_key != CONTEST_RANKING_KEY:
            queryset = queryset.filter(id=partial_key)
        for round in queryset:
            times = ccontroller.get_round_times(request, round)
            if can_see_all or times.is_active(timestamp) or times.is_past(timestamp):
                yield round

    def _render_ranking_page(self, key, data, page):
        request = self._fake_request(page)
        request.timestamp = timezone.now()
        data['is_admin'] = self.is_admin_key(key)
        data['blitz_status_endpoint'] = reverse('blitz_status', kwargs={'contest_id': self.contest.id})
        data['blitz_initial_payload'] = self.contest.controller.serialize_live_status(request)
        return render_to_string('blitz/ranking.html', context=data, request=request)

    def _get_csv_header(self, key, data):
        header = [_('No.'), _('Login'), _('First name'), _('Last name'), _('Points')]
        for pi, _statement_visible in data['problem_instances']:
            header.append(pi.get_short_name_display())
        return header

    def _get_csv_row(self, key, row):
        line = [row['place'], row['user'].username, row['user'].first_name, row['user'].last_name, row['sum']]
        line += [r.score if r and r.score is not None else '' for r in row['results']]
        return line

    def filter_users_for_ranking(self, key, queryset):
        return self.contest.controller.registration_controller().filter_participants(queryset).distinct()

    def serialize_ranking(self, key):
        partial_key = self.get_partial_key(key)
        rounds = list(self._rounds_for_key(key))
        pis = list(self.contest.controller.order_problem_instances(ProblemInstance.objects.filter(round__in=rounds).select_related('problem').prefetch_related('round')))
        users = self.filter_users_for_ranking(key, User.objects.all())
        results = (
            UserResultForProblem.objects.filter(problem_instance__in=pis, user__in=users)
            .prefetch_related('problem_instance__round')
            .select_related('submission_report', 'problem_instance', 'problem_instance__contest')
        )
        by_user = {}
        for result in results:
            by_user.setdefault(result.user_id, {})[result.problem_instance_id] = result

        data = []
        for user in users.order_by('last_name', 'first_name', 'username'):
            user_results = []
            total = IntegerScore(0)
            for pi in pis:
                result = by_user.get(user.id, {}).get(pi.id)
                if result and hasattr(result, 'submission_report') and result.submission_report is not None and hasattr(result.submission_report, 'submission_id'):
                    result.url = reverse('submission', kwargs={'contest_id': self.contest.id, 'submission_id': result.submission_report.submission_id})
                user_results.append(result)
                if result and result.score is not None:
                    total += result.score
            data.append({'user': user, 'results': user_results, 'sum': total})

        self._assign_places(data, itemgetter('sum'))
        return {
            'rows': data,
            'problem_instances': self._get_pis_with_visibility(key, pis),
            'participants_on_page': getattr(settings, 'PARTICIPANTS_ON_PAGE', 100),
        }
