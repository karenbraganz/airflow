#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import argparse
import contextlib
import json
import os
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock
from unittest.mock import MagicMock

import pendulum
import pytest
import time_machine
from sqlalchemy import select

from airflow import settings
from airflow.api_connexion.schemas.dag_schema import DAGSchema, dag_schema
from airflow.cli import cli_parser
from airflow.cli.commands.remote_commands import dag_command
from airflow.decorators import task
from airflow.exceptions import AirflowException
from airflow.models import DagBag, DagModel, DagRun
from airflow.models.baseoperator import BaseOperator
from airflow.models.dag import _run_inline_trigger
from airflow.models.serialized_dag import SerializedDagModel
from airflow.providers.standard.triggers.temporal import DateTimeTrigger, TimeDeltaTrigger
from airflow.triggers.base import TriggerEvent
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import DagRunState
from airflow.utils.types import DagRunType

from tests.models import TEST_DAGS_FOLDER
from tests_common.test_utils.config import conf_vars
from tests_common.test_utils.db import clear_db_dags, clear_db_runs, parse_and_sync_to_db

DEFAULT_DATE = timezone.make_aware(datetime(2015, 1, 1), timezone=timezone.utc)
if pendulum.__version__.startswith("3"):
    DEFAULT_DATE_REPR = DEFAULT_DATE.isoformat(sep=" ")
else:
    DEFAULT_DATE_REPR = DEFAULT_DATE.isoformat()

# TODO: Check if tests needs side effects - locally there's missing DAG

pytestmark = pytest.mark.db_test


