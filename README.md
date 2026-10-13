# ORBIT Task Scheduler

Strategy-based daily task scheduling with per-user resource limits.

A refactor of the legacy script in [`legacy/original.py`](legacy/original.py) into a
modular, testable, extensible service — built so that a growing team can add actions and
change storage without editing the scheduling core.

Python 3.10+. **No runtime dependencies.** `pytest` only for the test suite.

---

## Quick start

```bash
python main.py            # five-scene walkthrough, each scene a legacy defect removed
python main.py --dry-run  # executors describe their work and change nothing
python -m pytest -q       # 124 tests
python legacy/original.py # the starting point, for comparison
```

Nothing needs installing — the package is a flat directory, so `python main.py` and
`pytest` work from a clean checkout. `pyproject.toml` ships anyway, so
`pip install -e .` works when you want it on `sys.path` elsewhere.

### The smallest useful program

```python
from orbit_scheduler import build_scheduler, configure_logging, load_schedule

configure_logging()

config = load_schedule({
    "users": {"alice": {"quota": 3}},
    "tasks": [{"user": "alice", "time": "12:00", "action": "sync",
               "params": {"target": "/data/x"}}],
})

report = build_scheduler(config).tick()
print(report.summary())        # "1 attempted: 1 success"
```

---

## Documentation

| Document | What is in it |
|---|---|
| **This file** | Orientation, design decisions, how to extend, interview answers |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module boundaries, **ERD**, execution flow, sequence and state diagrams |
| [`docs/BRIEF.md`](docs/BRIEF.md) | The original requirements, with each one mapped to where it is satisfied |
| [`AI_COLLABORATION.md`](AI_COLLABORATION.md) | How AI was directed, and which of its suggestions were rejected and why |

All diagrams are Mermaid and render in GitHub, Notion and VS Code; all seven were parsed
and rendered to SVG as a check, not just eyeballed.

---

## Layout

```
orbit_scheduler/
├── models.py          ← REQUIRED MODULE: task data model
├── users.py           ← REQUIRED MODULE: user management & quota control
├── executors/
│   ├── base.py        ← REQUIRED MODULE: extensible task executor
│   └── builtin.py        sync / backup / delete
├── scheduler.py       ← REQUIRED MODULE: scheduling system
├── config.py             dict or JSON in, validated objects out
├── clock.py              injectable time, so tests never sleep
├── logging_setup.py      application logging + contextual adapter
└── errors.py             one exception hierarchy for the whole package

main.py                   the application: the only place logging is configured
config/                   example schedule
legacy/original.py        the code as supplied, kept for diffing
tests/                    124 tests, no sleeps, fully deterministic
```

The four modules the brief asks for map one-to-one onto four files, marked above.

---

## What the legacy code got wrong

The brief says you *"must carefully evaluate and verify correctness"* rather than trust
what you are given. Reorganising the original faithfully would have preserved four real
defects. Each is now fixed **structurally** — in a way that cannot be forgotten — and
each has a test that fails against the old behaviour.

| # | Defect in `legacy/original.py` | What it actually did | Fix | Test |
|---|---|---|---|---|
| 1 | `users[user]['executed']` never reset | The quota is *daily*, but the counter only grew. From day two onward alice is **permanently locked out**. | Consumption is keyed `(user_id, date)`. Tomorrow is a different key, so the reset is structural — no cron job, nothing to forget. | `test_quota_resets_the_next_day` |
| 2 | `if task['time'] == now` (string equality) | Two ticks inside the scheduled minute ran every task **twice**. No tick inside that minute and the task was **dropped silently, forever**. | Times are parsed, not compared as text. A task is due from its scheduled time onward; a `RunLog` makes a tick idempotent; a `catchup_window` bounds how late is too late. | `test_a_second_tick_in_the_same_minute_does_nothing`, `test_a_late_tick_still_runs_a_missed_task` |
| 3 | `users[task['user']]` unguarded | One task naming a deleted user raised `KeyError` out of the loop — **every task after it silently never ran**. | Each task is isolated. Unknown user, unknown action and executor crashes are logged with context and recorded as results; the loop continues. | `test_an_unknown_user_does_not_stop_the_run` |
| 4 | Quota incremented before the work succeeded | A failed action still burned part of the user's daily budget. | Reserve → execute → **refund on failure**. The reservation refunds itself on the way out of the `with` block. | `test_a_failing_task_refunds_its_quota` |

A fifth was found while writing the tests: `--dry-run` was consuming real quota and
marking tasks as run, so a rehearsal made the following real run skip everything it had
just previewed. Fixed in `Scheduler._execute_one`; see `test_dry_run_leaves_no_trace`.

---

## Design decisions worth defending

**Dispatch is a registry, not an `if/elif` chain.** `ExecutorRegistry` is the single
source of truth for which actions exist. No other module anywhere enumerates action
names, so adding one touches exactly one file.

