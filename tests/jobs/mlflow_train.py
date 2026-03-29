# ruff: noqa: INP001  # tests/ is not a package
"""Dummy training script for the SkyPilot integration test.

Logs hyperparams, a loss/accuracy curve, and a model-weights artifact to the
MLflow tracking server pointed to by MLFLOW_TRACKING_URI.
"""

import os
import pathlib

import mlflow

tracking_uri = os.environ["MLFLOW_TRACKING_URI"]
mlflow.set_tracking_uri(tracking_uri)
mlflow.set_experiment("skypilot-integration-test")

with mlflow.start_run(run_name="skypilot-test-run") as run:
    # ── Hyperparams ──────────────────────────────────────────────────────────
    mlflow.log_param("lr", 0.01)
    mlflow.log_param("epochs", 5)

    # ── Dummy training loop ──────────────────────────────────────────────────
    for epoch in range(5):
        loss = 1.0 / (epoch + 1)
        accuracy = 1.0 - loss * 0.5
        mlflow.log_metric("loss", loss, step=epoch)
        mlflow.log_metric("accuracy", accuracy, step=epoch)

    # ── Artifact (simulated model weights) ──────────────────────────────────
    artifact_file = pathlib.Path("model_weights.txt")
    artifact_file.write_text("weights: 0.42\nbias: 0.1\n")
    mlflow.log_artifact(str(artifact_file), artifact_path="model")
