# Incident Agent

A small Python project for learning how an AI agent investigates a question:
**“Why did checkout become slow?”**

The program gathers metrics, logs, and traces, lets a model choose useful next
steps, and produces a report with evidence. It uses ordinary Python functions,
not an agent framework.

For the concepts, read [Building an AI Agent](https://theobservable.one/articles/building-an-ai-agent/).

Start with the offline example below. You can add a real model or a local
telemetry lab when you're ready.

## Quick start

You need **Python 3.11+**. The project is developed and tested on Linux.
This first example needs **no Docker, API key, or paid model calls**.

```sh
git clone https://github.com/lalitb/incident-agent.git
cd incident-agent

python3 -m venv .venv
.venv/bin/python -m pip install .

.venv/bin/python -m evaluations.full_loop runs/first-look \
  --mode adaptive --case known_pool_exhaustion
```

This runs an investigation from collection through reporting using synthetic
telemetry and scripted model responses. It is a way to see the program work,
not a test of an actual model's reasoning.

Open `runs/first-look/evaluation.json` for the outcome and the location of its
saved evidence, decisions, and report.

## Try a real model

Create a local configuration file if you do not already have one:

```sh
test -f .env || cp .env.example .env
```

Edit `.env` to set `LLM_MODEL` and its matching API key. The template includes
Kimi, Gemini, OpenRouter, and OpenAI options. `.env` is git-ignored; keep your key
in that file, not in source code. Existing environment variables take precedence.

Then run one adaptive investigation with your configured model:

```sh
.venv/bin/python -m evaluations.full_loop runs/first-model \
  --mode adaptive --case known_pool_exhaustion --actual-model
```

Telemetry still comes from the same synthetic example, so Docker is not needed.
The difference is that the model now chooses the queries and writes the report.

Model requests use your provider account and may incur charges. The program
limits calls, but this command does not enforce a dollar spending cap or change
your provider's billing settings.

## How it works

**Observe → choose a next step → validate → collect → repeat.**

The model proposes a query. Python checks the tool, service, time window, and
remaining budget before running it. Results become evidence for the next
decision. A bounded review can identify a missing check before reporting.

An adaptive run allows up to six planning calls, eight telemetry attempts,
one review, and one report call. A temporary query failure can receive one
budgeted retry. Progress is saved, and unanswered checks stay visible.

There is also a **fixed collection** mode: Python chooses the query sequence
instead of the model. Comparing the two helps show what adaptive selection adds.

Queries are read-only. Approval is a separate **local simulation**, not an
infrastructure change. Python checks report structure and measurements; causal
explanations still need review.

## Read a saved run

Each run keeps its work in a directory:

| File | What it contains |
| --- | --- |
| `evidence.json` | Collected observations, query attempts, and failures |
| `controller.json` | Decisions, verification checks, limits, and outcome |
| `report.json` | The report, when reporting succeeds |
| `checkpoint.json` | Saved progress for supported adaptive resumes |

To inspect a run, use the directory listed in its evaluation summary:

```sh
.venv/bin/python -m agent.evaluate "path/to/run-directory"
```

For concrete examples, read the [saved actual-model investigations](examples/postfix-evaluation-20260930/README.md).
One identified a supported pool-wait explanation; another stopped with
insufficient evidence. Both are useful learning outcomes. Those examples use
synthetic telemetry, not live backends.

## Explore further

- **New to telemetry?** Read the [short observability introduction](prerequisites/README.md).
- **Want real metrics, logs, and traces?** Follow the [local telemetry lab](docs/telemetry-lab.md).
- **Want to follow the code?** See the [investigation reference](docs/investigation.md), including resume, reporting, and limits.
- **Want to compare scenarios?** Use the [evaluation guide](evaluations/README.md).

Run the offline tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The goal is to make agent behavior understandable and inspectable. A report that
clearly says what remains unknown can be more useful than a confident guess.
