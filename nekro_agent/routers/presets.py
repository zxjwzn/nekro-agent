import base64
import io
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, UploadFile
from PIL import Image
from tortoise.expressions import Q

from nekro_agent.core.logger import logger
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.message import Ret
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.systems.cloud.api.preset import create_preset as cloud_create_preset
from nekro_agent.systems.cloud.api.preset import delete_preset as cloud_delete_preset
from nekro_agent.systems.cloud.api.preset import get_preset
from nekro_agent.systems.cloud.api.preset import update_preset as cloud_update_preset
from nekro_agent.systems.cloud.schemas.preset import PresetCreate, PresetUpdate
from nekro_agent.tools.telemetry_util import generate_instance_id

router = APIRouter(prefix="/presets", tags=["Presets"])


@router.get("/list", summary="获取人设列表")
@require_role(Role.Admin)
async def get_preset_list(
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    remote_only: Optional[bool] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """获取人设列表"""
    query = DBPreset.all()

    # 搜索条件
    if search:
        query = query.filter(Q(name__contains=search) | Q(title__contains=search) | Q(description__contains=search))
    if tag:
        query = query.filter(tags__contains=tag)
    if remote_only is not None:
        if remote_only:
            query = query.filter(remote_id__not_isnull=True)
        else:
            query = query.filter(remote_id__isnull=True)

    # 获取总数
    total = await query.count()

    # 分页
    query = query.offset((page - 1) * page_size).limit(page_size)

    # 排序：先显示远程人设，再按更新时间排序
    query = query.order_by("-remote_id", "-update_time")

    # 获取列表
    presets = await query

    # 构建返回结果
    result = []
    for preset in presets:
        result.append(
            {
                "id": preset.id,
                "remote_id": preset.remote_id,
                "on_shared": preset.on_shared,
                "name": preset.name,
                "title": preset.title or preset.name,
                "avatar": preset.avatar,
                "description": preset.description,
                "tags": preset.tags,
                "author": preset.author,
                "is_remote": preset.remote_id is not None,
                "create_time": preset.create_time.strftime("%Y-%m-%d %H:%M:%S"),
                "update_time": preset.update_time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    return Ret.success(
        msg="获取成功",
        data={
            "total": total,
            "items": result,
        },
    )


@router.get("/{preset_id}", summary="获取人设详情")
@require_role(Role.Admin)
async def get_preset_detail(
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """获取人设详情"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    return Ret.success(
        msg="获取成功",
        data={
            "id": preset.id,
            "remote_id": preset.remote_id,
            "on_shared": preset.on_shared,
            "name": preset.name,
            "title": preset.title or preset.name,
            "avatar": preset.avatar,
            "content": preset.content,
            "description": preset.description,
            "tags": preset.tags,
            "author": preset.author,
            "is_remote": preset.remote_id is not None,
            "create_time": preset.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            "update_time": preset.update_time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


@router.post("", summary="创建人设")
@require_role(Role.Admin)
async def create_preset(
    name: str = Body(...),
    title: str = Body(None),
    avatar: str = Body(...),
    content: str = Body(...),
    description: str = Body(""),
    tags: str = Body(""),
    author: str = Body(""),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """创建人设"""
    preset = await DBPreset.create(
        name=name,
        title=title or name,
        avatar=avatar,
        content=content,
        description=description,
        tags=tags,
        author=author or _current_user.username,
        on_shared=False,
    )

    return Ret.success(
        msg="创建成功",
        data={"id": preset.id},
    )


@router.put("/{preset_id}", summary="更新人设")
@require_role(Role.Admin)
async def update_preset(
    preset_id: int,
    name: str = Body(...),
    title: str = Body(None),
    avatar: str = Body(...),
    content: str = Body(...),
    description: str = Body(""),
    tags: str = Body(""),
    author: str = Body(""),
    remove_remote: bool = Body(False),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """更新人设"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    # 更新字段
    preset.name = name
    preset.title = title or name
    preset.avatar = avatar
    preset.content = content
    preset.description = description
    preset.tags = tags
    preset.author = author or _current_user.username

    # 如果需要移除云端关联
    if remove_remote and preset.remote_id and not preset.on_shared:
        preset.remote_id = ""  # 使用空字符串代替None
        preset.on_shared = False

    await preset.save()

    return Ret.success(msg="更新成功")


@router.delete("/{preset_id}", summary="删除人设")
@require_role(Role.Admin)
async def delete_preset(
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """删除人设"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    await preset.delete()
    return Ret.success(msg="删除成功")


@router.post("/{preset_id}/sync", summary="同步云端人设")
@require_role(Role.Admin)
async def sync_preset(
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """同步云端人设"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    if not preset.remote_id:
        return Ret.fail(msg="此人设不是云端人设")

    # 从云端获取最新数据
    response = await get_preset(preset.remote_id)
    if not response.success or not response.data:
        return Ret.fail(msg=str(response.error))

    # 更新本地数据
    preset.name = response.data.name
    preset.title = response.data.title
    preset.avatar = response.data.avatar
    preset.content = response.data.content
    preset.description = response.data.description
    preset.tags = response.data.tags
    preset.author = response.data.author
    await preset.save()

    return Ret.success(msg="同步成功")


@router.post("/upload-avatar", summary="上传头像")
@require_role(Role.Admin)
async def upload_avatar(
    file: UploadFile = File(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """上传头像"""
    try:
        contents = await file.read()

        # 使用PIL打开图片
        img = Image.open(io.BytesIO(contents))

        # 压缩图片，保持尺寸不变
        output = io.BytesIO()

        # 如果是PNG等透明图片，保持透明通道
        if img.mode == "RGBA":
            img.save(output, format="PNG", optimize=True)
        else:
            img.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)

        # 检查大小，如果超过500KB，继续压缩
        img_data = output.getvalue()
        if len(img_data) > 500 * 1024:  # 500KB
            # 计算需要的压缩比例
            compression_ratio = 500 * 1024 / len(img_data)
            quality = int(85 * compression_ratio)
            quality = max(10, min(quality, 85))  # 确保不太低也不太高

            output = io.BytesIO()
            img.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
            img_data = output.getvalue()

            # 如果还是太大，降低分辨率
            if len(img_data) > 500 * 1024:
                width, height = img.size
                ratio = (500 * 1024 / len(img_data)) ** 0.5
                new_size = (int(width * ratio), int(height * ratio))
                # 使用数字2代替BICUBIC常量 (2=BICUBIC in PIL)
                img = img.resize(new_size, 2)

                output = io.BytesIO()
                img.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
                img_data = output.getvalue()

        # 转为Base64
        base64_encoded = base64.b64encode(img_data).decode("utf-8")

        # 添加数据URL前缀
        mime_type = "image/jpeg" if img.format == "JPEG" or img.mode != "RGBA" else "image/png"
        data_url = f"data:{mime_type};base64,{base64_encoded}"

        return Ret.success(msg="上传成功", data={"avatar": data_url})
    except Exception as e:
        logger.error(f"上传头像失败: {e}")
        return Ret.fail(msg=str(e))


@router.post("/{preset_id}/share", summary="共享人设到云端")
@require_role(Role.Admin)
async def share_preset(
    preset_id: int,
    is_sfw: bool = True,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """将人设共享到云端"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    # 检查是否已共享
    if preset.on_shared and preset.remote_id:
        return Ret.fail(msg="此人设已经共享到云端")

    # 检查必要字段
    if not preset.name:
        return Ret.fail(msg="人设名称不能为空")
    if not preset.content:
        return Ret.fail(msg="人设内容不能为空")
    if not preset.avatar:
        return Ret.fail(msg="人设头像不能为空")
    if not preset.description:
        return Ret.fail(msg="人设描述不能为空，请先编辑人设添加描述")

    # 如果有远程ID但未共享，表示是从云端下载的人设，需要重新共享为新人设
    instance_id = generate_instance_id()

    # 准备人设数据
    try:
        preset_data = PresetCreate(
            name=preset.name,
            title=preset.title or preset.name,
            avatar=preset.avatar,
            content=preset.content,
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data=preset.ext_data or "",
            is_sfw=is_sfw,
            instance_id=instance_id,
        )
    except Exception as e:
        logger.error(f"创建人设数据验证失败: {e}")
        return Ret.fail(msg=str(e))

    # 调用云端API创建人设
    response = await cloud_create_preset(preset_data)

    if not response.success or not response.data:
        return Ret.fail(msg=str(response.error))

    # 更新本地数据
    preset.remote_id = response.data.id
    preset.on_shared = True
    await preset.save()

    # 返回更详细的响应数据
    return Ret.success(
        msg="共享成功",
        data={
            "remote_id": response.data.id,
        },
    )


@router.post("/{preset_id}/unshare", summary="撤回共享人设")
@require_role(Role.Admin)
async def unshare_preset(
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """撤回已共享到云端的人设"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    # 检查是否已共享
    if not preset.on_shared or not preset.remote_id:
        return Ret.fail(msg="此人设未共享到云端")

    # 调用云端API删除人设
    instance_id = generate_instance_id()
    # 确保remote_id是字符串
    remote_id = str(preset.remote_id) if preset.remote_id else ""
    response = await cloud_delete_preset(remote_id, instance_id)

    # 无论成功与否，都清除本地共享状态
    preset.on_shared = False

    # 只有成功删除时才清除remote_id
    if response.success:
        preset.remote_id = ""  # 使用空字符串代替None，避免类型错误

    await preset.save()

    if not response.success:
        return Ret.fail(msg=str(response.error))

    return Ret.success(msg="撤回共享成功")


@router.post("/{preset_id}/sync-to-cloud", summary="同步人设到云端")
@require_role(Role.Admin)
async def sync_to_cloud(
    preset_id: int,
    is_sfw: bool = True,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Ret:
    """将本地修改的人设同步到云端"""
    preset = await DBPreset.get_or_none(id=preset_id)
    if not preset:
        return Ret.fail(msg="人设不存在")

    # 检查是否已共享
    if not preset.on_shared or not preset.remote_id:
        return Ret.fail(msg="此人设未共享到云端")

    # 检查必要字段
    if not preset.name:
        return Ret.fail(msg="人设名称不能为空")
    if not preset.content:
        return Ret.fail(msg="人设内容不能为空")
    if not preset.avatar:
        return Ret.fail(msg="人设头像不能为空")
    if not preset.description:
        return Ret.fail(msg="人设描述不能为空，请先编辑人设添加描述")

    # 准备人设数据
    instance_id = generate_instance_id()
    try:
        preset_data = PresetUpdate(
            name=preset.name,
            title=preset.title or preset.name,
            avatar=preset.avatar,
            content=preset.content,
            description=preset.description,
            tags=preset.tags,
            author=preset.author,
            ext_data=preset.ext_data or "",
            is_sfw=is_sfw,
            instance_id=instance_id,
        )
    except Exception as e:
        logger.error(f"更新人设数据验证失败: {e}")
        return Ret.fail(msg=str(e))

    # 确保remote_id是字符串
    remote_id = str(preset.remote_id) if preset.remote_id else ""

    # 调用云端API更新人设
    response = await cloud_update_preset(remote_id, preset_data)

    if not response.success:
        return Ret.fail(msg=str(response.error))

    return Ret.success(msg="同步成功")
