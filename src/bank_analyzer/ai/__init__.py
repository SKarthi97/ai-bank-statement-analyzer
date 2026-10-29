"""Local AI: a loopback-only model client, fixed prompts and strict response validation.

The model only ever classifies short text into an allowed enum and phrases explanations. It has
no tools, never computes money and its output is validated before anything is stored.
"""
