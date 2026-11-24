"""One GitHub poller, shared by the roadmap, coordinator and CI waiters."""
from collections import deque
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
from threading import Lock, Thread
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

OWNER = "julianthant"
REPOS = ("hr-relay", "dot-agents")
STALE_SECONDS = 60
REST_FLOOR = 100
SHA = re.compile(r"[a-f0-9]{40}", re.I)
RUN_FIELDS = ("id", "name", "path", "head_sha", "head_branch", "event", "status", "conclusion",
              "created_at", "updated_at", "html_url", "run_attempt")


class Unavailable(RuntimeError):
    pass


def task_workflow(repo):
    # dot-agents only runs its task checks on a PR.
    return ("tests.yml", "pull_request") if repo == "dot-agents" else ("development.yml", "push")


@dataclass(frozen=True)
class Sample:
    data: object
    at: float | None = None

    def age(self, now):
        return max(0, now - self.at) if self.at is not None else None

    def answer(self, now):
        age = self.age(now)
        return {"data": self.data, "ageSeconds": age, "stale": age is None or age > STALE_SECONDS}


class Rest:
    def __init__(self, *, clock=time.time, run=None):
        self.clock = clock
        self.run = run or subprocess.run
        self.calls = deque()
        self.remaining = None
        self.reset = None
        self.retry = 0
        self.lock = Lock()
        self.request_lock = Lock()

    def budget(self):
        with self.lock:
            now = self.clock()
            while self.calls and self.calls[0][0] <= now - 3600:
                self.calls.popleft()
            reset = self.reset if self.reset and self.reset > now else None
            remaining = self.remaining if reset else None
            return {"callsLastHour": len(self.calls),
                    "notModifiedLastHour": sum(code == 304 for _, code in self.calls),
                    "remaining": remaining, "resetAt": reset, "floor": REST_FLOOR,
                    "limitedUntil": max(self.retry if self.retry > now else 0,
                                        reset if remaining is not None and remaining <= REST_FLOOR else 0) or None}

    def slowdown(self):
        remaining = self.budget()["remaining"]
        if remaining is None or remaining >= 1000:
            return 1
        if remaining >= 500:
            return 2
        return 4 if remaining >= 250 else 8

    def poll(self, endpoint, etag=None):
        with self.request_lock:
            return self.request(endpoint, etag)

    def request(self, endpoint, etag):
        if self.budget()["limitedUntil"]:
            raise Unavailable("GitHub REST quota reserved for writes")
        code = 0
        try:
            result = self.run(["gh", "api", "--include", *(["-H", f"If-None-Match: {etag}"] if etag else []),
                               endpoint], capture_output=True, text=True, timeout=10)
            header, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
            status = re.match(r"HTTP/\S+ (\d+)", header)
            code = int(status[1]) if status else 0
            headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in header.splitlines()[1:])}
            with self.lock:
                if headers.get("x-ratelimit-resource", "core") == "core":
                    remaining, reset = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-reset")
                    if remaining and remaining.isdigit() and reset and reset.isdigit():
                        self.remaining, self.reset = int(remaining), int(reset)
                retry = headers.get("retry-after", "")
                if code == 429 or (code == 403 and retry.isdigit()):
                    self.retry = self.clock() + (int(retry) if retry.isdigit() else 60)
            return code, headers.get("etag", etag), body
        finally:
            with self.lock:
                self.calls.append((self.clock(), code))


def conditional(cache, endpoint, select, poll):
    etag, value = cache.get(endpoint, (None, None))
    status, tag, body = poll(endpoint, etag)
    if status == 200:
        value = select(json.loads(body))
        cache[endpoint] = tag, value
    elif status != 304 or not etag:
        raise ValueError(f"GitHub REST request returned {status}: {endpoint}")
    return value


def run_summary(run):
    return {key: run.get(key) for key in RUN_FIELDS}


def runs(value):
    return [run_summary(run) for run in value["workflow_runs"]]


def workflow_matches(run, workflow):
    return not workflow or (run.get("path") or "").split("@", 1)[0].rsplit("/", 1)[-1] == workflow