class TestCliDags:
    parser: argparse.ArgumentParser

    @classmethod
    def setup_class(cls):
        parse_and_sync_to_db(os.devnull, include_examples=True)
        cls.parser = cli_parser.get_parser()

    @classmethod
    def teardown_class(cls) -> None:
        clear_db_runs()
        clear_db_dags()

    def setup_method(self):
        clear_db_runs()  # clean-up all dag run before start each test

    def test_show_dag_dependencies_print(self):
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_dependencies_show(self.parser.parse_args(["dags", "show-dependencies"]))
        out = temp_stdout.getvalue()
        assert "digraph" in out
        assert "graph [rankdir=LR]" in out

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.render_dag_dependencies")
    def test_show_dag_dependencies_save(self, mock_render_dag_dependencies):
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_dependencies_show(
                self.parser.parse_args(["dags", "show-dependencies", "--save", "output.png"])
            )
        out = temp_stdout.getvalue()
        mock_render_dag_dependencies.return_value.render.assert_called_once_with(
            cleanup=True, filename="output", format="png"
        )
        assert "File output.png saved" in out

    def test_show_dag_print(self):
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_show(self.parser.parse_args(["dags", "show", "example_bash_operator"]))
        out = temp_stdout.getvalue()
        assert "label=example_bash_operator" in out
        assert "graph [label=example_bash_operator labelloc=t rankdir=LR]" in out
        assert "runme_2 -> run_after_loop" in out

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.render_dag")
    def test_show_dag_save(self, mock_render_dag):
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_show(
                self.parser.parse_args(["dags", "show", "example_bash_operator", "--save", "awesome.png"])
            )
        out = temp_stdout.getvalue()
        mock_render_dag.return_value.render.assert_called_once_with(
            cleanup=True, filename="awesome", format="png"
        )
        assert "File awesome.png saved" in out

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.subprocess.Popen")
    @mock.patch("airflow.cli.commands.remote_commands.dag_command.render_dag")
    def test_show_dag_imgcat(self, mock_render_dag, mock_popen):
        mock_render_dag.return_value.pipe.return_value = b"DOT_DATA"
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"OUT", b"ERR")
        mock_popen.return_value.__enter__.return_value = mock_proc
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_show(
                self.parser.parse_args(["dags", "show", "example_bash_operator", "--imgcat"])
            )
        out = temp_stdout.getvalue()
        mock_render_dag.return_value.pipe.assert_called_once_with(format="png")
        mock_proc.communicate.assert_called_once_with(b"DOT_DATA")
        assert "OUT" in out
        assert "ERR" in out

    def test_next_execution(self, tmp_path):
        dag_test_list = [
            ("future_schedule_daily", "timedelta(days=5)", "'0 0 * * *'", "True"),
            ("future_schedule_every_4_hours", "timedelta(days=5)", "timedelta(hours=4)", "True"),
            ("future_schedule_once", "timedelta(days=5)", "'@once'", "True"),
            ("future_schedule_none", "timedelta(days=5)", "None", "True"),
            ("past_schedule_once", "timedelta(days=-5)", "'@once'", "True"),
            ("past_schedule_daily", "timedelta(days=-5)", "'0 0 * * *'", "True"),
            ("past_schedule_daily_catchup_false", "timedelta(days=-5)", "'0 0 * * *'", "False"),
        ]

        for f in dag_test_list:
            file_content = os.linesep.join(
                [
                    "from airflow import DAG",
                    "from airflow.providers.standard.operators.empty import EmptyOperator",
                    "from datetime import timedelta; from pendulum import today",
                    f"dag = DAG('{f[0]}', start_date=today() + {f[1]}, schedule={f[2]}, catchup={f[3]})",
                    "task = EmptyOperator(task_id='empty_task',dag=dag)",
                ]
            )
            dag_file = tmp_path / f"{f[0]}.py"
            dag_file.write_text(file_content)

        with time_machine.travel(DEFAULT_DATE):
            clear_db_dags()
            parse_and_sync_to_db(tmp_path, include_examples=False)

        default_run = DEFAULT_DATE
        future_run = default_run + timedelta(days=5)
        past_run = default_run + timedelta(days=-5)

        expected_output = {
            "future_schedule_daily": (
                future_run.isoformat(),
                future_run.isoformat() + os.linesep + (future_run + timedelta(days=1)).isoformat(),
            ),
            "future_schedule_every_4_hours": (
                future_run.isoformat(),
                future_run.isoformat() + os.linesep + (future_run + timedelta(hours=4)).isoformat(),
            ),
            "future_schedule_once": (future_run.isoformat(), future_run.isoformat() + os.linesep + "None"),
            "future_schedule_none": ("None", "None"),
            "past_schedule_once": (past_run.isoformat(), "None"),
            "past_schedule_daily": (
                past_run.isoformat(),
                past_run.isoformat() + os.linesep + (past_run + timedelta(days=1)).isoformat(),
            ),
            "past_schedule_daily_catchup_false": (
                (default_run - timedelta(days=1)).isoformat(),
                (default_run - timedelta(days=1)).isoformat() + os.linesep + default_run.isoformat(),
            ),
        }

        for dag_id in expected_output:
            # Test num-executions = 1 (default)
            args = self.parser.parse_args(["dags", "next-execution", dag_id, "-S", str(tmp_path)])
            with contextlib.redirect_stdout(StringIO()) as temp_stdout:
                dag_command.dag_next_execution(args)
                out = temp_stdout.getvalue()
            assert expected_output[dag_id][0] in out

            # Test num-executions = 2
            args = self.parser.parse_args(
                ["dags", "next-execution", dag_id, "--num-executions", "2", "-S", str(tmp_path)]
            )
            with contextlib.redirect_stdout(StringIO()) as temp_stdout:
                dag_command.dag_next_execution(args)
                out = temp_stdout.getvalue()
            assert expected_output[dag_id][1] in out

        # Rebuild Test DB for other tests
        clear_db_dags()
        parse_and_sync_to_db(os.devnull, include_examples=True)

    @conf_vars({("core", "load_examples"): "true"})
    def test_cli_report(self):
        args = self.parser.parse_args(["dags", "report", "--output", "json"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_report(args)
            out = temp_stdout.getvalue()

        assert "airflow/example_dags/example_complex.py" in out
        assert "example_complex" in out

    @conf_vars({("core", "load_examples"): "true"})
    def test_cli_get_dag_details(self):
        args = self.parser.parse_args(["dags", "details", "example_complex", "--output", "yaml"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_details(args)
            out = temp_stdout.getvalue()

        dag_detail_fields = DAGSchema().fields.keys()

        # Check if DAG Details field are present
        for field in dag_detail_fields:
            assert field in out

        # Check if identifying values are present
        dag_details_values = ["airflow", "airflow/example_dags/example_complex.py", "16", "example_complex"]

        for value in dag_details_values:
            assert value in out

    @conf_vars({("core", "load_examples"): "true"})
    def test_cli_list_dags(self):
        args = self.parser.parse_args(["dags", "list", "--output", "json"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_list_dags(args)
            out = temp_stdout.getvalue()
            dag_list = json.loads(out)
        for key in ["dag_id", "fileloc", "owners", "is_paused"]:
            assert key in dag_list[0]
        assert any("airflow/example_dags/example_complex.py" in d["fileloc"] for d in dag_list)

    @conf_vars({("core", "load_examples"): "true"})
    def test_cli_list_dags_custom_cols(self):
        args = self.parser.parse_args(
            ["dags", "list", "--output", "json", "--columns", "dag_id,last_parsed_time"]
        )
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_list_dags(args)
            out = temp_stdout.getvalue()
            dag_list = json.loads(out)
        for key in ["dag_id", "last_parsed_time"]:
            assert key in dag_list[0]
        for key in ["fileloc", "owners", "is_paused"]:
            assert key not in dag_list[0]

    @conf_vars({("core", "load_examples"): "true"})
    def test_cli_list_dags_invalid_cols(self):
        args = self.parser.parse_args(["dags", "list", "--output", "json", "--columns", "dag_id,invalid_col"])
        with contextlib.redirect_stderr(StringIO()) as temp_stderr:
            dag_command.dag_list_dags(args)
            out = temp_stderr.getvalue()
        assert "Ignoring the following invalid columns: ['invalid_col']" in out

    @conf_vars({("core", "load_examples"): "false"})
    def test_cli_list_dags_prints_import_errors(self):
        dag_path = os.path.join(TEST_DAGS_FOLDER, "test_invalid_cron.py")
        args = self.parser.parse_args(["dags", "list", "--output", "yaml", "--subdir", dag_path])
        with contextlib.redirect_stderr(StringIO()) as temp_stderr:
            dag_command.dag_list_dags(args)
            out = temp_stderr.getvalue()
        assert "Failed to load all files." in out

    @conf_vars({("core", "load_examples"): "true"})
    @mock.patch("airflow.models.DagModel.get_dagmodel")
    def test_list_dags_none_get_dagmodel(self, mock_get_dagmodel):
        mock_get_dagmodel.return_value = None
        args = self.parser.parse_args(["dags", "list", "--output", "json"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_list_dags(args)
            out = temp_stdout.getvalue()
            dag_list = json.loads(out)
        for key in ["dag_id", "fileloc", "owners", "is_paused"]:
            assert key in dag_list[0]
        assert any("airflow/example_dags/example_complex.py" in d["fileloc"] for d in dag_list)

    @conf_vars({("core", "load_examples"): "true"})
    def test_dagbag_dag_col(self):
        valid_cols = [c for c in dag_schema.fields]
        dagbag = DagBag(include_examples=True)
        dag_details = dag_command._get_dagbag_dag_details(dagbag.get_dag("tutorial_dag"))
        assert list(dag_details.keys()) == valid_cols

    @conf_vars({("core", "load_examples"): "false"})
    def test_cli_list_import_errors(self):
        dag_path = os.path.join(TEST_DAGS_FOLDER, "test_invalid_cron.py")
        args = self.parser.parse_args(["dags", "list", "--output", "yaml", "--subdir", dag_path])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            with pytest.raises(SystemExit) as err_ctx:
                dag_command.dag_list_import_errors(args)
            out = temp_stdout.getvalue()
        assert "[0 100 * * *] is not acceptable, out of range" in out
        assert dag_path in out
        assert err_ctx.value.code == 1

    def test_cli_list_dag_runs(self):
        dag_command.dag_trigger(
            self.parser.parse_args(
                [
                    "dags",
                    "trigger",
                    "example_bash_operator",
                ]
            )
        )
        args = self.parser.parse_args(
            [
                "dags",
                "list-runs",
                "example_bash_operator",
                "--no-backfill",
                "--start-date",
                DEFAULT_DATE.isoformat(),
                "--end-date",
                timezone.make_aware(datetime.max).isoformat(),
            ]
        )
        dag_command.dag_list_dag_runs(args)

    def test_cli_list_jobs_with_args(self):
        args = self.parser.parse_args(
            [
                "dags",
                "list-jobs",
                "--dag-id",
                "example_bash_operator",
                "--state",
                "success",
                "--limit",
                "100",
                "--output",
                "json",
            ]
        )
        dag_command.dag_list_jobs(args)

    def test_pause(self):
        args = self.parser.parse_args(["dags", "pause", "example_bash_operator"])
        dag_command.dag_pause(args)
        assert DagModel.get_dagmodel("example_bash_operator").is_paused
        dag_command.dag_unpause(args)
        assert not DagModel.get_dagmodel("example_bash_operator").is_paused

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.ask_yesno")
    def test_pause_regex(self, mock_yesno):
        args = self.parser.parse_args(["dags", "pause", "^example_.*$", "--treat-dag-id-as-regex"])
        dag_command.dag_pause(args)
        mock_yesno.assert_called_once()
        assert DagModel.get_dagmodel("example_bash_decorator").is_paused
        assert DagModel.get_dagmodel("example_kubernetes_executor").is_paused
        assert DagModel.get_dagmodel("example_xcom_args").is_paused

        args = self.parser.parse_args(["dags", "unpause", "^example_.*$", "--treat-dag-id-as-regex"])
        dag_command.dag_unpause(args)
        assert not DagModel.get_dagmodel("example_bash_decorator").is_paused
        assert not DagModel.get_dagmodel("example_kubernetes_executor").is_paused
        assert not DagModel.get_dagmodel("example_xcom_args").is_paused

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.ask_yesno")
    def test_pause_regex_operation_cancelled(self, ask_yesno, capsys):
        args = self.parser.parse_args(["dags", "pause", "example_bash_operator", "--treat-dag-id-as-regex"])
        ask_yesno.return_value = False
        dag_command.dag_pause(args)
        stdout = capsys.readouterr().out
        assert "Operation cancelled by user" in stdout

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.ask_yesno")
    def test_pause_regex_yes(self, mock_yesno):
        args = self.parser.parse_args(["dags", "pause", ".*", "--treat-dag-id-as-regex", "--yes"])
        dag_command.dag_pause(args)
        mock_yesno.assert_not_called()
        dag_command.dag_unpause(args)

    def test_pause_non_existing_dag_do_not_error(self):
        args = self.parser.parse_args(["dags", "pause", "non_existing_dag"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_pause(args)
            out = temp_stdout.getvalue().strip().splitlines()[-1]
        assert out == "No unpaused DAGs were found"

    def test_unpause_non_existing_dag_do_not_error(self):
        args = self.parser.parse_args(["dags", "unpause", "non_existing_dag"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_unpause(args)
            out = temp_stdout.getvalue().strip().splitlines()[-1]
        assert out == "No paused DAGs were found"

    def test_unpause_already_unpaused_dag_do_not_error(self):
        args = self.parser.parse_args(["dags", "unpause", "example_bash_operator", "--yes"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_unpause(args)
            out = temp_stdout.getvalue().strip().splitlines()[-1]
        assert out == "No paused DAGs were found"

    def test_pausing_already_paused_dag_do_not_error(self):
        args = self.parser.parse_args(["dags", "pause", "example_bash_operator", "--yes"])
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_pause(args)
            dag_command.dag_pause(args)
            out = temp_stdout.getvalue().strip().splitlines()[-1]
        assert out == "No unpaused DAGs were found"

    def test_trigger_dag(self):
        dag_command.dag_trigger(
            self.parser.parse_args(
                [
                    "dags",
                    "trigger",
                    "example_bash_operator",
                    "--run-id=test_trigger_dag",
                    '--conf={"foo": "bar"}',
                ],
            ),
        )
        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.run_id == "test_trigger_dag").one()

        assert dagrun, "DagRun not created"
        assert dagrun.run_type == DagRunType.MANUAL
        assert dagrun.conf == {"foo": "bar"}

        # logical_date is None as it's not provided
        assert dagrun.logical_date is None

        # data_interval is None as logical_date is None
        assert dagrun.data_interval_start is None
        assert dagrun.data_interval_end is None

    def test_trigger_dag_with_microseconds(self):
        dag_command.dag_trigger(
            self.parser.parse_args(
                [
                    "dags",
                    "trigger",
                    "example_bash_operator",
                    "--run-id=test_trigger_dag_with_micro",
                    "--exec-date=2021-06-04T09:00:00.000001+08:00",
                    "--no-replace-microseconds",
                ],
            )
        )

        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.run_id == "test_trigger_dag_with_micro").one()

        assert dagrun, "DagRun not created"
        assert dagrun.run_type == DagRunType.MANUAL
        assert dagrun.logical_date.isoformat(timespec="microseconds") == "2021-06-04T01:00:00.000001+00:00"

    def test_trigger_dag_invalid_conf(self):
        with pytest.raises(ValueError):
            dag_command.dag_trigger(
                self.parser.parse_args(
                    [
                        "dags",
                        "trigger",
                        "example_bash_operator",
                        "--run-id",
                        "trigger_dag_xxx",
                        "--conf",
                        "NOT JSON",
                    ]
                ),
            )

    def test_trigger_dag_output_as_json(self):
        args = self.parser.parse_args(
            [
                "dags",
                "trigger",
                "example_bash_operator",
                "--run-id",
                "trigger_dag_xxx",
                "--conf",
                '{"conf1": "val1", "conf2": "val2"}',
                "--output=json",
            ]
        )
        with contextlib.redirect_stdout(StringIO()) as temp_stdout:
            dag_command.dag_trigger(args)
            # get the last line from the logs ignoring all logging lines
            out = temp_stdout.getvalue().strip().splitlines()[-1]
        parsed_out = json.loads(out)

        assert len(parsed_out) == 1
        assert parsed_out[0]["dag_id"] == "example_bash_operator"
        assert parsed_out[0]["dag_run_id"] == "trigger_dag_xxx"
        assert parsed_out[0]["conf"] == {"conf1": "val1", "conf2": "val2"}

    def test_delete_dag(self):
        DM = DagModel
        key = "my_dag_id"
        session = settings.Session()
        session.add(DM(dag_id=key))
        session.commit()
        dag_command.dag_delete(self.parser.parse_args(["dags", "delete", key, "--yes"]))
        assert session.query(DM).filter_by(dag_id=key).count() == 0
        with pytest.raises(AirflowException):
            dag_command.dag_delete(
                self.parser.parse_args(["dags", "delete", "does_not_exist_dag", "--yes"]),
            )

    def test_delete_dag_existing_file(self, tmp_path):
        # Test to check that the DAG should be deleted even if
        # the file containing it is not deleted
        path = tmp_path / "testfile"
        DM = DagModel
        key = "my_dag_id"
        session = settings.Session()
        session.add(DM(dag_id=key, fileloc=os.fspath(path)))
        session.commit()
        dag_command.dag_delete(self.parser.parse_args(["dags", "delete", key, "--yes"]))
        assert session.query(DM).filter_by(dag_id=key).count() == 0

    def test_cli_list_jobs(self):
        args = self.parser.parse_args(["dags", "list-jobs"])
        dag_command.dag_list_jobs(args)

    def test_dag_state(self):
        assert (
            dag_command.dag_state(
                self.parser.parse_args(["dags", "state", "example_bash_operator", DEFAULT_DATE.isoformat()])
            )
            is None
        )

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.get_dag")
    def test_dag_test(self, mock_get_dag):
        cli_args = self.parser.parse_args(["dags", "test", "example_bash_operator", DEFAULT_DATE.isoformat()])
        dag_command.dag_test(cli_args)

        mock_get_dag.assert_has_calls(
            [
                mock.call(subdir=cli_args.subdir, dag_id="example_bash_operator"),
                mock.call().test(
                    logical_date=timezone.parse(DEFAULT_DATE.isoformat()),
                    run_conf=None,
                    use_executor=False,
                    session=mock.ANY,
                    mark_success_pattern=None,
                ),
            ]
        )

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.get_dag")
    def test_dag_test_fail_raise_error(self, mock_get_dag):
        logical_date_str = DEFAULT_DATE.isoformat()
        mock_get_dag.return_value.test.return_value = DagRun(
            dag_id="example_bash_operator", logical_date=DEFAULT_DATE, state=DagRunState.FAILED
        )
        cli_args = self.parser.parse_args(["dags", "test", "example_bash_operator", logical_date_str])
        with pytest.raises(SystemExit, match=r"DagRun failed"):
            dag_command.dag_test(cli_args)

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.get_dag")
    @mock.patch("airflow.utils.timezone.utcnow")
    def test_dag_test_no_logical_date(self, mock_utcnow, mock_get_dag):
        now = pendulum.now()
        mock_utcnow.return_value = now
        cli_args = self.parser.parse_args(["dags", "test", "example_bash_operator"])

        assert cli_args.logical_date is None

        dag_command.dag_test(cli_args)

        mock_get_dag.assert_has_calls(
            [
                mock.call(subdir=cli_args.subdir, dag_id="example_bash_operator"),
                mock.call().test(
                    logical_date=mock.ANY,
                    run_conf=None,
                    use_executor=False,
                    session=mock.ANY,
                    mark_success_pattern=None,
                ),
            ]
        )

    @mock.patch("airflow.cli.commands.remote_commands.dag_command.get_dag")
    def test_dag_test_conf(self, mock_get_dag):
        cli_args = self.parser.parse_args(
            [
                "dags",
                "test",
                "example_bash_operator",
                DEFAULT_DATE.isoformat(),
                "-c",
                '{"dag_run_conf_param": "param_value"}',
            ]
        )
        dag_command.dag_test(cli_args)

        mock_get_dag.assert_has_calls(
            [
                mock.call(subdir=cli_args.subdir, dag_id="example_bash_operator"),
                mock.call().test(
                    logical_date=timezone.parse(DEFAULT_DATE.isoformat()),
                    run_conf={"dag_run_conf_param": "param_value"},
                    use_executor=False,
                    session=mock.ANY,
                    mark_success_pattern=None,
                ),
            ]
        )

    @mock.patch(
        "airflow.cli.commands.remote_commands.dag_command.render_dag", return_value=MagicMock(source="SOURCE")
    )
    @mock.patch("airflow.cli.commands.remote_commands.dag_command.get_dag")
    def test_dag_test_show_dag(self, mock_get_dag, mock_render_dag):
        mock_get_dag.return_value.test.return_value.run_id = "__test_dag_test_show_dag_fake_dag_run_run_id__"

        cli_args = self.parser.parse_args(
            ["dags", "test", "example_bash_operator", DEFAULT_DATE.isoformat(), "--show-dagrun"]
        )
        with contextlib.redirect_stdout(StringIO()) as stdout:
            dag_command.dag_test(cli_args)

        output = stdout.getvalue()

        mock_get_dag.assert_has_calls(
            [
                mock.call(subdir=cli_args.subdir, dag_id="example_bash_operator"),
                mock.call().test(
                    logical_date=timezone.parse(DEFAULT_DATE.isoformat()),
                    run_conf=None,
                    use_executor=False,
                    session=mock.ANY,
                    mark_success_pattern=None,
                ),
            ]
        )
        mock_render_dag.assert_has_calls([mock.call(mock_get_dag.return_value, tis=[])])
        assert "SOURCE" in output

    @mock.patch("airflow.models.dag._get_or_create_dagrun")
    def test_dag_test_with_custom_timetable(self, mock__get_or_create_dagrun):
        """
        when calling `dags test` on dag with custom timetable, the DagRun object should be created with
         data_intervals.
        """
        cli_args = self.parser.parse_args(
            ["dags", "test", "example_workday_timetable", DEFAULT_DATE.isoformat()]
        )
        from airflow.example_dags.plugins.workday import AfterWorkdayTimetable

        with mock.patch.object(AfterWorkdayTimetable, "get_next_workday", side_effect=[DEFAULT_DATE]):
            dag_command.dag_test(cli_args)
        assert "data_interval" in mock__get_or_create_dagrun.call_args.kwargs

    @mock.patch("airflow.models.dag._get_or_create_dagrun")
    def test_dag_with_parsing_context(self, mock__get_or_create_dagrun):
        """
        airflow parsing context should be set when calling `dags test`.
        """
        cli_args = self.parser.parse_args(
            ["dags", "test", "test_dag_parsing_context", DEFAULT_DATE.isoformat()]
        )
        dag_command.dag_test(cli_args)

        # if dag_parsing_context is not set, this DAG will only have 1 task
        assert len(mock__get_or_create_dagrun.call_args[1]["dag"].task_ids) == 2

    def test_dag_test_run_inline_trigger(self, dag_maker):
        now = timezone.utcnow()
        trigger = DateTimeTrigger(moment=now)
        e = _run_inline_trigger(trigger)
        assert isinstance(e, TriggerEvent)
        assert e.payload == now

    def test_dag_test_no_triggerer_running(self, dag_maker):
        with mock.patch("airflow.models.dag._run_inline_trigger", wraps=_run_inline_trigger) as mock_run:
            with dag_maker() as dag:

                @task
                def one():
                    return 1

                @task
                def two(val):
                    return val + 1

                trigger = TimeDeltaTrigger(timedelta(seconds=0))

                class MyOp(BaseOperator):
                    template_fields = ("tfield",)

                    def __init__(self, tfield, **kwargs):
                        self.tfield = tfield
                        super().__init__(**kwargs)

                    def execute(self, context, event=None):
                        if event is None:
                            print("I AM DEFERRING")
                            self.defer(trigger=trigger, method_name="execute")
                            return
                        print("RESUMING")
                        return self.tfield + 1

                task_one = one()
                task_two = two(task_one)
                op = MyOp(task_id="abc", tfield=task_two)
                task_two >> op
            dr = dag.test()
            assert mock_run.call_args_list[0] == ((trigger,), {})
            tis = dr.get_task_instances()
            assert next(x for x in tis if x.task_id == "abc").state == "success"

    @mock.patch("airflow.models.taskinstance.TaskInstance._execute_task_with_callbacks")
    def test_dag_test_with_mark_success(self, mock__execute_task_with_callbacks):
        """
        option `--mark-success-pattern` should mark matching tasks as success without executing them.
        """
        cli_args = self.parser.parse_args(
            [
                "dags",
                "test",
                "example_sensor_decorator",
                datetime(2024, 1, 1, 0, 0, 0).isoformat(),
                "--mark-success-pattern",
                "wait_for_upstream",
            ]
        )
        dag_command.dag_test(cli_args)

        # only second operator was actually executed, first one was marked as success
        assert len(mock__execute_task_with_callbacks.call_args_list) == 1
        assert mock__execute_task_with_callbacks.call_args_list[0].kwargs["self"].task_id == "dummy_operator"


class TestCliDagsReserialize:
    parser = cli_parser.get_parser()

    test_bundles_config = {
        "bundle1": TEST_DAGS_FOLDER / "test_example_bash_operator.py",
        "bundle2": TEST_DAGS_FOLDER / "test_sensor.py",
        "bundle3": TEST_DAGS_FOLDER / "test_dag_with_no_tags.py",
    }

    @classmethod
    def setup_class(cls):
        clear_db_dags()

    def teardown_method(self):
        clear_db_dags()

    @conf_vars({("core", "load_examples"): "false"})
    def test_reserialize(self, configure_dag_bundles, session):
        with configure_dag_bundles(self.test_bundles_config):
            dag_command.dag_reserialize(self.parser.parse_args(["dags", "reserialize"]))

        serialized_dag_ids = set(session.execute(select(SerializedDagModel.dag_id)).scalars())
        assert serialized_dag_ids == {"test_example_bash_operator", "test_dag_with_no_tags", "test_sensor"}

    @conf_vars({("core", "load_examples"): "false"})
    def test_reserialize_should_support_bundle_name_argument(self, configure_dag_bundles, session):
        with configure_dag_bundles(self.test_bundles_config):
            dag_command.dag_reserialize(
                self.parser.parse_args(["dags", "reserialize", "--bundle-name", "bundle1"])
            )

        serialized_dag_ids = set(session.execute(select(SerializedDagModel.dag_id)).scalars())
        assert serialized_dag_ids == {"test_example_bash_operator"}

    @conf_vars({("core", "load_examples"): "false"})
    def test_reserialize_should_support_multiple_bundle_name_arguments(self, configure_dag_bundles, session):
        with configure_dag_bundles(self.test_bundles_config):
            dag_command.dag_reserialize(
                self.parser.parse_args(
                    ["dags", "reserialize", "--bundle-name", "bundle1", "--bundle-name", "bundle2"]
                )
            )

        serialized_dag_ids = set(session.execute(select(SerializedDagModel.dag_id)).scalars())
        assert serialized_dag_ids == {"test_example_bash_operator", "test_sensor"}
