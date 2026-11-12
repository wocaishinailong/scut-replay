"""Incremental lesson pipeline: site transcript first, otherwise local audio ASR."""
import json
import os
import re
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import imageio_ffmpeg

from . import __version__
from . import asr
from .core import TZ, artifact_valid, atomic_json, digest, export_transcript, fingerprint, now
from .site import BASE

PIPELINE_CONFIG = {"version": 3, "language": "auto-from-speech", "vad":"silero-6.2.3", "asr_model": asr.MODEL_REPO,
                   "asr_revision": asr.MODEL_REVISION,
                   "chunk_seconds": asr.CHUNK_SECONDS, "overlap_seconds": asr.OVERLAP_SECONDS}
SIGNATURE = fingerprint(PIPELINE_CONFIG)


def site_segments(response, start_at, duration):
    if response.get("code") == 10002:
        return []
    if response.get("code") != 0:
        raise ValueError("网站字幕接口返回未知状态")
    records = response.get("list", [])
    if not records:
        return []
    record = records[0]
    content = record.get("all_content", [])
    if isinstance(content, str):
        content = json.loads(content)
    result = []
    if record.get("trans_type") == "ai":
        # Same absolute-time conversion as the platform player.
        rows = [r for r in content if r.get("time")]
        for index, row in enumerate(rows):
            value = datetime.fromisoformat(row["time"].strip().replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=TZ)
            start = value.timestamp()-float(start_at)
            text = row.get("zhtext") or row.get("text") or row.get("Text")
            if not text:
                raise ValueError("网站AI字幕字段发生变化")
            result.append({"start":start,"end":duration,"text":text})
        for a,b in zip(result,result[1:]):
            a["end"] = b["start"]
    else:
        for row in content:
            if row.get("Text"):
                result.append({"start":float(row["BeginSec"]),"end":float(row["EndSec"]),"text":row["Text"]})
        if content and not result:
            raise ValueError("网站字幕存在但格式未识别，未静默忽略")
    return result


def _identity(url):
    p = urlparse(url)
    return p.scheme + "://" + p.netloc + p.path


def download_media(url, target, headers=None, workers=4):
    target = Path(target)
    identity = _identity(url)
    meta_path = target.with_suffix(target.suffix + ".json")
    if target.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("identity") == identity and meta.get("sha256") == digest(target):
            print("复用已下载回放", flush=True)
            return meta
    if workers > 1:
        from .download import parallel_ranges
        assembled = parallel_ranges(url,target,identity,headers or {},workers)
        if assembled is not None:
            with assembled.open("rb") as stream:
                if stream.read(8)[4:8] != b"ftyp":
                    raise ValueError("合并的视频不是可识别的MP4文件")
            assembled.replace(target)
            meta={"identity":identity,"bytes":target.stat().st_size,"sha256":digest(target),"downloaded_at":now()}
            atomic_json(meta_path,meta)
            return meta
    part = target.with_suffix(target.suffix + ".part")
    partial_meta = target.with_suffix(target.suffix + ".part.json")
    for attempt in range(3):
        try:
            offset, validator = 0, None
            if part.exists() and partial_meta.exists():
                previous = json.loads(partial_meta.read_text())
                if previous.get("identity") == identity and previous.get("validator"):
                    offset, validator = part.stat().st_size, previous["validator"]
            request_headers = dict(headers or {})
            if offset:
                request_headers.update(Range=f"bytes={offset}-", **{"If-Range":validator})
            with httpx.Client(timeout=httpx.Timeout(90, connect=30), follow_redirects=False) as client:
                with client.stream("GET", url, headers=request_headers) as response:
                    response.raise_for_status()
                    # Never forward authentication automatically across redirects.
                    if response.status_code not in (200,206):
                        raise ValueError("媒体返回了不支持的响应")
                    if response.status_code == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",response.headers.get("content-range",""))
                        if not match or int(match[1]) != offset:
                            raise ValueError("服务器返回的下载续传区间不匹配")
                        expected = int(match[3])
                    else:
                        offset = 0
                        expected = int(response.headers.get("content-length",0))
                    mime = response.headers.get("content-type","")
                    if "html" in mime or "json" in mime:
                        raise ValueError("媒体地址返回登录页面或错误信息")
                    validator = response.headers.get("etag") or response.headers.get("last-modified")
                    atomic_json(partial_meta,{"identity":identity,"validator":validator,"expected_bytes":expected})
                    received, report_at = offset, time.monotonic()
                    with part.open("ab" if offset else "wb") as stream:
                        for block in response.iter_bytes(1024*1024):
                            stream.write(block)
                            received += len(block)
                            if time.monotonic()-report_at > 15:
                                print(f"下载回放：{received/1024/1024:.1f} / {expected/1024/1024:.1f} MiB",flush=True)
                                report_at = time.monotonic()
                    if expected and received != expected:
                        raise ValueError("下载字节数与服务器声明不一致")
            if part.stat().st_size < 32:
                raise ValueError("下载的视频为空或过短")
            with part.open("rb") as stream:
                if stream.read(8)[4:8] != b"ftyp":
                    raise ValueError("当前下载不是可识别的MP4文件")
            part.replace(target)
            meta = {"identity":identity,"bytes":target.stat().st_size,"sha256":digest(target),"downloaded_at":now()}
            atomic_json(meta_path,meta)
            return meta
        except (httpx.HTTPError, OSError) as exc:
            if attempt == 2:
                # Avoid leaking signed URLs or authorization in library exception text.
                raise RuntimeError(f"回放下载失败：{type(exc).__name__}，部分文件保留供续传") from None
            time.sleep(attempt+1)


