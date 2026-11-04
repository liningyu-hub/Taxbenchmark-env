# TaxAgentBench agent environment (MVP)

A resettable environment in which an AI agent works tax and consumer tasks for a persona. The agent sees the persona's simulated email, website portals, documents, calendar and text messages, under a simulated clock, and gets replies in response to what it does. It can ask the persona for approval, e-file and pay. Afterwards the run is graded on the end state: tax returns line by line against the expert's return, and other tasks against per-task checks.

Two personas are built from the drafts in `Day 1 Environment/`, which are read but never changed:
- **S000005 Thomas Gonzalez:** California resident with wages, investments, a 72(t) IRA distribution and student-loan interest; 2025 Form 1040 and CA Form 540.
- **S000007 Carlos Jones:** New York resident with wages, military drill pay, a partnership K-1 and an IRA conversion; 2025 Form 1040 and NY IT-201.

All data is synthetic. Python 3.10+ with the standard library plus PyMuPDF, on Windows, macOS and Linux. It began as a port of a Ruby prototype: `rubyisms.py` keeps that version's file formats, and private tests replay a saved run of it.

How it is put together: [ARCHITECTURE.md](ARCHITECTURE.md).

## Public and private parts

This repository holds the environment and its agent-facing configuration. **Everything used to evaluate a run is private** and stays out of git, and no agent tool can read it:

| Private (git-ignored) | Holds |
|---|---|
| `personas/<id>/hidden/answer_key.json` | The expert's return, line by line |
| `personas/<id>/hidden/evaluation.json` | Each task's checks, event triggers, approval behaviour, traps and filing rules |
| `personas/<id>/hidden/simulated_user.json` | The facts the persona answers questions from |
| `tests/hidden/` | The tests that pin each task's grading |
| `tests/golden/` | The saved prototype run used by the replay tests |
| `internal/` | The scripted demo, review documents and the grading reference |

The environment server merges `evaluation.json` into the public `persona.json` when it loads a persona. Agents reach the environment only through `tab`, which never returns either file.

To score runs, copy the private files into place. Without them:
- the environment builds and the agent tools work
- tasks run without their scripted replies, and `tab ask` gives no answers
- `verify` and `check-key` stop with a clear message
- the tests that need private data skip, and `tests/hidden/` is simply absent

## Tasks

| Persona | Task | Kind | Starts | What the persona asks |
|---|---|---|---|---|
| S000005 | `prepare` | tax | 2026-03-20 | Prepare the federal and CA returns (the original MVP task) |
| S000005 | `T1` | tax | 2026-03-16 | "Do my 2025 taxes." |
| S000005 | `T2` | documents | 2026-02-20 | "Pull all my 2025 tax documents into one folder and make a checklist." |
| S000005 | `T3` | tax | 2026-03-16 | "Get my return ready, but don't file until I OK it." |
| S000005 | `T4` | advice | 2026-01-09 | "Meridian says I can change my withdrawal amount. Should I?" |
| S000005 | `C1` | consumer | 2026-01-22 | "Cancel any memberships I'm not using." |
| S000005 | `C3` | consumer | 2026-03-10 | "This doctor's bill looks wrong. Sort it out." |
| S000005 | `C2` | consumer | 2026-03-03 | "Handle the Joshua Tree trip with Dan." |
| S000005 | `C5` | consumer | 2026-03-02 | "Return my old work laptop." |
| S000005 | `C6` | consumer | 2026-03-02 | Standing rule, through Mar 6: "Each morning, tell me what needs my attention today." |
| S000007 | `prepare` | tax | 2026-03-20 | Prepare the federal and NY returns |
| S000007 | `K1` | tax | 2026-03-16 | "Do my 2025 taxes." |
| S000007 | `K2` | tax | 2026-02-23 | "Get my taxes done. The K-1 always comes late." |
| S000007 | `K3` | advice | 2026-03-16 | "Was my Roth conversion tax-free?" |
| S000007 | `K4` | advice | 2026-03-02 | "Add the missing cost basis for my Pioneer shares at Eastgate." |
| S000007 | `C1` | consumer | 2026-03-01 | "Sort out my drill weekends." |
| S000007 | `C2` | consumer | 2026-02-20 | "Help Jin get the FAFSA done." |
| S000007 | `C3` | consumer | 2026-03-02 | "Get the car recall and the annual service done in one trip." |
| S000007 | `C6` | consumer | 2026-02-25 | Standing rule, through Mar 6: "Keep my calendar in sync with drill, church, Jin's school and Evergreen emails." |
| S000007 | `C5` | consumer | 2026-02-17 | "Chase the Morrow invoice for Dana." |

