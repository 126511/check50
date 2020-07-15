import collections
from contextlib import contextmanager
import concurrent.futures as futures
import enum
import functools
import inspect
import importlib
import gettext
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import traceback

import attr
import lib50

from . import internal, __version__
from ._api import log, Failure, _copy, _log, _data


_check_names = []
_serial_dependency_state = None


@attr.s(slots=True)
class CheckResult:
    """Record returned by each check"""
    name = attr.ib()
    description = attr.ib()
    passed = attr.ib(default=None)
    log = attr.ib(default=attr.Factory(list))
    cause = attr.ib(default=None)
    data = attr.ib(default=attr.Factory(dict))
    dependency = attr.ib(default=None)

    @classmethod
    def from_check(cls, check, *args, **kwargs):
        """Create a check_result given a check function, automatically recording the name,
        the dependency, and the (translated) description.
        """
        return cls(name=check.__name__,
                   description=_(check.__doc__ if check.__doc__ else check.__name__.replace("_", " ")),
                   dependency=check._check_dependency.__name__ if check._check_dependency else None,
                   *args,
                   **kwargs)

    @classmethod
    def from_dict(cls, d):
        """Create a CheckResult given a dict. Dict must contain at least the fields in the CheckResult.
        Throws a KeyError if not."""
        return cls(**{field.name: d[field.name] for field in attr.fields(cls)})


@attr.s(slots=True)
class DiscoveryResult:
    checks = attr.ib()
    check_runner_mode = attr.ib(default=internal.check_runner_mode)


class Timeout(Failure):
    def __init__(self, seconds):
        super().__init__(rationale=_("check timed out after {} seconds").format(seconds))


@contextmanager
def _timeout(seconds):
    """Context manager that runs code block until timeout is reached.

    Example usage::

        try:
            with _timeout(10):
                do_stuff()
        except Timeout:
            print("do_stuff timed out")
    """

    def _handle_timeout(*args):
        raise Timeout(seconds)

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def check(dependency=None, timeout=60, max_log_lines=100):
    """Mark function as a check.

    :param dependency: the check that this check depends on
    :type dependency: function
    :param timeout: maximum number of seconds the check can run
    :type timeout: int
    :param max_log_lines: maximum number of lines that can appear in the log
    :type max_log_lines: int

    When a check depends on another, the former will only run if the latter passes.
    Additionally, the dependent check will inherit the filesystem of its dependency.
    This is particularly useful when writing e.g., a ``compiles`` check that compiles a
    student's program (and checks that it compiled successfully). Any checks that run the
    student's program will logically depend on this check, and since they inherit the
    resulting filesystem of the check, they will immidiately have access to the compiled
    program without needing to recompile.

    Example usage::

        @check50.check() # Mark 'exists' as a check
        def exists():
            \"""hello.c exists\"""
            check50.exists("hello.c")

        @check50.check(exists) # Mark 'compiles' as a check that depends on 'exists'
        def compiles():
            \"""hello.c compiles\"""
            check50.c.compile("hello.c")

        @check50.check(compiles)
        def prints_hello():
            \"""prints "Hello, world!\\\\n\"""
            # Since 'prints_hello', depends on 'compiles' it inherits the compiled binary
            check50.run("./hello").stdout("[Hh]ello, world!?\\n", "hello, world\\n").exit()

    """
    def decorator(check):

        # Modules are evaluated from the top of the file down, so _check_names will
        # contain the names of the checks in the order in which they are declared
        _check_names.append(check.__name__)
        check._check_dependency = dependency

        @functools.wraps(check)
        def wrapper(dependency_state):

            # Result template
            result = CheckResult.from_check(check)

            # Any shared (returned) state
            state = None

            # In serial operation take the dependency state from memory (_serial_dependency_state)
            if internal.check_runner_mode == internal.CheckRunnerMode.SERIAL:
                global _serial_dependency_state
                dependency_state = _serial_dependency_state
                _serial_dependency_state = None

            try:
                # Setup check environment, copying disk state from dependency
                internal.run_dir = internal.run_root_dir / check.__name__
                src_dir = internal.run_root_dir / (dependency.__name__ if dependency else "-")
                shutil.copytree(src_dir, internal.run_dir)
                os.chdir(internal.run_dir)

                # Run registered functions before/after running check and set timeout
                with internal.register, _timeout(seconds=timeout):
                    args = (dependency_state,) if inspect.getfullargspec(check).args else ()
                    state = check(*args)
            except Failure as e:
                result.passed = False
                result.cause = e.payload
            except BaseException as e:
                result.passed = None
                result.cause = {"rationale": _("check50 ran into an error while running checks!"),
                                "error": {
                                    "type": type(e).__name__,
                                    "value": str(e),
                                    "traceback": traceback.format_tb(e.__traceback__),
                                    "data" : e.payload if hasattr(e, "payload") else {}
                                }}
            else:
                result.passed = True
            finally:
                # In serial operation avoid serialization, store state in memory instead
                if internal.check_runner_mode == internal.CheckRunnerMode.SERIAL:
                    _serial_dependency_state = state
                    state = None

                result.log = _log if len(_log) <= max_log_lines else ["..."] + _log[-max_log_lines:]
                result.data = _data
                return result, state
        return wrapper
    return decorator


