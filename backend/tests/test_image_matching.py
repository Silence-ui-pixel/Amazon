"""图片下载安全边界及模型超时不重试。"""

import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.domain.errors import DomainError
from app.domain.verification import SearchCard
from app.services.image_matching import ImageMatcher, download_image
from app.services.qwen import QwenClient, QwenConfig


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "http://m.media-amazon.com/a",
        "https://127.0.0.1/a",
        "https://m.media-amazon.com.evil.com/a",
        "https://u:p@m.media-amazon.com/a",
        "https://m.media-amazon.com:8000/a",
    ],
)
def test_unsafe_download_rejected_without_network(url: str) -> None:
    with pytest.raises(DomainError):
        download_image(url)


def test_redirect_bad_format_and_oversize_rejected() -> None:
    for response in [
        httpx.Response(302, headers={"location": "https://localhost/"}),
        httpx.Response(200, content=b"not image"),
        httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1)),
    ]:
        with pytest.raises(DomainError):
            download_image(
                "https://m.media-amazon.com/a",
                transport=httpx.MockTransport(lambda _, response=response: response),
            )
    assert (
        download_image(
            "https://m.media-amazon.com/a",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=png())),
        )
        == png()
    )


def test_model_timeout_blocks_later_paid_comparisons(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_download(_: str) -> bytes:
        return png()

    monkeypatch.setattr("app.services.image_matching.download_image", fake_download)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ReadTimeout("timeout", request=request)

    matcher = ImageMatcher(
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)), png()
    )
    for number in range(2):
        assert matcher.compare(
            [
                SearchCard(
                    asin="B0ABCDEF12", position=1, image_url=f"https://m.media-amazon.com/{number}"
                )
            ]
        ) == ["uncertain"]
    assert len(calls) == 1


def test_model_requires_complete_unique_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(_: str) -> bytes:
        return png()

    monkeypatch.setattr("app.services.image_matching.download_image", fake_download)

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"results": [{"index": 9, "verdict": "match", "reason": "相同"}]}
                            )
                        },
                    }
                ]
            },
        )

    matcher = ImageMatcher(
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)), png()
    )
    assert matcher.compare(
        [SearchCard(asin="B0ABCDEF12", position=1, image_url="https://m.media-amazon.com/a")]
    ) == ["uncertain"]


def test_resized_compressed_same_image_accepts_model_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = BytesIO()
    image = Image.new("RGB", (32, 32), "blue")
    image.putpixel((8, 8), (255, 255, 255))
    image.save(reference, format="PNG")
    thumbnail = BytesIO()
    # Pillow 的 resize 重载含 Unknown，严格模式只报类型未知，不影响运行。
    image.resize((16, 16)).save(  # pyright: ignore[reportUnknownMemberType]
        thumbnail, format="JPEG"
    )

    def fake_download(_: str) -> bytes:
        return thumbnail.getvalue()

    monkeypatch.setattr("app.services.image_matching.download_image", fake_download)

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "results": [
                                        {"index": 0, "verdict": "match", "reason": "外形近似"}
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
        )

    matcher = ImageMatcher(
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)),
        reference.getvalue(),
    )
    url = "https://m.media-amazon.com/a"
    assert matcher.compare([SearchCard(asin="B0ABCDEF12", position=1, image_url=url)]) == ["match"]
    assert not matcher.evidence[url].pixel_equal
    assert matcher.evidence[url].reason == "外形近似"


def test_exact_pixels_and_model_agreement_preserve_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_download(_: str) -> bytes:
        return png()

    monkeypatch.setattr("app.services.image_matching.download_image", fake_download)

    def handle(_: httpx.Request) -> httpx.Response:
        content = json.dumps({"results": [{"index": 0, "verdict": "match", "reason": "相同主图"}]})
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    matcher = ImageMatcher(
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)), png()
    )
    url = "https://m.media-amazon.com/a"
    assert matcher.compare([SearchCard(asin="B0ABCDEF12", position=1, image_url=url)]) == ["match"]
    evidence = matcher.evidence[url]
    assert evidence.pixel_equal and evidence.reason == "相同主图"
    assert evidence.reference_sha256 == evidence.candidate_sha256


def test_transient_image_download_retries_once() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary failure", request=request)
        return httpx.Response(200, content=png())

    assert (
        download_image("https://m.media-amazon.com/a", transport=httpx.MockTransport(handle))
        == png()
    )
    assert calls == 2


def test_similarity_handles_white_padding_resize_and_compression() -> None:
    from PIL import ImageDraw

    from app.services.image_matching import image_similarity

    original = Image.new("RGB", (160, 160), "white")
    draw = ImageDraw.Draw(original)
    draw.rectangle((20, 10, 45, 150), fill="black")
    draw.rectangle((100, 10, 125, 150), fill="black")
    draw.rectangle((20, 55, 125, 70), fill="blue")
    source = BytesIO()
    original.save(source, format="PNG")
    padded = Image.new("RGB", (240, 240), "white")
    padded.paste(original.resize((100, 100)), (70, 70))  # pyright: ignore[reportUnknownMemberType]
    candidate = BytesIO()
    padded.save(candidate, format="JPEG", quality=80)
    metrics = image_similarity(source.getvalue(), candidate.getvalue())
    assert metrics["hash_similarity"] > 0.8
    assert metrics["feature_similarity"] > 0.9


def test_dissimilar_image_is_rejected_before_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import ImageDraw

    original = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(original).rectangle((10, 5, 25, 60), fill="red")
    candidate = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(candidate).ellipse((5, 20, 60, 40), fill="blue")
    reference, other = BytesIO(), BytesIO()
    original.save(reference, format="PNG")
    candidate.save(other, format="PNG")

    def fake_download(_: str) -> bytes:
        return other.getvalue()

    def handle(_: httpx.Request) -> httpx.Response:
        pytest.fail("Clearly different images should not call the model")

    monkeypatch.setattr("app.services.image_matching.download_image", fake_download)
    matcher = ImageMatcher(
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)),
        reference.getvalue(),
    )
    url = "https://m.media-amazon.com/different"
    assert matcher.compare([SearchCard(asin="B0ABCDEF12", position=1, image_url=url)]) == [
        "no_match"
    ]
    assert matcher.evidence[url].model_verdict == ""
