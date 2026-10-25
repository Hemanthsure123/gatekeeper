`truncate(text, width)` kept `width` characters and then appended the suffix, so the result could be longer than `width` (see #1).

This change reserves room for the suffix, and handles a `width` smaller than the suffix by returning a shortened suffix.

Added two regression tests that fail on `main` and pass with the fix.

Closes #1
