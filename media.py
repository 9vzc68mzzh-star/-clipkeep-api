"""Inspect actual streams and produce a portable, seekable MP4 (or MP3)."""
import json
import subprocess
import tempfile
import time
from pathlib import Path


class MediaError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def probe(path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            capture_output=True, timeout=25, check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError):
        raise MediaError('conversion_failed') from None


def normalize(source, folder, kind, height, check, report, encoder_lock, max_bytes):
    """Copy video streams unchanged; convert only incompatible audio.

    The encoder semaphore bounds CPU/RAM independently of network downloads.
    MP4 is a container, not a guarantee of an iPhone-compatible video codec.
    """
    info = probe(source)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    if kind == 'mp4' and not video:
        raise MediaError('video_unavailable')
    if kind == 'mp3' and not audio:
        raise MediaError('audio_unavailable')
    duration = float(info.get('format', {}).get('duration') or (video or audio).get('duration') or 0)
    output = folder / ('ready.' + kind)
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source)]
    if kind == 'mp3':
        command += ['-map', f"0:{audio['index']}", '-vn', '-c:a', 'libmp3lame', '-b:a', '192k']
    else:
        compatible_video = (video.get('codec_name') == 'h264' and video.get('pix_fmt') == 'yuv420p'
                            and min(int(video.get('height') or 0),int(video.get('width') or 0)) <= height)
        command += ['-map', f"0:{video['index']}"]
        if audio:
            command += ['-map', f"0:{audio['index']}"]
        if not compatible_video:
            raise MediaError('compatible_video_unavailable')
        command += ['-c:v', 'copy']
        if audio:
            # HE-AAC can be problematic too; copy only ordinary AAC-LC.
            compatible_audio = audio.get('codec_name') == 'aac' and audio.get('profile') == 'LC'
            command += ['-c:a', 'copy' if compatible_audio else 'aac']
            if not compatible_audio:
                command += ['-b:a', '160k', '-ar', '48000', '-ac', '2']
        command += ['-tag:v', 'avc1', '-movflags', '+faststart', '-map_metadata', '-1', '-sn', '-dn']
    report('converting', 82)
    while not encoder_lock.acquire(timeout=.5):
        check()
    try:
        check()
        with tempfile.TemporaryFile(mode='w+b') as errors:
            progress_path = folder / 'encode-progress.txt'
            command += ['-progress', str(progress_path), str(output)]
            with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors) as process:
                try:
                    while process.poll() is None:
                        check()
                        if output.exists() and output.stat().st_size > max_bytes:
                            raise MediaError('file_too_large')
                        if progress_path.exists():
                            values = dict(line.split('=', 1) for line in progress_path.read_text().splitlines() if '=' in line)
                            try:
                                seconds = max(0, float(values.get('out_time_us') or 0)) / 1_000_000
                            except ValueError:
                                # FFmpeg may report N/A before its first output packet.
                                seconds = 0
                            percent = min(97, 82 + seconds / duration * 15) if duration else 82
                            report('converting', percent, (seconds, output.stat().st_size if output.exists() else 0))
                        time.sleep(.3)
                    if process.returncode:
                        raise MediaError('conversion_failed')
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
    finally:
        encoder_lock.release()
    check()
    if not output.exists() or not output.stat().st_size:
        raise MediaError('conversion_failed')
    if output.stat().st_size > max_bytes:
        raise MediaError('file_too_large')
    result = probe(output)
    result_video = next((s for s in result['streams'] if s['codec_type'] == 'video'), None)
    result_audio = next((s for s in result['streams'] if s['codec_type'] == 'audio'), None)
    if kind == 'mp4' and (not result_video or result_video.get('codec_name') != 'h264'
                          or result_video.get('pix_fmt') != 'yuv420p'
                          or audio and (not result_audio or result_audio.get('codec_name') != 'aac')):
        raise MediaError('conversion_failed')
    if kind == 'mp3' and (not result_audio or result_audio.get('codec_name') != 'mp3'):
        raise MediaError('audio_unavailable')
    # Remove the original streams after a verified output to avoid double storage.
    for path in folder.iterdir():
        if path != output and path.is_file():
            path.unlink(missing_ok=True)
    return output, {
        'height': result_video.get('height') if result_video else None,
        'video_codec': result_video.get('codec_name') if result_video else None,
        'audio_codec': result_audio.get('codec_name') if result_audio else None,
        'duration': float(result.get('format', {}).get('duration') or duration),
        'warning': 'คลิปต้นฉบับไม่มีแทร็กเสียง' if kind == 'mp4' and not result_audio else None,
    }
