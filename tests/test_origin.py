from ragtone.models import Hit
from ragtone.origin import Origins, origin_for, origin_url, site_origin
from ragtone.settings import Settings


def test_site_origin_accepts_host_or_url() -> None:
    assert site_origin("example.atlassian.net") == "https://example.atlassian.net"
    assert site_origin("https://example.atlassian.net/") == "https://example.atlassian.net"
    assert site_origin("https://example.atlassian.net/browse/ABC-1") == "https://example.atlassian.net"


def test_site_origin_skips_uuid_cloud_ids() -> None:
    assert site_origin("11111111-2222-3333-4444-555555555555") == ""


def test_jira_browse_url_uses_atlassian_cloud_id() -> None:
    assert (
        origin_url(
            source="jira",
            native_id="ABC-12",
            parent_id="ABC-12",
            url="https://api.atlassian.com/ex/jira/x/rest/api/3/issue/10001",
            atlassian="https://example.atlassian.net",
        )
        == "https://example.atlassian.net/browse/ABC-12"
    )


def test_jira_comment_opens_the_issue() -> None:
    assert (
        origin_url(
            source="jira",
            native_id="ABC-12:comment:9",
            parent_id="ABC-12",
            atlassian="example.atlassian.net",
        )
        == "https://example.atlassian.net/browse/ABC-12"
    )


def test_confluence_page_url_uses_atlassian_cloud_id() -> None:
    assert (
        origin_url(
            source="confluence",
            native_id="99:0",
            parent_id="99",
            channel_or_space="ENG",
            url="/wiki/spaces/ENG/pages/99",
            atlassian="https://example.atlassian.net",
        )
        == "https://example.atlassian.net/wiki/spaces/ENG/pages/99"
    )


def test_confluence_page_url_without_space() -> None:
    assert (
        origin_url(
            source="confluence",
            native_id="99",
            parent_id="99",
            atlassian="https://example.atlassian.net",
        )
        == "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=99"
    )


def test_slack_permalink_uses_workspace() -> None:
    assert (
        origin_url(
            source="chat",
            native_id="1710000000.000100",
            thread_id="1710000000.000100",
            channel_or_space="C024BE7LT",
            slack="https://example.slack.com",
        )
        == "https://example.slack.com/archives/C024BE7LT/p1710000000000100"
    )


def test_slack_falls_back_to_app_redirect() -> None:
    assert (
        origin_url(
            source="chat",
            native_id="1710000000.000100",
            channel_or_space="C024BE7LT",
        )
        == "https://slack.com/app_redirect?channel=C024BE7LT"
    )


def test_local_note_has_no_origin() -> None:
    assert origin_url(source="chat", native_id="local:1", thread_id="local:1") == ""


def test_origin_for_hit_reads_settings() -> None:
    hit = Hit(
        id="jira:ABC-12",
        score=1.0,
        source="jira",
        title="SSO",
        text="timeout",
        url="",
        native_id="ABC-12",
        parent_id="ABC-12",
    )
    origins = Origins.from_settings(
        Settings(atlassian_cloud_id="https://example.atlassian.net")
    )
    assert origin_for(hit, origins) == "https://example.atlassian.net/browse/ABC-12"