class Poller:
    def __init__(self, *, clock=time.time, rest=None):
        self.clock = clock
        self.rest = rest or Rest(clock=clock)
        self.samples = {name: Sample(None) for name in ("prs", "project", "issues", "heads", "pipeline")}
        self.pages = {repo: Sample(None) for repo in REPOS}
        self.history = {repo: () for repo in REPOS}
        self.cache = {}
        self.demand = {}
        self.lock = Lock()

    def set(self, name, data):
        self.samples[name] = Sample(data, self.clock())

    def remember(self, repo, rows):
        with self.lock:
            seen = {run["id"] for run in rows}
            previous = (run for run in self.history[repo] if run["id"] not in seen)
            self.history[repo] = tuple([*rows, *previous][:500])

    def poll_runs(self):
        errors = []
        for repo in REPOS:
            endpoint = f"repos/{OWNER}/{repo}/actions/runs?per_page=100"
            try:
                data = conditional(self.cache, endpoint, runs, self.rest.poll)
                self.remember(repo, data)
                self.pages[repo] = Sample(data, self.clock())
            except (Unavailable, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                errors.append(str(e))
        if errors:
            raise Unavailable("; ".join(errors))

    def poll_details(self):
        now = self.clock()
        with self.lock:
            for endpoint, (_, last_read, _) in list(self.demand.items()):
                if now - last_read > STALE_SECONDS:
                    del self.demand[endpoint]
                    self.cache.pop(endpoint, None)
            ready = [(sample[2], endpoint, sample[0]) for endpoint, sample in self.demand.items()
                     if now - sample[2] >= 15]
            if not ready:
                return
            _, endpoint, sample = min(ready, key=lambda entry: entry[0])
            self.demand[endpoint] = (sample, self.demand[endpoint][1], now)
        data = conditional(self.cache, endpoint, lambda value: value, self.rest.poll)
        with self.lock:
            self.demand[endpoint] = (Sample(data, self.clock()), self.demand[endpoint][1], now)

    def demand_sample(self, endpoint):
        now = self.clock()
        with self.lock:
            if endpoint not in self.demand and len(self.demand) >= 100:
                raise Unavailable("roadmap has too many pending GitHub lookups")
            sample, _, attempted = self.demand.get(endpoint, (Sample(None, now), now, float("-inf")))
            # Keep an unresolved lookup pending for at most a freshness window.
            self.demand[endpoint] = (sample, now, attempted)
            return sample

    def status(self):
        now = self.clock()
        sources = {**self.samples, **{f"runs:{repo}": sample for repo, sample in self.pages.items()}}
        ages = {}
        for name, sample in sources.items():
            age = sample.age(now)
            ages[name] = {"ageSeconds": age, "stale": age is None or age > STALE_SECONDS}
        return {**self.rest.budget(), "sources": ages}

    def read(self, source, query):
        if source in ("issues", "heads", "prs", "project"):
            sample = self.samples[source]
        else:
            repo = query.get("repo")
            if repo not in REPOS:
                raise ValueError("repo must be hr-relay or dot-agents")
            root = f"repos/{OWNER}/{repo}/actions"
            if source == "jobs":
                run, attempt = query.get("id", ""), query.get("attempt", "")
                if not run.isdecimal() or not attempt.isdecimal():
                    raise ValueError("run id and attempt must be decimal")
                endpoint = f"{root}/runs/{run}/attempts/{attempt}/jobs?per_page=100"
                sample = self.demand_sample(endpoint)
            elif source == "runs" and query.get("id", "").isdecimal():
                sample = self.demand_sample(f"{root}/runs/{query['id']}")
                sample = Sample(run_summary(sample.data) if sample.data else None, sample.at)
            elif source == "runs":
                sample = self.read_runs(repo, root, query)
            else:
                raise ValueError("unknown roadmap source")
        return sample.answer(self.clock())

    def read_runs(self, repo, root, query):
        sha, branch = query.get("sha"), query.get("branch")
        if bool(sha) == bool(branch) or (sha and not SHA.fullmatch(sha)):
            raise ValueError("provide a full SHA or a branch")
        workflow, event = query.get("workflow"), query.get("event")
        if workflow and not re.fullmatch(r"[\w.-]+\.ya?ml", workflow):
            raise ValueError("invalid workflow name")
        page = self.pages[repo]
        if page.answer(self.clock())["stale"]:
            return page
        key, value = ("head_sha", sha.lower()) if sha else ("head_branch", branch)

        def matches(run):
            return run[key] == value and workflow_matches(run, workflow) and (not event or run["event"] == event)

        found = [run for run in page.data if matches(run)]
        if found:
            return Sample(found, page.at)
        historical = next((run for run in self.history[repo] if run["id"] and matches(run)), None) if sha else None
        if historical:
            older = self.demand_sample(f"{root}/runs/{historical['id']}")
            return Sample([run_summary(older.data)] if older.data else None, older.at)
        path = f"{root}/workflows/{workflow}/runs" if workflow else f"{root}/runs"
        args = {"per_page": 100, "head_sha" if sha else "branch": value}
        if event:
            args["event"] = event
        older = self.demand_sample(f"{path}?{urlencode(args)}")
        discovered = [run for run in runs(older.data) if matches(run)] if older.data is not None else None
        if sha and discovered:
            self.remember(repo, discovered)
        return Sample(discovered, older.at)

    def schedule(self, stop, interval, operation, *, rest=False):
        def loop():
            while not stop.is_set():
                start = time.monotonic()
                try:
                    operation()
                except (Unavailable, RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
                    print(f"roadmap {operation.__name__}: {e}", file=sys.stderr)
                every = interval * (self.rest.slowdown() if rest else 1)
                stop.wait(max(0, every - (time.monotonic() - start)))

        thread = Thread(target=loop, daemon=True, name=operation.__name__)
        thread.start()
        return thread


def read(source, **query):
    base = os.environ.get("ROADMAP_URL", "http://127.0.0.1:8790").rstrip("/")
    url = f"{base}/{source}" + ("?" + urlencode(query) if query else "")
    try:
        with urlopen(url, timeout=5) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not isinstance(result.get("stale"), bool) or "data" not in result:
            raise ValueError("unexpected roadmap response")
    except (URLError, TimeoutError, OSError, ValueError) as e:
        raise Unavailable(f"roadmap server unavailable at {base} ({e})") from e
    if result["stale"]:
        age = result.get("ageSeconds")
        raise Unavailable(f"roadmap {source} data is stale" + (f" ({age:.0f}s old)" if age is not None else " (no fresh sample)"))
    return result["data"]
