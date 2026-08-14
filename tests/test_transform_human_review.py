from __future__ import annotations

import inspect

import pandas as pd
import pytest

import transform


def test_normal_mode_remains_default_and_invalid_mode_is_rejected():
    assert inspect.signature(transform.enriquecer_recape_com_geosampa).parameters["human_review_mode"].default == "off"
    with pytest.raises(ValueError):
        transform.enriquecer_recape_com_geosampa(pd.DataFrame(), human_review_mode="unknown")


def test_human_unresolved_failure_is_explicit():
    reason, detail = transform._failure_diagnosis(
        "HUMAN_UNRESOLVED", {"human_review_decision": "MARCAR_COMO_NAO_RESOLVIDO"}, {}
    )
    assert reason == "HUMAN_UNRESOLVED"
    assert "fuzzy" in detail
