import logging
import pickle
from collections import defaultdict
from datetime import timedelta  # pylint: disable=E0611

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from oioioi.base.fields import EnumField, EnumRegistry
from oioioi.base.models import PublicMessage
from oioioi.contests.models import Contest, Round

_configurable_ranking_structure_versions = defaultdict(int)


class RankingRecalc(models.Model):
    pass


class Ranking(models.Model):
    """Represents the state (i.e. is it up to date) and data (both in
    serialized and html formats) for a single ranking.

    For the purposes of this class, we identify the ranking by its contest
    and key. The generated ranking must NOT depend on the request or
    any other ranking.

    This class is responsible only for dealing with WHEN to recalculate
    and to store the serialized data and html for the ranking. Anything
    beyond that should be delegated to RankingController.

    Invalidation is handled explicitly. We assume our ranking is valid,
    until someone else (probably ContestController and friends) tells us
    that something changed. Then the ranking is marked as invalid (not up
    to date) with the help of invalidate_* methods.

    We use _cooldown_ strategy of recalculation. Anytime we regenerate
    ranking we set a cooldown, based on how much time the previous
    recalculation took. If the ranking is invalidated during the cooldown
    period, we don't recalculate until the cooldown period is over.

    Consider the following example of how cooldowns work:

        1) 00:01 - First invalidation event.
                   The ranking is invalid.
        2) 00:02 - The recalculation starts. It didn't start immediately
                   at the time of invalidation, because daemon polls
                   for the rankings needing regeneration so it needed some
                   time to notice.
        3) 00:12 - The recalculation ends, duration was 10 seconds. Ranking
                   is up to date now.
        4) 01:00 - Second invalidation event. Ranking is invalid.
        5) 01:01 - Second recalculation starts.
                   Let's assume RANKING_COOLDOWN_FACTOR = 2.
                   Then the cooldown is 20 seconds, until 01:21
        6) 01:03 - Third invalidation event.
        7) 01:05 - Fourth invalidation event.
        7) 01:08 - Ranking recalculation initiated by the second event ends.
                   Ranking is still invalid, because of the third event.
                   It took 7 seconds.
        8) 01:21 - Cooldown is over. We recalculate ranking because of 3rd
                   and 4th events.
                   The new cooldown is set for 14 seconds, until 01:35.
        9) 01:30 - The recalculation ends.

    The cooldowns can be configured by setting:
    RANKING_COOLDOWN_FACTOR - how long should the cooldown be, related to
                              the last recalculation.
    RANKING_MIN_COOLDOWN - minimum cooldown duration (safety limit)
    RANKING_MAX_COOLDOWN - maximum cooldown duration (safety limit)

    NOTE: We use the local time (and not the database time), for all time
    calculations, including the cooldowns, so be careful about drastic
    changes of system time on the generating machine.
    """

    contest = models.ForeignKey(Contest, on_delete=models.CASCADE)
    key = models.CharField(max_length=255)

    # advisory
    invalidation_date = models.DateTimeField(auto_now_add=True)
    last_recalculation_date = models.DateTimeField(null=True)

    # used to determine cooldown
    last_recalculation_duration = models.DurationField(default=timedelta(0))

    # internal, use serialized instead
    serialized_data = models.BinaryField(null=True)

    # internal to ranking recalculation mechanism
    # use invalidate_* and is_up_to_date instead
    needs_recalculation = models.BooleanField(default=True)
    cooldown_date = models.DateTimeField(auto_now_add=True)
    recalc_in_progress = models.ForeignKey(RankingRecalc, null=True, on_delete=models.SET_NULL)

    @property
    def serialized(self):
        """Serialized data of this ranking"""
        if not self.serialized_data:
            return None

        return pickle.loads(self.serialized_data)

    def controller(self):
        """RankingController of the contest"""
        return self.contest.controller.ranking_controller()

    @classmethod
    def invalidate_queryset(cls, qs):
        """Marks queryset of rankings as invalid"""
        qs.all().update(needs_recalculation=True, invalidation_date=timezone.now())

    @classmethod
    def invalidate_contest(cls, contest):
        """Marks all the keys in the constest as invalid"""
        return cls.invalidate_queryset(cls.objects.filter(contest=contest))

    def is_up_to_date(self):
        """Is all the data for this contest up to date (i.e. not invalidated
        since the last recalculation succeeded)?

        If it is not up_to_date we still guarantee that the data is
        in consistent state from the last recalculation.
        """
        return not self.needs_recalculation and self.recalc_in_progress_id is None

    class Meta:
        unique_together = ("contest", "key")


