from django import template
from django.urls import reverse

register = template.Library()


@register.inclusion_tag("blitz/_live_updates.html", takes_context=True)
def render_blitz_live_updates(context):
    request = context.get("request")
    contest = getattr(request, "contest", None) if request is not None else None
    controller = getattr(contest, "controller", None) if contest is not None else None

    if controller is None or controller.__class__.__module__ != "oioioi.blitz.controllers" or controller.__class__.__name__ != "BlitzContestController":
        return {}

    return {
        "blitz_status_endpoint": reverse("blitz_status", kwargs={"contest_id": contest.id}),
        "blitz_initial_payload": controller.serialize_live_status(request),
    }
