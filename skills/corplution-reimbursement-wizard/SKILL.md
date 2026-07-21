---
name: corplution-reimbursement-wizard
description: "Corplution reimbursement workflow for identifying, extracting, allocating, writing, and packaging invoice evidence from PDFs, images, trip reports, and consultant project context into auditable process files, the reimbursement Excel workbook, and a final submission package. Use whenever the user asks for help with Corplution reimbursement or expense claims — including Chinese requests such as 报销、贴票、整理发票、填报销单、发票整理、滴滴/高德行程单、差旅费用报销 — or uploads invoices, trip reports, or travel receipts to be matched to clients and charge codes and turned into the reimbursement workbook and submission package."
---

# Corplution Reimbursement Wizard

Author: Terence Wang

## Overview

Use this skill for the Corplution reimbursement workflow:

- Stage 1: inventory source files, identify invoice and support-document types, extract canonical fields, classify the first-pass expense category, and write `process/invoice-extraction.md` plus `process/invoice-extraction.json`.
- Stage 2: read the consultant's natural-language project context, match extracted expense evidence to clients, charge codes, cities, and projects, ask targeted follow-up questions, and write `process/expense-allocation.md` plus `process/expense-allocation.json`.
- Stage 3: ask for requester, assign overall proof numbers, convert confirmed allocations into reimbursement rows, and write the final Excel workbook.
- Stage 4: package the finished workbook, renamed invoice files, and support documents into the final reimbursement submission folder.

Do not write the reimbursement Excel workbook until stage 2 has no blocking open questions. Preserve uncertain results in the review queue or allocation question queue.

Company policy numbers (meal/hotel caps, first-tier cities) live in `assets/policy.toml`; when policy changes, edit that one file. The year-coded BD/ADMIN charge codes default from `policy.toml` but are overridden by the agent/user-writable `assets/special-code-definitions.json`; at a new fiscal year run `python scripts/special_codes.py set-year 2027` (or `set --admin CORP-2027-ADMIN --bd CORP-2027-BD`) and every stage reads the new codes. That file defines only the code strings — it never changes allocation behavior (telecom still auto-maps to ADMIN; other ADMIN matters still require the applicant to name them). Process JSONs are integrity-stamped: `invoice-extraction.json` may only be changed via `apply_extraction_corrections.py`, `expense-allocation.json` only via Composer-generated answers + `apply_allocation_answers.py`, and `final-expense-rows.json` only by re-running Stage 3. Any other edit makes the next script exit with code `4` and print recovery steps — follow them instead of patching further.

