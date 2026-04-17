import os
import sys


def _sanitize_sys_path():
    """Remove direct package-directory entries that shadow stdlib modules.

    Editable installs may place the inner ``.../oioioi`` package directory on
    ``sys.path``. In that shape, top-level imports such as ``import statistics``
    resolve to ``oioioi/statistics`` instead of the standard library module.
    Keep the project import that is already loaded, but drop those problematic
    path entries before Django imports its database backends.
    """

    def _is_inner_package_dir(path):
        normalized = os.path.normpath(path or "")
        return os.path.basename(normalized) == "oioioi" and os.path.isfile(os.path.join(normalized, "__init__.py"))

    sys.path[:] = [path for path in sys.path if not _is_inner_package_dir(path)]

    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath:
        separator = os.pathsep
        cleaned = [path for path in pythonpath.split(separator) if not _is_inner_package_dir(path)]
        os.environ["PYTHONPATH"] = separator.join(cleaned)


def init_env(settings_dir):
    _sanitize_sys_path()
    sys.path.insert(0, settings_dir)
    separator = os.pathsep
    if os.environ.get("PYTHONPATH"):
        os.environ["PYTHONPATH"] = settings_dir + separator + os.environ["PYTHONPATH"]
    else:
        os.environ["PYTHONPATH"] = settings_dir
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
