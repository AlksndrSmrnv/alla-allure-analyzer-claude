import asyncio

import httpx
import pytest

from alla_skill.client import Client
from alla_skill.config import Settings


def test_incomplete_pagination_is_rejected():
    def handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "jwt"})
        return httpx.Response(200, json={"content": [{"id": 1}], "last": True, "totalElements": 2})

    async def run():
        async with Client(
            Settings(endpoint="https://testops.test", token="token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ValueError):
                await client.get_all_test_results_for_launch(1)

    asyncio.run(run())


def test_auth_refresh_and_no_mutating_api_calls():
    calls, tokens = [], []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/oauth/token"):
            tokens.append("jwt-" + str(len(tokens)))
            return httpx.Response(200, json={"access_token": tokens[-1]})
        if request.headers["authorization"] == "Bearer jwt-0":
            return httpx.Response(401)
        return httpx.Response(200, json={"id": 1})

    async def run():
        async with Client(
            Settings(endpoint="https://testops.test", token="token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            assert (await client.get_launch(1)).id == 1

    asyncio.run(run())
    assert len(tokens) == 2
    assert all(method == "GET" or path.endswith("/oauth/token") for method, path in calls)


@pytest.mark.parametrize("kind", ["repeat", "change", "empty"])
def test_bad_pagination_never_looks_complete(kind):
    def handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "jwt"})
        page = int(request.url.params["page"])
        content = [{"id": 1 if kind == "repeat" else page + 1}]
        if page and kind == "empty":
            content = []
        return httpx.Response(
            200,
            json={
                "content": content,
                "totalElements": 3 if page and kind == "change" else 2,
                "last": page == 1,
            },
        )

    async def run():
        async with Client(
            Settings(endpoint="https://testops.test", token="token"),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ValueError):
                await client.get_attachments_for_test_result(1)

    asyncio.run(run())
