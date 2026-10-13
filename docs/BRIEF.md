# The brief

Transcribed from the ORBIT Notion page *Python_AI*, so the requirements this repository
was built against live beside the code. Content is reproduced as given; only the legacy
code block has been reformatted (the export flattened its whitespace).

> **Title:** Simulated Strategy-Based Task Scheduling and Resource Limitation System
> (AI-Assisted Refactoring)
>
> **Estimated time to complete with AI assistance:** 30 minutes
>
> You are free to use any AI tools (such as ChatGPT, Claude, Cursor, etc.) during the task.

---

## Description

> You are going to develop a backend service responsible for receiving user-submitted
> task strategies, executing them daily, and limiting each user's resource quota.
>
> Below is an initial version of code left by a previous engineer. Although it works, the
> structure is messy and difficult to extend.

```python
import datetime

users = {
    'alice': {'quota': 3, 'executed': 0},
    'bob': {'quota': 5, 'executed': 0},
}

tasks = [
    {'user': 'alice', 'time': '12:00', 'action': 'sync', 'target': '/data/x'},
    {'user': 'bob', 'time': '12:00', 'action': 'backup', 'target': '/srv/y'},
    {'user': 'alice', 'time': '12:00', 'action': 'delete', 'target': '/tmp/z'},
]


def run():
    now = datetime.datetime.now().strftime('%H:%M')
    for task in tasks:
        if task['time'] == now:
            user = task['user']
            if users[user]['executed'] >= users[user]['quota']:
                print(f"{user} has exceeded quota.")
                continue
            print(f"Executing {task['action']} on {task['target']} for {user}")
            users[user]['executed'] += 1
```

Kept verbatim at [`legacy/original.py`](../legacy/original.py).

---

## Refactoring goals

**1. Refactor into class-based / modular design, including:**

| Required module | Delivered as |
|---|---|
| User management & quota control module | [`orbit_scheduler/users.py`](../orbit_scheduler/users.py) |
| Task data model | [`orbit_scheduler/models.py`](../orbit_scheduler/models.py) |
| Task executor (extensible) | [`orbit_scheduler/executors/base.py`](../orbit_scheduler/executors/base.py) |
| Scheduling system (simple implementation is fine) | [`orbit_scheduler/scheduler.py`](../orbit_scheduler/scheduler.py) |

**2. Support the following requirements:**

| Requirement | Where it is satisfied | Test |
|---|---|---|
| A single user can have multiple tasks simultaneously | Quota is checked per execution, not per user per tick | `test_one_user_can_hold_several_simultaneous_tasks` |
| Task parameters should be configurable (e.g. via dictionary input) | `load_schedule(dict)`; `Task.params` is an open mapping | `test_loads_a_valid_config` |
| Task execution must include logging (use `logging` module) | `logging_setup.py`; every module uses `getLogger(__name__)` | visible in `python main.py` |

**3. Optional extensions (if time allows):**

*The source page has no content under this heading — verified against the raw export,
and it is not a collapsed toggle. Nothing was omitted here.* Scope was therefore taken as
goals 1 and 2; candidate extensions are described under "Known limitations" and
"Scaling" in the [README](../README.md) rather than built, since a 30-minute task rewards
scope judgement.

---

## Constraints

> - The goal of refactoring is to demonstrate your AI-assisted system design and
>   refactoring ability.
> - You may refer to AI suggestions, but must carefully evaluate and verify correctness.

The second constraint drove the analysis stage described in
[`AI_COLLABORATION.md`](../AI_COLLABORATION.md): the legacy code contains four defects
that a faithful refactor would have preserved. They are tabled in
[the README](../README.md#what-the-legacy-code-got-wrong).

---

## Possible follow-up questions (interview guidance)

All four are answered at the end of [the README](../README.md#interview-follow-up-questions).

1. How did you design your prompts to guide AI for refactoring?
2. Did you reject any AI-generated suggestions? Why?
3. If this system needs to handle tens of thousands of tasks daily, how would you scale
   the architecture?
4. Which parts would you extract into reusable modules for other teams?

---

## Evaluation criteria

| Category | Evaluation focus | Where to look |
|---|---|---|
| **Module & Logic Design** | Proper separation of logic and responsibilities | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §2 (layering) and §3 (ERD) |
| **Maintainability** | Easy to extend, clear naming, structured flow | README "Add a new task type"; ARCHITECTURE §7 and §9 |
| **AI Tool Usage** | Effectively guides AI instead of blindly relying on it | [`AI_COLLABORATION.md`](../AI_COLLABORATION.md) — prompt strategy, 12 rejected suggestions, 5 AI defects caught |
| **Error Handling & Logging** | Uses logging, clear and traceable error messages | `errors.py`, `logging_setup.py`; ARCHITECTURE §4 (every failure path) |

**Additional evaluation notes**

> - Ability to integrate AI-generated code into an existing system
> - Prompt accuracy and whether it solves real problems

On integration: the legacy flat task shape and the legacy `{'quota': n, 'executed': n}`
user shape both still load unchanged (`test_the_legacy_flat_task_shape_still_loads`), so
an existing deployment can migrate its code before its configuration.

---

## Expected performance

> **Senior level:** 10 minutes → architecture design; 20 minutes → modular implementation
> + testing
>
> **Junior level:** ~30 minutes → may only complete Refactoring Goal 1
