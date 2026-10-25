This PR adds input validation to `slugify`: it now rejects empty strings and non-string input with a clear `ValueError`.

It also adds tests for the invalid inputs.
