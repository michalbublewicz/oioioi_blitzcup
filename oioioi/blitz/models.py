from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _

from oioioi.contests.models import Contest, ProblemInstance, Submission


class BlitzContestConfig(models.Model):
    contest = models.OneToOneField(
        Contest,
        related_name='blitz_config',
        verbose_name=_('contest'),
        on_delete=models.CASCADE,
    )
    intermission_seconds = models.PositiveIntegerField(
        default=60,
        verbose_name=_('przerwa'),
        help_text=_('Ile przerwy po solvie.'),
    )

    class Meta:
        verbose_name = _('blitz contest configuration')
        verbose_name_plural = _('blitz contest configurations')

    def __str__(self):
        return f'{self.contest}: blitz'


class BlitzProblemState(models.Model):
    problem_instance = models.OneToOneField(
        ProblemInstance,
        related_name='blitz_state',
        verbose_name=_('problem instance'),
        on_delete=models.CASCADE,
    )
    solved_by = models.ForeignKey(
        User,
        related_name='blitz_solved_problems',
        blank=True,
        null=True,
        verbose_name=_('solved by'),
        on_delete=models.SET_NULL,
    )
    winning_submission = models.ForeignKey(
        Submission,
        related_name='blitz_wins',
        blank=True,
        null=True,
        verbose_name=_('winning submission'),
        on_delete=models.SET_NULL,
    )
    closed_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_('closed at'),
    )

    class Meta:
        verbose_name = _('blitz problem state')
        verbose_name_plural = _('blitz problem states')

    def __str__(self):
        return f'{self.problem_instance}: {self.solved_by or "open"}'
