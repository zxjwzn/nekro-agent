import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiodocker
from aiodocker.docker import DockerContainer

from nekro_agent.core.config import config
from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import SANDBOX_SHARED_HOST_DIR, USER_UPLOAD_DIR, OsEnv
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.schemas.chat_message import ChatMessage

from .ext_caller import CODE_PREAMBLE, get_api_caller_code

# 主机共享目录
HOST_SHARED_DIR = (
    Path(SANDBOX_SHARED_HOST_DIR) if SANDBOX_SHARED_HOST_DIR.startswith("/") else Path(SANDBOX_SHARED_HOST_DIR).resolve()
)
# 用户上传目录
USER_UPLOAD_DIR = Path(USER_UPLOAD_DIR) if USER_UPLOAD_DIR.startswith("/") else Path(USER_UPLOAD_DIR).resolve()

IMAGE_NAME = config.SANDBOX_IMAGE_NAME  # Docker 镜像名称
CONTAINER_SHARE_DIR = "/app/shared"  # 容器内共享目录 (读写)
CONTAINER_UPLOAD_DIR = "/app/uploads"  # 容器上传目录 (只读)
CONTAINER_WORK_DIR = "/app"  # 容器工作目录

CODE_FILENAME = "run_script.py.code"  # 要执行的代码文件名
RUN_CODE_FILENAME = "run_script.py"  # 要执行的代码文件名

API_CALLER_FILENAME = "api_caller.py.code"  # 外部 API 调用器文件名
RUN_API_CALLER_FILENAME = "api_caller.py"  # 外部 API 调用器文件名

# 代码运行结束标记
CODE_RUN_END_FLAGS = {
    ExecStopType.NORMAL: "[SANDBOX_RUN_ENDS_WITH_NORMAL]",  # 正常结束 (exit code 0)
    ExecStopType.ERROR: "[SANDBOX_RUN_ENDS_WITH_ERROR]",  # 错误停止 (exit code 非0)
    ExecStopType.TIMEOUT: "[SANDBOX_RUN_ENDS_WITH_TIMEOUT]",  # 超时停止
    ExecStopType.AGENT: "[SANDBOX_RUN_ENDS_WITH_AGENT]",  # 代理停止 (exit code 8)
    ExecStopType.MANUAL: "[SANDBOX_RUN_ENDS_WITH_MANUAL]",  # 手动停止 (exit code 9)
}

EXEC_SCRIPT = f"""
rm -f {CONTAINER_WORK_DIR}/{RUN_CODE_FILENAME} &&
cp {CONTAINER_SHARE_DIR}/{CODE_FILENAME} {CONTAINER_WORK_DIR}/{RUN_CODE_FILENAME} &&
cp {CONTAINER_SHARE_DIR}/{API_CALLER_FILENAME} {CONTAINER_WORK_DIR}/{RUN_API_CALLER_FILENAME} &&
export MPLCONFIGDIR=/app/tmp/matplotlib &&
python {RUN_CODE_FILENAME}
exit_code=$?
if [ $exit_code -eq 0 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.NORMAL]}"
elif [ $exit_code -eq 8 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.AGENT]}"
elif [ $exit_code -eq 9 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.MANUAL]}"
else
    echo "{CODE_RUN_END_FLAGS[ExecStopType.ERROR]}"
fi
"""

# 会话沙盒活跃时间记录表
chat_key_sandbox_map: Dict[str, float] = {}

# 会话沙盒容器记录表
chat_key_sandbox_container_map: Dict[str, DockerContainer] = {}

# 会话清理任务记录表
chat_key_sandbox_cleanup_task_map: Dict[str, asyncio.Task] = {}

# 沙盒并发限制
semaphore = asyncio.Semaphore(config.SANDBOX_MAX_CONCURRENT)


