"""Overlord: clip intelligence, editing, QC and YouTube distribution engine.

Pipeline: ingest -> transcribe -> analyse -> candidates -> score/rank -> render -> QC -> SEO -> publish -> analytics.
Every stage writes a checkpoint to the database so a restarted worker resumes where it stopped.
"""

__version__ = "0.1.0"
