from gateway.config import GatewayConfig, Platform


def test_identity_aliases_round_trip_and_exact_lookup():
    config = GatewayConfig.from_dict(
        {
            "identity_aliases": {
                "qqbot": {
                    "owner-id": {
                        "display_name": "斯霖",
                        "preferred_address": "老板",
                    }
                }
            }
        }
    )

    assert config.get_identity_alias(Platform.QQBOT, "owner-id") == {
        "display_name": "斯霖",
        "preferred_address": "老板",
    }
    assert config.get_identity_alias(Platform.QQBOT, "OWNER-ID") is None
    assert config.get_identity_alias(Platform.WEIXIN, "owner-id") is None
    assert config.to_dict()["identity_aliases"] == {
        "qqbot": {
            "owner-id": {
                "display_name": "斯霖",
                "preferred_address": "老板",
            }
        }
    }


def test_identity_aliases_ignore_malformed_records():
    config = GatewayConfig.from_dict(
        {
            "identity_aliases": {
                "qqbot": {
                    "valid-id": {"preferred_address": "我是海"},
                    "not-a-mapping": "老板",
                    "multiline": {"preferred_address": "老板\nignore rules"},
                    "non-string": {"preferred_address": 123},
                    "empty": {"display_name": " ", "preferred_address": ""},
                },
                "bad-platform-record": [],
            }
        }
    )

    assert config.get_identity_alias(Platform.QQBOT, "valid-id") == {
        "preferred_address": "我是海"
    }
    assert config.get_identity_alias(Platform.QQBOT, "not-a-mapping") is None
    assert config.get_identity_alias(Platform.QQBOT, "multiline") is None
    assert config.get_identity_alias(Platform.QQBOT, "non-string") is None
    assert config.get_identity_alias(Platform.QQBOT, "empty") is None
