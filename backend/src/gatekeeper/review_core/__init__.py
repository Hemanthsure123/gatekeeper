"""Review core: judges the work, never the contributor.

Nothing in this package may import ``gatekeeper.identity``, the ``ItemAuthor`` model,
or query the author identity table. It sees items only through the identity-free
``ItemView``. ``tests/test_contributor_blind_imports.py`` checks this on every run.
"""
