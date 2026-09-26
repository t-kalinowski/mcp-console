#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["joblib"]
# ///

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import joblib_processes, requires
from support.suites import run_this_suite


@requires(joblib_processes())
@executions(DIRECT, SANDBOXED)
def test_runs_sklearn_parallel_search(binary: Path, execution: Execution) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    # Exercise n_jobs=-1 without multiplying CI's own transcript concurrency.
    environment["LOKY_MAX_CPU_COUNT"] = "2"
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.send(python="import sys")
        assert last_tool_text(client) == "[done]"
        client.send(
            # fmt: python
            python=code("""
                from sklearn.datasets import make_classification
                from sklearn.ensemble import HistGradientBoostingClassifier
                from sklearn.model_selection import GridSearchCV
                from sklearn.pipeline import make_pipeline
                from sklearn.preprocessing import StandardScaler

                from joblib import effective_n_jobs
                import numpy as np

                assert effective_n_jobs(-1) == 2
                # Exceed joblib's default 1 MiB automatic memmapping threshold.
                X, y = make_classification(n_samples=2000, n_features=80, random_state=42)
                assert X.nbytes > 1024**2
                model = make_pipeline(
                    StandardScaler(),
                    HistGradientBoostingClassifier(max_iter=4, max_leaf_nodes=7, random_state=42),
                )
                parameters = {"histgradientboostingclassifier__l2_regularization": [0, 1]}
                parallel = GridSearchCV(model, parameters, cv=2, n_jobs=-1, error_score="raise").fit(
                    X, y
                )
                serial = GridSearchCV(model, parameters, cv=2, n_jobs=1, error_score="raise").fit(X, y)
                np.testing.assert_array_equal(
                    parallel.cv_results_["mean_test_score"],
                    serial.cv_results_["mean_test_score"],
                )
                np.testing.assert_array_equal(parallel.predict(X), serial.predict(X))
                print("parallel search matches serial")
                """)
        )
        assert last_tool_text(client) == (
            "[resolved PyPI distribution 'scikit-learn' for Python import 'sklearn']\n"
            "parallel search matches serial\n"
        ), client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                from sklearn.ensemble import RandomForestClassifier
                from sklearn.inspection import permutation_importance
                from sklearn.model_selection import cross_val_score

                forest = RandomForestClassifier(
                    n_estimators=8, max_depth=4, random_state=42, n_jobs=-1
                ).fit(X, y)
                parallel_scores = cross_val_score(forest, X, y, cv=2, n_jobs=-1, error_score="raise")
                serial_scores = cross_val_score(forest, X, y, cv=2, n_jobs=1, error_score="raise")
                np.testing.assert_array_equal(parallel_scores, serial_scores)
                parallel_importance = permutation_importance(
                    forest, X[:100], y[:100], n_repeats=2, random_state=42, n_jobs=-1
                )
                serial_importance = permutation_importance(
                    forest, X[:100], y[:100], n_repeats=2, random_state=42, n_jobs=1
                )
                np.testing.assert_array_equal(
                    parallel_importance.importances, serial_importance.importances
                )
                print("parallel cross-validation and importance match serial")
                """)
        )
        assert last_tool_text(client) == (
            "parallel cross-validation and importance match serial\n"
        ), client.transcript[-1]
        # Direct workers do not own descendant retirement. Close the reusable
        # pool explicitly so that this execution mode leaves no idle children.
        client.send(
            # fmt: python
            python=code("""
                from joblib.externals.loky import get_reusable_executor

                get_reusable_executor().shutdown(wait=True)
                """)
        )
        assert last_tool_text(client) == "[done]"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend(binary: Path, execution: Execution) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(
        python=python,
        requirements={"python": ["joblib"]},
    )
    output = last_result_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(python="import sys")
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_spawn_process_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import multiprocessing.spawn
        import sys

        initial_executable = sys.executable
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        import multiprocessing
        import sys

        import joblib

        context = multiprocessing.get_context("spawn")
        with context.Pool(1) as pool:
            child_executable = pool.apply(
                eval,
                ("__import__('sys').executable",),
            )

        (
            initial_executable != sys.executable,
            child_executable == sys.executable,
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "(True, True)\n", repr(output)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
