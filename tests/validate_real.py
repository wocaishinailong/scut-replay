"""Validate an actual completed lesson without invoking ASR or changing its artifacts."""
import argparse
import json
import math
from collections import Counter
from pathlib import Path

from scut_replay.core import artifact_valid, atomic_json
from scut_replay.pipeline import SIGNATURE


def validate(directory):
    directory=Path(directory)
    assert artifact_valid(directory,SIGNATURE),"Missing, corrupted or stale subtitle output"
    transcript=json.loads((directory/'transcript.json').read_text())
    segments=transcript['segments']
    duration=transcript['duration_seconds']
    assert segments
    assert all(math.isfinite(s[k]) for s in segments for k in ('start','end'))
    assert all(0<=s['start']<s['end']<=duration+0.001 for s in segments)
    assert all(a['start']<=b['start'] for a,b in zip(segments,segments[1:]))
    text=(directory/'transcript.txt').read_text()
    srt=(directory/'transcript.srt').read_text()
    assert len(srt.strip().split('\n\n'))==len(segments)
    assert all(s['text'] in text and s['text'] in srt for s in segments)
    chunks=transcript.get('chunks',[])
    if transcript['source']=='local-asr':
        assert chunks and chunks[0]['core'][0]==0
        assert chunks[-1]['core'][1]==duration
        assert all(a['core'][1]==b['core'][0] for a,b in zip(chunks,chunks[1:]))
        assert abs(transcript['coverage_seconds']-duration)<0.001
        assert transcript['asr_config']['task']=='transcribe'
        assert transcript['asr_config']['language']=='auto-from-speech'
    report={
        'course_id':transcript['course_id'],'sub_id':transcript['sub_id'],
        'source':transcript['source'],'duration_seconds':duration,'segment_count':len(segments),
        'first_segment_start':segments[0]['start'],'last_segment_end':segments[-1]['end'],
        'chunk_count':len(chunks),'languages':dict(Counter(c.get('language') for c in chunks)),
        'sum_chunk_processing_seconds':round(sum(c['elapsed_seconds'] for c in chunks),3),
        'text_characters':sum(len(s['text']) for s in segments),
        'low_logprob_segments':sum(s.get('avg_logprob',0)<-1 for s in segments),
        'high_compression_segments':sum(s.get('compression_ratio',0)>2.4 for s in segments),
        'adjacent_identical_text':sum(a['text']==b['text'] for a,b in zip(segments,segments[1:])),
        'review_required_segments':sum(bool(s.get('review_required')) for s in segments),
        'checks':'PASS: checksums, timestamps, text/SRT/JSON consistency, complete chunk coverage',
        'human_transcription_accuracy':'Not Executed; no manually labelled reference transcript',
    }
    atomic_json(directory/'validation.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    print(json.dumps(validate(args.directory),ensure_ascii=False,indent=2))
