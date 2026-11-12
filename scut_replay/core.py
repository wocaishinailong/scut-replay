"""Date selection, durable progress and transcript export (no model inference)."""
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(TZ).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def lesson_date(title, start_at=None):
    found = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", title)
    if found:
        return date(*map(int, found.groups())).isoformat()
    if start_at:
        return datetime.fromtimestamp(float(start_at), TZ).date().isoformat()
    raise ValueError(f"无法确定课次日期：{title!r}")


def select_lessons(lessons, *, on=None, since=None, through=None):
    if on and (since or through):
        raise ValueError("--on 不能与 --since/--through 混用")
    for value in (on, since, through):
        if value:
            date.fromisoformat(value)
    if since and through and since > through:
        raise ValueError("起始日期不能晚于截止日期")
    if not (on or since or through):
        raise ValueError("请指定 --on 或日期范围，避免意外处理整门课")
    selected = []
    seen = set()
    for lesson in lessons:
        key = (str(lesson["course_id"]), str(lesson["sub_id"]))
        if key in seen:
            continue
        seen.add(key)
        day = lesson["date"]
        if on and day != on or since and day < since or through and day > through:
            continue
        selected.append(lesson)
    return sorted(selected, key=lambda x: (x["date"], str(x["sub_id"])))


@contextmanager
def run_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个采集任务正在使用同一数据目录。") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS lessons (
            course_id TEXT NOT NULL, sub_id TEXT NOT NULL, title TEXT NOT NULL,
            lesson_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered',
            stage TEXT, error TEXT, output_dir TEXT, signature TEXT, updated_at TEXT,
            PRIMARY KEY(course_id, sub_id))""")
        self.db.commit()

    def discover(self, lesson):
        self.db.execute("""INSERT INTO lessons(course_id, sub_id, title, lesson_date, updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(course_id,sub_id) DO UPDATE SET
            title=excluded.title,lesson_date=excluded.lesson_date""",
            (str(lesson["course_id"]), str(lesson["sub_id"]), lesson["title"], lesson["date"], now()))
        self.db.commit()

    def get(self, lesson):
        row = self.db.execute("SELECT * FROM lessons WHERE course_id=? AND sub_id=?",
                              (str(lesson["course_id"]), str(lesson["sub_id"]))).fetchone()
        return dict(row) if row else None

    def update(self, lesson, status, stage, *, error=None, output_dir=None, signature=None):
        self.db.execute("""UPDATE lessons SET status=?,stage=?,error=?,
            output_dir=COALESCE(?,output_dir),signature=COALESCE(?,signature),updated_at=?
            WHERE course_id=? AND sub_id=?""",
            (status, stage, error, str(output_dir) if output_dir else None, signature, now(),
             str(lesson["course_id"]), str(lesson["sub_id"])))
        self.db.commit()

    def rows(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM lessons ORDER BY course_id,lesson_date,sub_id")]

    def close(self):
        self.db.close()


def artifact_valid(directory, signature):
    """A completed DB row alone is insufficient: files and their checksums must agree."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "complete.json").read_text())
        if manifest["signature"] != signature:
            return False
        artifacts = manifest["artifacts"]
        if not {"transcript.json", "transcript.srt", "transcript.txt"}.issubset(artifacts):
            return False
        return all(Path(name).name == name and digest(directory / name) == sha for name, sha in artifacts.items())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def timestamp(seconds, *, srt=False):
    ms = max(0, round(float(seconds) * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{',' if srt else '.'}{ms:03}"


def clean_segments(segments, duration=None):
    result = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        start, end = float(segment["start"]), float(segment["end"])
        if not all(math.isfinite(x) for x in (start, end)):
            raise ValueError("字幕包含非法时间戳")
        if not text:
            continue
        start = max(0.0, start)
        end = min(end, duration) if duration is not None else end
        if end <= start:
            continue
        result.append({**segment, "start": round(start, 3), "end": round(end, 3), "text": text})
    result.sort(key=lambda s: (s["start"], s["end"]))
    return result


def export_transcript(directory, metadata, segments, signature):
    directory = Path(directory)
    segments = clean_segments(segments, metadata.get("duration_seconds"))
    if not segments:
        raise ValueError("没有有效字幕片段；不会将课次标记为已完成")
    atomic_json(directory / "transcript.json", {**metadata, "segments": segments})
    def display(s):
        return ('[待核对] ' if s.get('review_required') else '')+s['text']
    srt = "\n\n".join(f"{i}\n{timestamp(s['start'], srt=True)} --> {timestamp(s['end'], srt=True)}\n{display(s)}"
                         for i, s in enumerate(segments, 1)) + "\n"
    txt = f"{metadata['course_name']}｜{metadata['lesson_title']}\n来源：{metadata['source']}\n\n"
    txt += "\n".join(f"[{timestamp(s['start'])}–{timestamp(s['end'])}] {display(s)}" for s in segments) + "\n"
    for name, content in (("transcript.srt", srt), ("transcript.txt", txt)):
        temp = directory / (name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(directory / name)
    names = ("transcript.json", "transcript.srt", "transcript.txt")
    atomic_json(directory / "complete.json", {"signature": signature, "completed_at": now(),
                                              "artifacts": {n: digest(directory / n) for n in names}})
    return len(segments)
