"""One rank of data-parallel logistic regression with a gradient all-reduce through object storage.

Every iteration, each of the world_size ranks computes the gradient on its own data shard,
publishes it under <prefix>/<run_id>/<iteration>/<rank>, and waits until the gradients of all
ranks are available (an all-gather, i.e., a barrier). This is bulk-synchronous training as in
distributed SGD with parameter averaging: it only progresses if all ranks run at the same time.
A rank that waits longer than barrier_timeout at a barrier gives up and reports how far it got.
"""

import io
import os
import time

import numpy as np

from . import storage

client = storage.storage.get_instance()


class ObjectStore:
    """Put and get under exact keys.

    The SeBS storage wrappers append a random suffix to uploaded names, so ranks could not
    address each other's objects; hence the underlying S3 (boto3) or MinIO client is used.
    """

    def __init__(self, bucket):
        self.bucket = bucket
        self.minio = hasattr(client.client, "fget_object")

    def put(self, key, data):
        if self.minio:
            client.client.put_object(self.bucket, key, io.BytesIO(data), len(data))
        else:
            client.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key):
        """The object, or None if it does not exist yet."""
        try:
            if self.minio:
                response = client.client.get_object(self.bucket, key)
                try:
                    return response.read()
                finally:
                    response.close()
                    response.release_conn()
            return client.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:
            if "NoSuchKey" in type(e).__name__ or "NoSuchKey" in str(e):
                return None
            raise


def shard(rank, samples, features):
    """Synthetic but learnable data: labels from a fixed true model, a different shard per rank."""
    true_weights = np.random.default_rng(0).standard_normal(features).astype(np.float32)
    rng = np.random.default_rng(1 + rank)
    x = rng.standard_normal((samples, features), dtype=np.float32)
    probability = 1.0 / (1.0 + np.exp(-x @ true_weights))
    y = (rng.random(samples) < probability).astype(np.float32)
    return x, y


def gradient_and_loss(weights, x, y):
    p = 1.0 / (1.0 + np.exp(-x @ weights))
    eps = 1e-7
    loss = -np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))
    return x.T @ (p - y) / len(y), float(loss)


def handler(event):
    begin = time.time()
    rank = event["rank"]
    world_size = event["world_size"]
    iterations = event["iterations"]
    timeout = event["barrier_timeout"]
    poll_interval = event["poll_interval"]
    store = ObjectStore(event["bucket"]["bucket"])
    prefix = os.path.join(event["bucket"]["output"], event["run_id"])

    x, y = shard(rank, event["samples_per_rank"], event["features"])
    weights = np.zeros(event["features"], dtype=np.float32)
    data_ready = time.time()

    log = []
    status = "completed"
    losses = []
    for iteration in range(iterations):
        compute_begin = time.time()
        grad, loss = gradient_and_loss(weights, x, y)
        losses.append(loss)
        publish_begin = time.time()
        store.put(f"{prefix}/{iteration}/{rank}", grad.astype(np.float32).tobytes())
        wait_begin = time.time()

        gradients = {rank: grad}
        polls = 0
        while len(gradients) < world_size and time.time() - wait_begin < timeout:
            for peer in range(world_size):
                if peer not in gradients:
                    data = store.get(f"{prefix}/{iteration}/{peer}")
                    if data is not None:
                        gradients[peer] = np.frombuffer(data, dtype=np.float32)
            polls += 1
            if len(gradients) < world_size:
                time.sleep(poll_interval)
        wait_end = time.time()

        log.append(
            {
                "iteration": iteration,
                "compute_seconds": publish_begin - compute_begin,
                "publish_seconds": wait_begin - publish_begin,
                "wait_seconds": wait_end - wait_begin,
                "polls": polls,
                "ranks_seen": len(gradients),
            }
        )
        if len(gradients) < world_size:
            status = "barrier_timeout"
            break
        weights -= event["learning_rate"] * np.mean([gradients[r] for r in range(world_size)], 0)

    end = time.time()
    return {
        "result": {
            "rank": rank,
            "world_size": world_size,
            "run_id": event["run_id"],
            "status": status,
            "iterations_completed": sum(1 for entry in log if entry["ranks_seen"] == world_size),
            "begin": begin,
            "data_ready": data_ready,
            "end": end,
            "loss": losses,
            "weights_checksum": round(float(weights.sum()), 4),
            "iterations": log,
        },
        "measurement": {"compute_seconds": sum(e["compute_seconds"] for e in log)},
    }
