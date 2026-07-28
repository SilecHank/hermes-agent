from gateway.ivd_runtime import begin_ivd_answer_turn, consume_ivd_search, end_ivd_answer_turn


def test_ivd_search_budget_is_bounded_and_resets():
    token = begin_ivd_answer_turn(max_searches=2, mode="answer")
    try:
        assert consume_ivd_search() == (True, 1, 2)
        assert consume_ivd_search() == (True, 2, 2)
        assert consume_ivd_search() == (False, 3, 2)
    finally:
        end_ivd_answer_turn(token)

    assert consume_ivd_search() == (True, 0, 0)


def test_maintenance_mode_does_not_apply_answer_budget():
    token = begin_ivd_answer_turn(max_searches=1, mode="maintenance")
    try:
        assert consume_ivd_search() == (True, 0, 0)
        assert consume_ivd_search() == (True, 0, 0)
    finally:
        end_ivd_answer_turn(token)
