import glob
import itertools
import os
import random
import gc
import shutil
import subprocess
import platform
import re
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import utils
from app.config import config

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
video_codec = "libx264"  # 默认CPU编码器，会在初始化时根据GPU检测结果更新
fps = 30

# GPU编码器映射
GPU_ENCODERS = {
    "nvidia": "h264_nvenc",
    "intel": "h264_qsv",
    "amd": "h264_amf",
    "apple": "h264_videotoolbox",  # macOS
}

def check_nvidia_driver_version() -> bool:
    """
    检查NVIDIA驱动版本是否支持NVENC
    需要驱动版本 >= 570.0 (NVENC API 13.0)
    返回: True if supported, False otherwise
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        if result.returncode == 0 and result.stdout.strip():
            driver_version_str = result.stdout.strip().split(chr(10))[0]
            try:
                # 提取主版本号（例如 "570.61" -> 570）
                major_version = int(driver_version_str.split('.')[0])
                if major_version >= 570:
                    logger.debug(f"NVIDIA驱动版本: {driver_version_str} (支持NVENC)")
                    return True
                else:
                    logger.warning(f"NVIDIA驱动版本: {driver_version_str} (需要 >= 570.0 才能使用NVENC)")
                    return False
            except (ValueError, IndexError):
                logger.debug(f"无法解析NVIDIA驱动版本: {driver_version_str}")
                return False
    except Exception as e:
        logger.debug(f"检查NVIDIA驱动版本失败: {str(e)}")
    
    return False

def detect_gpu() -> Optional[str]:
    """
    检测可用的GPU类型
    返回: "nvidia", "intel", "amd", "apple" 或 None
    """
    try:
        system = platform.system().lower()
        
        # 检测NVIDIA GPU
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu_name = result.stdout.strip().split(chr(10))[0]
                logger.info(f"检测到NVIDIA GPU: {gpu_name}")
                # 检查驱动版本是否支持NVENC
                if check_nvidia_driver_version():
                    return "nvidia"
                else:
                    logger.warning("NVIDIA驱动版本过旧，无法使用NVENC硬件加速")
                    return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        
        # Windows系统检测Intel/AMD GPU
        if system == "windows":
            try:
                # 检测Intel GPU
                result = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get", "name"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if result.returncode == 0:
                    output = result.stdout.lower()
                    if "intel" in output and ("uhd" in output or "iris" in output or "xe" in output):
                        logger.info("检测到Intel GPU")
                        return "intel"
                    if "amd" in output or "radeon" in output:
                        logger.info("检测到AMD GPU")
                        return "amd"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        
        # macOS检测Apple Silicon
        if system == "darwin":
            try:
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0 and "Apple" in result.stdout:
                    logger.info("检测到Apple GPU")
                    return "apple"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        
        # Linux检测（可选）
        if system == "linux":
            try:
                # 检测Intel
                if os.path.exists("/sys/class/drm/card0/device/vendor"):
                    with open("/sys/class/drm/card0/device/vendor", "r") as f:
                        vendor_id = f.read().strip()
                        if vendor_id == "0x8086":  # Intel
                            logger.info("检测到Intel GPU")
                            return "intel"
                        elif vendor_id == "0x1002":  # AMD
                            logger.info("检测到AMD GPU")
                            return "amd"
            except Exception:
                pass
        
    except Exception as e:
        logger.debug(f"GPU检测失败: {str(e)}")
    
    return None

def get_ffmpeg_path() -> str:
    """
    获取FFmpeg可执行文件路径
    优先使用config.toml中配置的ffmpeg_path
    """
    # 优先使用config.toml中配置的ffmpeg_path
    ffmpeg_exe = config.app.get("ffmpeg_path", "")
    if ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        return ffmpeg_exe
    # 回退到环境变量
    ffmpeg_exe = os.environ.get("IMAGEIO_FFMPEG_EXE", "ffmpeg")
    if os.path.isfile(ffmpeg_exe):
        return ffmpeg_exe
    # 最后回退到系统PATH中的ffmpeg
    return "ffmpeg"

def check_ffmpeg_encoder_support(encoder_name: str) -> bool:
    """
    检查FFmpeg是否支持指定的编码器
    """
    try:
        # 获取FFmpeg路径
        ffmpeg_exe = get_ffmpeg_path()
        
        result = subprocess.run(
            [ffmpeg_exe, "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        
        if result.returncode == 0:
            return encoder_name in result.stdout
    except Exception as e:
        logger.debug(f"检查FFmpeg编码器支持失败: {str(e)}")
    
    return False

def check_ffmpeg_filter_support(filter_name: str) -> bool:
    """
    检查FFmpeg是否支持指定的滤镜
    """
    try:
        # 获取FFmpeg路径
        ffmpeg_exe = get_ffmpeg_path()
        
        result = subprocess.run(
            [ffmpeg_exe, "-filters"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        
        if result.returncode == 0:
            return filter_name in result.stdout
    except Exception as e:
        logger.debug(f"检查FFmpeg滤镜支持失败: {str(e)}")
    
    return False

def get_gpu_scale_filter(gpu_type: Optional[str]) -> Optional[str]:
    """
    根据GPU类型返回对应的GPU缩放滤镜
    返回: 滤镜名称或None（使用CPU缩放）
    """
    if not gpu_type:
        return None
    
    gpu_filters = {
        "nvidia": "scale_npp",  # NVIDIA GPU缩放
        "intel": "scale_qsv",   # Intel GPU缩放
        "amd": "scale",          # AMD暂不支持GPU缩放，使用CPU
        "apple": "scale",        # Apple暂不支持GPU缩放，使用CPU
    }
    
    filter_name = gpu_filters.get(gpu_type)
    if filter_name and filter_name != "scale":
        # 检查FFmpeg是否支持该GPU滤镜
        if check_ffmpeg_filter_support(filter_name):
            logger.debug(f"✅ 检测到GPU缩放滤镜支持: {filter_name}")
            return filter_name
        else:
            logger.debug(f"⚠️ GPU缩放滤镜 {filter_name} 不支持，回退到CPU缩放")
    
    return None

# 全局变量：缓存GPU类型和缩放滤镜
_cached_gpu_type = None
_cached_scale_filter = None

def get_gpu_scale_filter_cached() -> Optional[str]:
    """
    获取GPU缩放滤镜（带缓存）
    """
    global _cached_gpu_type, _cached_scale_filter
    
    if _cached_scale_filter is None:
        if _cached_gpu_type is None:
            _cached_gpu_type = detect_gpu()
        _cached_scale_filter = get_gpu_scale_filter(_cached_gpu_type)
        if _cached_scale_filter:
            logger.info(f"✅ 使用GPU缩放: {_cached_scale_filter}")
        else:
            logger.info("ℹ️ 使用CPU缩放")
    
    return _cached_scale_filter

def get_best_video_codec() -> Tuple[str, str]:
    """
    自动选择最佳的视频编码器
    返回: (编码器名称, 描述信息)
    """
    gpu_type = detect_gpu()
    
    if gpu_type and gpu_type in GPU_ENCODERS:
        encoder = GPU_ENCODERS[gpu_type]
        if check_ffmpeg_encoder_support(encoder):
            gpu_names = {
                "nvidia": "NVIDIA GPU",
                "intel": "Intel GPU",
                "amd": "AMD GPU",
                "apple": "Apple GPU"
            }
            logger.info(f"✅ 使用GPU硬件加速: {encoder} ({gpu_names[gpu_type]})")
            return encoder, f"{encoder} ({gpu_names[gpu_type]})"
        else:
            logger.warning(f"⚠️ 检测到{gpu_type.upper()} GPU，但FFmpeg不支持{encoder}，回退到CPU编码")
    
    logger.info("ℹ️ 使用CPU编码: libx264")
    return "libx264", "libx264 (CPU)"

def write_videofile_with_fallback(clip, filename, codec=None, fallback_codec="libx264", **kwargs):
    """
    带错误回退的write_videofile包装函数
    如果GPU编码失败，自动回退到CPU编码
    """
    if codec is None:
        codec = video_codec
    
    # 如果是GPU编码器，尝试使用，失败则回退
    if codec != fallback_codec and codec in GPU_ENCODERS.values():
        try:
            clip.write_videofile(filename, codec=codec, **kwargs)
            return
        except Exception as e:
            error_msg = str(e).lower()
            # 检查是否是驱动版本或编码器相关的错误
            if any(keyword in error_msg for keyword in ["nvenc", "driver", "encoder", "not support", "invalid argument"]):
                logger.warning(f"⚠️ GPU编码器 {codec} 失败: {str(e)[:200]}")
                logger.info(f"🔄 自动回退到CPU编码: {fallback_codec}")
                # 回退到CPU编码
                clip.write_videofile(filename, codec=fallback_codec, **kwargs)
                return
            else:
                # 其他错误，直接抛出
                raise
    
    # 直接使用指定编码器（通常是CPU编码）
    clip.write_videofile(filename, codec=codec, **kwargs)

# 初始化时自动检测并设置最佳编码器
_video_codec, _video_codec_desc = get_best_video_codec()
video_codec = _video_codec
logger.info(f"视频编码器已设置为: {_video_codec_desc}")

# 初始化时检测GPU缩放滤镜
get_gpu_scale_filter_cached()  # 触发检测并缓存结果

def resize_clip_with_gpu(
    input_path: str,
    output_path: str,
    target_width: int,
    target_height: int,
    gpu_scale_filter: Optional[str] = None,
    codec: str = None,
    fps: int = 30
) -> bool:
    """
    使用GPU或CPU缩放视频
    返回: True if success, False otherwise
    """
    try:
        # 获取FFmpeg路径
        ffmpeg_exe = get_ffmpeg_path()
        
        if codec is None:
            codec = video_codec
        
        # 构建FFmpeg命令
        cmd = [
            ffmpeg_exe,
            "-i", input_path,
            "-vf", f"{gpu_scale_filter if gpu_scale_filter else 'scale'}={target_width}:{target_height}",
            "-c:v", codec,
            "-preset", "fast",
            "-crf", "23",
            "-r", str(fps),
            "-y",  # 覆盖输出文件
            output_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        else:
            logger.warning(f"GPU缩放失败: {result.stderr[:200] if result.stderr else 'unknown error'}")
            return False
    except Exception as e:
        logger.warning(f"GPU缩放异常: {str(e)}")
        return False

def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]
        
    for file in files:
        try:
            os.remove(file)
        except:
            pass

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file and os.path.exists(bgm_file):
        return bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        return random.choice(files)

    return ""


def process_single_clip(
    subclipped_item: SubClippedVideoClip,
    clip_index: int,
    video_width: int,
    video_height: int,
    output_dir: str,
    max_clip_duration: int,
    video_transition_mode: VideoTransitionMode,
    gpu_scale_filter: Optional[str] = None,
) -> Optional[SubClippedVideoClip]:
    """
    处理单个clip：加载、缩放、添加转场效果、写入文件
    返回: 处理后的SubClippedVideoClip或None（失败时）
    """
    try:
        logger.debug(f"processing clip {clip_index+1}: {subclipped_item.width}x{subclipped_item.height}")
        
        # 1. 加载视频
        clip = VideoFileClip(subclipped_item.file_path).subclipped(
            subclipped_item.start_time, 
            subclipped_item.end_time
        )
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        
        # 2. 缩放处理（如果需要）
        needs_resize = clip_w != video_width or clip_h != video_height
        temp_resized_path = None
        gpu_resize_success = False
        
        if needs_resize:
            clip_ratio = clip.w / clip.h
            video_ratio = video_width / video_height
            logger.debug(f"resizing clip {clip_index+1}, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, target: {video_width}x{video_height}, ratio: {video_ratio:.2f}")
            
            # 尝试使用GPU缩放（仅当宽高比相同时，GPU缩放更简单高效）
            if gpu_scale_filter and clip_ratio == video_ratio:
                temp_resized_path = f"{output_dir}/temp-resized-{clip_index+1}.mp4"
                # 先保存原始clip到临时文件（因为GPU缩放需要文件输入）
                temp_input_path = f"{output_dir}/temp-input-{clip_index+1}.mp4"
                try:
                    # 使用快速编码保存临时文件
                    clip.write_videofile(
                        temp_input_path,
                        codec="libx264",
                        preset="ultrafast",
                        logger=None,
                        fps=fps,
                        audio=False
                    )
                    
                    if os.path.exists(temp_input_path):
                        if resize_clip_with_gpu(
                            temp_input_path,
                            temp_resized_path,
                            video_width,
                            video_height,
                            gpu_scale_filter,
                            codec=video_codec,
                            fps=fps
                        ) and os.path.exists(temp_resized_path):
                            # GPU缩放成功，重新加载缩放后的视频
                            close_clip(clip)
                            clip = VideoFileClip(temp_resized_path)
                            clip_w, clip_h = clip.size
                            gpu_resize_success = True
                            logger.debug(f"✅ clip {clip_index+1} GPU缩放成功")
                except Exception as e:
                    logger.debug(f"GPU缩放失败，回退到CPU: {str(e)[:100]}")
                finally:
                    # 清理临时输入文件
                    try:
                        if os.path.exists(temp_input_path):
                            os.remove(temp_input_path)
                    except:
                        pass
            
            # 如果GPU缩放失败或未使用GPU，使用MoviePy CPU缩放
            if not gpu_resize_success:
                if clip_ratio == video_ratio:
                    clip = clip.resized(new_size=(video_width, video_height))
                else:
                    if clip_ratio > video_ratio:
                        scale_factor = video_width / clip_w
                    else:
                        scale_factor = video_height / clip_h

                    new_width = int(clip_w * scale_factor)
                    new_height = int(clip_h * scale_factor)

                    background = ColorClip(size=(video_width, video_height), color=(0, 0, 0)).with_duration(clip_duration)
                    clip_resized = clip.resized(new_size=(new_width, new_height)).with_position("center")
                    clip = CompositeVideoClip([background, clip_resized])
        
        # 3. 添加转场效果
        shuffle_side = random.choice(["left", "right", "top", "bottom"])
        if video_transition_mode.value == VideoTransitionMode.none.value:
            pass  # 不添加转场
        elif video_transition_mode.value == VideoTransitionMode.fade_in.value:
            clip = video_effects.fadein_transition(clip, 1)
        elif video_transition_mode.value == VideoTransitionMode.fade_out.value:
            clip = video_effects.fadeout_transition(clip, 1)
        elif video_transition_mode.value == VideoTransitionMode.slide_in.value:
            clip = video_effects.slidein_transition(clip, 1, shuffle_side)
        elif video_transition_mode.value == VideoTransitionMode.slide_out.value:
            clip = video_effects.slideout_transition(clip, 1, shuffle_side)
        elif video_transition_mode.value == VideoTransitionMode.shuffle.value:
            transition_funcs = [
                lambda c: video_effects.fadein_transition(c, 1),
                lambda c: video_effects.fadeout_transition(c, 1),
                lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
            ]
            shuffle_transition = random.choice(transition_funcs)
            clip = shuffle_transition(clip)

        # 4. 裁剪到最大时长
        if clip.duration > max_clip_duration:
            clip = clip.subclipped(0, max_clip_duration)
        
        # 5. 写入临时文件
        clip_file = f"{output_dir}/temp-clip-{clip_index+1}.mp4"
        write_videofile_with_fallback(clip, clip_file, codec=video_codec, logger=None, fps=fps)
        
        # 6. 清理资源
        close_clip(clip)
        if temp_resized_path and os.path.exists(temp_resized_path):
            try:
                os.remove(temp_resized_path)
            except:
                pass
        
        # 7. 返回处理结果
        return SubClippedVideoClip(
            file_path=clip_file,
            duration=clip_duration if clip_duration <= max_clip_duration else max_clip_duration,
            width=clip_w,
            height=clip_h
        )
        
    except Exception as e:
        logger.error(f"failed to process clip {clip_index+1}: {str(e)}")
        return None


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.sequential,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    audio_duration = audio_clip.duration
    logger.info(f"audio duration: {audio_duration} seconds")
    # Required duration of each clip
    req_dur = audio_duration / len(video_paths)
    req_dur = max_clip_duration
    logger.info(f"maximum clip duration: {req_dur} seconds")
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = VideoFileClip(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + max_clip_duration, clip_duration)            
            if clip_duration - start_time >= max_clip_duration:
                subclipped_items.append(SubClippedVideoClip(file_path= video_path, start_time=start_time, end_time=end_time, width=clip_w, height=clip_h))
            start_time = end_time    
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    # random subclipped_items order
    if video_concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(subclipped_items)
    
    # If using original aspect ratio, use the first clip's resolution as target
    if video_width is None or video_height is None:
        if subclipped_items:
            video_width = subclipped_items[0].width
            video_height = subclipped_items[0].height
            logger.info(f"using original aspect ratio: {video_width}x{video_height} (from first clip)")
        else:
            # Fallback to portrait if no clips available
            video_width, video_height = 1080, 1920
            logger.warning("no clips available, using default resolution 1080x1920")
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # 获取GPU缩放滤镜（如果支持）
    gpu_scale_filter = get_gpu_scale_filter_cached()
    
    # 并行处理clips
    # 限制并发数量，避免内存溢出（使用CPU核心数，但至少为1，最多不超过clip数量）
    max_workers = min(len(subclipped_items), max(1, os.cpu_count() or 4))
    logger.info(f"🚀 使用并行处理: {max_workers} 个worker处理 {len(subclipped_items)} 个clips")
    
    # 筛选需要处理的clips（根据音频时长）
    clips_to_process = []
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration > audio_duration:
            break
        clips_to_process.append((i, subclipped_item))
    
    # 使用线程池并行处理
    processed_clips_dict = {}  # 使用字典保持顺序
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_index = {
            executor.submit(
                process_single_clip,
                subclipped_item,
                i,
                video_width,
                video_height,
                output_dir,
                max_clip_duration,
                video_transition_mode,
                gpu_scale_filter,
            ): i
            for i, subclipped_item in clips_to_process
        }
        
        # 收集结果
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            try:
                result = future.result()
                if result:
                    processed_clips_dict[i] = result
                    video_duration += result.duration
                    logger.debug(f"✅ clip {i+1} 处理完成, duration: {result.duration:.2f}s, total: {video_duration:.2f}s")
                else:
                    logger.warning(f"⚠️ clip {i+1} 处理失败")
            except Exception as e:
                logger.error(f"❌ clip {i+1} 处理异常: {str(e)}")
    
    # 按索引排序，保持原始顺序
    processed_clips = [processed_clips_dict[i] for i in sorted(processed_clips_dict.keys())]
    
    # loop processed clips until the video duration matches or exceeds the audio duration.
    if video_duration < audio_duration:
        logger.warning(f"video duration ({video_duration:.2f}s) is shorter than audio duration ({audio_duration:.2f}s), looping clips to match audio length.")
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= audio_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        logger.info(f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, looped {len(processed_clips)-len(base_clips)} clips")
     
    # merge video clips using FFmpeg concat demuxer (much faster than MoviePy concatenate)
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files(processed_clips)
        logger.info("video combining completed")
        return combined_video_path
    
    # 优化：使用FFmpeg的concat demuxer一次性合并所有视频
    # 这种方式不需要重新编码，只是简单的文件拼接，速度极快
    try:
        # 创建concat文件列表
        concat_file = os.path.join(output_dir, "concat_list.txt")
        with open(concat_file, "w", encoding="utf-8") as f:
            for clip in processed_clips:
                # 使用绝对路径，并转义特殊字符
                clip_path = os.path.abspath(clip.file_path)
                if os.name == "nt":
                    # Windows路径转义
                    clip_path = clip_path.replace("\\", "/")
                # 写入格式：file 'path/to/file.mp4'
                f.write(f"file '{clip_path}'\n")
        
        # 使用FFmpeg concat demuxer合并视频
        ffmpeg_exe = get_ffmpeg_path()
        concat_file_abs = os.path.abspath(concat_file)
        if os.name == "nt":
            concat_file_abs = concat_file_abs.replace("\\", "/")
        
        logger.info(f"🚀 使用FFmpeg concat demuxer合并 {len(processed_clips)} 个视频（性能优化）")
        
        cmd = [
            ffmpeg_exe,
            "-f", "concat",
            "-safe", "0",  # 允许绝对路径
            "-i", concat_file_abs,
            "-c", "copy",  # 直接复制流，不重新编码（极快）
            "-y",  # 覆盖输出文件
            combined_video_path
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        
        if result.returncode == 0 and os.path.exists(combined_video_path):
            logger.info("✅ FFmpeg concat合并成功")
            # 清理临时文件
            try:
                if os.path.exists(concat_file):
                    os.remove(concat_file)
                clip_files = [clip.file_path for clip in processed_clips]
                delete_files(clip_files)
            except:
                pass
            logger.info("video combining completed")
            return combined_video_path
        else:
            error_msg = result.stderr[:500] if result.stderr else "unknown error"
            logger.warning(f"⚠️ FFmpeg concat合并失败: {error_msg}")
            logger.info("🔄 回退到MoviePy方式...")
            # 回退到MoviePy方式
    except Exception as e:
        logger.warning(f"⚠️ FFmpeg concat合并异常: {str(e)}")
        logger.info("🔄 回退到MoviePy方式...")
    
    # 回退方案：使用MoviePy逐个合并（原方式）
    # create initial video file as base
    base_clip_path = processed_clips[0].file_path
    temp_merged_video = f"{output_dir}/temp-merged-video.mp4"
    temp_merged_next = f"{output_dir}/temp-merged-next.mp4"
    
    # copy first clip as initial merged video
    shutil.copy(base_clip_path, temp_merged_video)
    
    # merge remaining video clips one by one
    for i, clip in enumerate(processed_clips[1:], 1):
        logger.info(f"merging clip {i}/{len(processed_clips)-1}, duration: {clip.duration:.2f}s")
        
        try:
            # load current base video and next clip to merge
            base_clip = VideoFileClip(temp_merged_video)
            next_clip = VideoFileClip(clip.file_path)
            
            # merge these two clips
            merged_clip = concatenate_videoclips([base_clip, next_clip])

            # save merged result to temp file
            write_videofile_with_fallback(
                merged_clip,
                filename=temp_merged_next,
                codec=video_codec,
                threads=threads,
                logger=None,
                temp_audiofile_path=output_dir,
                audio_codec=audio_codec,
                fps=fps,
            )
            close_clip(base_clip)
            close_clip(next_clip)
            close_clip(merged_clip)
            
            # replace base file with new merged file
            delete_files(temp_merged_video)
            os.rename(temp_merged_next, temp_merged_video)
            
        except Exception as e:
            logger.error(f"failed to merge clip: {str(e)}")
            continue
    
    # after merging, rename final result to target file name
    os.rename(temp_merged_video, combined_video_path)
    
    # clean temp files
    clip_files = [clip.file_path for clip in processed_clips]
    delete_files(clip_files)
    
    # 清理concat文件（如果存在）
    try:
        concat_file = os.path.join(output_dir, "concat_list.txt")
        if os.path.exists(concat_file):
            os.remove(concat_file)
    except:
        pass
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def hex_to_ass_color(hex_color: str) -> str:
    """
    将十六进制颜色转换为ASS格式的BGR颜色（十六进制格式）
    例如: #FFFFFF -> &HFFFFFF& (白色), #000000 -> &H000000& (黑色)
    ASS格式使用BGR顺序，不是RGB
    ASS标准格式：&HBBGGRR& (BGR顺序，十六进制)
    """
    hex_color = hex_color.strip().lstrip('#')
    if len(hex_color) != 6:
        return "&HFFFFFF&"  # 默认白色
    
    try:
        r = hex_color[0:2]  # 红色
        g = hex_color[2:4]  # 绿色
        b = hex_color[4:6]  # 蓝色
        # ASS使用BGR格式：&HBBGGRR&
        ass_color = f"&H{b}{g}{r}&"
        return ass_color
    except (ValueError, IndexError):
        return "&HFFFFFF&"  # 默认白色


def srt_time_to_ass_time(srt_time: str) -> str:
    """
    将SRT时间格式转换为ASS时间格式
    SRT: 00:00:01,234 (逗号分隔毫秒)
    ASS: 0:00:01.23 (点分隔百分秒，且去掉前导零)
    """
    # 替换逗号为点，并处理格式
    time_str = srt_time.replace(',', '.')
    parts = time_str.split(':')
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds_parts = parts[2].split('.')
        seconds = int(seconds_parts[0])
        # 将毫秒转换为百分秒（保留2位）
        if len(seconds_parts) > 1:
            centiseconds = seconds_parts[1][:2].ljust(2, '0')
        else:
            centiseconds = "00"
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds}"
    return time_str


def get_ass_alignment_and_margin(position: str, custom_position: float, video_height: int, font_size: int) -> Tuple[int, int]:
    """
    根据位置参数计算ASS格式的Alignment和MarginV
    ASS Alignment值:
    1 = 左下, 2 = 中下, 3 = 右下
    4 = 左中, 5 = 中中, 6 = 右中
    7 = 左上, 8 = 中上, 9 = 右上
    MarginV: 垂直边距（像素）
    """
    if position == "top":
        alignment = 8  # 中上
        margin_v = int(video_height * 0.05)
    elif position == "bottom":
        alignment = 2  # 中下
        margin_v = int(video_height * 0.05)
    elif position == "center":
        alignment = 5  # 中中
        margin_v = 0
    elif position == "custom":
        alignment = 5  # 中中
        # custom_position是百分比（从顶部），转换为像素
        margin_v = int((video_height - font_size) * (custom_position / 100))
        # 确保在有效范围内
        margin_v = max(10, min(margin_v, video_height - font_size - 10))
    else:
        alignment = 2  # 默认中下
        margin_v = int(video_height * 0.05)
    
    return alignment, margin_v


def get_font_internal_name(font_path: str) -> str:
    """
    使用PIL获取字体的真实内部名称
    返回: 字体族名称，如果失败则返回默认值
    """
    try:
        if not os.path.exists(font_path):
            logger.warning(f"字体文件不存在: {font_path}")
            return "Arial"
        
        # 使用PIL加载字体并获取真实名称
        font = ImageFont.truetype(font_path, 10)  # 使用小尺寸加载，仅用于获取名称
        # getname()返回(family, style)元组
        font_family, font_style = font.getname()
        logger.debug(f"字体真实名称: {font_family} (样式: {font_style})")
        return font_family
    except Exception as e:
        logger.warning(f"无法获取字体内部名称 {font_path}: {str(e)}")
        # 回退到文件名映射
        font_basename = os.path.basename(font_path).lower()
        font_mapping = {
            "microsoftyaheibold.ttc": "Microsoft YaHei",
            "microsoftyaheinormal.ttc": "Microsoft YaHei",
            "stheitimedium.ttc": "STHeiti",
            "stheitilight.ttc": "STHeiti",
            "charm-bold.ttf": "Charm",
            "charm-regular.ttf": "Charm",
        }
        font_name = font_mapping.get(font_basename)
        if not font_name:
            # 从文件名提取
            font_name = os.path.splitext(font_basename)[0]
            for suffix in ["bold", "regular", "medium", "light", "normal"]:
                if font_name.endswith(suffix):
                    font_name = font_name[:-len(suffix)].strip()
            font_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', font_name)
            font_name = font_name.title()
        return font_name or "Arial"


def srt_to_ass(srt_path: str, ass_path: str, params: VideoParams, video_width: int, video_height: int) -> bool:
    """
    将SRT字幕文件转换为ASS格式，应用所有样式参数
    返回: True if success, False otherwise
    """
    try:
        # 读取SRT文件（使用subtitle模块的函数）
        from app.services import subtitle
        subtitle_items = subtitle.file_to_subtitles(srt_path)
        
        if not subtitle_items:
            logger.warning(f"SRT文件为空或格式错误: {srt_path}")
            return False
        
        # 获取字体路径
        font_path = ""
        if params.subtitle_enabled:
            if not params.font_name:
                params.font_name = "STHeitiMedium.ttc"
            font_path = os.path.join(utils.font_dir(), params.font_name)
            if os.name == "nt":
                font_path = font_path.replace("\\", "/")
            
            # 验证字体文件是否存在
            if not os.path.exists(font_path):
                logger.error(f"❌ 字体文件不存在: {font_path}")
                return False
        
        # 使用PIL获取字体的真实内部名称
        font_name = get_font_internal_name(font_path) if font_path else "Arial"
        logger.info(f"📝 使用字体: {font_name} (文件: {os.path.basename(font_path) if font_path else 'N/A'})")
        
        # 转换颜色
        primary_color = hex_to_ass_color(params.text_fore_color or "#FFFFFF")
        outline_color = hex_to_ass_color(params.stroke_color or "#000000")
        
        # 字体大小和描边宽度
        # 根据视频分辨率缩放字体大小（默认60是针对1080p的）
        base_font_size = int(params.font_size or 60)
        logger.debug(f"字体大小计算: 基础大小={base_font_size}, 视频高度={video_height}")
        
        # 如果视频高度不是1920（标准竖屏1080p），按比例缩放字体
        if video_height != 1920:
            # 计算缩放比例（基于高度）
            scale_factor = video_height / 1920.0
            font_size = int(base_font_size * scale_factor)
            # 确保最小字体大小：基于视频高度的5%（至少40像素）
            # 对于1248高度的视频，最小字体约为62像素
            min_font_size = max(40, int(video_height * 0.05))
            font_size = max(min_font_size, min(font_size, 200))
            logger.debug(f"字体大小缩放: 缩放比例={scale_factor:.2f}, 计算后={int(base_font_size * scale_factor)}, 最小值={min_font_size}, 最终={font_size}")
        else:
            font_size = base_font_size
            # 即使对于1920高度的视频，也确保最小字体大小
            font_size = max(40, font_size)
            logger.debug(f"字体大小: 最终={font_size}")
        
        # 描边宽度也需要按比例缩放
        base_outline_width = float(params.stroke_width or 1.5)
        if video_height != 1920:
            outline_width = int(base_outline_width * scale_factor)
            outline_width = max(1, min(outline_width, 10))
        else:
            outline_width = int(base_outline_width)
        
        # 计算位置（使用缩放后的字体大小）
        alignment, margin_v = get_ass_alignment_and_margin(
            params.subtitle_position or "bottom",
            params.custom_position,
            video_height,
            font_size
        )
        
        # 生成ASS文件
        ass_lines = []
        
        # ASS文件头 - 关键：必须设置PlayResX和PlayResY，否则字幕坐标会错误
        ass_lines.append("[Script Info]")
        ass_lines.append("Title: MoneyPrinterTurbo Subtitle")
        ass_lines.append("ScriptType: v4.00+")
        ass_lines.append(f"PlayResX: {video_width}")  # 设置播放分辨率宽度
        ass_lines.append(f"PlayResY: {video_height}")  # 设置播放分辨率高度
        ass_lines.append("")
        ass_lines.append("[V4+ Styles]")
        ass_lines.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
        
        # 样式定义
        style_name = "Default"
        # ASS格式参数顺序（共23个参数）:
        # 1. Name, 2. Fontname, 3. Fontsize, 4. PrimaryColour, 5. SecondaryColour, 6. OutlineColour, 7. BackColour,
        # 8. Bold, 9. Italic, 10. Underline, 11. StrikeOut, 12. ScaleX, 13. ScaleY, 14. Spacing, 15. Angle,
        # 16. BorderStyle, 17. Outline, 18. Shadow, 19. Alignment, 20. MarginL, 21. MarginR, 22. MarginV, 23. Encoding
        # SecondaryColour和BackColour也使用十六进制格式
        secondary_color = "&HFFFFFF&"  # 默认白色
        back_color = "&H000000&"  # 默认黑色背景（通常设为0表示透明）
        
        # 修复：确保参数顺序正确，ScaleX和ScaleY必须是100（不是0）
        # Bold=0, Italic=0, Underline=0, StrikeOut=0, ScaleX=100, ScaleY=100, Spacing=0, Angle=0
        # BorderStyle=1, Outline={outline_width}, Shadow=0, Alignment={alignment}
        style_line = f"Style: {style_name},{font_name},{font_size},{primary_color},{secondary_color},{outline_color},{back_color},0,0,0,0,100,100,0,0,1,{outline_width},0,{alignment},10,10,{margin_v},1"
        ass_lines.append(style_line)
        
        # 验证Style行参数（用于调试）
        style_params = style_line.split(":")[1].split(",")
        if len(style_params) >= 23:
            logger.debug(f"Style行验证: ScaleX={style_params[11]}, ScaleY={style_params[12]}, Alignment={style_params[18]}, FontSize={style_params[2]}")
            # 检查关键参数
            if style_params[11] != "100" or style_params[12] != "100":
                logger.error(f"❌ Style行错误: ScaleX={style_params[11]}, ScaleY={style_params[12]} (应该是100)")
            if style_params[18] == "0":
                logger.error(f"❌ Style行错误: Alignment={style_params[18]} (不能为0)")
        else:
            logger.warning(f"⚠️ Style行参数数量不正确: {len(style_params)} (应该是23)")
        ass_lines.append("")
        ass_lines.append("[Events]")
        ass_lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
        
        # 转换字幕事件
        dialogue_count = 0
        for idx, (index, time_line, text) in enumerate(subtitle_items):
            # 解析时间
            time_match = re.match(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", time_line)
            if not time_match:
                continue
            
            start_time = srt_time_to_ass_time(time_match.group(1))
            end_time = srt_time_to_ass_time(time_match.group(2))
            
            # 清理文本（移除HTML标签等）
            text = text.strip()
            if not text:
                continue
            
            # 先处理换行，再转义其他字符
            text = text.replace("\r\n", "\n").replace("\r", "\n")  # 统一换行符
            # 转义ASS特殊字符（顺序很重要）
            text = text.replace("\\", "\\\\")  # 先转义反斜杠
            text = text.replace("{", "\\{")
            text = text.replace("}", "\\}")
            text = text.replace("\n", "\\N")  # ASS换行符
            
            # 字幕事件格式: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
            ass_lines.append(f"Dialogue: 0,{start_time},{end_time},{style_name},,0,0,0,,{text}")
            dialogue_count += 1
        
        if dialogue_count == 0:
            logger.warning("⚠️ 没有有效的字幕事件，ASS文件可能为空")
            return False
        
        # 写入ASS文件（使用绝对路径）
        ass_path_abs = os.path.abspath(ass_path)
        with open(ass_path_abs, "w", encoding="utf-8-sig") as f:  # UTF-8 with BOM for Windows compatibility
            f.write("\n".join(ass_lines))
        
        # 输出关键信息用于调试
        logger.info(f"✅ SRT转换为ASS成功: {ass_path_abs}")
        logger.info(f"   - 分辨率: {video_width}x{video_height}")
        logger.info(f"   - 字体: {font_name} (大小: {font_size}, 描边: {outline_width})")
        logger.info(f"   - 位置: alignment={alignment}, margin_v={margin_v}")
        logger.info(f"   - 字幕事件数: {dialogue_count}")
        
        # 验证ASS文件内容（输出前几行用于调试）
        if logger._core.min_level <= 10:  # DEBUG级别
            preview_lines = "\n".join(ass_lines[:15])  # 前15行
            logger.debug(f"ASS文件预览:\n{preview_lines}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ SRT转ASS失败: {str(e)}")
        return False


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()
    
    # If using original aspect ratio, get resolution from the input video
    if video_width is None or video_height is None:
        input_clip = VideoFileClip(video_path)
        video_width, video_height = input_clip.size
        close_clip(input_clip)
        logger.info(f"using original aspect ratio: {video_width} x {video_height} (from input video)")

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=params.font_size
        )
        interline = int(params.font_size * 0.25)
        size=(int(max_width), int(txt_height + params.font_size * 0.25 + (interline * (wrapped_txt.count("\n") + 1))))

        _clip = TextClip(
            text=wrapped_txt,
            font=font_path,
            font_size=params.font_size,
            color=params.text_fore_color,
            bg_color=params.text_background_color,
            stroke_color=params.stroke_color,
            stroke_width=params.stroke_width,
            # interline=interline,
            # size=size,
        )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    # 处理字幕：优先使用FFmpeg ass滤镜（更快），失败则回退到MoviePy
    use_ffmpeg_subtitle = False
    ass_path = None
    
    if subtitle_path and os.path.exists(subtitle_path) and params.subtitle_enabled:
        # 尝试使用FFmpeg ass滤镜
        ass_path = os.path.join(output_dir, "subtitle.ass")
        if srt_to_ass(subtitle_path, ass_path, params, video_width, video_height):
            # 检查FFmpeg是否支持ass滤镜
            if check_ffmpeg_filter_support("ass"):
                use_ffmpeg_subtitle = True
                logger.info("✅ 使用FFmpeg ass滤镜渲染字幕（性能优化）")
            else:
                logger.warning("⚠️ FFmpeg不支持ass滤镜，回退到MoviePy方式")
                use_ffmpeg_subtitle = False
        else:
            logger.warning("⚠️ SRT转ASS失败，回退到MoviePy方式")
            use_ffmpeg_subtitle = False
    
    video_clip = VideoFileClip(video_path).without_audio()
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    # 如果使用FFmpeg字幕，不需要在MoviePy中处理字幕
    if subtitle_path and os.path.exists(subtitle_path) and params.subtitle_enabled and not use_ffmpeg_subtitle:
        # 回退到MoviePy方式
        def make_textclip(text):
            return TextClip(
                text=text,
                font=font_path,
                font_size=params.font_size,
            )

        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)
        video_clip = CompositeVideoClip([video_clip, *text_clips])
        logger.info("ℹ️ 使用MoviePy TextClip渲染字幕（回退模式）")

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file).with_effects(
                [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                    afx.AudioLoop(duration=video_clip.duration),
                ]
            )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    video_clip = video_clip.with_audio(audio_clip)
    
    # 如果使用FFmpeg字幕，需要先生成无字幕视频，然后通过FFmpeg添加字幕
    if use_ffmpeg_subtitle and ass_path:
        # 先生成临时无字幕视频
        temp_video_no_sub = os.path.join(output_dir, "temp_no_subtitle.mp4")
        try:
            write_videofile_with_fallback(
                video_clip,
                temp_video_no_sub,
                codec=video_codec,
                audio_codec=audio_codec,
                temp_audiofile_path=output_dir,
                threads=params.n_threads or 2,
                logger=None,
                fps=fps,
            )
            video_clip.close()
            del video_clip
            
            # 使用FFmpeg添加字幕
            logger.info("🎬 使用FFmpeg添加字幕...")
            # 使用统一的FFmpeg路径获取函数
            ffmpeg_exe = get_ffmpeg_path()
            
            # 处理Windows路径：转换为绝对路径
            ass_path_abs = os.path.abspath(ass_path)
            temp_video_abs = os.path.abspath(temp_video_no_sub)
            output_file_abs = os.path.abspath(output_file)
            
            # Windows下，FFmpeg的ass滤镜路径需要特殊处理
            # 问题：FFmpeg在处理Windows路径时，会将路径中的冒号（:）误认为是滤镜参数的分隔符
            # 解决方案：使用正斜杠路径，转义冒号，并用单引号包裹
            if os.name == "nt":
                # 1. 确保是绝对路径
                ass_path_abs = os.path.abspath(ass_path)
                temp_video_abs = os.path.abspath(temp_video_no_sub)
                output_file_abs = os.path.abspath(output_file)
                font_dir_abs = os.path.abspath(utils.font_dir())
                
                # 2. 将反斜杠转换为正斜杠（FFmpeg在Windows上也支持正斜杠）
                ass_path_ffmpeg = ass_path_abs.replace("\\", "/")
                font_dir_ffmpeg = font_dir_abs.replace("\\", "/")
                
                # 3. 关键：转义驱动盘符后的冒号（例如 D: -> D\:）
                # 这样FFmpeg才会把 D\: 识别为路径的一部分，而不是参数分隔符
                ass_path_ffmpeg = ass_path_ffmpeg.replace(":", "\\:")
                font_dir_ffmpeg = font_dir_ffmpeg.replace(":", "\\:")
                
                # 4. 使用单引号包裹路径，处理空格和特殊字符
                ass_filter_base = f"ass='{ass_path_ffmpeg}'"
                font_dir_param = f":fontsdir='{font_dir_ffmpeg}'"
                
                # 输入/输出文件路径（滤镜外）可以使用标准正斜杠
                temp_video_abs_ffmpeg = temp_video_abs.replace("\\", "/")
                output_file_abs_ffmpeg = output_file_abs.replace("\\", "/")
            else:
                # Linux/Mac逻辑保持简单
                ass_path_abs_ffmpeg = os.path.abspath(ass_path)
                font_dir_ffmpeg = os.path.abspath(utils.font_dir())
                temp_video_abs_ffmpeg = temp_video_abs
                output_file_abs_ffmpeg = output_file_abs
                
                # 如果路径包含空格，使用引号
                if " " in ass_path_abs_ffmpeg:
                    ass_filter_base = f"ass='{ass_path_abs_ffmpeg}'"
                else:
                    ass_filter_base = f"ass={ass_path_abs_ffmpeg}"
                font_dir_param = f":fontsdir='{font_dir_ffmpeg}'" if " " in font_dir_ffmpeg else f":fontsdir={font_dir_ffmpeg}"
            
            # 构建FFmpeg命令
            # 注意：在Windows下，输出文件路径使用系统绝对路径即可，subprocess会自动处理
            # 添加色彩格式参数，确保视频色彩正确
            # 字体目录路径已在上面处理，这里直接使用font_dir_param
            ass_filter_with_fonts = f"{ass_filter_base}{font_dir_param}"
            
            # 构建完整的视频滤镜链：ass字幕 + format转换
            # 使用逗号分隔多个滤镜（在同一个滤镜链中）
            video_filter = f"{ass_filter_with_fonts},format=yuv420p"
            
            cmd = [
                ffmpeg_exe,
                "-i", temp_video_abs_ffmpeg,
                "-vf", video_filter,  # 视频滤镜链
                "-c:v", video_codec,
                "-c:a", audio_codec,
                "-preset", "fast",
                "-threads", str(params.n_threads or 2),
                "-pix_fmt", "yuv420p",  # 明确指定像素格式
                "-y",  # 覆盖输出文件
                output_file_abs  # 使用系统绝对路径，subprocess会自动处理
            ]
            
            # 输出详细的命令信息用于调试
            logger.info(f"🎬 FFmpeg字幕命令:")
            logger.info(f"   - ASS文件: {ass_path_abs}")
            logger.info(f"   - 字体目录: {font_dir_abs}")
            logger.info(f"   - 视频滤镜: {video_filter}")
            logger.debug(f"完整命令: {' '.join(cmd)}")
            
            # 验证ASS文件是否存在
            if not os.path.exists(ass_path_abs):
                logger.error(f"❌ ASS文件不存在: {ass_path_abs}")
                raise FileNotFoundError(f"ASS文件不存在: {ass_path_abs}")
            
            # 验证字体目录是否存在
            if not os.path.exists(font_dir_abs):
                logger.error(f"❌ 字体目录不存在: {font_dir_abs}")
                raise FileNotFoundError(f"字体目录不存在: {font_dir_abs}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10分钟超时
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            
            if result.returncode == 0 and os.path.exists(output_file):
                logger.info("✅ FFmpeg字幕添加成功")
                # 清理临时文件
                try:
                    if os.path.exists(temp_video_no_sub):
                        os.remove(temp_video_no_sub)
                    if os.path.exists(ass_path):
                        os.remove(ass_path)
                except:
                    pass
                return
            else:
                # 输出完整的错误信息用于调试
                error_msg = result.stderr if result.stderr else result.stdout if result.stdout else "unknown error"
                logger.warning(f"⚠️ FFmpeg字幕添加失败 (返回码: {result.returncode})")
                # 输出完整的错误信息（最多2000字符）
                if error_msg:
                    # 尝试提取关键错误信息（跳过版本信息等）
                    error_lines = error_msg.split('\n')
                    key_errors = [line for line in error_lines if any(keyword in line.lower() for keyword in ['error', 'failed', 'cannot', 'invalid', 'unable', 'no such', 'font', 'ass', 'could not'])]
                    if key_errors:
                        logger.error(f"FFmpeg关键错误:")
                        for err in key_errors[:10]:  # 显示前10个关键错误
                            logger.error(f"   - {err}")
                    else:
                        logger.debug(f"FFmpeg完整输出: {error_msg[:2000]}")
                
                # 如果失败，保留ASS文件用于调试
                logger.warning(f"⚠️ ASS文件已保留用于调试: {ass_path_abs}")
                logger.info("🔄 回退到MoviePy方式...")
                # 回退：重新加载视频并使用MoviePy方式
                video_clip = VideoFileClip(temp_video_no_sub).without_audio()
                audio_clip = AudioFileClip(audio_path).with_effects(
                    [afx.MultiplyVolume(params.voice_volume)]
                )
                
                def make_textclip(text):
                    return TextClip(
                        text=text,
                        font=font_path,
                        font_size=params.font_size,
                    )
                
                sub = SubtitlesClip(
                    subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
                )
                text_clips = []
                for item in sub.subtitles:
                    clip = create_text_clip(subtitle_item=item)
                    text_clips.append(clip)
                video_clip = CompositeVideoClip([video_clip, *text_clips])
                video_clip = video_clip.with_audio(audio_clip)
                
                write_videofile_with_fallback(
                    video_clip,
                    output_file,
                    codec=video_codec,
                    audio_codec=audio_codec,
                    temp_audiofile_path=output_dir,
                    threads=params.n_threads or 2,
                    logger=None,
                    fps=fps,
                )
                video_clip.close()
                del video_clip
                
                # 清理临时文件
                try:
                    if os.path.exists(temp_video_no_sub):
                        os.remove(temp_video_no_sub)
                    if os.path.exists(ass_path):
                        os.remove(ass_path)
                except:
                    pass
        except Exception as e:
            logger.error(f"❌ FFmpeg字幕处理异常: {str(e)}")
            logger.info("🔄 回退到MoviePy方式...")
            # 回退到MoviePy方式
            if os.path.exists(temp_video_no_sub):
                video_clip = VideoFileClip(temp_video_no_sub).without_audio()
            else:
                video_clip = VideoFileClip(video_path).without_audio()
            
            def make_textclip(text):
                return TextClip(
                    text=text,
                    font=font_path,
                    font_size=params.font_size,
                )
            
            sub = SubtitlesClip(
                subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
            )
            text_clips = []
            for item in sub.subtitles:
                clip = create_text_clip(subtitle_item=item)
                text_clips.append(clip)
            video_clip = CompositeVideoClip([video_clip, *text_clips])
            video_clip = video_clip.with_audio(audio_clip)
            
            write_videofile_with_fallback(
                video_clip,
                output_file,
                codec=video_codec,
                audio_codec=audio_codec,
                temp_audiofile_path=output_dir,
                threads=params.n_threads or 2,
                logger=None,
                fps=fps,
            )
            video_clip.close()
            del video_clip
            
            # 清理临时文件
            try:
                if os.path.exists(temp_video_no_sub):
                    os.remove(temp_video_no_sub)
                if ass_path and os.path.exists(ass_path):
                    os.remove(ass_path)
            except:
                pass
    else:
        # 不使用FFmpeg字幕，直接使用MoviePy方式
        write_videofile_with_fallback(
            video_clip,
            output_file,
            codec=video_codec,
            audio_codec=audio_codec,
            temp_audiofile_path=output_dir,
            threads=params.n_threads or 2,
            logger=None,
            fps=fps,
        )
        video_clip.close()
        del video_clip
        
        # 清理临时ASS文件（如果存在）
        if ass_path and os.path.exists(ass_path):
            try:
                os.remove(ass_path)
            except:
                pass


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    for material in materials:
        if not material.url:
            continue

        ext = utils.parse_extension(material.url)
        try:
            clip = VideoFileClip(material.url)
        except Exception:
            clip = ImageClip(material.url)

        width = clip.size[0]
        height = clip.size[1]
        if width < 480 or height < 480:
            logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
            continue

        if ext in const.FILE_TYPE_IMAGES:
            logger.info(f"processing image: {material.url}")
            # Create an image clip and set its duration to 3 seconds
            clip = (
                ImageClip(material.url)
                .with_duration(clip_duration)
                .with_position("center")
            )
            # Apply a zoom effect using the resize method.
            # A lambda function is used to make the zoom effect dynamic over time.
            # The zoom effect starts from the original size and gradually scales up to 120%.
            # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
            # Note: 1 represents 100% size, so 1.2 represents 120% size.
            zoom_clip = clip.resized(
                lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
            )

            # Optionally, create a composite video clip containing the zoomed clip.
            # This is useful when you want to add other elements to the video.
            final_clip = CompositeVideoClip([zoom_clip])

            # Output the video to a file.
            video_file = f"{material.url}.mp4"
            write_videofile_with_fallback(final_clip, video_file, codec=video_codec, fps=30, logger=None)
            close_clip(clip)
            material.url = video_file
            logger.success(f"image processed: {video_file}")
    return materials