**Everything the scheduler needs is injected.** Quota manager, executor registry, clock
and run log are all constructor arguments. That is what makes the test suite
deterministic without sleeping, and what lets a team swap in a database-backed run log or
a Redis quota manager without opening `scheduler.py`.

**Validation runs at load time, not at execution time.** Each executor's
`validate_params()` is called by the config loader. A task with a mistyped parameter
fails when the service starts, in front of whoever is deploying — not at 12:00 in front
of nobody.

**Validation accumulates.** `load_schedule()` collects every problem and raises once.
Three typos cost one run, not three.

**A `Task` is immutable.** It is a statement of intent; nothing downstream may rewrite
it. `params` is wrapped in a `MappingProxyType`, so an executor cannot mutate shared
state behind the scheduler's back.

**`Task` and `ExecutionResult` are separate types.** "What should happen" and "what
happened" are different things. The legacy code conflated them by mutating a counter
mid-loop, which is precisely why its state could not be inspected or tested.

**Libraries do not configure logging.** `configure_logging()` is called by `main.py` and
nowhere else. Every module uses `logging.getLogger(__name__)`, so each line names its
origin and an operator can tune verbosity per module.

**Context is bound once.** `TaskLogAdapter` injects `run`, `task`, `user` and `action`
into every record, instead of each call site re-interpolating the same four identifiers.

---

## Add a new task type

Three steps, and nothing in `orbit_scheduler` changes.

**1. Write the executor.**

```python
from orbit_scheduler.errors import InvalidParamsError
from orbit_scheduler.executors import PathTaskExecutor, default_registry

@default_registry.register
class CompressExecutor(PathTaskExecutor):
    action = "compress"
    description = "Compress the target path in place."

    def validate_params(self, params):          # optional — runs at config load
        super().validate_params(params)         # inherits the 'target' contract
        if not 1 <= params.get("level", 6) <= 9:
            raise InvalidParamsError("compress: 'level' must be between 1 and 9")

    def execute(self, task, ctx):               # raising marks the attempt failed
        ctx.logger.info("Compressing %s", self.target_of(task))
        return f"compressed {self.target_of(task)}"
```

**2. Make sure the module is imported** so the decorator runs.

**3. Use it in config.**

```json
{ "user": "alice", "time": "02:00", "action": "compress",
  "params": { "target": "/data/big", "level": 9 } }
```

`tests/test_executors.py::test_a_new_action_needs_no_change_to_the_system` does exactly
this from outside the package, as a standing proof that the extension point works.

**Conventions for a growing team**

- Inherit `PathTaskExecutor` for anything that takes a `target`, so the path rule stays
  in one place.
- Put *expensive or destructive* preconditions in `validate_params` — load time is free,
  12:00 is not.
- Log through `ctx.logger`, never a module logger, or you lose the run/task correlation.
- Read the clock from `ctx.now`, never `datetime.now()`, or you break deterministic tests.
- An action name is claimed process-wide; a collision raises at import time, by design.

---

## Configuration

```json
{
  "users": { "alice": { "quota": 3 }, "bob": 5 },
  "tasks": [
    { "id": "nightly-sync", "user": "alice", "time": "12:00",
      "action": "sync", "params": { "target": "/data/x" } }
  ],
  "settings": { "catchup_minutes": 60 }
}
```

| Field | Notes |
|---|---|
| `users.<id>.quota` | Executions allowed per calendar day. `0` blocks the user. Shorthand: `"bob": 5`. |
| `users.<id>.enabled` | `false` parks every task the user owns. |
| `tasks[].id` | Optional. Defaults to a **content-derived** id (`user:action:time`) — stable under reordering, because the run log keys on it. |
| `tasks[].time` | `HH:MM`, local. |
| `tasks[].params` | Executor-specific. The legacy flat shape (`target` beside `action`) is accepted too, so an existing deployment can migrate code before config. |
| `tasks[].enabled` | `false` parks one task without deleting it. |
| `settings.catchup_minutes` | How late a tick may be and still run a missed task. `-1` disables the window. Default 60. |

### Quota policy, stated explicitly

Quota is a business rule, so it is written down rather than left implied by the code:

- One successful execution costs **one** unit.
- The executor is resolved **before** quota is consumed, so an unroutable action is free.
- A **failed** execution is **refunded** — the user is not charged for a broken action.
- A **refused** execution (over quota) is never consumed in the first place.
- A **dry run** costs nothing and records nothing.
- Allowance is per calendar day, per user, in local time.

---

## Testing

```bash
python -m pytest -q          # 124 tests, ~0.3s
python -m pytest -v -k quota
uv tool run ruff check .     # clean
```

No test sleeps or depends on wall-clock time: every one drives a `FrozenClock`, which is
the payoff of injecting time instead of calling `datetime.now()` inside the scheduler.
Each test also gets its own registry and run log, so a double registered in one test
cannot leak into another.

