import pytest

from url_parser import (
    PLATFORM_APP_STORE,
    PLATFORM_GOOGLE_PLAY,
    UrlParseError,
    parse_url,
)


def test_google_play_basic():
    parsed = parse_url(
        "https://play.google.com/store/apps/details?id=com.example.app"
    )
    assert parsed.platform == PLATFORM_GOOGLE_PLAY
    assert parsed.app_id == "com.example.app"
    assert parsed.country is None


def test_google_play_with_extra_query_params():
    parsed = parse_url(
        "https://play.google.com/store/apps/details?id=com.company.app&hl=en&gl=US"
    )
    assert parsed.app_id == "com.company.app"


def test_google_play_missing_id_raises():
    with pytest.raises(UrlParseError):
        parse_url("https://play.google.com/store/apps/details?foo=bar")


def test_app_store_basic():
    parsed = parse_url("https://apps.apple.com/us/app/example-app/id123456789")
    assert parsed.platform == PLATFORM_APP_STORE
    assert parsed.app_id == "123456789"
    assert parsed.country == "us"


def test_app_store_no_country_in_path():
    parsed = parse_url("https://apps.apple.com/app/example-app/id987654321")
    assert parsed.app_id == "987654321"
    assert parsed.country is None


def test_app_store_with_query_string():
    parsed = parse_url(
        "https://apps.apple.com/gb/app/example/id555?mt=8&l=en"
    )
    assert parsed.app_id == "555"
    assert parsed.country == "gb"


def test_app_store_missing_id_raises():
    with pytest.raises(UrlParseError):
        parse_url("https://apps.apple.com/us/app/example-app")


def test_unsupported_host_raises():
    with pytest.raises(UrlParseError):
        parse_url("https://example.com/app/id123")


def test_empty_url_raises():
    with pytest.raises(UrlParseError):
        parse_url("   ")
