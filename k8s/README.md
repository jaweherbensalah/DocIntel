# Kubernetes deployment

Plain manifests — no Helm, no operators — applied in filename order.

| File | Contents |
|------|----------|
| `00-namespace.yaml` | `docintel` namespace |
| `10-config.yaml` | ConfigMap (non-secret settings) + example Secret |
| `20-postgres.yaml` | Postgres StatefulSet with a PVC, plus its Service |
| `30-redis.yaml` | Redis Deployment + Service (Celery broker) |
| `35-migrate-job.yaml` | One-shot Job running `alembic upgrade head` |
| `40-api.yaml` | FastAPI Deployment (2 replicas) + Service |
| `50-worker.yaml` | Celery worker Deployment (2 replicas) |
| `60-networkpolicy.yaml` | Restrict Postgres/Redis to this app's pods |

## Deploy to a local cluster (kind)

The image is built locally and side-loaded, so no registry is needed:

```bash
docker build -t docintel:latest .

kind create cluster --name docintel
kind load docker-image docintel:latest --name docintel

kubectl apply -f k8s/
kubectl -n docintel wait --for=condition=complete job/docintel-migrate --timeout=5m
kubectl -n docintel rollout status deploy/docintel-api
kubectl -n docintel rollout status deploy/docintel-worker
```

Then exercise it:

```bash
kubectl -n docintel port-forward svc/docintel-api 8000:80 &

curl -s localhost:8000/health

id=$(curl -s -X POST localhost:8000/extract \
      -H "x-api-key: replace-me-before-deploying" \
      -F "file=@fixtures/cv_jane_doe.txt" | jq -r .id)

curl -s localhost:8000/results/$id -H "x-api-key: replace-me-before-deploying" | jq
```

With minikube, swap the load step for `minikube image load docintel:latest`.

## Validate without a cluster

```bash
kubectl apply --dry-run=client -f k8s/
# or, for full schema validation:
kubeconform -strict -summary k8s/
```

## Why it looks like this

- **Postgres is a StatefulSet with a PVC**; Redis is a Deployment, because the
  queue is rebuildable state and the database is not.
- **Every container is non-root** with `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false` and all capabilities dropped. Writable
  paths (`/tmp`, `/app/uploads`, Redis `/data`) are explicit `emptyDir` mounts.
- **The API has a `startupProbe`** so slow first-boot table creation can't be
  mistaken for a hung process by the liveness probe. The worker serves no HTTP,
  so its liveness probe is a Celery `inspect ping`.
- **`initContainers` wait for Postgres and Redis** so pods don't crash-loop
  during a cold start of the whole namespace.
- **Requests and limits are set on every container** so the scheduler can place
  them and one pod can't starve its neighbours.
- **Secrets are separate from config.** The committed Secret holds obvious
  placeholders; a real deployment sources it from a secret manager and never
  commits it.

## Known limitations

These are deliberate scope choices, not oversights:

- **`uploads/` is per-pod scratch.** The debug copies of uploads go to an
  `emptyDir`, so they are pod-local and lost on restart. If they mattered they
  would belong in object storage, not a volume.
- **No autoscaling.** Replica counts are fixed; scaling workers off queue depth
  is Ticket 15.
- **No Ingress.** Access is via `port-forward`, since the ingress controller and
  TLS/hostname setup are environment-specific.
