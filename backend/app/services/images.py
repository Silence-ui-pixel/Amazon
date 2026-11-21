"""图片接收服务：验证真实内容、保存文件，并记录项目归属。"""

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.schemas import ImageAsset, ImageExtension, ImageMime

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
IMAGE_TYPES: dict[str, tuple[ImageMime, ImageExtension]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class ImageService:
    """将文件系统操作留在服务层，数据库仓库只负责元数据事务。"""

    def __init__(self, repository: Repository, root: Path) -> None:
        self.repository = repository
        self.root = root

    def save(self, project_id: UUID, filename: str, data: bytes, key: str) -> ImageAsset:
        self.repository.get_project(project_id)
        if len(data) > MAX_IMAGE_BYTES:
            raise DomainError(413, "IMAGE_TOO_LARGE", "每张图片最多 10 MB")
        width, height, content_type, extension = self.validate(data)
        # 原文件名仅用于显示；实际路径全部由服务端 UUID 和内容类型生成。
        original_name = filename.replace("\\", "/").rsplit("/", 1)[-1][:200].strip() or "image"
        checksum = hashlib.sha256(data).hexdigest()
        fingerprint = hashlib.sha256((original_name + ":" + checksum).encode()).hexdigest()
        asset_id = uuid4()
        asset = ImageAsset(
            id=asset_id,
            project_id=project_id,
            original_name=original_name,
            content_type=content_type,
            extension=extension,
            size_bytes=len(data),
            width=width,
            height=height,
            sha256=checksum,
            created_at=datetime.now(UTC),
            content_url=f"/api/v1/projects/{project_id}/images/{asset_id}/content",
        )
        # 数据库只存图片元数据与相对 API 地址；服务器迁移还必须复制 uploads 实体文件。
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{asset.id}{asset.extension}"
        try:
            # 独占创建避免覆盖；数据库提交前先落盘，读者不会拿到半写入文件。
            with target.open("xb") as output:
                output.write(data)
            stored = self.repository.create_image(asset, key, fingerprint)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if stored.id != asset.id:
            # 并发或网络重试命中旧记录时，清理此次临时副本。
            target.unlink(missing_ok=True)
        return stored

    @staticmethod
    def validate(data: bytes) -> tuple[int, int, ImageMime, ImageExtension]:
        """不能信任扩展名或客户端 MIME：检查格式、尺寸、完整性并实际解码。"""
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in IMAGE_TYPES:
                    raise DomainError(422, "INVALID_IMAGE", "仅支持 JPG、PNG、WebP 图片")
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise DomainError(422, "INVALID_IMAGE", "图片像素总量不能超过 2500 万")
                content_type, extension = IMAGE_TYPES[image.format]
                image.verify()
            # verify 不保证像素流可完整解码，需要再次打开并加载像素。
            with Image.open(BytesIO(data)) as image:
                image.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise DomainError(422, "INVALID_IMAGE", "文件不是完整有效的图片") from exc
        return width, height, content_type, extension

    def content(self, project_id: UUID, asset_id: UUID) -> tuple[ImageAsset, Path]:
        asset = self.repository.get_image(project_id, asset_id)
        target = self.root / f"{asset.id}{asset.extension}"
        if not target.is_file():
            raise DomainError(404, "IMAGE_FILE_MISSING", "图片文件不存在")
        return asset, target
