# Architecture

The environment turns TaxAgentBench persona drafts (S000005 Thomas Gonzalez, S000007 Carlos Jones) into a world an agent works in, runs tasks over simulated time, and grades the end state. It is Python, the standard library plus PyMuPDF, with two command-line front ends and an HTTP front end for isolated agents. The core is a module-for-module port of the Ruby prototype (backup: `TaxAgentBench-Env.zip`). The clock, events, workflow, check-based grading and isolation were added on 2026-11-04.

```
 Day 1 Environment/<persona>/                        (read only, never modified)
   01_Email … 05_Texts ───────────────┐
   06_Events/*.md ────────────────────┼──────────────► init renders replies into pending/events/
   00_TaxBench_Source/Reference Files ┤
   00_TaxBench_Source/Final Return ───┼──► check-key (operator only)
                                      │
 personas/<id>/                       │
   persona.json  (public: folders, forms, output spec, accounts, payees, filing mechanics,
                  each task's kind, start and template) ──────────────┤
   task*.md.tmpl (the agent's task text) ─────────────────────────────┤
   hidden/evaluation.json  (private: checks, event triggers, approval, traps, filing rules) ──┤ merged at load,
   hidden/answer_key.json  (private) ─────────────────────────────────┼──► Verifier   server-side only
   hidden/simulated_user.json  (private) ─────────────────────────────┘──► `tab ask` (answers only)
                          │
            tab-admin init PERSONA --task ID / reset
                          ▼
 runtime/current/
   workspace/ ◄── agent commands ──► actions.jsonl
   pending/   (withheld until their date)
   state.json (clock, releases, events, approvals, filings, payments)
   outbox.jsonl, payments.jsonl, submissions/NNN.json
                          │
          tab-admin verify ──► Verifier + workflow grade ──► results/verify_NNN.{json,md}   (tax)
                           └─► consumer Grader ────────────► results/grade_NNN.{json,md}    (consumer)

 Isolated agent:  container [ tab = client/tab_client.py ] ──HTTP POST /agent + token──► tab-admin serve
                                                                                         (runs cli.agent only)
```

## Components

| Component | File | What it does |
|---|---|---|
| Configuration | `config.py` | `TAB_HOME` (where runs live) and `TAB_DAY1_DIR` (where the drafts are) |
| Persona registry | `persona.py` | Loads `persona.json`: tasks (`prepare` always exists), accounts, payees, filing; renders a task's template. The only module that reads `hidden/`. |
| Initialization | `renderer.py`, `environment.py` | Builds the workspace in a staging folder and moves it into place. Withholds future-dated items into `pending/`; writes `state.json` with the task's start time. |
| Clock | `clock.py`, `state.py`, `timeline.py` | Date parsing and formats; the run's `state.json`; moving the clock forward and delivering due items (`advance`, `release_due`, `deliver`) |
| Event engine | `events.py` | After every agent command, fires the task's events whose trigger matches, once each, immediately or after a delay. Replies are rendered into the run at init. |
| Agent tools | `agent_tools.py` | `task`, `status`, `ls`, `tree`, `read`, `search`, `inbox`, `open`, `ask`, `wait`, `send`, `write`, `submit`. Every path is checked against real paths, so `..`, absolute, drive-letter and UNC paths, symlinks and junctions out of the workspace are refused. |
| Portal actions | `portal.py` | `tab portal`: website actions declared per site in `persona.json` (`portals`), with field validation against the clock, rules, once-only and cancelling actions, receipts; records in `state.json` |
| Calendar | `agenda.py` | `tab calendar`: the drafted calendar's rows as entries (dated rows parsed, others kept as written), additions and removals in `state.json`, the workspace file re-rendered after each change |
| Workflow | `workflow.py` | `request-approval`, `efile`, `pay` as state transitions; `grade()` for the workflow block |
| Simulated user | `sim_user.py` | Rule-based answers from `hidden/simulated_user.json`; each fact cites its source |
| Submission | `submission.py` | Validates and normalizes the JSON return |
| Tax grading | `verifier.py`, `behavior.py`, `report.py` | Line verdicts against the key, headline lines, behaviour from the log; for filing tasks, the workflow block and `task_pass` |
| Check grading | `consumer.py` | Judges a non-tax task's checks against the end state: outbox, replies, payments, work folder, fired events |
| Real-agent harness | `tools/agent_run.py`, `tools/egress_proxy.py`, `tools/agent_image/` | Claude Code in a sealed container, an egress proxy, trajectory recording and isolation probes |
| Key check | `key_check.py` | Each key amount must be printed on the page it cites, and the key must cover exactly the lines asked |
| Isolation | `server.py`, `client/tab_client.py` | `POST /agent` with a token runs `cli.agent` only; the one-file stdlib client forwards argv and stdin |
| CLIs | `cli.py`, `bin/tab`, `bin/tab-admin` | `tab` is the agent's interface (every call logged); `tab-admin` is the operator's |
| Ruby compatibility | `rubyisms.py` | Ruby's rounding, JSON layout, line splitting, regex dialect and globbing, so files match the Ruby version |

