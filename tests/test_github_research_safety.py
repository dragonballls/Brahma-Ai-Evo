import pytest

from core.github_research import GitHubResearchClient, GitHubResearchError


class _Response:
    def __init__(self, status=200, chunks=(), redirect=False):
        self.status_code = status
        self._chunks = list(chunks)
        self.is_redirect = redirect
        self.headers = {}

    def iter_content(self, chunk_size=8192):
        yield from self._chunks


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        return self.response


def test_github_research_rejects_oversized_response():
    client = GitHubResearchClient()
    client.session = _Session(_Response(chunks=[b"x" * (256 * 1024 + 1)]))

    with pytest.raises(GitHubResearchError, match="exceeded the safety limit"):
        client._get("/search/repositories")


def test_github_research_rejects_redirects_instead_of_following_them():
    client = GitHubResearchClient()
    client.session = _Session(_Response(status=302, chunks=[b"ignored"], redirect=True))

    with pytest.raises(GitHubResearchError, match="unexpected redirect"):
        client._get("/repos/example")


def test_github_research_accepts_bounded_valid_json():
    client = GitHubResearchClient()
    client.session = _Session(_Response(chunks=[b'{"ok":true}']))

    assert client._get("/repos/example") == {"ok": True}
