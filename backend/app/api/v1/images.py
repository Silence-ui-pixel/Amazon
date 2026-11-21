"""项目图片上传及预览接口；文件内容与客户端声明分别处理。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Header, Response, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.v1.routes import IdempotencyKey
from app.domain.schemas import ImageAsset
from app.services.images import MAX_IMAGE_BYTES, ImageService


def build_image_router(service: ImageService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects", tags=["images"])

    @router.post("/{project_id}/images", status_code=201)
    async def upload_image(
        project_id: UUID,
        response: Response,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
    ) -> ImageAsset:
        """每次上传一张图片；读取上限多一个字节，用于判断是否超限。"""
        try:
            data = await file.read(MAX_IMAGE_BYTES + 1)
            # 解码和落盘属于阻塞工作，移到线程池以免阻塞异步请求循环。
            result = await run_in_threadpool(
                service.save, project_id, file.filename or "image", data, idempotency_key
            )
        finally:
            await file.close()
        response.headers["Location"] = result.content_url
        return result

    @router.get("/{project_id}/images/{asset_id}/content")
    def image_content(project_id: UUID, asset_id: UUID) -> FileResponse:
        """校验项目归属后再返回文件，客户端不能直接指定磁盘路径。"""
        asset, path = service.content(project_id, asset_id)
        return FileResponse(
            path,
            media_type=asset.content_type,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

    return router
