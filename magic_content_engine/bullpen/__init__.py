"""Bullpen architecture — constrained multi-agent content pipeline.

Each agent in the bullpen operates under the principle of least privilege:
it receives only the inputs it needs, can only use explicitly allowed tools
enforced via IAM execution roles, and can only write to designated outputs.

Pipeline sequence:
  Researcher → Desk Editor → Writer → Subeditor → (approval gate) → Publisher

The Archivist (Whakaaro) runs on a separate nightly cadence.
"""
