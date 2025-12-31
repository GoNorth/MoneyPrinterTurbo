import glob
import itertools
import os
import random
import gc
import shutil
import subprocess
import platform
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

def check_ffmpeg_encoder_support(encoder_name: str) -> bool:
    """
    检查FFmpeg是否支持指定的编码器
    """
    try:
        # 获取FFmpeg路径
        ffmpeg_exe = os.environ.get("IMAGEIO_FFMPEG_EXE", "ffmpeg")
        if not os.path.isfile(ffmpeg_exe):
            ffmpeg_exe = "ffmpeg"
        
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
        ffmpeg_exe = os.environ.get("IMAGEIO_FFMPEG_EXE", "ffmpeg")
        if not os.path.isfile(ffmpeg_exe):
            ffmpeg_exe = "ffmpeg"
        
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
        ffmpeg_exe = os.environ.get("IMAGEIO_FFMPEG_EXE", "ffmpeg")
        if not os.path.isfile(ffmpeg_exe):
            ffmpeg_exe = "ffmpeg"
        
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
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
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
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
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

    video_clip = VideoFileClip(video_path).without_audio()
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        text_clips = []
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)
        video_clip = CompositeVideoClip([video_clip, *text_clips])

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