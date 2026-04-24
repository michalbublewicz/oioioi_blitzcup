import io
import zipfile
from datetime import UTC, datetime
from unittest.mock import patch
from xml.sax.saxutils import escape

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import RequestFactory
from django.test.utils import override_settings
from django.urls import reverse

from oioioi.base.tests import TestCase, fake_time
from oioioi.blitz.models import BlitzContestConfig, BlitzProblemState
from oioioi.blitz.services import generate_match_contests
from oioioi.contests.current_contest import ContestMode
from oioioi.contests.models import (
    Contest,
    ContestAttachment,
    ContestLink,
    ContestPermission,
    FilesMessage,
    LimitsVisibilityConfig,
    ProblemEditorial,
    ProblemInstance,
    ProblemStatementConfig,
    RankingVisibilityConfig,
    RegistrationAvailabilityConfig,
    Round,
    SubmissionMessage,
    SubmitMessage,
    SubmissionsMessage,
    SubmissionReport,
)
from oioioi.filetracker.tests import TestStreamingMixin
from oioioi.participants.models import Participant, TermsAcceptedPhrase
from oioioi.problems.models import ProblemStatement
from oioioi.problems.utils import copy_problem_instance
from oioioi.programs.models import ProgramSubmission, ProgramsConfig


def build_xlsx_file(rows):
    def _column_name(index):
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    def _cell(column_index, row_index, value):
        return (
            f'<c r="{_column_name(column_index)}{row_index}" t="inlineStr">'
            f"<is><t>{escape(str(value))}</t></is></c>"
        )

    header = ("match_code", "player1_username", "player2_username")
    xml_rows = []
    for row_index, row in enumerate((header, *rows), start=1):
        cells = "".join(_cell(column_index, row_index, value) for column_index, value in enumerate(row, start=1))
        xml_rows.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(xml_rows)}</sheetData>"
        "</worksheet>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return SimpleUploadedFile(
        "matches.xlsx",
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def build_html_statement_file(body="<p>Blitz statement</p>"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "index.html",
            f"<!DOCTYPE html><html><body>{body}</body></html>",
        )
    return ContentFile(buffer.getvalue(), name="statement.html.zip")


