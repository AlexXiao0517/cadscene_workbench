# Short Project Identifiers Design

## Goal

Shorten automatically generated project directory names so new Windows projects retain more path budget without changing existing projects or user-supplied identifiers.

## Decision

- Automatically generated project IDs use `p-` followed by the first 16 hexadecimal characters of a UUID identity, for a fixed length of 18 characters.
- Existing IDs such as `project-<uuid>` and `dataset-<uuid>` remain valid and load unchanged.
- An explicitly supplied `project_id` remains unchanged after the existing safety validation.
- Creation retries with a fresh identity if the generated short ID already exists.
- The offline launcher path probe uses the new 18-character project ID shape while retaining the existing job and revision path budgets.

## Compatibility and Testing

- No manifests or directories are renamed.
- API responses, URLs, uploads, jobs, rendering, and project-library discovery continue to use the selected project ID as before.
- Tests cover the generated format, explicit-ID preservation, collision retry, legacy project discovery, and the launcher path probe.
