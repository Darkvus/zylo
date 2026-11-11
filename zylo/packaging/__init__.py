"""Packaging context: manifests, environments, lock files and installing packages.

Everything about how Zylo programs depend on other code: `zylo.toml`, virtual environments, `zylo.lock`, the
installer, and the validation of Rust dependency declarations. Talking to git is an infrastructure concern; the
installer receives a downloader instead of importing one.
"""