@override_settings(CONTEST_MODE=ContestMode.neutral)
class TestBlitzMatchGeneration(TestCase, TestStreamingMixin):
    fixtures = [
        "test_users",
        "test_contest",
        "test_full_package",
        "test_problem_instance",
        "test_permissions",
    ]

    def setUp(self):
        self.source_contest = Contest.objects.get(id="c")
        self.source_contest.controller_name = "oioioi.blitz.controllers.BlitzContestController"
        self.source_contest.default_submissions_limit = 17
        self.source_contest.contact_email = "ladder@example.com"
        self.source_contest.enable_editor = True
        self.source_contest.show_contest_rules = False
        self.source_contest.save()

        self.round_one = Round.objects.get(contest=self.source_contest)
        self.round_one.start_date = datetime(2024, 1, 1, 10, tzinfo=UTC)
        self.round_one.end_date = datetime(2024, 1, 1, 12, tzinfo=UTC)
        self.round_one.results_date = datetime(2024, 1, 1, 12, 30, tzinfo=UTC)
        self.round_one.public_results_date = datetime(2024, 1, 1, 13, tzinfo=UTC)
        self.round_one.save()

        self.round_two = Round.objects.create(
            contest=self.source_contest,
            name="Round 2",
            start_date=datetime(2024, 1, 2, 10, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, 12, tzinfo=UTC),
            results_date=datetime(2024, 1, 2, 12, 30, tzinfo=UTC),
            public_results_date=datetime(2024, 1, 2, 13, tzinfo=UTC),
            is_trial=False,
        )

        self.source_problem = ProblemInstance.objects.get(contest=self.source_contest)
        self.source_problem.submissions_limit = 11
        self.source_problem.order = 1
        self.source_problem.save()

        self.second_problem = copy_problem_instance(ProblemInstance.objects.get(pk=self.source_problem.pk), self.source_contest)
        self.second_problem.round = self.round_two
        self.second_problem.short_name = "sum2"
        self.second_problem.order = 2
        self.second_problem.submissions_limit = 13
        self.second_problem.save()

        BlitzContestConfig.objects.filter(contest=self.source_contest).update(intermission_seconds=90)
        ProgramsConfig.objects.create(contest=self.source_contest, execution_mode="cpu", subtask_parallel_limit=3)
        ProblemStatementConfig.objects.create(contest=self.source_contest, visible="YES")
        RankingVisibilityConfig.objects.create(contest=self.source_contest, visible="NO")
        LimitsVisibilityConfig.objects.create(contest=self.source_contest, visible="YES")
        RegistrationAvailabilityConfig.objects.create(
            contest=self.source_contest,
            enabled="CONFIG",
            registration_available_from=datetime(2023, 12, 1, 10, tzinfo=UTC),
            registration_available_to=datetime(2023, 12, 31, 10, tzinfo=UTC),
        )
        TermsAcceptedPhrase.objects.create(contest=self.source_contest, text="Terms")
        FilesMessage.objects.create(contest=self.source_contest, content="Files message")
        SubmissionsMessage.objects.create(contest=self.source_contest, content="Submissions message")
        SubmitMessage.objects.create(contest=self.source_contest, content="Submit message")
        SubmissionMessage.objects.create(contest=self.source_contest, content="Submission message")
        ContestLink.objects.create(contest=self.source_contest, description="Discord", url="https://example.com", order=7)
        ContestAttachment.objects.create(
            contest=self.source_contest,
            description="Rules",
            content=ContentFile(b"rules", name="rules.txt"),
            round=self.round_two,
            pub_date=datetime(2024, 1, 1, 9, tzinfo=UTC),
        )

        self.player_a = User.objects.create_user(username="ladder_a", password="pass")
        self.player_b = User.objects.create_user(username="ladder_b", password="pass")
        self.player_c = User.objects.create_user(username="ladder_c", password="pass")
        self.player_d = User.objects.create_user(username="ladder_d", password="pass")
        self.extra_source_participant = User.objects.create_user(username="source_only", password="pass")

        Participant.objects.create(contest=self.source_contest, user=self.player_a)
        Participant.objects.create(contest=self.source_contest, user=self.player_b)
        Participant.objects.create(contest=self.source_contest, user=self.extra_source_participant)

        self.admin_user = User.objects.get(username="test_contest_admin")
        self.regular_user = User.objects.get(username="test_user")
        ContestPermission.objects.get_or_create(
            user=self.admin_user,
            contest=self.source_contest,
            permission="contests.contest_admin",
        )

    def _generate(self, rows, prefix="ladder-l1", level_label="Level 1"):
        return generate_match_contests(
            source_contest=self.source_contest,
            level_label=level_label,
            contest_id_prefix=prefix,
            workbook_file=build_xlsx_file(rows),
        )

    def _make_request(self, timestamp, user=None):
        request = RequestFactory().get("/")
        request.contest = self.source_contest
        request.timestamp = timestamp
        request.user = user or self.player_a
        return request

    def _solve_problem(self, problem_instance, user, solve_time):
        submission = ProgramSubmission.objects.create(
            problem_instance=problem_instance,
            user=user,
            date=solve_time,
            kind="NORMAL",
            source_file=ContentFile(b"int main(void) { return 0; }", name="solve.c"),
        )
        submission.status = "OK"
        submission.save(update_fields=["status"])

        report = SubmissionReport.objects.create(
            submission=submission,
            status="ACTIVE",
            kind="NORMAL",
        )
        SubmissionReport.objects.filter(pk=report.pk).update(creation_date=solve_time)

        with transaction.atomic():
            self.source_contest.controller.reconcile_problem_states()

        return submission

    def test_generate_matches_view_permissions(self):
        url = reverse("blitz_generate_matches", kwargs={"contest_id": self.source_contest.id})

        self.client.force_login(self.admin_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Generate matches")

        self.client.force_login(self.regular_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        non_blitz_contest = Contest.objects.create(
            id="plaincontest",
            name="Plain contest",
            controller_name="oioioi.programs.controllers.ProgrammingContestController",
        )
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("blitz_generate_matches", kwargs={"contest_id": non_blitz_contest.id}))
        self.assertEqual(response.status_code, 404)

    def test_generate_matches_success(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("blitz_generate_matches", kwargs={"contest_id": self.source_contest.id}),
            {
                "level_label": "Quarterfinals",
                "contest_id_prefix": "ladder-qf",
                "xlsx_file": build_xlsx_file(
                    [
                        ("M1", self.player_a.username, self.player_b.username),
                        ("M2", self.player_c.username, self.player_d.username),
                    ]
                ),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        contest_one = Contest.objects.get(id="ladder-qf-m1")
        contest_two = Contest.objects.get(id="ladder-qf-m2")

        self.assertEqual(
            contest_one.name,
            f"{self.source_contest.name} - Quarterfinals - M1: {self.player_a.username} vs {self.player_b.username}",
        )
        self.assertEqual(contest_one.controller_name, self.source_contest.controller_name)
        self.assertEqual(contest_one.default_submissions_limit, self.source_contest.default_submissions_limit)
        self.assertEqual(contest_one.contact_email, self.source_contest.contact_email)
        self.assertFalse(contest_one.show_contest_rules)
        self.assertTrue(contest_one.enable_editor)

        self.assertEqual(contest_one.blitz_config.intermission_seconds, 90)
        self.assertEqual(contest_one.programs_config.execution_mode, "cpu")
        self.assertEqual(contest_one.programs_config.subtask_parallel_limit, 3)
        self.assertEqual(contest_one.problemstatementconfig.visible, "YES")
        self.assertEqual(contest_one.rankingvisibilityconfig.visible, "NO")
        self.assertEqual(contest_one.limitsvisibilityconfig.visible, "YES")
        self.assertEqual(contest_one.registrationavailabilityconfig.enabled, "CONFIG")
        self.assertEqual(contest_one.terms_accepted_phrase.text, "Terms")
        self.assertEqual(contest_one.filesmessage.content, "Files message")
        self.assertEqual(contest_one.submissionsmessage.content, "Submissions message")
        self.assertEqual(contest_one.submitmessage.content, "Submit message")
        self.assertEqual(contest_one.submissionmessage.content, "Submission message")

        self.assertEqual(ContestLink.objects.filter(contest=contest_one).count(), 1)
        attachment = ContestAttachment.objects.get(contest=contest_one)
        self.assertEqual(attachment.description, "Rules")
        self.assertEqual(attachment.round.name, self.round_two.name)
        self.assertIn(f"contests/{contest_one.id}/", attachment.content.name)
        self.assertEqual(attachment.content.read(), b"rules")

        round_names = list(Round.objects.filter(contest=contest_one).values_list("name", flat=True))
        self.assertEqual(round_names, [self.round_one.name, self.round_two.name])

        cloned_problems = list(
            ProblemInstance.objects.filter(contest=contest_one)
            .select_related("round")
            .order_by("round__start_date", "order", "short_name")
        )
        self.assertEqual(
            [(pi.short_name, pi.order, pi.submissions_limit, pi.round.name) for pi in cloned_problems],
            [
                (
                    self.source_problem.short_name,
                    self.source_problem.order,
                    self.source_problem.submissions_limit,
                    self.round_one.name,
                ),
                (
                    self.second_problem.short_name,
                    self.second_problem.order,
                    self.second_problem.submissions_limit,
                    self.round_two.name,
                ),
            ],
        )
        self.assertEqual(BlitzProblemState.objects.filter(problem_instance__contest=contest_one).count(), 2)

        self.assertEqual(set(Participant.objects.filter(contest=contest_one).values_list("user__username", flat=True)), {self.player_a.username, self.player_b.username})
        self.assertEqual(set(Participant.objects.filter(contest=contest_two).values_list("user__username", flat=True)), {self.player_c.username, self.player_d.username})
        self.assertEqual(Participant.objects.filter(contest=contest_one).count(), 2)
        self.assertNotIn(self.extra_source_participant.username, Participant.objects.filter(contest=contest_one).values_list("user__username", flat=True))

        self.assertTrue(ContestPermission.objects.filter(contest=contest_one, user=self.admin_user, permission="contests.contest_admin").exists())

        self.client.force_login(self.player_a)
        self.assertEqual(
            self.client.get(reverse("default_contest_view", kwargs={"contest_id": contest_one.id}), follow=True).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("default_contest_view", kwargs={"contest_id": contest_two.id}), follow=True).status_code, 403)

    def test_blitz_problems_list_shows_editorial_link(self):
        ProblemEditorial.objects.create(
            problem_instance=self.source_problem,
            content=ContentFile(b"%PDF-1.4 blitz-editorial", name="blitz-editorial.pdf"),
            publication_date=datetime(2024, 1, 1, 9, tzinfo=UTC),
        )

        self.client.force_login(self.player_a)
        problems_url = reverse("problems_list", kwargs={"contest_id": self.source_contest.id})
        editorial_url = reverse(
            "problem_editorial",
            kwargs={
                "contest_id": self.source_contest.id,
                "problem_instance": self.source_problem.short_name,
            },
        )

        with fake_time(datetime(2024, 1, 1, 10, 30, tzinfo=UTC)):
            response = self.client.get(problems_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, editorial_url)

    def test_blitz_status_payload_includes_problem_id_for_latest_event(self):
        solve_time = datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
        self._solve_problem(self.source_problem, self.player_a, solve_time)

        payload = self.source_contest.controller.serialize_live_status(
            self._make_request(datetime(2024, 1, 1, 10, 31, tzinfo=UTC))
        )

        self.assertEqual(payload["latest_event"]["problem_id"], self.source_problem.id)

    def test_blitz_html_statement_includes_inline_alert_wiring(self):
        ProblemStatement.objects.create(
            problem=self.source_problem.problem,
            content=build_html_statement_file("<h1>Round one</h1>"),
        )
        self.client.force_login(self.player_a)

        with fake_time(datetime(2024, 1, 1, 10, tzinfo=UTC)):
            response = self.client.get(
                reverse(
                    "problem_statement",
                    kwargs={
                        "contest_id": self.source_contest.id,
                        "problem_instance": self.source_problem.short_name,
                    },
                ),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="blitz-statement-page"')
        self.assertContains(response, 'id="blitz-statement-alert"')
        self.assertContains(response, "blitz-statement-problem")
        self.assertContains(response, "blitz-live-popup")
        self.assertNotContains(response, 'class="blitz-statement-page__hero"')
        self.assertContains(response, "$(window).on('blitzStatusUpdated'")
        self.assertContains(response, "$(window).on('blitzProblemSolved'")

    def test_blitz_pdf_statement_renders_wrapper_with_live_popup(self):
        self.client.force_login(self.player_a)

        with fake_time(datetime(2024, 1, 1, 10, tzinfo=UTC)):
            response = self.client.get(
                reverse(
                    "problem_statement",
                    kwargs={
                        "contest_id": self.source_contest.id,
                        "problem_instance": self.source_problem.short_name,
                    },
                ),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.streaming)
        self.assertContains(response, 'class="statement-shell__object"')
        self.assertContains(response, 'id="blitz-live-popup"')
        self.assertContains(response, 'id="blitz-statement-alert"')
        self.assertNotContains(response, 'class="blitz-statement-page__hero"')
        self.assertContains(
            response,
            reverse(
                "problem_statement_file",
                kwargs={
                    "contest_id": self.source_contest.id,
                    "problem_instance": self.source_problem.short_name,
                },
            ),
        )
        self.assertContains(response, 'type="application/pdf"')
        self.assertContains(response, "calc(100vh - 8.5rem)")

    def test_blitz_pdf_statement_file_view_streams_pdf(self):
        self.client.force_login(self.player_a)

        with fake_time(datetime(2024, 1, 1, 10, tzinfo=UTC)):
            response = self.client.get(
                reverse(
                    "problem_statement_file",
                    kwargs={
                        "contest_id": self.source_contest.id,
                        "problem_instance": self.source_problem.short_name,
                    },
                )
            )

        self.assertEqual(response.status_code, 200)
        content = self.streamingContent(response)
        self.assertTrue(content.startswith(b"%PDF"))

    def test_blitz_inline_alert_wiring_is_only_present_on_statement_page(self):
        self.client.force_login(self.player_a)

        with fake_time(datetime(2024, 1, 1, 10, tzinfo=UTC)):
            response = self.client.get(reverse("problems_list", kwargs={"contest_id": self.source_contest.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "blitz-live-popup")
        self.assertNotContains(response, 'id="blitz-statement-alert"')
        self.assertNotContains(response, "blitz-statement-problem")

    def test_generate_matches_copies_problem_editorials(self):
        publication_date = datetime(2024, 1, 1, 9, tzinfo=UTC)
        ProblemEditorial.objects.create(
            problem_instance=self.source_problem,
            content=ContentFile(b"%PDF-1.4 cloned-editorial", name="cloned-editorial.pdf"),
            publication_date=publication_date,
        )

        created = self._generate([("M1", self.player_a.username, self.player_b.username)])
        target_contest = created[0]
        target_problem = ProblemInstance.objects.get(
            contest=target_contest,
            short_name=self.source_problem.short_name,
        )

        self.assertTrue(hasattr(target_problem, "editorial"))
        self.assertEqual(target_problem.editorial.publication_date, publication_date)
        self.assertEqual(target_problem.editorial.content.read(), b"%PDF-1.4 cloned-editorial")

    def test_generate_matches_rejects_missing_username(self):
        with self.assertRaises(ValidationError):
            self._generate([("M1", self.player_a.username, "")])
        self.assertEqual(Contest.objects.filter(id__startswith="ladder-l1").count(), 0)

    def test_generate_matches_rejects_unknown_user(self):
        with self.assertRaises(ValidationError):
            self._generate([("M1", self.player_a.username, "does_not_exist")])
        self.assertEqual(Contest.objects.filter(id__startswith="ladder-l1").count(), 0)

    def test_generate_matches_rejects_same_user_in_one_match(self):
        with self.assertRaises(ValidationError):
            self._generate([("M1", self.player_a.username, self.player_a.username)])
        self.assertEqual(Contest.objects.filter(id__startswith="ladder-l1").count(), 0)

    def test_generate_matches_rejects_same_user_in_multiple_matches(self):
        with self.assertRaises(ValidationError):
            self._generate(
                [
                    ("M1", self.player_a.username, self.player_b.username),
                    ("M2", self.player_a.username, self.player_c.username),
                ]
            )
        self.assertEqual(Contest.objects.filter(id__startswith="ladder-l1").count(), 0)

    def test_generate_matches_rejects_duplicate_match_code_after_normalization(self):
        with self.assertRaises(ValidationError):
            self._generate(
                [
                    ("M 1", self.player_a.username, self.player_b.username),
                    ("m-1", self.player_c.username, self.player_d.username),
                ]
            )
        self.assertEqual(Contest.objects.filter(id__startswith="ladder-l1").count(), 0)

    def test_generate_matches_rejects_generated_id_collision(self):
        Contest.objects.create(
            id="ladder-l1-m1",
            name="Existing",
            controller_name="oioioi.programs.controllers.ProgrammingContestController",
        )

        with self.assertRaises(ValidationError):
            self._generate([("M1", self.player_a.username, self.player_b.username)])
        self.assertEqual(Contest.objects.filter(id="ladder-l1-m1").count(), 1)

    def test_generate_matches_works_without_openpyxl(self):
        with patch("oioioi.blitz.services._read_xlsx_rows_with_openpyxl", side_effect=ImportError):
            created = self._generate([("M1", self.player_a.username, self.player_b.username)])

        self.assertEqual([contest.id for contest in created], ["ladder-l1-m1"])
        self.assertEqual(
            set(Participant.objects.filter(contest=created[0]).values_list("user__username", flat=True)),
            {self.player_a.username, self.player_b.username},
        )