class RankingPage(models.Model):
    """Single page of a ranking"""

    ranking = models.ForeignKey(Ranking, related_name="pages", on_delete=models.CASCADE)
    nr = models.IntegerField()
    data = models.TextField()


def clamp(minimum, x, maximum):
    return max(minimum, min(x, maximum))


@transaction.atomic
def choose_for_recalculation():
    now = timezone.now()
    r = (
        Ranking.objects.filter(
            needs_recalculation=True,
            cooldown_date__lt=now,
            recalc_in_progress=None,
        )
        .order_by("last_recalculation_date")
        .select_for_update()
        .first()
    )
    if r is None:
        return None
    cooldown_duration = clamp(
        timedelta(seconds=settings.RANKING_MIN_COOLDOWN),
        r.last_recalculation_duration * settings.RANKING_COOLDOWN_FACTOR,
        timedelta(seconds=settings.RANKING_MAX_COOLDOWN),
    )
    r.cooldown_date = now + cooldown_duration
    r.needs_recalculation = False
    recalc = RankingRecalc()
    recalc.save()
    r.recalc_in_progress = recalc
    r.save()
    return recalc


@transaction.atomic
def save_pages(ranking, pages_list):
    ranking.pages.all().delete()
    for nr, page_data in enumerate(pages_list, 1):
        page = RankingPage(ranking=ranking, nr=nr, data=page_data)
        page.save()


@transaction.atomic
def save_recalc_results(recalc, date_before, date_after, serialized, pages_list, cooldown_date):
    try:
        r = Ranking.objects.filter(recalc_in_progress=recalc).select_for_update().get()
    except Ranking.DoesNotExist:
        return
    if serialized is not None:
        assert pages_list is not None
        r.serialized_data = pickle.dumps(serialized)
        save_pages(r, pages_list)
    r.last_recalculation_date = date_before
    r.last_recalculation_duration = date_after - date_before
    old_recalc = r.recalc_in_progress
    r.recalc_in_progress = None
    if cooldown_date is not None:
        r.cooldown_date = cooldown_date
    r.save()
    old_recalc.delete()


def recalculate(recalc):
    date_before = timezone.now()
    try:
        r = Ranking.objects.filter(recalc_in_progress=recalc).select_related("contest").get()
    except Ranking.DoesNotExist:
        return
    ranking_controller = r.controller()
    try:
        serialized, pages_list = ranking_controller.build_ranking(r.key)
        cooldown_date = None
    except Exception as e:
        if getattr(settings, "MOCK_RANKINGSD", False):
            raise
        logger = logging.getLogger(__name__ + ".recalculation")
        logger.exception("An error occurred while recalculating ranking", e)
        cooldown_duration = timedelta(seconds=settings.RANKINGS_ERROR_COOLDOWN)
        cooldown_date = timezone.now() + cooldown_duration
        serialized, pages_list = (None, None)
    date_after = timezone.now()
    save_recalc_results(recalc, date_before, date_after, serialized, pages_list, cooldown_date)


class RankingMessage(PublicMessage):
    class Meta:
        verbose_name = _("ranking message")
        verbose_name_plural = _("ranking messages")


configurable_ranking_medal_schemes = EnumRegistry()
configurable_ranking_medal_schemes.register("none", _("No medals"))
configurable_ranking_medal_schemes.register("ioi", _("IOI"))
configurable_ranking_medal_schemes.register("og", _("Olympic podium"))

configurable_ranking_score_modes = EnumRegistry()
configurable_ranking_score_modes.register("best", _("Best submission"))
configurable_ranking_score_modes.register("last", _("Last submission"))
configurable_ranking_score_modes.register("last_revealed", _("Last submission with revealed-score fallback"))

configurable_ranking_source_types = EnumRegistry()
configurable_ranking_source_types.register("round", _("Round"))
configurable_ranking_source_types.register("sub_ranking", _("Sub-ranking"))