async def limited_run_code(
    code_text: str,
    cot_content: str,
    from_chat_key: str,
    output_limit: int = 1000,
    generation_time: int = 0,
    chat_message: Optional[ChatMessage] = None,
) -> Tuple[str, int]:
    """限制并发运行代码"""

    async with semaphore:
        return await run_code_in_sandbox(
            code_text,
            cot_content,
            from_chat_key,
            output_limit,
            generation_time,
            chat_message,
        )


async def run_code_in_sandbox(
    code_text: str,
    cot_content: str,
    from_chat_key: str,
    output_limit: int,
    generation_time: int = 0,
    chat_message: Optional[ChatMessage] = None,
) -> Tuple[str, int]:
    """在沙盒容器中运行代码并获取输出"""

    # 记录开始时间
    start_time = time.time()

    # container_key = f'{time.strftime("%Y%m%d%H%M%S")}_{os.urandom(4).hex()}'
    container_key = f"sandbox_{from_chat_key}"
    container_name = f"nekro-agent-sandbox-{container_key}-{os.urandom(4).hex()}"

    host_shared_dir = Path(HOST_SHARED_DIR / container_key)
    host_shared_dir.mkdir(parents=True, exist_ok=True)

    # 写入预置依赖代码
    api_caller_file_path = Path(host_shared_dir) / API_CALLER_FILENAME
    api_caller_file_path.write_text(
        get_api_caller_code(container_key=container_key, from_chat_key=from_chat_key),
        encoding="utf-8",
    )

    # 写入要执行的代码
    code_file_path = Path(host_shared_dir) / CODE_FILENAME
    code_file_path.write_text(f"{CODE_PREAMBLE.strip()}\n\n{code_text}", encoding="utf-8")

    # 设置共享目录权限
    try:
        Path.chmod(host_shared_dir, 0o777)
        logger.debug(f"设置共享目录权限: {host_shared_dir} 777")
    except Exception as e:
        logger.error(f"设置共享目录权限失败: {e}")

    # 清理过期任务
    if from_chat_key in chat_key_sandbox_cleanup_task_map:
        try:
            chat_key_sandbox_cleanup_task_map[from_chat_key].cancel()
            logger.debug(f"清理过期任务: {from_chat_key}")
        except Exception as e:
            logger.error(f"清理过期任务失败: {e}")
        del chat_key_sandbox_cleanup_task_map[from_chat_key]

    # 清理过期沙盒
    if from_chat_key in chat_key_sandbox_container_map:
        try:
            await chat_key_sandbox_container_map[from_chat_key].delete()
            logger.debug(f"清理过期沙盒: {from_chat_key} | {container_name}")
        except Exception as e:
            if "404" in str(e):
                logger.debug(f"沙盒容器已不存在: {from_chat_key} | {container_name}")
            else:
                logger.error(f"清理过期沙盒失败: {e}")
        del chat_key_sandbox_container_map[from_chat_key]

    # 启动容器
    docker = aiodocker.Docker()
    container: DockerContainer = await docker.containers.run(
        name=container_name,
        config={
            "Image": IMAGE_NAME,
            "Cmd": ["bash", "-c", EXEC_SCRIPT],
            "HostConfig": {
                "Binds": [
                    f"{host_shared_dir}:{CONTAINER_SHARE_DIR}:rw",
                    f"{USER_UPLOAD_DIR}/{from_chat_key}:{CONTAINER_UPLOAD_DIR}:ro",
                ],
                "Memory": 512 * 1024 * 1024,  # 内存限制 (512MB)
                "NanoCPUs": 1000000000,  # CPU 限制 (1 core)
                "SecurityOpt": (
                    []
                    if OsEnv.RUN_IN_DOCKER
                    else [
                        # "no-new-privileges",  # 禁止提升权限
                        "apparmor=unconfined",  # 禁止 AppArmor 配置
                    ]
                ),
                "NetworkMode": "bridge",
                "ExtraHosts": ["host.docker.internal:host-gateway"],
            },
            "User": "nobody",  # 非特权用户
            "AutoRemove": True,
        },
    )
    chat_key_sandbox_container_map[from_chat_key] = container
    logger.debug(f"启动容器: {container_name} | ID: {container.id}")

    # 获取输出和退出类型
    output_text, stop_type = await run_container_with_timeout(
        container,
        config.SANDBOX_RUNNING_TIMEOUT,
    )

    # 记录执行耗时
    exec_time = int((time.time() - start_time) * 1000)  # 转换为毫秒
    # 记录总耗时（生成耗时 + 执行耗时）
    total_time = generation_time + exec_time

    logger.debug(f"容器 {container_name} 输出: {output_text} | 退出类型: {stop_type}")

    # 沙盒共享目录超过 30 分钟未活动，则自动清理
    async def cleanup_container_shared_dir(box_last_active_time):
        nonlocal from_chat_key, container
        await asyncio.sleep(30 * 60)
        if box_last_active_time == chat_key_sandbox_map.get(from_chat_key):
            try:
                shutil.rmtree(host_shared_dir)
            except Exception as e:
                logger.error(f"清理容器共享目录时发生错误: {e}")
            with contextlib.suppress(Exception):
                await container.delete()  # 清理沙盒

    box_last_active_time = time.time()
    chat_key_sandbox_map[from_chat_key] = box_last_active_time
    chat_key_sandbox_cleanup_task_map[from_chat_key] = asyncio.create_task(
        cleanup_container_shared_dir(box_last_active_time),
    )

    await DBExecCode.create(
        chat_key=from_chat_key,
        code_text=code_text,
        thought_chain=cot_content,
        outputs=output_text,
        success=stop_type in [ExecStopType.NORMAL, ExecStopType.AGENT],  # AGENT 状态也视为成功
        stop_type=stop_type,
        exec_time_ms=exec_time,
        generation_time_ms=generation_time,
        total_time_ms=total_time,
        trigger_user_id=int(chat_message.sender_id) if chat_message else 0,
        trigger_user_name=chat_message.sender_real_nickname if chat_message else "System",
    )

    final_output = (
        output_text
        if len(output_text) <= output_limit
        else f"(output too long, hidden {len(output_text) - output_limit} characters)...{output_text[-output_limit:]}"
    )
    return final_output, stop_type.value


