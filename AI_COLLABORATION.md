# AI collaboration record

The brief evaluates **"AI Tool Usage — effectively guides AI instead of blindly relying
on it"** and asks two follow-up questions: how the prompts were designed, and which AI
suggestions were rejected. This is the answer, written during the work rather than
reconstructed afterwards.

The short version: AI wrote most of the characters in this repository. It did not make
the decisions, and three of its outputs were wrong in ways that testing caught and
review did not.

---

## 1. How the work was directed

Four stages, each with one job. The split matters more than the wording of any individual
prompt.

| Stage | Goal | Why separate |
|---|---|---|
| **1. Analyse** | Find what is *wrong* with the legacy code, before any structure is proposed | The single highest-leverage decision. See below. |
| **2. Design** | Module boundaries and seams, no implementation | Forces the architecture to be judged on its own, when it is still cheap to change |
| **3. Implement** | Write the code against the agreed design | Scoped work — the model cannot quietly redesign while coding |
| **4. Adversarially review** | Attack the result: edge cases, races, lies in the docs | A model asked to find faults in code finds faults; a model asked "is this good?" says yes |

### The decision that mattered

Stage 1 existed at all.

The obvious prompt for this task is *"refactor this into a class-based design."* That
prompt produces a clean, well-named, modular system **that faithfully preserves every bug
in the original** — because reorganising is what was asked for, and the lifetime quota
counter looks like intent, not like a defect.

The instruction that changed the outcome was closer to:

> Before proposing any structure: this code runs **daily** and the quota is described as
> a **daily** quota. Trace what actually happens on day two, and on a second tick inside
> the same minute. List the defects you find and rank them by blast radius. Do not
> suggest a design yet.

