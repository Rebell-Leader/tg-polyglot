import os
import subprocess
import uuid
from pathlib import Path

# Directories for temporary files
TEMP_DIR = Path("temp")
TRANSLATED_AUDIO_DIR = TEMP_DIR / "audio"
TRANSLATED_VIDEO_DIR = TEMP_DIR / "video"
TRANSLATED_TEXT_DIR = TEMP_DIR / "text"

# Ensure directories exist
for directory in [TEMP_DIR, TRANSLATED_AUDIO_DIR, TRANSLATED_VIDEO_DIR, TRANSLATED_TEXT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

def locate_file(directory):
    """
    Locate the first file in the given directory.

    Args:
        directory (Path): The directory to search.

    Returns:
        Path: The path to the located file, or None if no files found.
    """
    files = list(directory.glob("*"))
    if files:
        return files[0]  # Return the first file in the directory
    return None

def run_translation_script(video_url, source_lang, target_lang, output_type):
    """
    Run the translation script based on the specified parameters.

    Args:
        video_url (str): The video URL.
        source_lang (str): The source language code.
        target_lang (str): The target language code.
        output_type (str): The desired output type ("video", "audio", "text").

    Returns:
        Path: The directory containing the generated file(s).
    """
    output_id = uuid.uuid4().hex
    output_dir = None

    try:
        # Determine output directory based on the requested type
        if output_type == "text":
            output_dir = TRANSLATED_TEXT_DIR / output_id
            output_dir.mkdir(parents=True, exist_ok=True)
            command = [
                "vot-cli",
                f"--lang={source_lang}",
                f"--reslang={target_lang}",
                "--subs",
                f"--output={output_dir}",
                video_url,
            ]
        elif output_type == "audio":
            output_dir = TRANSLATED_AUDIO_DIR / output_id
            output_dir.mkdir(parents=True, exist_ok=True)
            command = [
                "vot-cli",
                f"--lang={source_lang}",
                f"--reslang={target_lang}",
                f"--output={output_dir}",
                video_url,
            ]
        elif output_type == "video":
            output_dir = TRANSLATED_AUDIO_DIR / output_id  # Start with audio generation
            output_dir.mkdir(parents=True, exist_ok=True)
            command = [
                "vot-cli",
                f"--lang={source_lang}",
                f"--reslang={target_lang}",
                f"--output={output_dir}",
                video_url,
            ]
        else:
            raise ValueError("Invalid output type specified.")

        # Run the translation script
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Translation script error: {result.stderr}")

    except Exception as e:
        raise RuntimeError(f"Failed to run translation script: {e}")

    return output_dir


def process_video(video_url, languages, output_type):
    """
    Process a video by downloading, translating, and creating the desired output.

    Args:
        video_url (str): The video URL.
        languages (str): A string in the format "source_lang to target_lang".
        output_type (str): The desired output type ("video", "audio", "text").

    Returns:
        tuple: A tuple containing the output type and the file path.
    """
    try:
        # Parse language settings
        source_lang, target_lang = map(str.strip, languages.split("to"))

        # Run the translation script
        translation_output_dir = run_translation_script(video_url, source_lang, target_lang, output_type)

        # Locate the generated file in the output directory
        translated_file = locate_file(translation_output_dir)
        if not translated_file or not translated_file.is_file():
            raise RuntimeError(f"Generated file not found in {translation_output_dir}")

        if output_type == "video":
            # Prepare final video output
            video_output_path = TRANSLATED_VIDEO_DIR / f"{uuid.uuid4().hex}.mp4"

            # Download the original video
            downloaded_video = TEMP_DIR / f"{uuid.uuid4().hex}_original.mp4"
            download_command = ["yt-dlp", "-o", str(downloaded_video), video_url]
            download_result = subprocess.run(download_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if download_result.returncode != 0:
                raise RuntimeError(f"Video download failed: {download_result.stderr}")

            # Replace the audio track with the translated one using ffmpeg
            replace_audio_command = [
                "ffmpeg", "-i", str(downloaded_video), "-i", str(translated_file),
                "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0",
                "-shortest", str(video_output_path),
                "-y"
            ]
            ffmpeg_result = subprocess.run(replace_audio_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if ffmpeg_result.returncode != 0:
                raise RuntimeError(f"FFmpeg audio replacement failed: {ffmpeg_result.stderr}")

            # Clean up the downloaded video
            downloaded_video.unlink()

            return "video", str(video_output_path)

        elif output_type == "audio":
            return "audio", str(translated_file)

        elif output_type == "text":
            return "text", str(translated_file)

    except Exception as e:
        raise RuntimeError(f"Error processing video: {e}")


def cleanup_files(output_dir):
    """
    Clean up the temporary directory for a specific user.

    Args:
        output_dir (Path): The directory to clean.
    """
    try:
        for file in output_dir.iterdir():
            if file.is_file():
                file.unlink()
        output_dir.rmdir()  # Remove the directory itself
    except Exception as e:
        print(f"Failed to clean up {output_dir}: {e}")
