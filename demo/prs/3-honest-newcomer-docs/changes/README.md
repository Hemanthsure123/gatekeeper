# textkit

Tiny text helpers: `slugify`, `word_count` and `truncate`.

This repository is a synthetic playground for trying Gatekeeper, a pull request triage tool.

## Usage

```python
from textkit import slugify, truncate, word_count

slugify("Hello, World!")      # "hello-world"
word_count("one two three")   # 3
truncate("A long sentence", 8)
```

`truncate` keeps short text unchanged and shortens longer text, adding `...` at the end.

## Run the tests

```sh
python -m pytest
```
