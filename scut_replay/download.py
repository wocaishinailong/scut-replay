"""Bounded four-connection Range download with verified, reusable pieces."""
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from .core import atomic_json, digest, fingerprint

PART_BYTES = 8 * 1024 * 1024


def parallel_ranges(url, target, identity, headers, workers=4):
    """Return assembled part or None when server/file does not qualify for parallel ranges."""
    timeout = httpx.Timeout(90, connect=30)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("GET",url,headers={**headers,"Range":"bytes=0-0"}) as response:
            response.raise_for_status()
            content_range=response.headers.get("content-range","")
            if response.status_code != 206 or not content_range.startswith("bytes 0-0/"):
                return None
            total=int(content_range.split("/")[1])
            validator=response.headers.get("etag") or response.headers.get("last-modified")
            if not validator or total < 32*1024*1024:
                return None
            if sum(len(b) for b in response.iter_bytes()) != 1:
                raise ValueError("Range探测返回的字节数不正确")
    signature=fingerprint({"identity":identity,"total":total,"validator":validator})
    pieces=target.parent/(target.name+".pieces")/signature[:16]
    pieces.mkdir(parents=True,exist_ok=True)
    chunks=[(i,start,min(total,start+PART_BYTES)-1) for i,start in enumerate(range(0,total,PART_BYTES))]
    old_part=target.with_suffix(target.suffix+".part")
    old_meta=target.with_suffix(target.suffix+".part.json")
    # Reuse complete pieces from the earlier sequential attempt, only with matching validator.
    if old_part.exists() and old_meta.exists():
        meta=json.loads(old_meta.read_text())
        if meta.get("identity")==identity and meta.get("validator")==validator:
            with old_part.open("rb") as source:
                for i,start,end in chunks:
                    p=pieces/f"{i:04}.bin"
                    if end >= old_part.stat().st_size:
                        break
                    if not p.exists():
                        source.seek(start)
                        data=source.read(end-start+1)
                        p.write_bytes(data)
                        atomic_json(p.with_suffix(".json"),{"sha256":digest(p)})

    def one(chunk):
        i,start,end=chunk
        path=pieces/f"{i:04}.bin"
        checksum=path.with_suffix(".json")
        if path.exists() and checksum.exists() and path.stat().st_size==end-start+1:
            try:
                if json.loads(checksum.read_text())["sha256"]==digest(path):
                    return path,True
            except (ValueError,KeyError):
                pass
        for attempt in range(3):
            try:
                with httpx.Client(timeout=timeout,follow_redirects=False) as client:
                    with client.stream("GET",url,headers={**headers,"Range":f"bytes={start}-{end}","If-Range":validator}) as response:
                        response.raise_for_status()
                        if response.status_code!=206 or response.headers.get("content-range")!=f"bytes {start}-{end}/{total}":
                            raise ValueError("分段下载的响应区间不匹配；未合并文件")
                        received=0
                        temp=path.with_suffix(".tmp")
                        with temp.open("wb") as stream:
                            for block in response.iter_bytes(256*1024):
                                received+=len(block)
                                if received>end-start+1:
                                    raise ValueError("分段下载超过预期大小")
                                stream.write(block)
                        if received != end-start+1:
                            raise ValueError("分段下载不完整")
                temp.replace(path)
                atomic_json(checksum,{"sha256":digest(path)})
                return path,False
            except (httpx.HTTPError,OSError):
                if attempt==2:
                    raise RuntimeError(f"第{i+1}个视频分段下载失败；已完成分段已保存") from None
                time.sleep(attempt+1)

    complete,reused=0,0
    pool=ThreadPoolExecutor(max_workers=min(max(workers,1),4))
    try:
        futures={pool.submit(one,c):c for c in chunks}
        for future in as_completed(futures):
            _,cached=future.result()
            complete+=1;reused+=int(cached)
            print(f"回放分段：{complete}/{len(chunks)}（复用{reused}段）",flush=True)
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
    assembled=target.with_suffix(target.suffix+".assembled")
    with assembled.open("wb") as stream:
        for i,_,_ in chunks:
            with (pieces/f"{i:04}.bin").open("rb") as source:
                shutil.copyfileobj(source,stream)
    if assembled.stat().st_size != total:
        raise ValueError("合并后的视频长度不正确")
    return assembled
