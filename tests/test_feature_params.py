"""Feature params end up in prompts: every free-text field has a length cap."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailed
from app.features import FEATURES
from app.services.analysis import parse_params


def test_every_free_text_param_is_capped():
    for kind, feature in FEATURES.items():
        for name, field in feature.params_model.model_fields.items():
            if field.annotation is not str:
                continue
            caps = [getattr(m, "max_length", None) for m in field.metadata]
            assert any(caps), f"{kind}.{name} has no max_length"


def test_too_long_param_is_a_user_error():
    with pytest.raises(ValidationFailed, match="Текст письма"):
        parse_params(FEATURES["letter_critic"], {"letter_text": "я" * 8001})
    assert parse_params(FEATURES["letter_critic"], {"letter_text": "я" * 8000}).letter_text
