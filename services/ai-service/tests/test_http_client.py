import pytest

from app.integrations.http import close_http_client, get_http_client


@pytest.mark.asyncio
async def test_shared_client_is_reused_and_closed():
    first = get_http_client()
    assert get_http_client() is first

    await close_http_client()
    assert first.is_closed

    second = get_http_client()
    assert second is not first
    await close_http_client()
