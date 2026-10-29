from app.config import StageRule
from app.pipeline import decide


def test_new_lead_sends_event():
    rule = StageRule(key="interesado", rank=2, event="SubmitForm")
    decision = decide({}, rule)
    assert decision.send is True
    assert decision.event_name == "SubmitForm"


def test_lower_stage_is_skipped_after_higher_stage():
    rule = StageRule(key="interesado", rank=2, event="SubmitForm")
    decision = decide({"max_rank": 4}, rule)
    assert decision.send is False
    assert decision.reason == "etapa_ya_enviada_o_inferior"


def test_same_stage_is_skipped():
    rule = StageRule(key="compra", rank=4, event="Purchase")
    decision = decide({"max_rank": 4}, rule)
    assert decision.send is False


def test_higher_stage_is_sent():
    rule = StageRule(key="compra", rank=4, event="Purchase")
    decision = decide({"max_rank": 2}, rule)
    assert decision.send is True


def test_unmapped_stage_is_skipped():
    decision = decide({}, None)
    assert decision.send is False
    assert decision.reason == "etapa_sin_mapeo"
