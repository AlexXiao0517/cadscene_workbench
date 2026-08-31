# Compact Windows Publication Paths Design

## Goal

Eliminate Windows `MAX_PATH` failures during video-analysis publication without requiring testers to enable long-path support.

## Design

- The server owns generated project identifiers. New projects use `p-` plus 16 hexadecimal characters; the upload portal no longer sends a client-generated `dataset-UUID`.
- Existing long project identifiers remain valid and are not renamed or migrated.
- Full SHA-256 values remain the logical immutable identities stored in manifests and used for content verification.
- Physical video-analysis directories use bounded names: `r-` plus 16 base32 characters for analysis revisions and `va-` plus 16 base32 characters for published artifacts.
- Source and destination publication staging directories use `tempfile.mkdtemp()` with short `.pub-` prefixes. Atomic publication, rehashing, collision rejection, and cleanup remain unchanged.
- The Windows launcher checks the actual deepest paths using a legacy long project identifier, so old projects are covered.

## Compatibility and failure behavior

Previously published full-name artifact directories are loaded from their recorded paths and remain valid. A compact-name collision never overwrites data: existing content is rehashed and conflicting content raises `FileExistsError`. Failed staging directories are removed as before.

## Verification

Tests cover server-generated IDs, portal ownership of IDs, compact physical paths with legacy project IDs, collision rejection, launcher path budgeting, focused publication/API/UI suites, the full suite, wheel installation, and offline bundle verification.
