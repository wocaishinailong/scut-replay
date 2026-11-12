import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .core import TZ, Ledger, artifact_valid, atomic_json, run_lock, select_lessons

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / ".data"


def semester_start(reference):
    day = date.fromisoformat(reference)
    if day.month >= 8:
        return f"{day.year}-08-01"
    if day.month <= 2:
        return f"{day.year-1}-08-01"
    return f"{day.year}-02-01"


def parser():
    p = argparse.ArgumentParser(description="华工课程回放本地增量采集与字幕转写（不含LLM总结）")
    p.add_argument("--data-dir",type=Path,default=DEFAULT_ROOT,help="独立数据/登录状态目录")
    p.add_argument("--headless",action="store_true",help="已有有效登录时不显示浏览器")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("login",help="在专用浏览器中手工登录；登录后自动保存会话")
    sub.add_parser("status",help="查看本地处理进度，无需联网")
    sub.add_parser("prepare-model",help="只下载锁定版本的本地ASR模型")
    courses = sub.add_parser("courses",help="按本人课表发现不同课程")
    courses.add_argument("--since")
    courses.add_argument("--through",default=datetime.now(TZ).date().isoformat())
    sync = sub.add_parser("sync",help="按课程/日期处理，自动复用完成结果")
    group = sync.add_mutually_exclusive_group(required=True)
    group.add_argument("--course",help="课程全名；同名课程多于一个时需用ID")
    group.add_argument("--course-id",help="本人课表中的课程ID")
    sync.add_argument("--on",help="只处理某一天，例如2026-10-22")
    sync.add_argument("--since",help="包含起始日")
    sync.add_argument("--through",help="包含截止日")
    sync.add_argument("--dry-run",action="store_true",help="只列计划，不下载/转写")
    sync.add_argument("--site-only",action="store_true",help="只取平台字幕，不运行ASR")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.data_dir.expanduser().resolve()
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY","1")
    try:
        if args.command == "status":
            ledger = Ledger(root)
            try:
                for row in ledger.rows():
                    print(f"{row['course_id']} / {row['sub_id']}  {row['title']}  {row['status']}  {row['stage']}  {row['error'] or ''}")
            finally:
                ledger.close()
            return 0
        with run_lock(root):
            if args.command == "prepare-model":
                from .asr import prepare_model
                model = prepare_model(root)
                print(f"模型已就绪：{model['repo']} @ {model['revision']}")
                return 0
            from .site import Site
            from .pipeline import SIGNATURE, process_lesson
            if args.command == "sync":
                # Validate selectors before opening the browser.
                select_lessons([],on=args.on,since=args.since,through=args.through)
            with Site(root,headless=args.headless) as site:
                if args.command == "login":
                    print("登录会话已保存在本地专用浏览器目录。")
                    return 0
                end = args.through or (args.on if args.command == "sync" else None) or datetime.now(TZ).date().isoformat()
                date.fromisoformat(end)
                # Discover across the term even if selected day has no class.
                start = site.term_start(end) or semester_start(end)
                if args.since:
                    start = args.since if args.command == "courses" else min(args.since,start)
                print(f"读取本人课表：{start} 至 {end}",flush=True)
                courses = site.courses(start,end)
                if args.command == "courses":
                    for c in courses:
                        print(f"{c['course_id']}  {c['name']}  {c['teacher']}  {c['first_date']}–{c['last_date']}")
                    return 0
                matches = [c for c in courses if c['course_id']==args.course_id] if args.course_id else [c for c in courses if c['name']==args.course]
                if len(matches) != 1:
                    print("未找到唯一匹配的本人课程。请运行 courses 查看课程ID；同名课程请使用 --course-id。",file=sys.stderr)
                    for c in matches:
                        print(json.dumps(c,ensure_ascii=False),file=sys.stderr)
                    return 2
                course = matches[0]
                lessons = site.catalogue(course)
                atomic_json(root / "catalogues" / (course["course_id"]+".json"),{"course":course,"lessons":lessons})
                selected = select_lessons(lessons,on=args.on,since=args.since,through=args.through)
                if not selected:
                    print("日期范围内没有找到课次；没有处理其他日期。",file=sys.stderr)
                    return 2
                ledger = Ledger(root)
                counts = {"completed":0,"skipped":0,"pending":0,"failed":0}
                try:
                    for lesson in selected:
                        directory = root/"courses"/lesson["course_id"]/(lesson["date"]+"_"+lesson["sub_id"])
                        completed = artifact_valid(directory,SIGNATURE)
                        plan = "跳过已完成" if completed else "处理回放" if lesson["status"]=="6" else "等待回放"
                        print(f"{course['name']} / {lesson['title']} / {lesson['sub_id']}：{plan}",flush=True)
                        if not args.dry_run:
                            counts[process_lesson(site,ledger,lesson,site_only=args.site_only)] += 1
                finally:
                    ledger.close()
                if not args.dry_run:
                    atomic_json(root/"last-run.json",{"course":course,"selector":{"on":args.on,"since":args.since,"through":args.through},"counts":counts})
                    print("本次结果："+json.dumps(counts,ensure_ascii=False),flush=True)
                return 1 if counts["failed"] else 0
    except KeyboardInterrupt:
        print("已中断；完成的文件和字幕分段已保留，下次运行继续。",file=sys.stderr)
        return 130
    except Exception as exc:
        message = str(exc)
        if any(s in message for s in ("http://","https://","Bearer ","token=")):
            message = type(exc).__name__
        print(f"错误：{message}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
