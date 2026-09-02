"""Deterministic compliance gate.

Contains no model inference, by design and by import-linter contract. Every
outbound action passes through evaluate() before it may execute. See ADR-0005."""