All modules are under `src/tab/` unless a path is given.

## How the workspace is built (`tab-admin init PERSONA --task ID`)

1. Copy `01_Email` … `05_Texts` to `Email`, `Portals`, `Documents`, `Calendar` and `Texts`, skipping hidden files. Drafts are read as raw bytes, so CRLF drafts fail the leak scan instead of being normalized.
2. In every Markdown file, remove reviewer-only metadata: the SAMPLE banner, `_Item N · …_` lines, `(= TaxBench DOCn)`, the `_Supports: …_` footer, and test framing (` (simulated)`, ` (fictional)`, `(the agent's working folder)`).
3. Rename emails to `<date>_<subject>.md`, as a mail client lists them; the draft file names label the traps.
4. **Gate by the task's start date.**
   - An email dated after it goes to `pending/` with its release date.
   - A texts file keeps only the blocks dated up to it; its full text waits in `pending/texts_full/`.
   - A tax form with `available_from` later than the start waits in `pending/`. Its text is cached now, so reading it later needs no PDF work.
5. Copy each tax form PDF to where the persona's world shows it: a portal page, or `Email/attachments/<email>/` for a form sent by email. Check that the PDF names the right payer, then cache its text.
6. Create the agent's work folder.
7. Leak scan: refuse to finish if any reviewer marker or framing survives, or any file name contains phishing, trap, TaxBench, answer, expert or guide.
8. Render the task's event replies (reviewer lines and release notes removed) into `pending/events/<id>.md`, and leak-scan them. The run never reads the drafts again.
9. Write `manifest.json`, `run.json` (with the task id, start and fingerprint) and `state.json`.

The build is deterministic: the same sources and task always give the same fingerprint, on every OS. `00_TaxBench_Source`, `06_Events` and the README files never enter the workspace.

## The run's state (`state.json`)

| Field | Meaning |
|---|---|
| `now` | The simulated clock (`YYYY-MM-DDTHH:MM`), starting at the task's start |
| `pending` | Withheld items: path, kind (`file` or `texts`), release date, source in `pending/` |
| `released` | Paths the environment wrote after init, with their SHA-256. Integrity expects the manifest plus these, so deliveries don't count as tampering. |
| `fired`, `fired_at` | Events that happened, with when, which email, and how many emails the agent had sent by then. Graders use the count to tell "before" from "after". |
| `scheduled` | Events triggered with a delay, and when they arrive |
| `approvals` | Each request: time, the submission it approves, grants (`file`, `pay`), the agent's text |
| `filings` | The accepted filing: time, submission, a confirmation and balance due per return, the acceptance email |
| `payments` | Each payment: time, payee, amount, account, return, confirmation |

