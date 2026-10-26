"""Shared ATS event/status contract for CLI producers.

The checked-in JSON is mirrored by ATS-app and versioned independently of its UI.
"""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parent / 'contracts' / 'events.v1.json').read_text(encoding='utf-8'))


def accepted_event(event, persisted=False):
    if not isinstance(event, dict):
        return False
    keys = 'persisted_required' if persisted else 'required'
    return all(isinstance(event.get(key), str) and event[key] for key in contract()[keys])


def valid_phase(stage):
    return stage in contract()['phase_stages']