async def run_container_with_timeout(container: DockerContainer, timeout: int) -> Tuple[str, ExecStopType]:
    """运行容器并返回输出结果和退出类型"""
    try:
        task = asyncio.create_task(asyncio.wait_for(container.wait(), timeout=timeout))
        await asyncio.wait_for(task, timeout=timeout)
        outputs = await container.log(stdout=True, stderr=True)
        await container.delete()
        logger.info(f"容器 {container.id} 运行结束退出")

        # 检查输出中的结束标记来确定退出类型
        output_text = "".join(outputs).strip()
        stop_type = ExecStopType.ERROR  # 默认为错误退出

        # 移除所有结束标记并确定退出类型
        for _type, end_flag in CODE_RUN_END_FLAGS.items():
            if end_flag in output_text:
                stop_type = _type
                output_text = output_text.replace(end_flag, "").strip()
                break

    except asyncio.TimeoutError:
        logger.warning(f"容器 {container.id} 运行超过 {timeout} 秒，强制停止容器")
        outputs = await container.log(stdout=True, stderr=True)
        outputs.append(f"# This container has been killed because it exceeded the {timeout} seconds limit.")
        await container.kill()
        await container.delete()
        output_text = "".join(outputs).strip()
        # 移除所有可能的结束标记
        for end_flag in CODE_RUN_END_FLAGS.values():
            output_text = output_text.replace(end_flag, "").strip()
        return output_text, ExecStopType.TIMEOUT
    else:
        return output_text, stop_type


async def cleanup_sandbox_containers():
    docker = aiodocker.Docker()
    try:
        containers = await docker.containers.list(all=True)
        for container in containers:
            container_info = await container.show()
            if IMAGE_NAME in container_info["Name"]:
                await container.kill()
                await container.delete()
                logger.info(f"已清理容器 {container_info['Name']}")
    finally:
        await docker.close()