That surfaced the quota-reset defect (`executed` never resets, so every user is
permanently locked out from day two) and the double-execution defect. Both are invisible
to a refactor that trusts its input. All four defects and their fixes are tabled in the
[README](README.md#what-the-legacy-code-got-wrong).

### The other prompts, in shape

**Design:**
> Propose a module layout for a team that will grow to several squads. Optimise for:
> adding a new action type touches exactly one file; swapping storage never touches
> scheduling. For each module, name what it owns and what it must *not* know. Where two
> designs are defensible, give me both and say which you would ship.

Asking for the rejected alternative is the part that works. A model that must defend a
choice against a named alternative produces a reviewable decision; a model asked for "the
best design" produces an assertion.

**Implement:**
> Implement `users.py` against that design. Constraints: quota resets daily without a
> scheduled job; check-and-increment must be atomic; a failed execution must not consume
> allowance. Docstrings explain *why*, not what the code already says.

Stating the invariants rather than the mechanism is what produced the `(user_id, date)`
key instead of a reset job. The mechanism was the model's idea; the property it had to
satisfy was not negotiable.

**Review:**
> You are reviewing this as the engineer who will be on call for it. Where does it lose
> data, double-execute, deadlock, or grow without bound? Where does a docstring claim
> something the code does not do?

The unbounded-growth finding (`QuotaLedger` and `InMemoryRunLog` accumulate one entry per
day forever) came from this prompt and produced `purge_history()`.

---

## 2. Suggestions rejected

Every one of these was proposed or is the default answer for this task. Each was rejected
for a stated reason.

| # | Suggestion | Why it was rejected |
|---|---|---|
| 1 | Use **APScheduler** or the `schedule` library | The scheduling design *is* the thing under review. A dependency replaces it with somebody else's abstractions and leaves nothing to evaluate. The brief explicitly allows "a simple implementation". Zero runtime dependencies is also a real maintenance asset. |
| 2 | One **`threading.Timer` per task** | Does not survive contact with scale (10k timers), leaks threads on reschedule, and is effectively untestable without sleeping. A polling tick is boring and correct. |
| 3 | A **faithful port** keeping `executed` on the user | Correct to the source, wrong to the requirement. It re-creates defect #1 inside prettier classes — the most dangerous possible outcome, because the structure now implies the logic was reviewed. |
| 4 | **`if/elif` dispatch** on `task.action` | Closed for extension. Every new action edits the same function, so every squad conflicts in the same place and the scheduler is re-tested for a change that is not its concern. |
| 5 | A **plugin autoloader** via `importlib` / entry points | The opposite failure. Real extensibility is the registry; autoloading adds a layer of indirection, import-order bugs, and a debugging story nobody needs at three executors. |
| 6 | **async/await** throughout | There is no I/O-bound work here — the executors simulate. Async would add colour-function constraints and a harder debugging story to buy nothing. The scaling answer is a worker pool, not an event loop. |
| 7 | A **`reset_quotas()`** method called at midnight | Correct until the process restarts at 23:59, or the job fails, or the timezone shifts. Keying the ledger on `(user_id, date)` makes the reset *structural*: there is no counter to reset, so there is nothing to forget. |
| 8 | **`datetime.now()`** inside `Scheduler` | Makes every scheduling test either sleep or monkeypatch. An injected `Clock` is ten lines and the entire test suite runs in 0.3s with no flakes. |
| 9 | **`logging.basicConfig()`** at package import | A library that configures the root logger hijacks the logging of every program that imports it. `configure_logging()` exists and only `main.py` calls it. |
| 10 | **Fail-on-first** config validation | Three typos become three deploy cycles. Accumulating problems costs about ten lines and is the difference between a config format a growing team tolerates and one it resents. |
| 11 | A **mutable `Task`** dataclass | An executor could rewrite a task mid-run and nothing would catch it. `frozen=True` plus `MappingProxyType` on `params` makes shared state safe by construction. |
| 12 | A **`src/` layout** | Correct for a published library, friction here: a reviewer would need `pip install -e .` before `pytest` runs. A flat package plus a shipped `pyproject.toml` gets both. |

Rejections 3 and 7 are the ones worth discussing in an interview: both are cases where
the AI's output was *internally consistent and well-written*, and wrong only against a
requirement stated in one word — "daily".

---

## 3. Where the AI was wrong, and what caught it

Review did not catch these. Tests and tools did. That distinction is the actual argument
for not relying on AI output.

| Defect in generated code | Found by | Consequence if shipped |
|---|---|---|
| **`--dry-run` consumed real quota and marked tasks as run** | Writing `test_dry_run_leaves_no_trace` | A rehearsal would silently cause the real run to skip everything it had just previewed. Fixed in `Scheduler._execute_one`. |
| **`main.py` reached into `scheduler._quota`** | Reading the demo output critically | A private attribute in the one file a reviewer reads first. Fixed by adding read-only `quota` / `registry` / `run_log` / `clock` properties. |
| **`match="users.alice"` in a test** | `ruff` (RUF043) | `.` is a regex metacharacter, so the assertion was weaker than it read — it would have passed on `users_alice` or `usersXalice`. Now `r"users\.alice"`. |
| **`pytest.raises(Exception)`** for immutability | `ruff` (B017) | Would have passed on *any* error, including a typo in the test itself. Now asserts `dataclasses.FrozenInstanceError`. |
| Unused `Sequence` import, unsorted imports, a Yoda condition | `ruff` (42 auto-fixed) | Noise, but noise is what a reviewer's attention gets spent on instead of logic. |

The pattern is consistent: the model is strong at structure and weak at the second-order
consequences of a flag it added three files earlier. The `--dry-run` bug is the clean
example — a correct feature, correctly implemented in the executors, with a side effect
in the scheduler that nothing in the feature's own code reveals.

---

## 4. How correctness was verified independently

Not by asking the model whether its code was correct.

1. **A test per claimed fix.** Each of the five defects has a named test that fails
   against the old behaviour. The claim "this fixes the daily reset" is only worth
   something attached to `test_quota_resets_the_next_day`.
2. **A runnable demonstration.** `python main.py` walks through five scenes, each one a
   legacy defect, with a frozen clock so the output is identical everywhere. A reviewer
   can see the behaviour without reading a line of the implementation.
3. **A linter with opinions.** `ruff` with `E,F,I,UP,B,SIM,RUF` found 64 issues, two of
   which were real test weaknesses rather than style.
4. **The documentation was generated from the code, not from memory.** The dependency
   graph in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) was extracted by walking the
   real imports with `ast`, specifically so the diagram cannot drift into describing a
   system that was merely intended.
5. **Deterministic tests.** 124 tests, no `sleep`, no wall-clock dependency. A suite that
   is flaky is a suite nobody trusts enough to act on.

---

## 5. What I would do differently

- **Write the test before accepting the implementation**, not after. Three of the five
  defects above were found while writing tests; the dry-run one would have been found
  earlier if the test had come first.
- **Give the adversarial-review prompt a narrower target.** "Find problems" returned both
  the unbounded-growth finding and a page of generic advice. "Find every place state is
  mutated and say who else can see it" would have returned the first without the second.
- **Ask for the rejected alternative earlier.** It became a habit by the design stage; at
  the analysis stage the first answers were accepted too readily.

---

## 6. Answers to the brief's two AI questions, in one line each

**How did you design your prompts?** Four narrow stages instead of one broad request,
with analysis separated from design — because a model asked to refactor will faithfully
refactor the bugs too.

**Did you reject suggestions?** Twelve, tabled above. The three that mattered: a
third-party scheduler (it would hide the design being evaluated), a thread per task (does
not scale, cannot be tested), and a faithful port that preserved the lifetime quota
counter (right to the source, wrong to the requirement).
