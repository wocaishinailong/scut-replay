"""Local MLX Whisper inference with independently resumable audio chunks."""
import importlib.metadata
import json
import math
import platform
import time
import wave
from pathlib import Path

from .core import atomic_json, digest, fingerprint, now

MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
MODEL_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
CHUNK_SECONDS = 300
OVERLAP_SECONDS = 2
_vad_model = None


def speech_regions(samples):
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps
    global _vad_model
    torch.set_num_threads(1)
    if _vad_model is None:
        _vad_model = load_silero_vad()
    return get_speech_timestamps(torch.from_numpy(samples),_vad_model,sampling_rate=16000,
        threshold=0.5,min_speech_duration_ms=250,min_silence_duration_ms=500,speech_pad_ms=200)


def detect_language(samples, regions, model_path):
    """Use actual speech, not a possibly silent first 30s, to identify language."""
    import numpy as np
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder
    from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim, N_FRAMES, N_SAMPLES
    # This API matches the pinned mlx-whisper version; keep task='transcribe' downstream.
    reference=np.concatenate([samples[r['start']:r['end']] for r in regions])[:30*16000]
    model=ModelHolder.get_model(model_path,mx.float16)
    mel=log_mel_spectrogram(reference,n_mels=model.dims.n_mels,padding=N_SAMPLES)
    _,probabilities=model.detect_language(pad_or_trim(mel,N_FRAMES,axis=-2).astype(mx.float16))
    language=max(probabilities,key=probabilities.get)
    return language,float(probabilities[language])


def overlaps_speech(segment, regions):
    duration=segment['end']-segment['start']
    overlap=sum(max(0,min(segment['end'],r['end'])-max(segment['start'],r['start'])) for r in regions)
    return overlap >= min(0.2,max(0,duration)*0.1) and overlap>0


def annotate_quality(segment, regions, language_probability):
    """Preserve candidate words, but mark unsupported stretches rather than hiding them."""
    duration=segment['end']-segment['start']
    overlap=sum(max(0,min(segment['end'],r['end'])-max(segment['start'],r['start'])) for r in regions)
    ratio=overlap/duration if duration>0 else 0
    flags=[]
    if duration>10 and ratio<0.2:
        flags.append('sparse_speech')
    if language_probability is not None and language_probability<0.8:
        flags.append('uncertain_language')
    if segment.get('avg_logprob',0)<-1:
        flags.append('low_asr_logprob')
    if segment.get('compression_ratio',0)>2.4:
        flags.append('possible_repetition')
    return {**segment,'speech_overlap_ratio':round(ratio,4),'review_required':bool(flags),'quality_flags':flags}


def prepare_model(root):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("本版本本地 ASR 使用 Apple Silicon/MLX；其他系统仍可使用平台字幕。")
    from huggingface_hub import snapshot_download
    manifest_path = root / "models" / "model.json"
    if manifest_path.exists():
        model = json.loads(manifest_path.read_text())
        if model["repo"] != MODEL_REPO or model["revision"] != MODEL_REVISION:
            raise RuntimeError("本地模型版本与程序锁定版本不一致；未自动切换模型。")
    else:
        model = {"repo": MODEL_REPO, "revision": MODEL_REVISION}
    path = snapshot_download(repo_id=model["repo"], revision=model["revision"],
                             allow_patterns=["config.json", "*.safetensors", "*.npz"],
                             cache_dir=str(root / "models" / "cache"))
    model.update(path=path)
    atomic_json(manifest_path, model)
    return model


def config(model):
    return {"engine": "mlx-whisper", "package_version": importlib.metadata.version("mlx-whisper"),
            "model": model["repo"], "revision": model["revision"], "language": "auto-from-speech",
            "task": "transcribe", "temperature": 0.0, "condition_on_previous_text": False,
            "chunk_seconds": CHUNK_SECONDS, "overlap_seconds": OVERLAP_SECONDS, "seed": 0,
            "vad":{"package":"silero-vad","version":importlib.metadata.version('silero-vad'),
                   "threshold":0.5,"min_speech_ms":250,"min_silence_ms":500,"pad_ms":200}}


