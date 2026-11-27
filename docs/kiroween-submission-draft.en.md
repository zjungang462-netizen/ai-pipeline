# AI Pipeline · Kiroween submission draft

## One-line pitch

AI Pipeline turns coding agents' output into a reviewable change by running real tests, routing failures back to coding, and recording the evidence before a human approves delivery.

## What it does

A developer can assemble a workflow from model APIs, local coding CLIs, ordinary test commands, review stages, and approval gates. The engine archives each attempt with its change, test result, and review artifact. If a test fails, it can return the failure to the coding stage within a configured retry limit. A developer can inspect the attempts in a local workbench and resume from recorded checkpoints.

The zero-key demo uses scripted adapters to give every reviewer the same reproducible case. A first implementation returns the input unchanged. Real Python assertions fail and the engine records the feedback. A second implementation normalizes the string, and the same assertions pass. A separate scripted AST check writes a structured verdict before the workbench asks for human approval.

## How to try it

Extract the judge ZIP and open `START-HERE.md`. Python 3.11 or later and Git must be on `PATH`. In the archive root, run the command below to verify the whole demo. The English guide in the ZIP also covers launching the workbench and approving the run in the browser.

```bash
python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py
```

## How Kiro fits

The repository contains Kiro specs for the engine, evidence contracts, adapter layer, and workbench. Its steering file captures repository rules. The `PostFileSave` hook calls the same offline demo check used by CI after a matching file is saved by the agent. The hook command has been verified from the repository root. A screen recording of the IDE hook firing can be added after that behavior is observed in Kiro.

## English recording script · 2 minutes 40 seconds

| Time | Screen | Voiceover |
| --- | --- | --- |
| 0:00–0:25 | English workbench home and demo launcher | “AI Pipeline is for developers who need to verify a coding agent's work before accepting it. For this demo, `normalize` should turn ` A  B ` into `a-b`. The offline adapters make the same failure and repair reproducible without any account.” |
| 0:25–1:10 | First test failure and feedback event | “The first implementation returns the input unchanged. The Python test command exits with code 1. The workbench records the failed assertion and the engine sends that result back to coding.” |
| 1:10–2:00 | Second coding diff, passing test record, and structured verdict | “The second attempt changes the function. The same command exits with code 0. The scripted reviewer checks the source structure and saves its verdict independently of the coding adapter.” |
| 2:00–2:40 | Approve, confirm five completed stages, and show the Kiro spec and hook | “I can inspect the evidence before approving. The run keeps both attempts and the approval in its history. The repository includes the Kiro spec, steering rules, and a hook that runs this same verification command.” |

Add the public repository, judge ZIP, and demo video URLs after publication. Before sending, test all three links from a browser without the maintainer's login.