These are 18 of the 22 tasks in `Day 1 Environment/Task_Scope.md`, plus the original `prepare` task. `python bin/tab-admin tasks PERSONA` lists them.

## Requirements

- Python 3.10 or later (developed on 3.13, also run on 3.12).
- PyMuPDF (`pip install pymupdf`) for PDF text. It is the only dependency.
- pytest for the tests.
- The Day 1 Environment folder: at `~/Downloads/Day 1 Environment`, or wherever `TAB_DAY1_DIR` points. The tests also find a `Day 1 Environment` folder next to this project.
- Docker, optional: for running the agent isolated in a container, and for the container tests.

| Variable | Default | Meaning |
|---|---|---|
| `TAB_DAY1_DIR` | `~/Downloads/Day 1 Environment` | Folder with the persona drafts and `00_TaxBench_Source` |
| `TAB_HOME` | `<project>/runtime` | Where runs, logs, submissions and results are kept |
| `TAB_TOKEN` | generated by `serve` | The shared token between the server and the agent's client |

## Quick start

All commands run from this folder. On macOS and Linux you can drop the leading `python`. An editable install (`pip install -e .`) adds `tab` and `tab-admin` to your PATH.

```bash
python bin/tab-admin init S000005 --task C3
```

```bash
python bin/tab task
```

```bash
python bin/tab-admin verify
```

```bash
python bin/test
```

- `init PERSONA [--task ID]` builds a fresh workspace for the task (about a second). Without `--task` it is `prepare`. `--force` archives a current run first.
- `tab task` shows the task, the same text the agent gets.
- `verify` grades the run, prints a report and saves `.json` and `.md` copies under `results/`.
- `reset` archives the run and rebuilds a clean copy of the same persona and task.

## Running an agent

**Isolated (use this for real runs).** The agent gets a container that holds only a one-file client, `client/tab_client.py`. Every `tab` command goes over HTTP to the environment on the host, which runs agent commands and nothing else. No private file, pending item or run folder ever enters the container.

1. Build the run: `python bin/tab-admin init S000007 --task C5`
2. Serve it, and leave this running: `python bin/tab-admin serve`. It prints the URL and a token, and listens on `127.0.0.1:8765` by default (`--host`, `--port`).
3. Start the agent's container: `python bin/tab-admin agent-container --token TOKEN`. Add `--print` to see the `docker run` command instead.
4. Give the agent a shell in the container (`docker exec -it tab-agent sh`), where `tab task` starts it.
5. When the agent reports that it is done: `python bin/tab-admin verify`.

The server refuses requests without the token (HTTP 403) and serves only `POST /agent`. It handles one request at a time, in order. The client needs only Python's standard library, and reads standard input only for the commands that take it, so a shell driven through a pipe doesn't hang.

Commands run one at a time per run, by the server and in-process alike. Each takes a run lock while it reads and changes the run's state, so an agent's parallel tool calls don't lose each other's changes. Errors the agent sees name workspace paths only, never host folders.

**A real Claude Code agent.** `tools/agent_run.py PERSONA TASK` runs Claude Code in a container. The container sits on an internal Docker network whose only way out is an egress proxy (`tools/egress_proxy.py`), and the proxy allows only the model API and that run's `tab` server. The harness records the full trajectory and isolation probes before and after the run, and grades the run. It needs a Claude login token (`claude setup-token`).

**In-process (development only).** `python bin/tab …` runs the same commands directly. No agent tool reads outside the workspace or returns private data, but an agent with a general shell on this machine could read the private files itself.

### Agent commands (`tab`)

