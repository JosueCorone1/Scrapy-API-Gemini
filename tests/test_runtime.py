"""Offline regression tests: no Instagram requests or real credentials."""
import csv
import json
from unittest.mock import Mock

import pytest

import benford
import main
from instagram import fetch, users


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(fetch, "_next_request_at", 0.0)
    monkeypatch.setattr(fetch, "_requests_blocked_until", 0.0)
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 100.0)
    response = Mock(status_code=429, headers={"Retry-After": "120"})
    response.text = '{"message": "Please wait a few minutes before you try again."}'
    session = Mock()
    session.get.return_value = response
    return session


def test_429_stops_immediately_and_honors_retry_after(transport):
    with pytest.raises(fetch.InstagramRateLimited, match="429"):
        fetch.paced_request(transport, "get", "https://example.invalid")
    assert fetch._requests_blocked_until == 220.0
    with pytest.raises(fetch.InstagramRateLimited):
        fetch.paced_request(transport, "get", "https://example.invalid")
    transport.get.assert_called_once()


def test_429_with_wait_message_is_not_a_cookie_error(transport, monkeypatch):
    monkeypatch.setattr(fetch, "_session", lambda: transport)
    with pytest.raises(fetch.InstagramRateLimited):
        fetch.www_json("test/", params={}, label="test", session_auth=True)
    transport.get.assert_called_once()


def test_429_without_retry_after_uses_configured_delay(transport, monkeypatch):
    transport.get.return_value.headers = {}
    monkeypatch.setattr(fetch.config, "INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS", 900)
    with pytest.raises(fetch.InstagramRateLimited):
        fetch.paced_request(transport, "get", "https://example.invalid")
    assert fetch._requests_blocked_until == 1000.0


@pytest.mark.parametrize("error", [fetch.InstagramBlocked("HTTP 403"), fetch.InstagramRateLimited("HTTP 429")])
@pytest.mark.parametrize("kind", ["posts", "reels"])
def test_block_does_not_try_another_endpoint(error, kind, monkeypatch):
    monkeypatch.setattr(users, "lookup", lambda user: {"pk": "1", "username": "demo"})
    if kind == "posts":
        monkeypatch.setattr(users, "_web_posts", Mock(side_effect=error))
        fallback = Mock()
        monkeypatch.setattr(users, "_app_posts", fallback)
        operation = users.get_posts
    else:
        monkeypatch.setattr(users, "mobile", Mock(side_effect=error))
        fallback = Mock()
        monkeypatch.setattr(users, "_web_reels", fallback)
        operation = users.get_reels
    with pytest.raises(type(error)):
        operation({"username": "demo"})
    fallback.assert_not_called()


def test_profile_enrichment_does_not_hide_rate_limit():
    with pytest.raises(fetch.InstagramRateLimited):
        users._quiet(Mock(side_effect=fetch.InstagramRateLimited("HTTP 429")))


@pytest.mark.parametrize("error, marker", [
    (fetch.InstagramGated("login_required"), "sesión"),
    (fetch.InstagramRateLimited("HTTP 429: espera 120 s"), "429"),
    (ValueError("invalid response"), "ValueError"),
])
def test_pagination_preserves_partial_data_on_error(error, marker):
    row = {"id": "1", "username": "demo"}
    pages = Mock(side_effect=[{"users": [row], "has_more": True, "next_cursor": "next"}, error])
    result = main.collect_all_pages(pages, item_key="users", count_key="user_count", label="Test")
    assert result["users"] == [row]
    assert result["status"] == "partial"
    assert marker in result["error"]
    assert result["stop_requests"] == isinstance(error, fetch.InstagramUpstreamError)


@pytest.mark.parametrize("error", [fetch.InstagramGated("login_required"), fetch.InstagramRateLimited("HTTP 429")])
def test_profile_error_is_reported_without_name_error(error, monkeypatch):
    monkeypatch.setattr(main.config, "instagram_cookies", lambda: "test-only")
    monkeypatch.setattr(main, "get_profile", Mock(side_effect=error))
    with pytest.raises(SystemExit, match="Instagram|429"):
        main.run_export("demo", 1, gemini_consent=False)


def test_benford_scan_saves_partial_rows_and_stops_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.config, "instagram_cookies", lambda: "test-only")
    monkeypatch.setattr(main, "get_followers", Mock(side_effect=[
        {"users": [{"id": "1", "username": "demo_follower"}], "has_more": True, "next_cursor": "next"},
        fetch.InstagramRateLimited("HTTP 429: espera 120 s"),
    ]))
    following = Mock()
    profile = Mock()
    monkeypatch.setattr(main, "get_following", following)
    monkeypatch.setattr(main, "resolve_profile", profile)
    result = main.run_benford_scan("demo")
    following.assert_not_called()
    profile.assert_not_called()
    assert result["collection"]["followers"]["status"] == "partial"
    assert result["collection"]["following"]["status"] == "not_requested"
    assert result["collection"]["metrics_interrupted"]
    saved = json.loads((tmp_path / "benford_scan_demo.json").read_text(encoding="utf-8"))
    assert saved["followers"][0]["username"] == "demo_follower"
    with (tmp_path / "benford_scan_demo.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_export_stops_reels_and_media_after_post_block(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.config, "instagram_cookies", lambda: "test-only")
    monkeypatch.setattr(main.config, "gemini_api_key", lambda: None)
    monkeypatch.setattr(main, "get_profile", lambda user: {"username": "demo"})
    monkeypatch.setattr(main, "get_posts", Mock(side_effect=[
        {"posts": [{"id": "1", "caption": "Example"}], "has_more": True, "next_cursor": "next"},
        fetch.InstagramRateLimited("HTTP 429"),
    ]))
    reels = Mock()
    download = Mock()
    monkeypatch.setattr(main, "get_reels", reels)
    monkeypatch.setattr(main, "download_content", download)
    report, path = main.run_export("demo", 5, gemini_consent=False)
    reels.assert_not_called()
    download.assert_not_called()
    assert path.is_file()
    assert report["content"]["count"] == 1
    assert report["content"]["collection"]["reels"]["status"] == "not_requested"
    assert report["content"]["items"][0]["media_downloads"]["status"] == "not_requested"


@pytest.mark.parametrize("value, expected", [(123, 1), (0.0045, 4), ("2.5e8", 2), ("1,234", 1), (0, None), (-5, None), (None, None), (float("nan"), None)])
def test_first_digit_of_positive_counts(value, expected):
    assert benford.first_significant_digit(value) == expected


def test_benford_empty_sample_has_no_statistic():
    result = benford.analyze_counts([0, None, -1, "invalid"])
    assert result["sample_size"] == 0
    assert result["chi_square"] is None
    assert result["p_value"] is None


def test_benford_expected_distribution_and_uniform_counterexample():
    # Synthetic samples used only for verification, not real collected data.
    counts = [301, 176, 125, 97, 79, 67, 58, 51, 46]
    sample = [digit for digit, count in enumerate(counts, 1) for _ in range(count)]
    result = benford.analyze_counts(sample)
    assert result["sample_size"] == 1000
    assert sum(result["expected"].values()) == pytest.approx(1000)
    assert result["proportions"][1] == pytest.approx(0.3010299956639812)
    assert result["degrees_of_freedom"] == 8
    assert result["chi_square"] < 0.01
    assert result["p_value"] > 0.99
    assert benford.analyze_counts(list(range(1, 10)) * 100)["p_value"] < 0.001
