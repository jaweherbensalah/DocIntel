# Take-home: Backend / Cloud Engineer

> **Format:** you take ownership of a small, real service. Below is a backlog of tickets — some harden what's there, some extend it.
> **Pick what you want.** Nothing is mandatory. Tackle the tickets that interest you, in any order — one, a few, or all of them. We'd rather see a handful done well, with your reasoning, than everything rushed.

---

## The context

You're joining our backend team. We build AI-powered services for clients — typically a Python API in front of an LLM/ML pipeline, containerised and deployed to Kubernetes.

A few weeks ago a contractor started **`docintel`**, a small **document-intelligence service**. The idea: a client uploads a **CV** (PDF/image), the service uses an LLM to **extract a structured profile** (skills, experience, education…), and then **scores that profile against a job description**, returning a match score with a short rationale.

It demos. The contractor showed it working on their laptop and then rolled off. It is *not* something we'd put in front of a client yet — the heavy work blocks the API, the authentication is weak, the container leaks our source code, the API contract is loose, and it only runs on a developer's machine.

You're taking ownership — the backlog is below.

---

## What the service does today

The starter repository (`./docintel/`) contains a runnable FastAPI service exposing roughly:

- `POST /extract` — upload a document; returns an extracted profile (runs the LLM **inline**, inside the request).
- `POST /match` — given an extracted profile and a job description, returns a match score.
- `GET  /results/{id}` — fetch a previous result.
- `GET  /health` — liveness check.

It ships with a **mock LLM provider** so the whole thing **runs offline, with no API key and no cost** — bring it up with `docker compose up` and you can hit it immediately. (You can point it at a real model if you like, but nothing here requires it.)

**Start by reading and running it before you change anything.**

---

## The backlog

These are the tickets, the way we'd hand them to a new engineer — pick the ones you want to take on, none are required. Order and combination are up to you. For each ticket you tackle, we care as much about *why* you did it the way you did as the code itself.

### Hardening the service

### Ticket 1 — The pipeline doesn't scale
Extraction runs inline in the request, so a single slow document ties up a worker and the service buckles under load. **Re-architect it so the heavy work no longer runs on the request path** — we reach for a task queue (Celery + Redis) here, and how you manage that queue matters to us.

### Ticket 2 — The authentication is unsafe
The current authentication has real security weaknesses, and secrets aren't handled the way they should be. **Identify the flaws and fix them.** Treat this service as internet-facing and handling personal data (CVs).

### Ticket 3 — The image leaks our code
We ship this image to clients. Right now a client can `exec` into the running container and read all of our source files straight off the filesystem — our pipeline logic is proprietary and that's not acceptable. **Make the image "encrypted" so the code inside it is protected.** How you achieve that is up to you.

### Ticket 4 — The image is too big
The image we build and ship is large — slow to build, push, and pull. **Bring it down to a size you'd be comfortable shipping.**

### Ticket 5 — The API contract is loose
The endpoints have inconsistent request/response shapes, ad-hoc error handling, and wrong status codes. **Bring the API up to a standard you'd be happy to hand an external integrator.**

### Ticket 6 — The API docs are poor
The auto-generated OpenAPI schema and its Swagger UI are thin and don't match how the API really behaves, so nobody could integrate against the docs alone. **Make the documentation something an external developer could build against without asking us questions.**

### Extending the service

### Ticket 7 — Make it observable
Make the service observable enough that we could run it in production and debug it when something goes wrong.

### Ticket 8 — Add a feature
Add a new capability that fits the product — for example, **batch matching**: one job description against many candidate profiles, returning a ranked shortlist. Propose your own if you'd prefer.

### Ticket 9 — Deploy it to Kubernetes
Provide Kubernetes manifests that deploy the service and its dependencies correctly and safely. They're delivered as files — they don't need to run on a live cluster for us to evaluate them. *(Bonus: actually deploy to a local cluster — minikube/kind — and include proof.)*

### Ticket 10 — Ship it with CI/CD
Set up a CI/CD pipeline with GitHub Actions that you'd trust to ship this service.

### Advanced challenges

### Ticket 11 — Evolve the data model with zero downtime
Today every result is dumped into a single JSON text column, so we can't query, index, or report on any of it. Move to a data model we can actually query — with a migration that is safe to run against a live, in-use database, with no downtime and no data loss.

### Ticket 12 — Enforce per-client cost and rate budgets
This AI work is expensive, and each client is sold a monthly budget and a rate limit. Enforce both accurately — a client must not be able to exceed their budget or their rate by sending many requests at the same time.

### Ticket 13 — Harden the pipeline against malicious documents
The document text we feed the model is fully attacker-controlled. Assume an uploaded CV is hostile — trying to manipulate the model or pull out information it shouldn't. Make the pipeline robust against that, and show us how you'd verify it holds.

### Ticket 14 — Make it truly multi-tenant
Several clients share this service. Make it properly multi-tenant: each client's documents, results, and budgets fully isolated, with per-client credentials — and give us a guarantee, backed by a test, that one client can never read or affect another's data.

### Ticket 15 — Autoscale the workers, and never drop a job
Load is bursty: the queue backs up while pods sit idle, and deploys or scale-downs kill jobs mid-flight. Make the workers scale with the actual backlog, and make sure no in-flight job is ever lost to a restart, scale-down, or deploy.

### Ticket 16 — Trace a request end to end
When something is slow or fails, we can't see where the time went once a request crosses into the queue and the worker. Give us a single trace that follows one request all the way through — API, queue, worker, and the model call.

### Ticket 17 — Load-test it and fix what breaks
We don't actually know how much load this service can take. Build a load test that drives it the way real traffic would — many concurrent clients using the API at once — and use it to find the point where it falls over. Show us what breaks, fix it, and show the before-and-after. **We reach for Locust here.**

### Ticket 18 — Make the image supply chain trustworthy
A client runs our image in their own environment. Give them a way to verify that what they're running is exactly what we built from our source, untampered — and make sure we're not shipping known-vulnerable components.

### Go further (bonus)
Smaller extras: stream results to the client (SSE) instead of polling · webhook delivery on completion · multi-arch image build & push.

---

## Ground rules

- **What you submit must run.** `docker compose up` (or your documented equivalent) should bring it up offline, with no external API keys.
- **Don't gold-plate.** Clean, correct, and well-reasoned beats large.

---

Good luck, and have fun with it.
