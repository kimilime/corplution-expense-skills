# Command Exit Codes

Use an exit code to decide whether the command ran successfully. Use the JSON/text payload to decide whether the reimbursement workflow can advance.

Code uses the shared `scripts/exit_codes.py::ExitCode` enum. New command entry points must return one of those named values instead of introducing another numeric meaning.

## Chief query commands

`chief_orchestrator.py status`, `next`, and `lineage` return `0` whenever the requested inspection completes and its result is printed. This includes valid results whose workflow `kind` is `needs_user` or `blocked`. The legacy read-only `check_workflow_status.py` command follows the same rule.

Automation must inspect the query payload instead of using shell short-circuiting to infer workflow readiness:

- `kind`: one of `command`, `needs_user`, `blocked`, or `complete` for `next` results.
- `integrity_blocked`: whether an integrity failure prevents safe continuation.

A query returns nonzero only when the query command itself cannot execute or produce a result.

## Chief run commands

`chief_orchestrator.py run ...` preserves the child script's exit code. Chief uses `2` only when it cannot validate or launch the requested child command, and `130` when the run is interrupted from the keyboard.

Common child-script conventions are:

| Code | Meaning |
| --- | --- |
| `0` | The command completed successfully. |
| `1` | A utility-specific operational action failed, such as dependency installation. Read that command's error output. |
| `2` | Input, validation, readiness, or execution failure; the requested deliverable was not completed. |
| `3` | A review artifact was produced, but blocking review issues remain. |
| `4` | Process-file integrity validation failed. Use the sanctioned updater or regenerate the affected stage. |
| `130` | The command was interrupted from the keyboard. |

These meanings describe command execution, not business-workflow state. A successful query reporting a blocked workflow is therefore exit `0`, while a failed `run` remains nonzero and keeps the child code.
