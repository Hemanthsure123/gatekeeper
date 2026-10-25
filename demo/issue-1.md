# truncate() returns strings longer than the requested width

`truncate(text, width)` is documented to return at most `width` characters, but when the text is too long it keeps `width` characters and then appends the suffix.

```python
>>> from textkit import truncate
>>> truncate("abcdefghij", 5)
'abcde...'   # 8 characters, expected at most 5
```

Expected: the result, including the suffix, is never longer than `width`.
