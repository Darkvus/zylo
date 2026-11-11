"""Semantics context: deciding what a program means.

The type checker (inference, scopes, mutability, narrowing) and the linker (resolving `import`s into one program).
It turns a syntax tree into a typed one and reports the mistakes a user can make.
"""
