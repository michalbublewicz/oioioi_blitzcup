import xmlrpc.client
from operator import itemgetter  # pylint: disable=E0611

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from oioioi.base.admin import system_admin_menu_registry
from oioioi.base.permissions import enforce_condition, is_superuser

server = xmlrpc.client.ServerProxy(settings.SIOWORKERSD_URL, allow_none=True)


def get_info_about_workers():
    return server.get_workers()


def get_selected_names(post_data):
    return [key.removeprefix("work-") for key in post_data if key.startswith("work-")]


def del_worker(value):
    for i in value:
        server.forget_worker(i)


def get_workers_unavailable_warning():
    return _(
        "Couldn't connect to the worker daemon at %(url)s. "
        "Check whether sioworkersd is running and whether SIOWORKERSD_URL is correct."
    ) % {"url": settings.SIOWORKERSD_URL}


@enforce_condition(is_superuser)
def show_info_about_workers(request):
    readonly = False
    announce = None
    warning = None
    delete = False
    if request.method == "POST":
        if request.POST.get("delete"):
            readonly = True
            announce = _(
                """You are about to delete the selected workers.
                Please confirm"""
            )
            delete = True
        if request.POST.get("confirm"):
            selected = get_selected_names(request.POST)
            try:
                del_worker(selected)
            except (OSError, xmlrpc.client.Error):
                warning = get_workers_unavailable_warning()
            else:
                announce = _("Successfully deleted selected workers")

    try:
        workers_info = get_info_about_workers()
    except (OSError, xmlrpc.client.Error):
        workers_info = []
        warning = get_workers_unavailable_warning()

    def transform_dict(d):
        select = request.POST.get("work-" + d["name"])
        info = d["info"]
        result = {
            "name": d["name"],
            "ram": info.get("available_ram_mb", "<unknown>"),
            "concurrency": info["concurrency"],
            "can_run_cpu_exec": info["can_run_cpu_exec"],
            "select": select,
        }
        return result

    workers_info = list(map(transform_dict, workers_info))

    if workers_info and not any(map(itemgetter("can_run_cpu_exec"), workers_info)) and not settings.USE_UNSAFE_EXEC:
        warning = _("There aren't any workers allowed to run cpu-exec jobs.")

    context = {
        "workers": workers_info,
        "readonly": readonly,
        "announce": announce,
        "warning": warning,
        "delete": delete,
    }
    return render(request, "workers/list_workers.html", context)


@enforce_condition(is_superuser)
def get_load_json(request):
    try:
        data = get_info_about_workers()
    except (OSError, xmlrpc.client.Error):
        return JsonResponse({"capacity": 0, "load": 0, "unavailable": True})

    capacity = 0
    load = 0
    for i in data:
        concurrency = int(i["info"]["concurrency"])
        capacity += concurrency
        if bool(i["is_running_cpu_exec"]):
            load += concurrency
        else:
            load += len(i["tasks"])
    return JsonResponse({"capacity": capacity, "load": load})


system_admin_menu_registry.register(
    "workers_management_admin",
    _("Manage workers"),
    lambda request: reverse("show_workers"),
    order=100,
)