def chunk_bounds(duration, chunk_seconds=CHUNK_SECONDS, overlap=OVERLAP_SECONDS):
    for i in range(math.ceil(duration / chunk_seconds)):
        start = i * chunk_seconds
        end = min(duration, (i + 1) * chunk_seconds)
        yield i, start, end, max(0, start - overlap), min(duration, end + overlap)


def own_segments(segments, core_start, core_end, offset):
    """Assign overlap segments by midpoint; clip to the owning core interval."""
    result = []
    for segment in segments:
        start, end = offset + float(segment["start"]), offset + float(segment["end"])
        if not core_start <= (start + end) / 2 < core_end:
            continue
        result.append({"start": max(core_start, start), "end": min(core_end, end),
                       "text": segment["text"],
                       **{k: segment[k] for k in ("avg_logprob", "compression_ratio", "no_speech_prob") if k in segment}})
    return result


def transcribe(audio_path, directory, model, progress=None):
    import mlx.core as mx
    import mlx_whisper
    import numpy as np

    settings = config(model)
    audio_sha = digest(audio_path)
    job_signature = fingerprint({"audio_sha256": audio_sha, "settings": settings})
    checkpoint_dir = directory / "asr-chunks" / job_signature[:16]
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    segments, chunks = [], []
    with wave.open(str(audio_path), "rb") as stream:
        rate = stream.getframerate()
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or rate != 16000:
            raise ValueError("ASR requires mono 16 kHz PCM16 WAV")
        duration = stream.getnframes() / rate
        bounds = list(chunk_bounds(duration))
        for i, start, end, read_start, read_end in bounds:
            checkpoint = checkpoint_dir / f"{i:04}.json"
            data = None
            if checkpoint.exists():
                try:
                    candidate = json.loads(checkpoint.read_text())
                    if candidate["signature"] == job_signature and candidate["core"] == [start, end]:
                        saved = candidate["segments"]
                        if all(math.isfinite(float(s[k])) for s in saved for k in ("start", "end")):
                            data = candidate
                except (ValueError, TypeError, KeyError):
                    pass
            if data is None:
                message = f"本地转写 {i+1}/{len(bounds)}：{start/60:.1f}–{end/60:.1f} 分钟"
                if progress:
                    progress(message)
                print(message, flush=True)
                stream.setpos(round(read_start * rate))
                pcm = stream.readframes(round((read_end-read_start)*rate))
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                mx.random.seed(0)
                np.random.seed(0)
                began = time.monotonic()
                speech=speech_regions(samples)
                absolute_speech=[{'start':read_start+r['start']/rate,'end':read_start+r['end']/rate} for r in speech]
                language,probability=None,None
                rejected=[]
                if speech:
                    language,probability=detect_language(samples,speech,model['path'])
                    left,right=speech[0]['start'],speech[-1]['end']
                    result = mlx_whisper.transcribe(samples[left:right], path_or_hf_repo=model["path"], language=language,
                        task="transcribe", temperature=0.0, condition_on_previous_text=False,
                        verbose=None, word_timestamps=False)
                    candidates=own_segments(result["segments"],start,end,read_start+left/rate)
                    owned=[]
                    for segment in candidates:
                        if overlaps_speech(segment,absolute_speech):
                            owned.append(segment)
                        else:
                            rejected.append(segment)
                else:
                    owned=[]
                data = {"signature": job_signature, "core": [start, end], "read": [read_start, read_end],
                        "elapsed_seconds": round(time.monotonic()-began, 3), "segments": owned,
                        "language":language,"language_probability":probability,
                        "speech_regions":absolute_speech,"rejected_no_speech":rejected,
                        "completed_at": now()}
                atomic_json(checkpoint, data)
            else:
                print(f"复用字幕分段 {i+1}/{len(bounds)}", flush=True)
            segments.extend(annotate_quality(s,data['speech_regions'],data['language_probability']) for s in data['segments'])
            chunks.append({k: data[k] for k in ("core", "elapsed_seconds", "completed_at","language","language_probability")})
    return segments, {"duration_seconds": duration, "audio_sha256": audio_sha,
                       "asr_config": settings, "chunks": chunks, "coverage_seconds": sum(c["core"][1]-c["core"][0] for c in chunks)}
