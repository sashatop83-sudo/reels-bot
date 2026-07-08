import subprocess
from pathlib import Path

from bot.services.ffmpeg_utils import get_ffmpeg_binary


def merge_video_parts(parts: list[Path], output_path: Path) -> Path:
    """Склеить части видео в один файл (ffmpeg concat)."""
    if not parts:
        raise RuntimeError("Нет частей для склейки")
    if len(parts) == 1:
        output_path.write_bytes(parts[0].read_bytes())
        return output_path

    work_dir = output_path.parent
    list_path = work_dir / "concat_list.txt"
    lines = [f"file '{p.resolve()}'" for p in parts]
    list_path.write_text("\n".join(lines), encoding="utf-8")

    ffmpeg = get_ffmpeg_binary()
    # сначала пробуем без перекодирования (быстро)
    result = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # запасной вариант — перекодировать (если части с разными кодеками)
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac",
                "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        tail = (result.stderr or "")[-800:]
        raise RuntimeError(f"Не удалось склеить части: {tail}")

    return output_path