| File | Covers |
|---|---|
| `test_models.py` | Immutability, time parsing, legacy-shape migration |
| `test_quota.py` | Allowance, **daily reset**, refunds, disabled users |
| `test_executors.py` | Registration, collisions, param validation, **a new action end to end** |
| `test_scheduler.py` | Due-task selection, **idempotency**, catch-up, **failure isolation**, dry run |
| `test_config.py` | **Accumulated** validation errors, file loading, assembly |

---

## Known limitations

Stated rather than hidden — each is a deliberate scope decision for a service of this
size, and each has a named upgrade path.

| Limitation | Consequence | Upgrade path |
|---|---|---|
| Naive local time | "Daily" means a different 24 hours for a user in Jakarta than in Berlin. | Per-user timezone on `User`; make `Clock` timezone-aware. |
| In-memory run log and ledger | A restart can re-run a task that already ran today. | Implement the `RunLog` Protocol over Postgres; move the ledger to Redis. |
| Single process | Two scheduler processes would double-dispatch and race on quota. | Persistent run log with a unique `(task_id, date)` constraint; atomic Redis `INCR`. |
| Executors run inline | One slow action delays every later task in the tick. | Dispatch to a worker pool or broker (see below). |
| Daily tasks only | No weekly, monthly or cron expressions. | Replace `scheduled_at: time` with a recurrence rule; `due_tasks()` is the only consumer. |

---

## Interview follow-up questions

The brief lists four. Answers are kept here so they are reviewable rather than improvised.

### How did you design your prompts to guide AI for refactoring?

In four stages, each with a narrow job, rather than one "refactor this" request. The
critical stage was the first: **analyse before designing**, with an explicit instruction
to look for latent bugs rather than reorganise what was there. That is what surfaced the
quota-reset defect — asked to "refactor into classes", every model cheerfully preserves
it. Full prompts and reasoning are in [`AI_COLLABORATION.md`](AI_COLLABORATION.md).

### Did you reject any AI-generated suggestions? Why?

Yes — twelve, listed with reasons in [`AI_COLLABORATION.md`](AI_COLLABORATION.md). The
three that mattered most: adding `APScheduler` (a dependency would hide the scheduling
design that is the actual subject of review); one executor thread per task (does not
scale, and makes the system untestable); and a faithful port that kept the lifetime
counter intact (correct to the source, wrong for the requirement).

### How would you scale this to tens of thousands of tasks daily?

**First, the honest arithmetic:** 10,000 tasks a day is about **7 per minute**. Raw
volume is not the problem, and saying otherwise would be theatre. Two things actually
break:

1. **Burstiness.** Every task in the example fires at 12:00. Ten thousand tasks in one
   minute is ~170/second against whatever the executors talk to.
2. **Execution time.** Executors run inline, so one 30-second backup delays every task
   behind it.

So the changes that pay, in the order they pay:

| Change | Why |
|---|---|
| Stop scanning every task | `due_tasks()` is O(all tasks) per tick. Store tasks in Postgres with an index on `next_run_at` and select only what is due. |
| Split dispatcher from worker | The scheduler enqueues `(task_id, scheduled_date)` onto a broker; a worker pool executes. The tick becomes bounded work, and slow actions stop blocking fast ones. |
| Move quota to Redis | `INCR` on `quota:{user}:{date}` with a TTL is atomic across workers and expires itself. The `(user, date)` key already used here maps onto it directly. |
| Make the run log authoritative | A unique constraint on `(task_id, scheduled_date)` turns a double-dispatch into a no-op — exactly-once in practice rather than by hope. |
| Smear the thundering herd | Jitter within a window, or shard by `hash(user) % N`, so 12:00 is not a cliff. |
| Retries with backoff and a dead-letter queue | At this volume transient failures are constant, and a task that fails forever must stop retrying and become visible. |
| Make `RunReport` emit metrics | Counts per status are already structured for it: success rate, quota-rejection rate, execution latency, queue depth. |

The design anticipates most of this: `RunLog` is already a Protocol, `QuotaManager`
already hides its storage, and the `(user, date)` and `(task_id, date)` keys are already
the right shape for Redis and a unique index respectively. The refactor is mostly
substitution rather than rewriting.

### Which parts would you extract into reusable modules for other teams?

In descending order of value to somebody else:

1. **`clock.py`** — tiny, zero-dependency, and solves a problem every team with
   time-dependent logic has. Lift as-is.
2. **`executors/base.py`** — the registry plus validating-ABC pattern is a general plugin
   mechanism. Generalise `Task` to a type parameter and it serves any pluggable-handler
   problem.
3. **`users.py` quota control** — "N units per principal per period" is a rate limiter.
   Generalise `date` to a period key and it covers API rate limits and billing credits.
4. **`logging_setup.py`** — the `bind()`-style contextual adapter should be one shared
   implementation per organisation, not one per service.
5. **The accumulating-validation idiom in `config.py`** — worth extracting as a small
   `ProblemCollector` helper once a third service wants it. Not before.

What should *not* be extracted: `scheduler.py` and `builtin.py`. They encode this
product's policy — daily cadence, catch-up semantics, which paths a delete may touch.
Shared policy is how a platform team ends up owning everyone's business rules.