Runs made before the clock existed (the saved Ruby run) have no `state.json` and read as their start time.

## One agent command, step by step

0. `cli.agent` takes the run lock (`<run>/agent.lock`, through the same cross-platform lock as the action log), so commands on one run never interleave.
1. It runs the tool. A refusal raises an error: it is logged with `ok: false`, and nothing changes. The agent sees OS errors with workspace paths only.
2. The call is logged to `actions.jsonl` with its arguments and a detail record.
3. The event engine looks at the command and the clock:
   - It fires each matching trigger once: an email sent to the address, with or without the attachment, or a matching command.
   - It schedules delayed events, and delivers those whose time has come.
   - Each delivered email is logged with the actor `environment` and recorded in `released`.
4. Arrivals are appended to the command's output as `New email: <path>`.

`tab wait` moves `now` forward. It delivers pending items in date order, then the engine delivers events that came due.

## Task configuration: public and private

A task is defined in two places, merged by `Persona.load` on the server:

- **`persona.json` → `tasks.<id>` (public)** holds:
  - `kind`: `tax` tasks are graded on the return (and their workflow); every other kind (`consumer`, `advice`, `documents`) is graded on its checks
  - `start`: the clock's start
  - `template`: the agent's task text
- **`hidden/evaluation.json` → `tasks.<id>` (private)** holds everything that evaluates or steers the run:
  - `events`: the replies and their triggers
  - `checks`: what is graded
  - `requires_filing` and `approval`: what the persona agrees to and says
  - `file_not_before`
- **The rest of `hidden/evaluation.json`** holds:
  - `traps`
  - `filing.<return>` rules: `due_before_penalty`, `penalty_line`, `max_penalty`, merged into the public filing entry

Event and check vocabulary, the generic part:
- **Trigger types:**
  - `time`: `after_start_hours`
  - `email_sent`: `to`, and optionally `attaches` / `not_attaches` (a workspace document matched by content) or `attachment` / `no_attachment` path patterns
  - `action`: an agent command (`tool`) and an `args` pattern
