import json
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scut_replay.asr import chunk_bounds, own_segments
from scut_replay.core import (Ledger, artifact_valid, atomic_json, digest, export_transcript,
                              lesson_date, run_lock, select_lessons, timestamp)
from scut_replay.pipeline import SIGNATURE, download_media, process_lesson, site_segments
from scut_replay.site import SiteError, safe_url, schedule_days


def lesson(day="2026-10-22", sid="2", cid="100", status="6"):
    return {"course_id":cid,"sub_id":sid,"date":day,"title":day+"第3-4节",
            "course_name":"数字系统设计","teacher":"测试教师","status":status}


class FakeSite:
    auth = None

    def __init__(self):
        self.calls = []

    def resources(self, item):
        self.calls.append(item["sub_id"])
        return {"status":"6","duration_seconds":60,"start_at":0,"media_url":None,
                "transcript_response":{"code":0,"list":[{"all_content":[
                    {"BeginSec":1,"EndSec":3,"Text":"测试课堂原话，不进行总结。"}]}]},
                "slides":[{"sequence":1,"start_seconds":2,"image_url":"https://example.test/ppt.jpg"}],
                "warnings":[]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_dates_include_cutoff_and_keep_distinct_same_day_lessons(self):
        items = [lesson("2026-10-22","1"),lesson("2026-10-22","2"),lesson("2026-10-23","3")]
        self.assertEqual([x["sub_id"] for x in select_lessons(items+items,on="2026-10-22")],["1","2"])
        self.assertEqual(len(select_lessons(items,through="2026-10-22")),2)
        self.assertEqual(len(select_lessons(items,since="2026-10-23",through="2026-11-22")),1)

    def test_invalid_dates_fail_before_processing(self):
        for kw in ({"on":"2026-02-30"},{"on":"2026-10-22","through":"2026-11-22"},
                   {"since":"2026-11-22","through":"2026-10-22"},{}):
            with self.assertRaises(ValueError):
                select_lessons([],**kw)

    def test_date_fallback_uses_shanghai_timezone(self):
        self.assertEqual(lesson_date("2026-10-22第3-4节"),"2026-10-22")
        self.assertEqual(lesson_date("无日期",1792598400),"2026-10-22")

    def test_real_empty_schedule_does_not_hide_other_errors(self):
        self.assertEqual(schedule_days({"success":True,"result":{"code":400,"msg":"课表为空"}}),[])
        for msg in ("缺少权限","服务器错误",""):
            with self.assertRaises(SiteError):
                schedule_days({"success":True,"result":{"code":400,"msg":msg}})

    def test_no_token_in_saved_urls(self):
        self.assertEqual(safe_url("https://school.test/api?course_id=3&token=secret&ticket=secret"),
                         "https://school.test/api?course_id=3")

    def test_incremental_processing_retries_old_pending_and_skips_completed(self):
        site, ledger = FakeSite(), Ledger(self.root)
        first = [lesson("2026-10-01","1",status="3"),lesson("2026-10-22","2")]
        self.assertEqual([process_lesson(site,ledger,i) for i in first],["pending","completed"])
        later = [lesson("2026-10-01","1"),lesson("2026-10-22","2"),lesson("2026-11-20","3")]
        self.assertEqual([process_lesson(site,ledger,i) for i in later],["completed","skipped","completed"])
        self.assertEqual(site.calls,["2","1","3"])
        self.assertTrue(all(r["status"]=="done" for r in ledger.rows()))
        ledger.close()

    def test_courses_do_not_collide_and_recovery_checks_output_files(self):
        site, ledger = FakeSite(), Ledger(self.root)
        a,b = lesson(cid="1"),lesson(cid="2")
        process_lesson(site,ledger,a)
        process_lesson(site,ledger,b)
        self.assertEqual(len(ledger.rows()),2)
        directory = Path(ledger.get(a)["output_dir"])
        self.assertTrue(artifact_valid(directory,SIGNATURE))
        (directory/"transcript.srt").write_text("damaged")
        self.assertFalse(artifact_valid(directory,SIGNATURE))
        self.assertEqual(process_lesson(site,ledger,a),"completed")
        self.assertTrue(artifact_valid(directory,SIGNATURE))
        ledger.close()

    def test_resource_failure_is_retryable_and_not_done(self):
        site, ledger = FakeSite(), Ledger(self.root)
        item = lesson()
        with patch.object(site,"resources",side_effect=SiteError("暂时失败")):
            self.assertEqual(process_lesson(site,ledger,item),"failed")
        self.assertEqual(ledger.get(item)["stage"],"resources")
        self.assertEqual(process_lesson(site,ledger,item),"completed")
        ledger.close()

    def test_site_only_missing_transcript_remains_pending(self):
        site, ledger = FakeSite(), Ledger(self.root)
        value = site.resources(lesson())
        value["transcript_response"] = {"code":10002,"list":[]}
        with patch.object(site,"resources",return_value=value):
            self.assertEqual(process_lesson(site,ledger,lesson(),site_only=True),"pending")
        self.assertEqual(ledger.get(lesson())["stage"],"waiting_transcript")
        ledger.close()

    def test_empty_transcript_never_creates_completion_marker(self):
        with self.assertRaises(ValueError):
            export_transcript(self.root,{"duration_seconds":60},[],"sig")
        self.assertFalse((self.root/"complete.json").exists())

    def test_platform_transcript_keeps_original_words_and_times(self):
        response = {"code":0,"list":[{"all_content":[{"Text":"这个不考","BeginSec":"12.3","EndSec":"14.8"}]}]}
        self.assertEqual(site_segments(response,0,60),[{"start":12.3,"end":14.8,"text":"这个不考"}])
        with self.assertRaises(ValueError):
            site_segments({"code":500},0,60)
        with self.assertRaises(ValueError):
            site_segments({"code":0,"list":[{"all_content":[{"unknown":"text"}]}]},0,60)

    def test_absolute_platform_timestamps_use_china_timezone(self):
        response={"code":0,"list":[{"trans_type":"ai","all_content":[
            {"time":"2026-10-22 10:40:12","zhtext":"第一句"},
            {"time":"2026-10-22 10:40:18","zhtext":"第二句"}]}]}
        result=site_segments(response,1792636800,60)
        self.assertEqual([(r["start"],r["end"]) for r in result],[(12,18),(18,60)])

    def test_chunks_cover_tail_without_gaps(self):
        bounds=list(chunk_bounds(5746.279))
        self.assertEqual(len(bounds),20)
        self.assertAlmostEqual(sum(b[2]-b[1] for b in bounds),5746.279)
        self.assertTrue(all(a[2]==b[1] for a,b in zip(bounds,bounds[1:])))
        self.assertEqual(bounds[-1][2],5746.279)

    def test_overlapping_chunk_boundary_has_one_owner(self):
        left=own_segments([{"start":299,"end":301,"text":"边界句"}],0,300,0)
        right=own_segments([{"start":1,"end":3,"text":"边界句"}],300,600,298)
        self.assertEqual(left,[])
        self.assertEqual(len(right),1)
        self.assertEqual(right[0]["start"],300)

    def test_srt_timestamp_rounding_and_output_is_literal(self):
        self.assertEqual(timestamp(59.9996,srt=True),"00:01:00,000")
        metadata={"course_name":"课程","lesson_title":"课次","source":"platform","duration_seconds":61}
        export_transcript(self.root,metadata,[{"start":59.5,"end":60.5,"text":"原话"}],"s")
        self.assertIn("00:00:59,500 --> 00:01:00,500\n原话",(self.root/"transcript.srt").read_text())

    def test_concurrent_jobs_do_not_write_same_state(self):
        with run_lock(self.root):
            with self.assertRaises(RuntimeError):
                with run_lock(self.root):
                    pass

    def test_local_asr_resumes_completed_chunks_after_failure(self):
        from scut_replay import asr
        audio=self.root/"audio.wav"
        with wave.open(str(audio),"wb") as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
            out.writeframes(b'\x00\x00'*16000*6)
        model={"repo":"test","revision":"fixed","path":"unused"}
        bounds=[(0,0,2,0,2),(1,2,4,2,4),(2,4,6,4,6)]
        segment={"segments":[{"start":0,"end":1,"text":"原话"}]}
        with patch.object(asr,"speech_regions",return_value=[{'start':0,'end':32000}]), patch.object(asr,"detect_language",return_value=('en',.99)), patch.object(asr,"chunk_bounds",return_value=iter(bounds)), patch("mlx_whisper.transcribe",side_effect=[segment,RuntimeError("interrupted")]) as engine:
            with self.assertRaises(RuntimeError):
                asr.transcribe(audio,self.root,model)
            self.assertEqual(engine.call_count,2)
        with patch.object(asr,"speech_regions",return_value=[{'start':0,'end':32000}]), patch.object(asr,"detect_language",return_value=('en',.99)), patch.object(asr,"chunk_bounds",return_value=iter(bounds)), patch("mlx_whisper.transcribe",return_value=segment) as engine:
            segments,stats=asr.transcribe(audio,self.root,model)
            self.assertEqual(engine.call_count,2)  # First successful chunk was not transcribed again.
            self.assertEqual([s["start"] for s in segments],[0,2,4])
            self.assertEqual(stats["coverage_seconds"],6)

    def test_silent_audio_never_reaches_asr_decoder(self):
        from scut_replay import asr
        audio=self.root/'silent.wav'
        with wave.open(str(audio),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
            out.writeframes(b'\x00\x00'*16000*3)
        with patch('mlx_whisper.transcribe',side_effect=AssertionError('silence reached decoder')):
            segments,stats=asr.transcribe(audio,self.root,{'repo':'test','revision':'fixed','path':'unused'})
        self.assertEqual(segments,[])
        self.assertEqual(stats['coverage_seconds'],3)

    def test_vad_filter_preserves_speech_but_removes_empty_pause(self):
        from scut_replay.asr import overlaps_speech
        regions=[{'start':90,'end':110}]
        self.assertFalse(overlaps_speech({'start':30,'end':60},regions))
        self.assertTrue(overlaps_speech({'start':99,'end':104},regions))

    def test_uncertain_candidates_are_marked_and_not_silently_removed(self):
        from scut_replay.asr import annotate_quality
        segment={'start':10,'end':40,'text':'待回听的候选文字'}
        flagged=annotate_quality(segment,[{'start':11,'end':12}],.49)
        self.assertTrue(flagged['review_required'])
        self.assertEqual(flagged['text'],segment['text'])
        self.assertEqual(flagged['quality_flags'],['sparse_speech','uncertain_language'])
        metadata={'course_name':'课程','lesson_title':'课次','source':'local-asr','duration_seconds':60}
        export_transcript(self.root,metadata,[flagged],'quality')
        self.assertIn('[待核对] 待回听的候选文字',(self.root/'transcript.txt').read_text())
        self.assertIn('[待核对] 待回听的候选文字',(self.root/'transcript.srt').read_text())
        reliable=annotate_quality({'start':10,'end':12,'text':'可靠候选'},[{'start':10,'end':12}],.99)
        self.assertFalse(reliable['review_required'])

    def test_parallel_range_download_recovers_and_verifies_cached_pieces(self):
        from scut_replay.download import parallel_ranges
        payload=b'\x00\x00\x00\x20ftyp'+b'y'*(32*1024*1024)
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                bounds=self.headers["Range"].split("=")[1].split("-")
                start,end=map(int,bounds)
                requests.append((start,end))
                self.send_response(206)
                self.send_header("ETag",'"v1"')
                self.send_header("Content-Range",f"bytes {start}-{end}/{len(payload)}")
                self.send_header("Content-Length",str(end-start+1))
                self.end_headers();self.wfile.write(payload[start:end+1])
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            url=f"http://127.0.0.1:{server.server_port}/video.mp4"
            target=self.root/"video.mp4"
            assembled=parallel_ranges(url,target,url,{},4)
            self.assertEqual(assembled.read_bytes(),payload)
            requests.clear()
            parallel_ranges(url,target,url,{},4)
            self.assertEqual(requests,[(0,0)])  # Only a fresh validator probe, no repeated file data.
            piece=next((self.root/"video.mp4.pieces").glob("*/0001.bin"))
            piece.write_bytes(b'broken')
            requests.clear()
            assembled=parallel_ranges(url,target,url,{},4)
            self.assertEqual(assembled.read_bytes(),payload)
            self.assertEqual(sorted(requests),[(0,0),(8*1024*1024,16*1024*1024-1)])
        finally:
            server.shutdown();server.server_close();worker.join()


class DownloadTests(unittest.TestCase):
    def test_resume_and_server_ignoring_range(self):
        payload=b'\x00\x00\x00\x20ftyp'+b'x'*65536
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                requested=self.headers.get("Range")
                requests.append((self.path,requested))
                offset=int(requested.split("=")[1].split("-")[0]) if requested else 0
                if self.path=="/ignore.mp4":offset=0
                self.send_response(206 if offset else 200)
                self.send_header("Content-Type","video/mp4")
                self.send_header("ETag",'"v1"')
                self.send_header("Content-Length",str(len(payload)-offset))
                if offset:self.send_header("Content-Range",f"bytes {offset}-{len(payload)-1}/{len(payload)}")
                self.end_headers();self.wfile.write(payload[offset:])
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                for name in ("resume.mp4","ignore.mp4"):
                    target=Path(td)/name
                    url=f"http://127.0.0.1:{server.server_port}/{name}"
                    target.with_suffix('.mp4.part').write_bytes(payload[:4096])
                    atomic_json(target.with_suffix('.mp4.part.json'),{"identity":url,"validator":'"v1"'})
                    result=download_media(url,target,workers=1)
                    self.assertEqual(target.read_bytes(),payload)
                    self.assertEqual(result["sha256"],digest(target))
                    count=len(requests)
                    download_media(url,target,workers=1)
                    self.assertEqual(len(requests),count)
                self.assertTrue(all(r[1]=="bytes=4096-" for r in requests))
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=="__main__":unittest.main()
