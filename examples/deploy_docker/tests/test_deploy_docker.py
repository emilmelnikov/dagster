import json
import os
import subprocess
import time
from contextlib import contextmanager

import pytest
import requests
from dagster import file_relative_path


@contextmanager
def docker_service_up(docker_compose_file, git_commit):
    print("Removing any existing services")
    try:
        subprocess.check_output(["docker-compose", "-f", docker_compose_file, "stop"])
        subprocess.check_output(["docker-compose", "-f", docker_compose_file, "rm", "-f"])
    except subprocess.CalledProcessError:
        pass

    print("Starting build process")

    build_process = subprocess.Popen(
        [
            "docker-compose",
            "-f",
            docker_compose_file,
            "build",
            "--build-arg",
            "DAGSTER_GIT_COMMIT={git_commit}".format(git_commit=git_commit),
        ],
    )

    build_process.wait()

    up_process = subprocess.Popen(["docker-compose", "-f", docker_compose_file, "up", "--no-start"])

    up_process.wait()

    start_process = subprocess.Popen(["docker-compose", "-f", docker_compose_file, "start"])

    start_process.wait()

    try:
        yield
    finally:
        subprocess.check_output(["docker-compose", "-f", docker_compose_file, "stop"])
        subprocess.check_output(["docker-compose", "-f", docker_compose_file, "rm", "-f"])


IS_BUILDKITE = os.getenv("BUILDKITE") is not None


PIPELINES_OR_ERROR_QUERY = """
{
    repositoriesOrError {
        ... on PythonError {
            message
            stack
        }
        ... on RepositoryConnection {
            nodes {
                pipelines {
                    name
                }
            }
        }
    }
}
"""

RUN_QUERY = """
query RunQuery($runId: ID!) {
  pipelineRunOrError(runId: $runId) {
    __typename
    ... on PipelineRun {
      status
    }
  }
}
"""

LAUNCH_PIPELINE_MUTATION = """
mutation($executionParams: ExecutionParams!) {
  launchPipelineExecution(executionParams: $executionParams) {
    __typename
    ... on LaunchPipelineRunSuccess {
      run {
        runId
        status
      }
    }
    ... on PipelineNotFoundError {
      message
      pipelineName
    }
    ... on PythonError {
      message
    }
  }
}
"""


@pytest.mark.skipif(
    IS_BUILDKITE,
    reason="Need to figure out docker-compose networking on buildkite for this test to run",
)
def test_deploy_docker():
    # To test this locally, push your local change to a git branch and reference it here
    with docker_service_up(
        file_relative_path(__file__, "../docker-compose.yml"), "origin/dockerexamplecontents"
    ):
        # Wait for server to wake up

        start_time = time.time()

        while True:
            if time.time() - start_time > 15:
                raise Exception("Timed out waiting for dagit server to be available")

            try:
                sanity_check = requests.get("http://localhost:3000/dagit_info")
                assert "dagit" in sanity_check.text
                break
            except:
                pass

            time.sleep(1)

        res = requests.get(
            "http://localhost:3000/graphql?query={query_string}".format(
                query_string=PIPELINES_OR_ERROR_QUERY,
            )
        ).json()

        data = res.get("data")
        assert data

        repositoriesOrError = data.get("repositoriesOrError")
        assert repositoriesOrError

        nodes = repositoriesOrError.get("nodes")
        assert nodes

        assert nodes[0]["pipelines"][0]["name"] == "my_pipeline"

        variables = {
            "executionParams": {
                "selector": {
                    "repositoryLocationName": "user_code",
                    "repositoryName": "deploy_docker_repository",
                    "pipelineName": "my_pipeline",
                },
                "mode": "default",
            }
        }

        launch_res = requests.post(
            "http://localhost:3000/graphql?query={query_string}&variables={variables}".format(
                query_string=LAUNCH_PIPELINE_MUTATION, variables=json.dumps(variables)
            )
        ).json()

        assert (
            launch_res["data"]["launchPipelineExecution"]["__typename"]
            == "LaunchPipelineRunSuccess"
        )

        run = launch_res["data"]["launchPipelineExecution"]["run"]
        run_id = run["runId"]
        assert run["status"] == "QUEUED"

        start_time = time.time()

        while True:
            if time.time() - start_time > 60:
                raise Exception("Timed out waiting for launched run to complete")

            run_res = requests.get(
                "http://localhost:3000/graphql?query={query_string}&variables={variables}".format(
                    query_string=RUN_QUERY, variables=json.dumps({"runId": run_id})
                )
            ).json()

            status = run_res["data"]["pipelineRunOrError"]["status"]
            assert status and status != "FAILED"

            if status == "SUCCEEDED":
                break

            time.sleep(1)