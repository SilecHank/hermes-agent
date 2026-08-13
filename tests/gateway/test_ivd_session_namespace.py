from gateway.session_context import (
    EffectNamespace,
    TaskEpochRegistry,
    build_effect_session_key,
)


def test_same_user_on_two_platforms_has_distinct_effect_contexts():
    qq = build_effect_session_key(
        platform="qqbot",
        chat="group-1",
        thread="",
        participant="u1",
        task_epoch=3,
        effect=EffectNamespace.CLARIFICATION,
    )
    weixin = build_effect_session_key(
        platform="weixin",
        chat="group-1",
        thread="",
        participant="u1",
        task_epoch=3,
        effect=EffectNamespace.CLARIFICATION,
    )

    assert qq != weixin


def test_group_members_share_conversation_but_not_effect_state():
    first = build_effect_session_key(
        platform="qqbot",
        chat="group-1",
        thread="topic-1",
        participant="u1",
        task_epoch=0,
        effect=EffectNamespace.CLARIFICATION,
    )
    second = build_effect_session_key(
        platform="qqbot",
        chat="group-1",
        thread="topic-1",
        participant="u2",
        task_epoch=0,
        effect=EffectNamespace.CLARIFICATION,
    )

    assert first != second


def test_effect_namespaces_do_not_collide_for_identical_short_text():
    common = dict(
        platform="weixin",
        chat="dm-u1",
        thread="",
        participant="u1",
        task_epoch=7,
    )

    review = build_effect_session_key(
        **common,
        effect=EffectNamespace.REVIEW_NAVIGATION,
    )
    answer = build_effect_session_key(
        **common,
        effect=EffectNamespace.ANSWER_MESSAGE,
    )

    assert review != answer


def test_task_epoch_only_advances_for_explicit_boundaries():
    epochs = TaskEpochRegistry()
    identity = ("weixin", "dm-u1", "", "u1")

    assert epochs.current(*identity) == 0
    assert epochs.observe_text(*identity, text="换个问题") == 0
    assert epochs.advance(*identity, reason="explicit_topic_switch") == 1
    assert epochs.advance(*identity, reason="auto_new_session") == 2