configurable_ranking_column_visibility = EnumRegistry()
configurable_ranking_column_visibility.register("never", _("Never visible"))
configurable_ranking_column_visibility.register("after_start", _("Visible after the round starts"))
configurable_ranking_column_visibility.register("after_end", _("Visible after the round ends"))
configurable_ranking_column_visibility.register("after_results", _("Visible after results are published"))


def configurable_ranking_partial_key(ranking_id):
    return f"cr{ranking_id}"


def bump_configurable_ranking_structure_version(contest_id):
    if contest_id is not None:
        _configurable_ranking_structure_versions[contest_id] += 1


def get_configurable_ranking_structure_version(contest_id):
    return _configurable_ranking_structure_versions.get(contest_id, 0)


def invalidate_configurable_ranking_cache(contest_id, ranking_id):
    partial_key = configurable_ranking_partial_key(ranking_id)
    predicate = Q()
    for permission in ("admin", "observer", "regular"):
        full_key = f"{permission}#{partial_key}"
        predicate |= Q(key=full_key) | Q(key__startswith=full_key + "|")
    Ranking.invalidate_queryset(Ranking.objects.filter(contest_id=contest_id).filter(predicate))


class ConfigurableRanking(models.Model):
    contest = models.ForeignKey(Contest, related_name="configurable_rankings", on_delete=models.CASCADE)
    name = models.CharField(max_length=255, verbose_name=_("name"))
    order = models.IntegerField(default=0, verbose_name=_("order"))
    medal_scheme = EnumField(
        configurable_ranking_medal_schemes,
        default="none",
        verbose_name=_("medal scheme"),
    )
    show_sum = models.BooleanField(default=True, verbose_name=_("show sum"))
    show_percentage = models.BooleanField(default=False, verbose_name=_("show percentage"))
    show_difference = models.BooleanField(default=False, verbose_name=_("show leader difference"))

    class Meta:
        ordering = ("contest", "order", "name", "id")
        verbose_name = _("configurable ranking")
        verbose_name_plural = _("configurable rankings")

    def __str__(self):
        return str(self.name)

    @property
    def partial_key(self):
        return configurable_ranking_partial_key(self.id)

    def clean(self):
        if self.contest_id and not self.contest.controller.supports_configurable_round_rankings():
            raise ValidationError(_("This contest type does not support configurable rankings."))

    def invalidate_cached_rankings(self):
        if self.id is not None:
            invalidate_configurable_ranking_cache(self.contest_id, self.id)


class ConfigurableRankingSettings(models.Model):
    contest = models.OneToOneField(Contest, on_delete=models.CASCADE)
    show_default_rankings = models.BooleanField(
        default=True,
        verbose_name=_("show default rankings"),
        help_text=_("Determines whether the built-in contest and round rankings remain visible alongside configurable ones."),
    )

    class Meta:
        verbose_name = _("configurable ranking settings")
        verbose_name_plural = _("configurable ranking settings")

    def clean(self):
        if self.contest_id and not self.contest.controller.supports_configurable_round_rankings():
            raise ValidationError(_("This contest type does not support configurable rankings."))