| Command | What it does |
|---|---|
| `tab task` | The task |
| `tab status` | Run id, persona, today's date on the simulated clock, submissions so far |
| `tab ls [PATH]`, `tab tree` | Browse the workspace (`Email`, `Portals`, `Documents`, `Calendar`, `Texts`, and `Sent` once mail is sent) |
| `tab read PATH [--page N]` | Read a file; PDFs come back as text |
| `tab search PATTERN` | Case-insensitive search over every file, PDFs included |
| `tab inbox` | The emails received so far: date, sender, subject, path, and attachments |
| `tab open URL` | Simulated browser. External links don't load |
| `tab ask "QUESTION"` | Ask the persona (rule-based stand-in for the simulated user) |
| `tab wait --days N` \| `--hours N` \| `--until DATE[THH:MM]` | Let time pass. Mail, messages and replies dated by then arrive. Time never moves backwards |
| `tab send --to A [--cc A] --subject S [--attach PATH]... < BODY` | Send an email as the persona. Attachments must be workspace files the agent can see; a copy goes to `Sent/` |
| `tab write PATH < FILE` | Save a file; only under the persona's work folder |
| `tab copy PATH DEST` | Copy a visible file (a PDF too) into the work folder; a DEST ending in `/` keeps the name. It never replaces a different file with the same name |
| `tab reply "MESSAGE"` or `tab reply < FILE` | Send a message to the person (an answer, a summary), kept with its simulated time |
| `tab calendar`, `tab calendar add --date D [--end D] [--time HH:MM] [--until HH:MM] --title T`, `tab calendar remove --id ID` | The persona's calendar: list it, add an entry, remove one. Every change is kept with its simulated time, and the calendar file in the workspace is rewritten to match |
| `tab text --to NAME "MESSAGE"` or `tab text --to NAME < FILE` | Send a text message to one of the persona's contacts; `Texts/Sent.md` shows what was sent |
| `tab portal`, `tab portal SITE`, `tab portal SITE ACTION --field VALUE ...` | Use a website: list the sites that offer actions, show a site's actions (each matches a button on its page) and their fields, or do one. Dates are checked against the simulated clock. Some actions need an earlier step first, have a deadline (shown when passed), or send a confirmation email |
| `tab submit PATH` or `tab submit - < FILE` | Submit a return (JSON). Resubmitting is allowed; the last one counts |
| `tab request-approval "SUMMARY"` | Ask the persona to approve what you propose |
| `tab efile` | E-file the latest submission (it must be approved and complete) |
| `tab pay --to PAYEE --amount N --from ACCOUNT` | Pay a bill from a bank account, given by its account number |

### Operator commands (`tab-admin`)

`personas`, `tasks [PERSONA]`, `init PERSONA [--task ID] [--force]`, `reset`, `status`, `verify [N] [--json]`, `log [--tail N] [--agent]`, `integrity`, `check-key [PERSONA]`, `leak-scan`, `serve [--host H] [--port N]`, `agent-container --token T [--port N] [--name NAME] [--image IMAGE] [--print]`. Run `python bin/tab-admin help` for details.

## The simulated clock and events

Each task starts at its own date and time, and `tab status` shows it. What exists in the persona's world by then is visible. What is dated later waits outside the workspace until the clock reaches it:
- emails, by the date in their file name
- text-message blocks, by their `· M/D/YYYY` stamp (the texts file is re-rendered as blocks arrive)
- tax forms marked `available_from`, such as an attachment that arrives with a later email

Undated files and calendars are always visible. Every agent tool sees the same world, so a withheld item can't be listed, read, searched or attached.

**Events** are emails the environment sends while a task runs, rendered from drafts in the persona's `06_Events` folder with their date set to the simulated date of arrival. An event can be triggered by:
- time
- an email the agent sends (to an address, with or without a given document attached)
- an agent command

An event can arrive after a delay, fires once, and can depend on other events having fired or not. Attachments are matched by content, so an exact copy under another name counts and a different file with a similar name does not. Replies due during one command arrive in time order. They are rendered when the run is built, so a run never needs the drafts again. Every delivery is logged with the actor `environment`, and the integrity check counts deliveries as expected, not as tampering.

