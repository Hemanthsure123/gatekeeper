"""Review core: judges the work, never the contributor.

Nothing in this package may import ``gatekeeper.identity``, the ``ItemAuthor`` model,
or touch the ``item_authors`` table. It sees items only through the identity-free
``ItemView``. ``tests/test_contributor_blind_imports.py`` checks this on every run.
"""