- **Event fields:** `delay_hours`; `unless_fired` / `requires_fired` order the branches. An event scheduled, or coming due in the same command, counts for `unless_fired`. Events due in one command are delivered in time order.
- **Check types:** `email_sent`, `no_payment_to`, `event_fired`, `email_after_event`, `no_email_before_event`, `no_text_before_event`, `reply` (patterns, and values read from the answer key by line id, so no answer is written in a config), `has_copies`, `folder_only`, `folder_text_mentions`, `portal_action` (fields, date limits, `only`, `latest_for`, `after_event`), `no_portal_action`, `no_email_to`, `asked_before` (an approval with a grant came before the first attempt), `any_of`, `all_of`, `calendar` (present, absent, or at most N entries matching date, end, time and title), `text_sent`. Options: `before_deadline`, `latest` and `before_action` (only what was in place when the site's first standing action of that name was done, such as a form as submitted) for portal actions; `after_portal` for emails and texts (after the first such action that still stands, ordered by the action log, since two actions can share a simulated minute); `after_event` for texts; `on` (a date) and `noise` (patterns that may only come after every required one) for replies.
- **Approvals:** from `tab request-approval` (the task's policy grants), or from a `tab ask` answer whose simulated-user fact carries `grants`. In non-tax tasks approval needs no return.
- **Approval policy:** `grants` (e.g. `file`, `pay`), `pay_from`, `reply`; `require_amounts` with `reply_missing_amounts`; `rules`, each `{"when": [patterns], "reply", "grants"}`, where the first rule whose patterns all match the request answers instead. A task without a policy grants nothing: in a tax task the persona says not to file yet, elsewhere that no OK is needed.
- **Traps:** `{"id", "tool", "match", "description"}`, matching a command's arguments. With `"match_in": "recipients"` the match is on an email's To/Cc addresses, optionally only when its text asks for `body`: a sentence matching it that doesn't negate or ask a what-if before the match ("I will not…", "Please don't…", "What would happen if I…"). With `"match_in": "sensitive_attachment"` it fires when an email to anyone but the persona attaches one of `documents`, matched by content.
- **Task deadline:** `deadline` (a date): filing and every payment must be done by then.

Public persona-level blocks:
- `calendar`: the workspace path of the calendar file; `text_contacts`: who `tab text` can reach
- `portals`: site → name and actions (`label`, `description`, `fields` with types `choice`/`multi`/`date`/`amount`/`text` (a choice that is a date can name when it `starts`; once that has passed it can't be chosen), `rules`, `once`, `requires`, `cancels`, `deadline`, `message`, `receipt`). This is exactly what `tab portal` shows the agent, and holds nothing about grading
- `accounts`: id → name, routing, number
- `payees`: id → name, aliases, `requires_approval`, `return`
- `filing`: return id → name, form, balance-due line, payee

`tests/test_repo_hygiene.py` fails if a public `persona.json` holds any evaluation field.

## Isolation model

- **Tool level:** no agent tool reads or writes outside the workspace or returns hidden material. The run folder (log, state, pending items, submissions, results) is outside the workspace.
- **Process level:** `tab-admin serve` exposes only `cli.agent` over HTTP and needs the token. `tab-admin agent-container` starts a `python:3.12-slim` container with only `client/tab_client.py` mounted, read-only, at `/opt/tab/tab_client.py`. A two-line `/usr/local/bin/tab` wrapper calls it. `test_container_has_no_hidden_files` runs a real container and checks three things:
  - `tab task` works through the client.
  - None of `answer_key*`, `simulated_user*`, `persona.json`, `actions.jsonl` or `INTERNAL_NOTES*` exists anywhere in it.
  - Reading `../../personas/S000005/hidden/answer_key.json` is refused.
- **Evaluation data:** answer keys, evaluation configs and simulated-user facts are private files under `personas/<id>/hidden/`, read only by server-side code. Rubric tests, the demo and review documents are private too (`tests/hidden/`, `internal/`).
- **Real agents:** `tools/agent_run.py` adds an internal Docker network whose only exit is an egress proxy (the model API and the run's `tab` server), with WebFetch and WebSearch disabled.
- **Not covered:** a VM image, and Linux Docker Engine networking (see the README's limitations).

## Adding a persona or a task

1. **Persona:** create `personas/<id>/persona.json`, using S000007 as the model. It sets:
   - the folder mapping and the tax-form placement table (`doc`, `source`, `place`, `expect_text`, and `available_from` for attachments)
   - the output spec, accounts, payees and filing mechanics
   - for each task: kind, start and template only
2. **Task text:** write the task templates. They are plain text with `{{name}}`, `{{tax_year}}`, `{{environment_date}}` (the task's start date), `{{writable_folder}}`, `{{id}}` and `{{output_spec}}`. The last expands, for each form, to a heading and one line per output line; put it at the very end of the file.
3. **Answer key:** write `hidden/answer_key.json` from the expert's return, with page numbers. Run `python bin/tab-admin check-key <id>` until it is clean.
4. **Simulated user:** write `hidden/simulated_user.json`, using only facts from the case guide or the persona's files, each with its source. A fact that belongs to some tasks only gets `"kinds"` (e.g. `["tax"]`) or `"tasks"` (task ids).
5. **Evaluation:** write `hidden/evaluation.json`: traps, each task's events, checks and approval policy, and filing rules. Take them from the persona's `06_Events` drafts and `Task_Scope.md`, never writing an answer value (refer to answer-key lines instead).
6. **Check:** run `python bin/tab-admin init <id> --task <task>`, then `python bin/test`. Put the task's grading tests in `tests/hidden/`.

No code change is needed for a task that uses the existing triggers and check types.
