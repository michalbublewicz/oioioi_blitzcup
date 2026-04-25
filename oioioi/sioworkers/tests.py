from importlib.metadata import entry_points

from django.test import TestCase

from oioioi import default_settings
from oioioi.sioworkers.jobs import run_sioworkers_job, run_sioworkers_jobs


class TestSioworkersBindings(TestCase):
    def test_sioworkers_bindings(self):
        env = run_sioworkers_job({"job_type": "ping", "ping": "e1"})
        self.assertEqual(env.get("pong"), "e1")
        envs = run_sioworkers_jobs(
            {
                "key1": {"job_type": "ping", "ping": "e1"},
                "key2": {"job_type": "ping", "ping": "e2"},
            }
        )
        self.assertEqual(envs["key1"].get("pong"), "e1")
        self.assertEqual(envs["key2"].get("pong"), "e2")
        self.assertEqual(len(envs), 2)


class TestCompilerEntryPoints(TestCase):
    def test_default_cxx23_compiler_is_registered_for_sioworkers(self):
        compiler_name = "g++14_2_cpp23_amd64"

        self.assertEqual(default_settings.DEFAULT_COMPILERS["C++"], compiler_name)

        compiler_entry_points = entry_points(group="sio.compilers")
        entry_point = next(
            (entry_point for entry_point in compiler_entry_points if entry_point.name == compiler_name),
            None,
        )

        self.assertIsNotNone(entry_point)
        self.assertEqual(
            entry_point.value,
            "oioioi.sioworkers.compilers:run_cpp_gcc14_2_cpp23_amd64",
        )

        from oioioi.sioworkers.compilers import run_cpp_gcc14_2_cpp23_amd64

        self.assertIs(entry_point.load(), run_cpp_gcc14_2_cpp23_amd64)