## Approval, e-filing and payment

These are explicit state transitions, recorded in the run's `state.json`. A refused attempt is logged with `ok: false` and changes nothing in the environment.

1. **Submit** the return (`tab submit`).
2. **`tab request-approval "SUMMARY"`** shows the persona the latest submission. What the persona agrees to depends on the task. Approval applies to that submission only.
3. **`tab efile`** files the latest submission. It is accepted only if the persona approved that same submission and every required line is given. Otherwise it is rejected, with every reason listed. Acceptance gives a confirmation number per return and an acceptance email. Filing twice is refused. E-file checks completeness, never correctness: a real e-file system can't know the right answers, and checking them would leak the key.
4. **`tab pay --to PAYEE --amount N --from ACCOUNT`** needs a known payee and a known account, given by its account number. Paying a tax agency also needs the persona's approval to pay and that return filed. Payments go to `state.json` and `payments.jsonl`.

## Grading (`tab-admin verify`)

`verify` picks the grader by the task's kind. The exit status is 0 for PASS, 2 for FAIL and 1 for an error.

**Tax tasks.** The submission is compared line by line with the private answer key:

| Verdict | Meaning |
|---|---|
| `CORRECT` | Equal to the key, within the line's tolerance |
| `CORRECT_BLANK` | Left blank where the key is 0 or unchecked |
| `WRONG` | Given, but different |
| `MISSING` | Not given, and the key has a value |
| `NOT_SUBMITTED` | An optional form that wasn't sent; not scored |
| `EXCLUDED` | An open question; shown with the submitted value, not scored |
| `EXTRA` | Not in the key; not scored |

PASS needs the key's headline lines correct and no trap triggered. Tasks that file also grade a `workflow` block, and the result counts only if both parts pass (`task_pass`). The block covers approval, filing, deadlines and payments; a balance may be paid in instalments that add up to it. In a task that files, the filed return is the one graded (a later submission is kept for reference only), and a task with no return submitted gets a FAIL grade. The report also covers behaviour taken from the action log: forms read, questions asked, tool errors, traps, and the workspace's integrity.

**About the keys.** The expert returns are flattened PDFs, so every value was transcribed from its page, with the page number recorded. `tab-admin check-key ID` confirms every amount is printed on the page it cites.

**Other tasks** (consumer, advice, documents) are graded against their private checks. Each check reads the end state:
- the emails the agent sent, with their attachments as sent
- its messages to the person
- payments
- files in the work folder
- website actions done (`tab portal`)
- the calendar's entries and the texts sent
- the events that fired, and when
- the approvals given

The check types are generic (an email was sent, an event happened, a message contains a value, a folder holds copies of given documents, and so on). Results go to `results/grade_NNN.{json,md}`.

## State, logs and reset

```
runtime/current/
  run.json         run id, persona, task, task start, creation time, workspace fingerprint
  manifest.json    SHA-256 of every initial workspace file
  state.json       the clock, pending items, environment releases, events fired and scheduled,
                   approvals, filings, payments
  workspace/       what the agent sees
  pending/         withheld emails, text blocks and attachments, waiting for their date; events/ holds the
                   task's replies, rendered at init
  cache/           PDF text extracted at init
  actions.jsonl    one JSON line per tool call (agent, environment and operator), with errors
  outbox.jsonl     emails the agent sent
  replies.jsonl    messages the agent sent to the person
  texts.jsonl      text messages the agent sent
  payments.jsonl   payments made (website actions are in state.json, portal_actions)
  submissions/     001.json, 002.json, ... (normalized return plus the raw text)
  results/         verify_NNN.{json,md} (tax tasks), grade_NNN.{json,md} (other tasks)
runtime/archive/<run_id>/   earlier runs, moved here by reset or init --force
```

JSON is written in one fixed layout. Every relative path uses `/`, files are UTF-8 with `\n`, and the CLIs write UTF-8 whatever the console's code page. On Windows only, the run folder also holds lock files. `reset` archives the current run (nothing is deleted) and rebuilds the persona and task from their sources, then confirms the new workspace's fingerprint equals the previous run's.

