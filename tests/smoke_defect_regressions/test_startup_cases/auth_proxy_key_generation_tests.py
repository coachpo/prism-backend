from app.core.auth import build_proxy_api_key, parse_proxy_api_key


class TestDEF067_ProxyApiKeyPrefixGeneration:
    def test_build_proxy_api_key_uses_unique_lookup_prefix(self):
        raw_key, key_prefix, last_four = build_proxy_api_key()

        assert key_prefix.startswith("pm-")
        assert len(key_prefix) == 11
        assert raw_key.startswith(key_prefix)
        assert len(raw_key) > len(key_prefix)
        assert last_four == raw_key[-4:]

    def test_parse_proxy_api_key_returns_generated_lookup_prefix(self):
        raw_key, key_prefix, _ = build_proxy_api_key()

        normalized_key, parsed_prefix = parse_proxy_api_key(raw_key)

        assert normalized_key == raw_key
        assert parsed_prefix == key_prefix

    def test_parse_proxy_api_key_keeps_supported_prefix_format(self):
        raw_key = "prism_live_example_lookup_secret"

        normalized_key, parsed_prefix = parse_proxy_api_key(raw_key)

        assert normalized_key == raw_key
        assert parsed_prefix == "prism_live_example_lookup"
