# Copyright (c) 2026 David Schmid
"""Run bounded SkyPilot operations in a child process with secrets on stdin."""

import json
import os
import pathlib
import sys
import time

import sky


def main() -> None:
    """Persist the launch request ID before waiting, enabling precise cancellation."""
    configuration = json.load(sys.stdin)
    os.environ["SKYPILOT_API_SERVER_ENDPOINT"] = configuration["endpoint"]
    request_file = pathlib.Path(configuration["request_file"])
    cluster = configuration["cluster"]
    if configuration["action"] == "cleanup":
        try:
            if request_file.exists():
                request = request_file.read_text()
                sky.get(sky.api_cancel(request_ids=[request], silent=True))
        finally:
            sky.get(sky.down(cluster))
        return
    task = sky.Task.from_yaml_config(configuration["task"])
    request = sky.launch(
        task,
        cluster_name=cluster,
        retry_until_up=False,
        idle_minutes_to_autostop=1,
        down=True,
    )
    request_file.write_text(str(request))
    job_id, _handle = sky.get(request)
    while True:
        statuses = sky.get(sky.job_status(cluster, job_ids=[job_id]))
        status = statuses.get(job_id)
        if status == sky.JobStatus.SUCCEEDED:
            return
        if status is not None and status.is_terminal():
            msg = "The GPU acceptance job did not succeed."
            raise RuntimeError(msg)
        time.sleep(2)


if __name__ == "__main__":
    main()