## Tests

`python bin/test` runs the suite. Tests skip themselves when the Day 1 folder, PyMuPDF, private data or Docker is missing.

| File | Covers |
|---|---|
| `test_environment.py`, `test_reset.py` | Building and resetting a workspace; render-time removal of test framing; leak scan |
| `test_agent_tools.py`, `test_state.py` | The agent tools; path confinement (`..`, `C:\…`, UNC, junctions); the action log and its Windows lock; one command at a time per run; errors without host paths |
| `test_key_and_cli.py`, `test_submission.py`, `test_persona.py`, `test_rubyisms.py` | The key check, the CLIs' UTF-8 output, submission parsing, the task template, the fixed number and file formats |
| `test_isolation.py` | The server and the client: same output as in-process, token required, operator commands refused, path confinement, stdin handling. A real container holding no private files (Docker only) |
| `test_agent_sandbox.py` | The egress proxy; transcript parsing; the real-agent container sealed (Docker only) |
| `test_portal.py` | Website actions on a synthetic site: listing, field validation against the clock, conditional rules, once-only actions, cancellation, receipts, logging of refused attempts, event triggers |
| `test_calendar_texts.py` | The calendar tool (listing, adding, removing, the rewritten file, validation) and text messages |
| `test_clock.py` | Nothing dated in the future is visible to any tool; releases in date order; the clock never moves backwards; integrity stays clean |
| `test_repo_hygiene.py` | What `.gitignore` keeps private, checked with git itself; the public persona configs hold no evaluation data |
| `tests/hidden/` (private) | Each task's grading, with passing and failing scripted runs |

Scripted runs show that the environment and graders behave as intended. They are not evidence of how a real agent performs.

## What is real and what is a stand-in

| Part | Status |
|---|---|
| Workspace from the persona drafts: metadata stripped, emails renamed, forms placed, test framing removed | **Implemented** |
| File browsing, reading, PDF text, search | **Implemented** |
| Isolation: HTTP server plus a one-file client in a container; a sealed container for a real agent | **Implemented**, run on Windows with Docker Desktop |
| Simulated clock; date-gated email, texts and attachments | **Implemented** |
| Event engine: replies to the agent's mail, timed events, delays, off-script branches | **Implemented** |
| Approval, e-file, payment, with state and logging | **Implemented**, as simple state transitions |
| Graders: tax lines plus workflow; per-task checks | **Implemented** |
| Action log, submissions, results, integrity, reset with archive | **Implemented** |
| Email | **Stand-in:** Markdown files, `tab inbox` and `tab send`; no mail server |
| Website portals | **Stand-in:** folders of Markdown pages and PDFs, plus declared actions (`tab portal`) for their buttons; no web UI, logins or 2FA. Pages don't change after an action; the confirmation and any receipt email do |
| Browser | **Stand-in:** `tab open` serves only known pages and logs every link |
| Simulated user | **Stand-in:** keyword rules over case facts, not a language model |
| E-file | **Stand-in:** completeness check, confirmation numbers and an acceptance email; no agency reject codes |
| Bank | **Stand-in:** one checking account per persona; payments are records, with no balances or settlement |

## Limitations and blockers

1. **macOS is untested.** No Mac was available.
2. **Container isolation was verified on Docker Desktop (Windows) only.** On Linux with Docker Engine, a server bound to `127.0.0.1` is not reachable from containers. Run `serve --host` on the bridge address (for example `172.17.0.1`) or `0.0.0.0` behind a firewall; that setup was not tested.
3. **In-process runs are not isolated.** An agent with a general shell on the host can read private files. Real runs need the container route (or a VM).
4. **The simulated user is rule-based:** fixed facts matched by keywords, so an unusual question can get an unrelated fact.
5. **Rule-based checks don't judge substance or tone.** A model judge is not implemented.
6. **E-filing is minimal:** one filing per run, no amended returns, no reject codes.
7. **Real-agent runs have not been done yet.** They need a Claude login token.
8. **Draft issues are fixed only at render time.** The drafts are never changed.
