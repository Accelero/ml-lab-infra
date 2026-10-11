# Copyright (c) 2026 David Schmid
# ruff: noqa: INP001  # tests/ is not a package
"""Log test parameters, metrics, and an artifact to MLflow."""

import ctypes
import os
import pathlib

import mlflow

tracking_uri = os.environ["MLFLOW_TRACKING_URI"]
mlflow.set_tracking_uri(tracking_uri)
mlflow.set_experiment(os.environ["TEST_EXPERIMENT_NAME"])

with mlflow.start_run(
    run_name="skypilot-test-run",
    tags={"test_invocation": os.environ["TEST_RUN_ID"]},
):
    driver = ctypes.CDLL("libcuda.so.1")
    count = ctypes.c_int()
    if (
        driver.cuInit(0)
        or driver.cuDeviceGetCount(ctypes.byref(count))
        or count.value != 1
    ):
        msg = "The acceptance job requires one accessible CUDA GPU."
        raise RuntimeError(msg)
    mlflow.log_metric("gpu_count", count.value)
    mlflow.log_param("lr", 0.01)
    mlflow.log_param("epochs", 5)

    for epoch in range(5):
        loss = 1.0 / (epoch + 1)
        accuracy = 1.0 - loss * 0.5
        mlflow.log_metric("loss", loss, step=epoch)
        mlflow.log_metric("accuracy", accuracy, step=epoch)

    artifact_file = pathlib.Path("model_weights.txt")
    artifact_file.write_text("weights: 0.42\nbias: 0.1\n")
    mlflow.log_artifact(str(artifact_file), artifact_path="model")