class CheckRunner:
    def __init__(self, checks_path, included_files):
        self.checks_path = checks_path
        self.included_files = included_files


    def run(self, targets=None):
        """
        Run checks concurrently.
        Returns a list of CheckResults ordered by declaration order of the checks in the imported module
        targets allows you to limit which checks run. If targets is false-y, all checks are run.
        """
        with futures.ProcessPoolExecutor(max_workers=1) as executor:
            discovery_job = executor.submit(discover_checks(self.checks_path))
            discovered = discovery_job.result()

            self.check_names = [check["name"] for check in discovered.checks]

            # Map each check name to its description
            self.check_descriptions = {check["name"]: check["description"] for check in discovered.checks}

            # Map each check to tuples containing the names of the checks that depend on it
            self.dependency_graph = collections.defaultdict(set)
            for check in discovered.checks:
                self.dependency_graph[check["dependency"]].add(check["name"])

            # If there are targetted checks, build a subgraph with just the relevant checks
            if targets:
                self.dependency_graph = self.build_subgraph(targets)

            if discovered.check_runner_mode == internal.CheckRunnerMode.SERIAL:
                return self.run_checks_serial(executor)

        return self.run_checks_parallel()


    def run_checks_serial(self, executor):
        # Grab all jobs to run from the dependency graph: jobs
        all_checks = set(job for job in self.dependency_graph.keys() if job)
        for dependents in self.dependency_graph.values():
            all_checks |= dependents

        # A serial queue of all checks to work through
        check_queue = []

        # A map from check_name to CheckResult
        results = {}

        # Enter the jobs by their definition order (self.check_names)
        for name in self.check_names:
            if name in all_checks:
                check_queue.append(name)
                results[name] = None

        # Run the checks in the queue
        while check_queue:

            # Get the first check from the queue
            check = check_queue[0]
            del check_queue[0]

            # Run the check
            result, state = executor.submit(run_check(check, self.checks_path)).result()
            results[check] = result

            # In case the check failed
            if not result.passed:

                # Record skipped results for each child (dependent)
                self._skip_children(check, results)

                # Remove each dependent from the queue
                for dependent in self.dependencies_of((check,)):
                    try:
                        check_queue.remove(dependent)
                    except ValueError:
                        pass

        return results.values()


    def run_checks_parallel(self):
        try:
            max_workers = int(os.environ.get("CHECK50_WORKERS"))
        except (ValueError, TypeError):
            max_workers = None

        # Ensure that dictionary is ordered by check declaration order (via self.check_names)
        # NOTE: Requires CPython 3.6. If we need to support older versions of Python, replace with OrderedDict.
        results = {name: None for name in self.check_names}

        with futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Start all checks that have no dependencies
            not_done = set(executor.submit(run_check(name, self.checks_path))
                           for name in self.dependency_graph[None])
            not_passed = []

            while not_done:
                done, not_done = futures.wait(not_done, return_when=futures.FIRST_COMPLETED)
                for future in done:
                    # Get result from completed check
                    result, state = future.result()
                    results[result.name] = result
                    if result.passed:
                        # Dispatch dependent checks
                        for child_name in self.dependency_graph[result.name]:
                            not_done.add(executor.submit(
                                run_check(child_name, self.checks_path, state)))
                    else:
                        not_passed.append(result.name)

        for name in not_passed:
            self._skip_children(name, results)

        # Don't include checks we don't have results for (i.e. in the case that targets != None) in the list.
        return list(filter(None, results.values()))


    def build_subgraph(self, targets):
        """
        Build minimal subgraph of self.dependency_graph that contains each check in targets
        """
        checks = self.dependencies_of(targets)
        subgraph = collections.defaultdict(set)
        for dep, children in self.dependency_graph.items():
            # If dep is not a dependency of any target,
            # none of its children will be either, may as well skip.
            if dep is not None and dep not in checks:
                continue
            for child in children:
                if child in checks:
                    subgraph[dep].add(child)
        return subgraph


    def dependencies_of(self, targets):
        """Get all unique dependencies of the targetted checks (targets)."""
        inverse_graph = self._create_inverse_dependency_graph()
        deps = set()
        for target in targets:
            if target not in inverse_graph:
                raise internal.Error(_("Unknown check: {}").format(e.args[0]))
            curr_check = target
            while curr_check is not None and curr_check not in deps:
                deps.add(curr_check)
                curr_check = inverse_graph[curr_check]
        return deps


    def _create_inverse_dependency_graph(self):
        """Build an inverse dependency map, from a check to its dependency."""
        inverse_dependency_graph = {}
        for check_name, dependents in self.dependency_graph.items():
            for dependent_name in dependents:
                inverse_dependency_graph[dependent_name] = check_name
        return inverse_dependency_graph


    def _skip_children(self, check_name, results):
        """
        Recursively skip the children of check_name (presumably because check_name
        did not pass).
        """
        for name in self.dependency_graph[check_name]:
            if results[name] is None:
                results[name] = CheckResult(name=name, description=self.check_descriptions[name],
                                            passed=None,
                                            dependency=check_name,
                                            cause={"rationale": _("can't check until a frown turns upside down")})
                self._skip_children(name, results)


    def __enter__(self):
        # Remember the student's directory
        internal.student_dir = Path.cwd()

        # Set up a temp dir for the checks
        self._working_area_manager = lib50.working_area(self.included_files, name='-')
        internal.run_root_dir = self._working_area_manager.__enter__().parent

        # Change current working dir to the temp dir
        self._cd_manager = lib50.cd(internal.run_root_dir)
        self._cd_manager.__enter__()

        return self


    def __exit__(self, type, value, tb):
        # Destroy the temporary directory for the checks
        self._working_area_manager.__exit__(type, value, tb)

        # cd back to the directory check50 was called from
        self._cd_manager.__exit__(type, value, tb)