def extract_audio(video, audio, expected_duration):
    meta_path = audio.with_suffix(".json")
    video_sha = digest(video)
    if audio.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("video_sha256") == video_sha and meta.get("audio_sha256") == digest(audio):
            print("复用已提取音轨", flush=True)
            return meta
    temp = audio.with_name("audio.partial.wav")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg,"-nostdin","-hide_banner","-y","-i",str(video),"-map","0:a:0",
               "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(temp)]
    with (audio.parent / "ffmpeg.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=log)
    if result.returncode:
        raise RuntimeError("提取音轨失败，见课次目录中的 ffmpeg.log")
    with wave.open(str(temp),"rb") as stream:
        duration = stream.getnframes()/stream.getframerate()
    if duration <= 0 or (expected_duration and abs(duration-expected_duration)>max(5,expected_duration*.01)):
        raise ValueError("提取音频时长与回放时长不匹配，停止转写")
    temp.replace(audio)
    meta = {"duration_seconds":duration,"video_sha256":video_sha,"audio_sha256":digest(audio),
            "ffmpeg_version":imageio_ffmpeg.get_ffmpeg_version(),"extracted_at":now()}
    atomic_json(meta_path,meta)
    return meta


def process_lesson(site, ledger, lesson, *, site_only=False):
    root = ledger.root
    directory = root / "courses" / lesson["course_id"] / (lesson["date"]+"_"+lesson["sub_id"])
    ledger.discover(lesson)
    if artifact_valid(directory,SIGNATURE):
        ledger.update(lesson,"done","complete",output_dir=directory,signature=SIGNATURE)
        print(f"跳过已完成：{lesson['title']}",flush=True)
        return "skipped"
    if lesson["status"] != "6":
        ledger.update(lesson,"pending","waiting_replay",error=f"平台状态 {lesson['status']}：尚无可用回放")
        print(f"保留待处理：{lesson['title']}（平台状态 {lesson['status']}）",flush=True)
        return "pending"
    directory.mkdir(parents=True,exist_ok=True)
    stage = "resources"
    try:
        ledger.update(lesson,"running",stage,output_dir=directory)
        resources = site.resources(lesson)
        atomic_json(directory / "resources.json",resources)
        atomic_json(directory / "lesson.json",lesson)
        if resources["status"] != "6":
            ledger.update(lesson,"pending","waiting_replay",error="课次详情显示回放尚未就绪")
            return "pending"
        segments = site_segments(resources["transcript_response"],resources["start_at"],resources["duration_seconds"])
        metadata = {"course_id":lesson["course_id"],"sub_id":lesson["sub_id"],"course_name":lesson["course_name"],
            "lesson_title":lesson["title"],"date":lesson["date"],"teacher":lesson["teacher"],
            "replay_url":BASE+f"/livingroom?course_id={lesson['course_id']}&sub_id={lesson['sub_id']}&tenant_code=21",
            "duration_seconds":resources["duration_seconds"],"source":"platform", "tool_version":__version__,
            "warnings":resources["warnings"],"created_at":now()}
        if not segments:
            if site_only:
                ledger.update(lesson,"pending","waiting_transcript",error="平台暂无字幕；按 --site-only 跳过本地转写")
                return "pending"
            if not resources["media_url"]:
                ledger.update(lesson,"pending","waiting_media",error="尚无可用媒体地址")
                return "pending"
            stage = "download"
            ledger.update(lesson,"running",stage)
            video = directory / "video.mp4"
            headers = {"Authorization":site.auth} if site.auth else {}
            download_media(resources["media_url"],video,headers=headers)
            stage = "audio"
            ledger.update(lesson,"running",stage)
            audio = directory / "audio.wav"
            audio_meta = extract_audio(video,audio,resources["duration_seconds"])
            stage = "model"
            ledger.update(lesson,"running",stage)
            model = asr.prepare_model(root)
            stage = "asr"
            ledger.update(lesson,"running",stage)
            segments, stats = asr.transcribe(audio,directory,model,
                progress=lambda message: ledger.update(lesson,"running","asr",error=message))
            metadata.update(stats)
            metadata.update(source="local-asr",audio=audio_meta)
        duration = metadata["duration_seconds"]
        for slide in resources["slides"]:
            slide["outside_recording"] = not 0 <= slide["start_seconds"] <= duration
        atomic_json(directory / "slides.json",{"meaning":"课堂截图序号，未经OCR/原始课件页码校验",
            "alignment":"平台原始时间轴，尚未人工校准","slides":resources["slides"]})
        stage = "export"
        ledger.update(lesson,"running",stage)
        metadata['review_required_segments']=sum(bool(s.get('review_required')) for s in segments)
        metadata['quality_note']='待核对为自动信号检测标记，非人工准确率评估；原始识别文字保留在JSON中。'
        count = export_transcript(directory,metadata,segments,SIGNATURE)
        ledger.update(lesson,"done","complete",output_dir=directory,signature=SIGNATURE)
        print(f"完成：{lesson['title']}，{count} 条字幕，{directory}",flush=True)
        return "completed"
    except KeyboardInterrupt:
        ledger.update(lesson,"interrupted",stage,error="用户中断；可重新运行续接")
        raise
    except Exception as exc:
        # Detailed local errors without signed URL/credential payloads.
        safe_message = str(exc) if not re.search(r"https?://|Bearer |token=",str(exc)) else type(exc).__name__
        ledger.update(lesson,"failed",stage,error=safe_message)
        print(f"失败：{lesson['title']}，步骤={stage}，{safe_message}",flush=True)
        return "failed"
