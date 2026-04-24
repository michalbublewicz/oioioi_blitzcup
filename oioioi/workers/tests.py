from django.urls import reverse

from oioioi.base.tests import TestCase
from oioioi.workers import views


class TestServer:
    def get_workers(self):
        return [
            {
                "name": "Komp4",
                "info": {"concurrency": 2, "can_run_cpu_exec": True},
                "tasks": [],
                "is_running_cpu_exec": False,
            }
        ]


class UnavailableServer:
    def get_workers(self):
        raise ConnectionRefusedError(111, "Connection refused")

    def forget_worker(self, name):
        raise ConnectionRefusedError(111, "Connection refused")


class TestWorkersInfo(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        # monkeypatch test server instead of XMLRPC
        views.server = TestServer()

    def test_admin_can_see(self):
        self.assertTrue(self.client.login(username="test_admin"))
        url = reverse("show_workers")
        response = self.client.get(url)
        self.assertContains(response, "Komp4")

    def test_mundane_user_cant_see(self):
        self.assertTrue(self.client.login(username="test_user"))
        url = reverse("show_workers")
        response = self.client.get(url)
        self.assertNotContains(response, "Komp4", status_code=403)


class TestWorkersUnavailable(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        views.server = UnavailableServer()
        self.assertTrue(self.client.login(username="test_admin"))

    def test_admin_gets_warning_instead_of_500(self):
        response = self.client.get(reverse("show_workers"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Couldn't connect to the worker daemon", response.context["warning"])

    def test_load_json_returns_empty_load_when_workers_are_unavailable(self):
        response = self.client.get(reverse("get_load_json"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"capacity": 0, "load": 0, "unavailable": True})