class CheckJob:
    # All attributes shared between check50's main process and each checks' process
    # Required for "spawn": https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    CROSS_PROCESS_ATTRIBUTES = (
        "internal.check_dir",
        "internal.slug",
        "internal.check_runner_mode",
        "internal.student_dir",
        "internal.run_root_dir",
        "sys.excepthook",
        "__version__"
    )

    def __init__(self, checks_path):
        self.checks_path = checks_path
        self.checks_spec = importlib.util.spec_from_file_location("checks", self.checks_path)
        self._attribute_values = tuple(eval(name) for name in self.CROSS_PROCESS_ATTRIBUTES)

    @staticmethod
    def _set_attribute(name, value):
        """Get an attribute from a name in global scope and set its value."""
        parts = name.split(".")

        obj = sys.modules[__name__]
        for part in parts[:-1]:
            obj = getattr(obj, part)

        setattr(obj, parts[-1], value)

    def __call__(self):
        for name, val in zip(self.CROSS_PROCESS_ATTRIBUTES, self._attribute_values):
            self._set_attribute(name, val)


class discover_checks(CheckJob):
    def __call__(self):
        super().__call__()

        # # Clear check_names, import module, then save check_names. Not thread safe.
        # # Ideally, there'd be a better way to extract declaration order than @check mutating global state,
        # # but there are a lot of subtleties with using `inspect` or similar here
        _check_names.clear()
        check_module = importlib.util.module_from_spec(self.checks_spec)
        self.checks_spec.loader.exec_module(check_module)
        check_names = _check_names.copy()
        _check_names.clear()

        # Grab all checks from the module
        checks = inspect.getmembers(check_module, lambda f: hasattr(f, "_check_dependency"))
        checks = {name: check for name, check in checks}

        # Sort checks by their order in the module
        discovered_checks = []
        for name in check_names:
            check = checks[name]
            discovered_checks.append({
                "name": name,
                "dependency": None if check._check_dependency is None else check._check_dependency.__name__,
                "description": check.__doc__
            })

        return DiscoveryResult(checks=discovered_checks,
                               check_runner_mode=internal.check_runner_mode)


class run_check(CheckJob):
    """
    Check job that runs in a separate process.
    This is only a class to get around the fact that `pickle` can't serialize closures.
    This class is essentially a function that reimports the check module and runs the check.
    """
    def __init__(self, check_name, checks_path, state=None):
        super().__init__(checks_path)
        self.check_name = check_name
        self.state = state

    def __call__(self):
        super().__call__()
        mod = importlib.util.module_from_spec(self.checks_spec)
        self.checks_spec.loader.exec_module(mod)
        internal.check_running = True
        try:
            return getattr(mod, self.check_name)(self.state)
        finally:
            internal.check_running = False