Every input file is reimbursement evidence until the user explicitly says to drop it. Never skip or ignore a file because it cannot be read automatically — an unidentified file may be an invoice that needs OCR or visual reading, a partner approval screenshot, or a payment proof (paper receipt, Alipay/WeChat screenshot). Unidentified files become blocking questions; ask the user what each one is, and record an explicit exclusion (with the user's reason) for anything dropped.

## Intake Behavior

On first use in a conversation, if the user has not already provided enough invoice files and project context to start processing, read `references/opening-message.md` and send a short Chinese intake message. The message should invite invoices, trip reports, natural-language project notes, and optional special explanations, and should explain that unclear items will be confirmed through chat.

If the user already provided files or context, acknowledge what is present, ask only for missing essentials, and proceed with the relevant stage.

## Quick Start

Resolve `SKILL_ROOT` as the directory containing this `SKILL.md`. Command examples below are relative to `SKILL_ROOT`. When the task working directory is elsewhere, invoke the exact absolute path `<SKILL_ROOT>/scripts/chief_orchestrator.py`. Never create `run_chief.py`, modify `sys.path`, import `chief_orchestrator`, copy it into the task, or call `chief_orchestrator.main()`; Chief rejects wrapper launchers and prints the correct direct command.

1. In a new environment, check bundled script dependencies before running workflow scripts. The skill-local dependency file is `requirements.txt`.

```bash
python scripts/chief_orchestrator.py run dependencies
```

If Python packages are missing, install them from the skill-local requirements file:

```bash
python scripts/chief_orchestrator.py run dependencies --install
```

Use the bundled `scripts/chief_orchestrator.py` as the default workflow entry. It fills canonical process paths, dispatches the existing scripts without duplicating their business logic, preserves the child exit code, and prints one authoritative `CHIEF NEXT` result after every run. Direct script calls remain supported for developer debugging only; they are not journaled and are not a workaround for a failed Chief command.

The centralized command-exit contract is documented in `references/exit-codes.md`. In particular, `status`, `next`, and `lineage` return `0` when the query itself succeeds even if the reported workflow state is blocked; automation must inspect the reported `kind` or `integrity_blocked` field.

```bash
python scripts/chief_orchestrator.py status
python scripts/chief_orchestrator.py next
python scripts/chief_orchestrator.py next --json
```

At any point, and ALWAYS when the user asks about progress or status, run the Chief `status` command and relay its output. `next` returns exactly one of `command`, `needs_user`, `blocked`, or `complete`; it emits executable `argv` only when all required parameters are known. Never turn a `needs_user` result into a guessed command. The legacy `python scripts/check_workflow_status.py` command uses the same shared state engine and remains compatible.

If a workflow command fails or the recovery path is unclear, read `references/troubleshooting.md`, run Chief `status`, and follow the one current `CHIEF NEXT` action. Do not improvise a helper, patch a process JSON, or bypass a failed stage.

Runs dispatched by Chief append privacy-minimized events to `process/workflow-journal.jsonl`. The journal records stage/script, timestamps, exit code, duration, artifact hashes, and counts only; it must not contain raw invoice text, applicant answers, client details, full command arguments, or source paths. It is observational rather than an integrity authority, never enters the final package, and a journal-write failure must not replace the underlying script's exit code.

Missing OCR system tools such as Tesseract or Poppler do not block text-layer PDFs or Excel/package stages. If OCR is unavailable, stage 1 must mark scan-only inputs as `manual_review` instead of inventing fields.

If the user mainly provides scanned PDFs or images and expects OCR, run the stricter check and explain any missing system tool in chat:

```bash
python scripts/chief_orchestrator.py run dependencies --strict-ocr
```

2. For invoice extraction, read `references/stage-1-output.md` before changing the schema, classification rules, or process-file format.
3. Run the bundled extractor when the user provides PDFs or images. Pass the whole upload/evidence folder (or every provided file) in one call — never pre-filter by which files look readable. Do not pass a task root that contains `process`, `output`, or the workflow journal; Chief rejects that overlap so generated artifacts cannot be mistaken for newly supplied evidence.

```bash
python scripts/chief_orchestrator.py run extract <input-file-or-folder> [...]
```

4. The extractor prints an `INPUT RECONCILIATION` block followed by an extraction review list, and writes the same review to `process/invoice-extraction.md` in UTF-8. Copy or summarize it directly in chat before moving to allocation, so the user can confirm recognized files by item number and source filename. Unsupported files such as OFD/eml are persisted in `unresolved_input_files` with a SHA-256 and block later stages until the user explicitly excludes them (with a reason) or provides a readable replacement through `input_resolutions`. Exact-file and invoice-number duplicates also remain Stage 1 review items: keep one source and record the user's exclusion against the duplicate through `correct-extraction`; dropping a later allocation unit does not resolve the evidence ledger. A shared SHA-256 identifies duplicate content, not one physical copy: to exclude one copy, match the shared `sha256` together with that copy's exact `source_file`. SHA-only exclusions that match multiple files are rejected, and Chief blocks any duplicate group that does not retain exactly one active copy. Apply a prepared corrections/resolutions file with `python scripts/chief_orchestrator.py run correct-extraction --corrections <corrections.json>`. If terminal output is garbled or truncated, read the UTF-8 Markdown process file instead of writing a one-off extraction helper script. Read a file visually when possible and record what you saw; otherwise ask the user using the extractor's question template. Never hand-edit `invoice-extraction.json`; corrections persist in `process/extraction-corrections.json` and replay automatically on every re-run. Do not ask the user to open `process/invoice-extraction.md/json`.
5. For project allocation, read `references/stage-2-allocation.md`, then parse the user's natural-language project context and match it against `process/invoice-extraction.json`. Convert the user's notes yourself into the exact `project_context.v1` root structure in `assets/project-context-template.json`; do not guess keys such as `projects`, `charge_code`, or `notes`, and do not ask the user to write JSON. Create one context object per distinct project/travel date window, even when several windows share the same Client and Code. Ask the user only for missing business facts such as date range, city, client name, or Client Charge Code.

```bash
python scripts/chief_orchestrator.py run allocate --context project-context.json
```

Every review-list line starts with a `[N@ref]` token: N is the display number (valid only within this generation) and ref is the item's short evidence identity; the allocation also stores the full SHA-256 identity and rejects missing or duplicate identities. Decisions files must reference items by the full token and declare `for_allocation_fingerprint` (the 8-char generation code printed by allocate); Composer refuses stale generations and mismatched refs before touching any data. Every official allocation write archives the prior stamped generation under `process/allocation-generations/` and records a lineage pointer, so repeated allocate/apply runs cannot overwrite the last decided state. When invoices are added or removed later, follow `CHIEF NEXT`: if effective project contexts, policy, and `allocation_engine_revision` are unchanged, Chief first runs rebase, then Composer/updater. Composer and updater reject ordinary answers while a transferable lineage generation is pending; even a zero-carry rebase must pass once through Composer/updater to record lineage clearance. Rebase carries official user-set unit fields and explicit `R@ref` record resolutions whose evidence identities remain valid. Within a multi-unit hint match, links already dropped/excluded in the prior generation are pruned; if none remain, the old resolution is not carried and the record returns for confirmation. Every old allocation item that disappears from the new generation is also written to the stamped `removed_evidence` ledger. A prior `drop/exclude/non_reimbursable` item closes automatically; every other disappearance is shown in chat as `M@ref + original filename + amount + date/category` and blocks Composer until the applicant confirms `intentional_removal`, identifies exact current replacement `N@ref` item(s), or asks to restore the evidence. Fill the generated `process/rebase-removal-resolutions.json` internally from the user's natural language and rerun Chief `rebase --resolutions ...`; never hand-edit the stamped rebase packet. A `restore_required` answer remains blocked until the source evidence is restored and Stages 1/2/rebase are rerun. Changed/new evidence also returns to the user. If any business basis changed, review the regenerated allocation from scratch. Never reuse an old decisions file by editing its fingerprint or manually select an older generation when Chief has discovered the lineage source.

Allocation validates the project-context schema before creating any units, refuses to run while unsupported inputs or any Stage 1 review-required/unknown document remains open, and records hashes for both extraction and project context. If either input changes later, regenerate allocation and recompose answers rather than reusing the old allocation or answers file. A schema failure means rewrite the same context file from the bundled template; never bypass it with a launcher or patch script.

Treat every concrete expense line supplied by the applicant as an expected-record item. Put a meal record in `meal_hints` or another record in `expense_hints`, never duplicate the same record in both arrays; Stage 2 nevertheless deduplicates legacy cross-array copies by context/category/date/amount and compatible merchant evidence. It writes `expense_hint_reconciliation` for every distinct record. Records without unique extracted matches are grouped by expense type in chat and labeled with generation-safe `R1@ref` tokens. Translate every full R@ref answer into `expense_hint_resolutions` using one canonical action: `matched_existing`, `covered_by_invoice`, `not_reimbursed`, or `pending_invoice`. The first three close that record when valid; `pending_invoice` records the applicant's intention but remains blocking until evidence is supplied or the applicant changes it to `not_reimbursed`. Never close these questions through free-text `question_updates`. Dropping a linked unit reopens the record.

The allocation script prints an applicant review list, then a ready-to-send 转发块 containing all blocking questions in Chinese. Relay the allocator's 转发块 VERBATIM — do not summarize or shorten it — then wait for the user's answers. The Mirror Warden and Gate Challenger audits run later, immediately before Stage 3 (see Preferred Subagent Checkpoints), not at this point. If terminal output is garbled, read the Markdown process file instead of creating temporary print/extraction scripts.

When the user answers allocation questions, use the bundled Composer to turn those decisions into the current allocation's canonical `unit_updates` and `expense_hint_resolutions`; do not invent another schema such as `answers[].allocations`. Composer resolves the actual `user_no` values printed in chat, supports unit/question/context/hint-resolution updates plus confirm/drop/exclude actions, binds the live allocation fingerprint, dry-runs the updater, and publishes `process/allocation-answers.json` only after validation succeeds. Then run the updater and repeat until no blocking questions remain. After ANY extraction or allocation re-run, treat every old `DOC-xxx`, `UNIT-xxx`, displayed item number, and bare/old `R1@ref` reference as expired. Rebind explicit user-confirmed facts to the new review list using the source descriptors in `references/troubleshooting.md`; never replay number-only memory or an old decisions file.

### Preferred Subagent Checkpoints

Immediately before Stage 3, run two independent read-only audits over the confirmed allocation. Both are the default execution order before the canonical next action printed by Chief; skip only when no genuinely fresh isolated Agent capability exists or the user explicitly opts out (workflow complexity or prior errors are not reasons to skip). The deterministic Stage 3 preflight remains the fail-open fallback. Subagents are read-only reasoning passes: never give them filesystem paths or mutation tools, and never let them write process JSON.

On Claude Code, the fresh isolated Agent IS the built-in Agent/Task tool: spawn a general-purpose subagent and pass the packet plus result template inline in its prompt (there is no attachment channel — paste the complete JSON into the prompt). That is the concrete handoff. Do not treat "fresh subagent" as unavailable merely because there is no resource-attachment mechanism.

**Otako, the Mirror Warden** — precision-first factual reconciliation of the confirmed allocation against claimed evidence (attribution, journey coherence, dates/routes, over-claiming, true duplicate economic expenses, and genuinely unresolved material). Otako does not invent document requirements for unclaimed contextual travel or question ordinary trip behavior.

```bash
python scripts/chief_orchestrator.py run prepare-agent --role mirror_warden
```

Read the generated compact task packet and result template yourself, then hand both to a fresh Agent/Task-tool subagent with no prior conversation context by pasting their complete JSON into its initial prompt. Never tell the subagent to find or read a workspace file. When the host supports structured output, pass the packet's `response_json_schema`; otherwise require the exact result-template shape. The packet embeds `references/otako-mirror-warden.md`. Save the exact JSON response outside `process/`, then validate it:

```bash
python scripts/chief_orchestrator.py run accept-agent \
    --role mirror_warden --result <utf8-result.json>
```

**Kaede, the Gate Challenger** — narrow policy gate for explicit treatment rules, required approvals, plainly non-reimbursable expenses, Admin semantics, and substitute-invoice compliance. Kaede never optimizes the claim amount, recomputes daily caps, treats unrelated categories as duplicates, or reviews Stage 3/4 presentation/package artifacts before they exist.

```bash
python scripts/chief_orchestrator.py run prepare-agent --role gate_challenger
python scripts/chief_orchestrator.py run accept-agent \
    --role gate_challenger --result <utf8-result.json>
```

The packets embed `references/otako-mirror-warden.md` and `references/kaede-gate-challenger.md`. Both can be prepared or accepted only after Stage 2 is fully ready. Each result's `coverage[].status` only permits `completed` or `not_applicable`; `pass`/`advisory`/`block`/`unavailable` belong only in `outcome`, and every finding must use a role-specific code and severity allowed by the generated contract. A current validated `block` result from EITHER role prevents Stage 3 and packaging until the cited items are resolved through Composer/Updater and a fresh audit is run. Every accepted audit is written first to an immutable per-role archive under `process/subagent-audit-generations/<role>/`; deleting or corrupting the convenience sidecar cannot clear an accepted blocker. `pass`, `advisory`, and explicit `unavailable` results are recorded in final rows. Only the absence of any accepted current-task audit (missing/stale/invalid with no valid current archive) fails open to the deterministic preflight; never synthesize a pass. The integrity stamp proves only that the accepted result was not subsequently edited, not that the model was truly independent.

After `accept-agent`, relay the generated `SUBAGENT REVIEW SUMMARY TO SHOW IN CHAT` block verbatim. Only its `需要处理（阻断）` section may become applicant questions. Its `供参考（无需回复）` section is never a decision list: do not ask the applicant to choose, do not alter allocation automatically, and continue the standard workflow. Deterministic Stage 3/4 results override conflicting subagent arithmetic, placeholder, column, or package opinions.

When the applicant resolves a blocker by supplying a durable fact rather than changing an amount/project — for example, `该航班由公司携程商旅统一采购，不由个人报销或开票` — persist that fact through Composer/Updater in the relevant `project_contexts[].user_notes` or item `expense_note`, then run a fresh audit. Do not leave a fact needed by a fresh subagent only in conversation history.

If the host cannot start a fresh isolated subagent, skip these checkpoints and continue the deterministic workflow. Do not imitate independence by asking the same context-laden agent to rubber-stamp its own work.

### Batch Answers

For every confirmation or correction batch, use Composer. It resolves current item numbers, writes the live allocation fingerprint, validates field aliases and text safety, dry-runs the updater, and publishes `allocation-answers.json` only after that dry-run passes.

```bash
# Use --set for one or two scalar fields; direct UTF-8 Chinese and value spaces are supported.
python scripts/chief_orchestrator.py run compose \
    --set "3@a1b2c3d4: note=出差酒店（2晚，2026-06-01-2026-06-03） status=confirmed"

# Use the canonical UTF-8 decisions file for complex values or larger batches.
python scripts/chief_orchestrator.py run compose \
    --decisions process/batch-decisions.json
```

Create `process/batch-decisions.json` from `assets/allocation-decisions-template.json`. Keep `schema_version: allocation_decisions.v1` and only these root actions: `decisions`, `expense_hint_resolutions`, `question_updates`, `project_contexts`, `confirm_units`, `drop_units`, and `exclude_units`. Composer compiles all of them; there is no helper-script exception. Use the field quick reference in `references/stage-2-allocation.md` instead of guessing names.

If Composer exits nonzero, read its updater error, correct the same UTF-8 decisions file, and rerun Composer. Never switch to `build_allocation_answers_template.py`, generate `fill_answers.py`, create `patch_allocation.py`/`fix_*.py`, edit `expense-allocation.json`, or modify a bundled script. The template builder is developer-only diagnostic output and is structurally rejected by the updater.

```bash
python scripts/chief_orchestrator.py run apply
```

Composer already ran the official updater in `--dry-run` mode before publishing the answers file. Apply only the file Composer published.

### Decisions Encoding Rule

Write the decisions JSON directly as UTF-8 with the agent's file-editing tool. Do not inject Chinese through PowerShell inline Python, `-Command`, shell interpolation, or a console pipeline. Chief launches every child with Python UTF-8 mode and `PYTHONIOENCODING=utf-8`; direct entry scripts also configure UTF-8, so the agent should not need to prepend environment variables to normal commands. If a terminal merely displays a UTF-8 file incorrectly, read it through a UTF-8-capable file tool instead of rewriting correct data from the garbled display. Composer and the updater reject consecutive `??` markers, replacement characters, and common mojibake; Stage 3 repeats the check on final-visible fields and workbook cells. A single ASCII `?` in legitimate English free text remains allowed.

For generation visibility, `python scripts/chief_orchestrator.py lineage` prints the full current extraction/allocation fingerprints, the deterministic rebase source selected by Chief, and whether an existing rebase packet is current, stale, or invalid. `CHIEF NEXT` also prints the short allocation generation. Rebase source selection is always the nearest same-basis ancestor containing official user decisions; do not hand-pick a snapshot.

If the user says a recognized item is wrong, first trace that user-facing item number back to its source files, then ask for or apply the corrected fields.

```bash
python scripts/chief_orchestrator.py run trace --item 9
```

6. For Excel output, read `references/stage-3-excel-output.md`, ask the user for requester if missing, and write rows from `process/expense-allocation.json`. By default, the workbook is generated directly by script using `assets/reimbursement-workbook-layout.toml` for static workbook layout and Python code for business logic, formulas, sorting, and project blocks. The legacy template remains bundled at `assets/reimbursement-template.xlsx`; pass `--template bundled` or a custom `.xlsx` path only when a template-based fallback is explicitly needed.

```bash
python scripts/chief_orchestrator.py run write --output <filled.xlsx> --requester <name>
```

Stage 3 verifies that allocation still belongs to the current extraction generation and that no unsupported input remains unresolved. A mismatch requires a fresh Stage 2 run, not a manual repair.

Stage 3 promotes the workbook plus `final-expense-rows.json/md` as one validated artifact generation and preserves the prior generation when a new write is blocked or promotion fails. Read the terminal `STAGE3_RESULT`: only `ok` with exit code `0` and `package_allowed=true` may proceed to Stage 4. `review_required` has current review artifacts but remains non-packageable; `blocked` wrote no new generation. Chief preserves the writer's exit code, prints an unmistakable `DO NOT RUN PACKAGE` banner for nonzero results, and still prints the authoritative `CHIEF NEXT` recovery action.

7. For final packaging, read `references/stage-4-package.md`, then copy and rename source files using the final proof numbers.

```bash
python scripts/chief_orchestrator.py run package
```

After packaging, copy or summarize the final package summary in chat: package folder, workbook name, invoice/support-document counts, and any unresolved package issues.

If packaging exits with code `3`, it created a review package with blocking missing-file or approval issues. Do not call it complete or submit it; show the issues in chat, resolve them, then re-run Stage 4.

## Routed Business Rules

Detailed extraction, classification, allocation, Excel, packaging, and final-validation rules live in `references/workflow-core-rules.md`. Read only the sections relevant to the current stage, plus `Validation Expectations` before declaring the reimbursement workflow complete.

- Stage 1: read `Extraction Decision Tree`, `Classification Priorities`, `First-Pass Categories`, and `Notes For Downstream Work` there together with `references/stage-1-output.md`.
- Stage 2: read `Stage 2 Allocation Rules` there together with `references/stage-2-allocation.md`.
- Stage 3: read `Stage 3 Excel Output Rules` there together with `references/stage-3-excel-output.md`.
- Stage 4: read `Stage 4 Packaging Rules` there together with `references/stage-4-package.md`.
- Completion: read `Validation Expectations` there and verify every applicable invariant.