class ConfigurableRankingRound(models.Model):
    ranking = models.ForeignKey(
        ConfigurableRanking,
        related_name="round_configs",
        verbose_name=_("ranking"),
        on_delete=models.CASCADE,
    )
    source_type = EnumField(
        configurable_ranking_source_types,
        default="round",
        verbose_name=_("source type"),
    )
    round = models.ForeignKey(
        Round,
        verbose_name=_("round"),
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )
    sub_ranking = models.ForeignKey(
        "ConfigurableRanking",
        related_name="referencing_round_configs",
        verbose_name=_("sub-ranking"),
        blank=True,
        null=True,
        on_delete=models.CASCADE,
    )
    order = models.IntegerField(default=0, verbose_name=_("order"))
    coefficient = models.IntegerField(default=1, verbose_name=_("coefficient"))
    score_mode = EnumField(
        configurable_ranking_score_modes,
        default="best",
        verbose_name=_("score mode"),
    )
    all_time_coefficient = models.IntegerField(default=0, verbose_name=_("all-time coefficient"))
    all_time_score_mode = EnumField(
        configurable_ranking_score_modes,
        default="best",
        verbose_name=_("all-time score mode"),
    )
    column_visibility = EnumField(
        configurable_ranking_column_visibility,
        default="after_results",
        verbose_name=_("column visibility"),
    )
    ignore_submissions_after = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("ignore submissions after"),
    )

    class Meta:
        ordering = ("ranking", "order", "id")
        verbose_name = _("configurable ranking source")
        verbose_name_plural = _("configurable ranking sources")
        constraints = [
            models.UniqueConstraint(
                fields=("ranking", "round"),
                condition=Q(round__isnull=False),
                name="rankings_unique_round_source_per_ranking",
            ),
            models.UniqueConstraint(
                fields=("ranking", "sub_ranking"),
                condition=Q(sub_ranking__isnull=False),
                name="rankings_unique_subranking_source_per_ranking",
            ),
        ]

    def __str__(self):
        return self.source_name

    @property
    def source_name(self):
        if self.source_type == "sub_ranking" and self.sub_ranking_id:
            return str(self.sub_ranking.name)
        if self.round_id:
            return str(self.round.name)
        return str(_("Unknown source"))

    def _sub_ranking_descendants(self):
        seen = set()
        pending = [self.sub_ranking_id]
        while pending:
            ranking_id = pending.pop()
            if ranking_id in seen or ranking_id is None:
                continue
            seen.add(ranking_id)
            pending.extend(
                ConfigurableRankingRound.objects.filter(
                    ranking_id=ranking_id,
                    sub_ranking_id__isnull=False,
                ).values_list("sub_ranking_id", flat=True)
            )
        return seen

    def clean(self):
        if self.source_type == "round":
            if not self.round_id:
                raise ValidationError({"round": _("A round source must have a round selected.")})
            if self.ranking_id and self.ranking.contest_id != self.round.contest_id:
                raise ValidationError({"round": _("The selected round must belong to the same contest as the ranking.")})
            if self.sub_ranking_id:
                raise ValidationError({"sub_ranking": _("Round sources cannot also select a sub-ranking.")})
            return

        if self.source_type == "sub_ranking":
            if not self.sub_ranking_id:
                raise ValidationError({"sub_ranking": _("A sub-ranking source must have a sub-ranking selected.")})
            if self.round_id:
                raise ValidationError({"round": _("Sub-ranking sources should not select a round.")})
            if self.ranking_id and self.sub_ranking.contest_id != self.ranking.contest_id:
                raise ValidationError({"sub_ranking": _("The selected sub-ranking must belong to the same contest as the ranking.")})
            if self.ranking_id and self.sub_ranking_id == self.ranking_id:
                raise ValidationError({"sub_ranking": _("A ranking cannot include itself as a sub-ranking.")})
            if self.ranking_id and self.ranking_id in self._sub_ranking_descendants():
                raise ValidationError({"sub_ranking": _("This sub-ranking would create a cycle.")})
            return

        raise ValidationError(_("Unknown configurable ranking source type."))

    def invalidate_cached_rankings(self):
        self.ranking.invalidate_cached_rankings()


@receiver(post_save, sender=ConfigurableRanking)
def _invalidate_configurable_ranking_on_save(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.contest_id)
    instance.invalidate_cached_rankings()


@receiver(post_delete, sender=ConfigurableRanking)
def _invalidate_configurable_ranking_on_delete(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.contest_id)
    instance.invalidate_cached_rankings()


@receiver(post_save, sender=ConfigurableRankingRound)
def _invalidate_configurable_ranking_round_on_save(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.ranking.contest_id)
    instance.invalidate_cached_rankings()


@receiver(post_delete, sender=ConfigurableRankingRound)
def _invalidate_configurable_ranking_round_on_delete(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.ranking.contest_id)
    instance.invalidate_cached_rankings()


@receiver(post_save, sender=ConfigurableRankingSettings)
def _invalidate_configurable_ranking_settings_on_save(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.contest_id)


@receiver(post_delete, sender=ConfigurableRankingSettings)
def _invalidate_configurable_ranking_settings_on_delete(sender, instance, **kwargs):
    bump_configurable_ranking_structure_version(instance.contest_id)
