"""
What: Standalone drill-down for GitHub Actions — no local dependencies
Why: GitHub runner can't import adaptive_pipeline (Windows-specific, local state)
When: Triggered by workflow_dispatch from timeline webpage
How: DVR health check → download clip → extract frames → upload Drive → generate gallery HTML
"""
import subprocess, sys, json, base64, time, argparse, os, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

TW = timezone(timedelta(hours=8))


def dvr_health_check(host, port, user, password):
    """Quick DVR liveness check via net_jpeg.cgi."""
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = f"http://{host}:{port}/cgi-bin/net_jpeg.cgi?ch=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read()
        elapsed = time.time() - t0
        if len(data) > 5000:
            return True, elapsed
        return False, f"small response ({len(data)} bytes)"
    except Exception as e:
        return False, str(e)


def wait_for_dvr(host, port, user, password, max_retries=5):
    """Wait until DVR is available (not busy with another stream)."""
    for attempt in range(max_retries):
        ok, detail = dvr_health_check(host, port, user, password)
        if ok:
            if isinstance(detail, float) and detail < 5.0:
                print(f"  DVR ready (responded in {detail:.1f}s)")
                return True
            else:
                print(f"  DVR slow ({detail}s), might be busy. Retry {attempt+1}/{max_retries}...")
        else:
            print(f"  DVR check failed: {detail}. Retry {attempt+1}/{max_retries}...")
        if attempt < max_retries - 1:
            time.sleep(60)
    return False


def download_frames_direct(host, port, user, password, camera, start_ts, end_ts, out_dir, interval_s):
    """Download frames directly from DVR multipart MJPEG stream.

    iCatch DVR's net_video.cgi returns multipart/x-mixed-replace MIME with
    JPEG frames. Parse boundaries in Python — no ffmpeg dependency for download.
    """
    ch = int(camera[2:])
    iframe = 1 << (ch - 1)
    duration = end_ts - start_ts
    auth_b64 = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = (f"http://{host}:{port}/cgi-bin/net_video.cgi"
           f"?hq=1&iframe={iframe}&pframe=65535&audio=0&beg={start_ts}&end={end_ts}")

    out_dir.mkdir(parents=True, exist_ok=True)
    timeout_s = max(int(duration * 5), 300)
    print(f"  Downloading {camera} frames ({duration//60}m{duration%60}s, interval={interval_s}s)...")
    t0 = time.time()

    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth_b64}"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
    except Exception as e:
        print(f"  [ERR] HTTP request failed: {e}")
        return []

    # Parse multipart boundary from Content-Type
    ct = resp.headers.get("Content-Type", "")
    boundary = None
    if "boundary=" in ct:
        boundary = ct.split("boundary=")[1].strip().encode()
    if not boundary:
        boundary = b"myboundary"
    boundary_marker = b"--" + boundary

    # Read stream and extract JPEG frames at interval
    frames = []
    frame_count = 0
    last_saved_ts = 0
    buf = b""
    target_frames = duration // interval_s

    try:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf += chunk

            # Extract complete JPEG frames from buffer
            while boundary_marker in buf:
                idx = buf.find(boundary_marker)
                part = buf[:idx]
                buf = buf[idx + len(boundary_marker):]

                # Find JPEG data in this part (starts after \r\n\r\n header)
                header_end = part.find(b"\r\n\r\n")
                if header_end < 0:
                    continue
                jpeg_data = part[header_end + 4:]
                if len(jpeg_data) < 1000:
                    continue

                frame_count += 1
                # Save at interval (DVR sends ~25fps, we want 1 frame per interval_s)
                frame_time = start_ts + (frame_count / 25.0)
                if frame_time - last_saved_ts >= interval_s or last_saved_ts == 0:
                    last_saved_ts = frame_time
                    dt = datetime.fromtimestamp(frame_time, TW)
                    fname = f"{dt.strftime('%H%M%S')}.jpg"
                    fpath = out_dir / fname
                    fpath.write_bytes(jpeg_data)
                    frames.append(fpath)

                    if len(frames) % 10 == 0:
                        print(f"    {len(frames)} frames saved...")

                    if len(frames) >= target_frames:
                        break

            if len(frames) >= target_frames:
                break
            if time.time() - t0 > timeout_s:
                print(f"  [WARN] Timeout after {len(frames)} frames")
                break
    except Exception as e:
        print(f"  [WARN] Stream ended: {e}")

    elapsed = time.time() - t0
    print(f"  Downloaded {len(frames)} frames in {elapsed:.0f}s (from {frame_count} total)")
    return frames


def extract_frames(clip_path, out_dir, interval_s, start_ts):
    """Extract frames at interval, rename to HHMMSS.jpg."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(clip_path),
        "-vf", f"fps=1/{interval_s}",
        "-q:v", "2",
        str(out_dir / "frame_%04d.jpg")
    ]
    subprocess.run(cmd, capture_output=True, timeout=120)
    raw_frames = sorted(out_dir.glob("frame_*.jpg"))
    frames = []
    for i, f in enumerate(raw_frames):
        frame_ts = start_ts + i * interval_s
        dt = datetime.fromtimestamp(frame_ts, TW)
        new_name = f"{dt.strftime('%H%M%S')}.jpg"
        dest = out_dir / new_name
        if dest.exists():
            dest.unlink()
        f.rename(dest)
        frames.append(dest)
    print(f"  Extracted {len(frames)} frames (every {interval_s}s)")
    return frames


def upload_to_drive(frames, camera, date_str, start_str, end_str, token_path):
    """Upload frames to Google Drive, return URL map."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    with open(str(token_path), encoding="utf-8") as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired or not creds.valid:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(str(token_path), "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False, indent=2)

    service = build("drive", "v3", credentials=creds)

    def find_or_create_folder(name, parent_id=None):
        q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        results = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            meta["parents"] = [parent_id]
        folder = service.files().create(body=meta, fields="id").execute()
        return folder["id"]

    print(f"  Uploading {len(frames)} frames to Drive...")
    root_id = find_or_create_folder("GrandMaMonitor_images")
    date_id = find_or_create_folder(date_str, root_id)
    cam_id = find_or_create_folder(camera, date_id)
    dd_id = find_or_create_folder("drilldown", cam_id)
    seg_name = f"{start_str.replace(':','')}-{end_str.replace(':','')}"
    seg_id = find_or_create_folder(seg_name, dd_id)

    # Make folders public
    for fid in [root_id, date_id, cam_id, dd_id, seg_id]:
        try:
            service.permissions().create(
                fileId=fid, body={"type": "anyone", "role": "reader"}
            ).execute()
        except Exception:
            pass

    url_map = {}
    uploaded = 0
    for f in frames:
        try:
            # Check if already exists
            q = f"name='{f.name}' and '{seg_id}' in parents and trashed=false"
            existing = service.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
            if existing:
                file_id = existing[0]["id"]
            else:
                meta = {"name": f.name, "parents": [seg_id]}
                media = MediaFileUpload(str(f), mimetype="image/jpeg", resumable=False)
                result = service.files().create(body=meta, media_body=media, fields="id").execute()
                file_id = result["id"]
                service.permissions().create(
                    fileId=file_id, body={"type": "anyone", "role": "reader"}
                ).execute()
            url = f"https://lh3.googleusercontent.com/d/{file_id}"
            url_map[f.name] = url
            uploaded += 1
            if uploaded % 30 == 0:
                print(f"    {uploaded}/{len(frames)}")
        except Exception as e:
            print(f"    [ERR] {f.name}: {e}")
        time.sleep(0.05)
    print(f"  Uploaded {uploaded}/{len(frames)}")
    return url_map


def generate_gallery_html(camera, date_str, start_str, end_str, frames, url_map, out_path):
    """Generate standalone drilldown gallery HTML with Drive URLs."""
    html = []
    html.append("<!DOCTYPE html><html><head>")
    html.append('<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    html.append(f"<title>Drill-Down {camera} {date_str} {start_str}-{end_str}</title>")
    html.append("""<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#1a1a2e;color:#e0e0e0;padding:16px}
h1{font-size:1.2em;margin-bottom:12px}
.info{color:#888;font-size:0.85em;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px}
.frame{position:relative;cursor:pointer}
.frame img{width:100%;border-radius:4px;border:2px solid #333;transition:border-color 0.15s}
.frame img:hover{border-color:#e94560;box-shadow:0 0 12px rgba(233,69,96,0.5)}
.frame .ts{position:absolute;top:4px;left:6px;background:rgba(0,0,0,0.7);color:#fff;padding:2px 8px;border-radius:3px;font-size:0.8em}
a{color:#e94560}
</style></head><body>""")
    html.append(f"<h1>Drill-Down: {camera} {date_str} {start_str}~{end_str}</h1>")
    html.append(f'<div class="info">{len(frames)} frames every 5s | ')
    html.append(f'<a href="timeline_{date_str}.html">Back to timeline</a></div>')
    html.append('<div class="grid">')

    for f in frames:
        ts_label = f.stem
        if len(ts_label) == 6:
            ts_label = f"{ts_label[:2]}:{ts_label[2:4]}:{ts_label[4:6]}"
        url = url_map.get(f.name, "")
        if url:
            html.append(f'<div class="frame" onclick="window.open(\'{url}\',\'_blank\')">')
            html.append(f'<img src="{url}" loading="lazy">')
            html.append(f'<span class="ts">{ts_label}</span></div>')

    html.append("</div>")
    html.append(f'<div class="info" style="margin-top:16px">Generated {datetime.now(TW).strftime("%Y-%m-%d %H:%M")}</div>')
    html.append("</body></html>")

    with open(str(out_path), "w", encoding="utf-8") as fout:
        fout.write("\n".join(html))
    size_kb = out_path.stat().st_size / 1024
    print(f"  Gallery HTML: {out_path.name} ({size_kb:.0f}KB)")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Drill-down for CI (GitHub Actions)")
    ap.add_argument("--camera", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--dvr-host", default=os.environ.get("DVR_HOST", "122.117.191.83"))
    ap.add_argument("--dvr-port", type=int, default=int(os.environ.get("DVR_PORT", "2401")))
    ap.add_argument("--dvr-user", default=os.environ.get("DVR_USER", "admin"))
    ap.add_argument("--dvr-password", default=os.environ.get("DVR_PASSWORD", ""))
    ap.add_argument("--token-path", default="gdrive_token.json")
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()

    camera = args.camera.upper()
    y, mo, d = map(int, args.date.split("-"))
    sh, sm = map(int, args.start.split(":"))
    eh, em = map(int, args.end.split(":"))
    start_ts = int(datetime(y, mo, d, sh, sm, tzinfo=TW).timestamp())
    end_ts = int(datetime(y, mo, d, eh, em, tzinfo=TW).timestamp())

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"

    print("=" * 60)
    print(f"Drill-Down CI: {camera} {args.date} {args.start}~{args.end}")
    print("=" * 60)

    # Step 1: DVR health check + wait
    print("\n[1/3] DVR health check...")
    if not wait_for_dvr(args.dvr_host, args.dvr_port, args.dvr_user, args.dvr_password):
        print("[FAIL] DVR unavailable after retries")
        sys.exit(1)

    # Step 2: Download frames directly (no ffmpeg needed — parse multipart MJPEG)
    print("\n[2/3] Downloading frames from DVR...")
    frames = download_frames_direct(args.dvr_host, args.dvr_port, args.dvr_user, args.dvr_password,
                                    camera, start_ts, end_ts, frames_dir, args.interval)
    if not frames:
        print("[FAIL] No frames downloaded")
        sys.exit(1)

    # Step 3: Upload to Drive
    print("\n[3/3] Uploading to Drive...")
    token_path = Path(args.token_path)
    if not token_path.exists():
        print(f"[FAIL] Token file not found: {token_path}")
        sys.exit(1)
    url_map = upload_to_drive(frames, camera, args.date, args.start, args.end, token_path)

    # Generate gallery HTML
    gallery_name = f"drilldown_{camera}_{args.date}_{args.start.replace(':','')}-{args.end.replace(':','')}.html"
    gallery_path = out_dir / gallery_name
    generate_gallery_html(camera, args.date, args.start, args.end, frames, url_map, gallery_path)

    # Save metadata for workflow to use
    meta = {
        "camera": camera, "date": args.date,
        "start": args.start, "end": args.end,
        "frames": len(frames), "uploaded": len(url_map),
        "gallery": gallery_name,
    }
    meta_path = out_dir / "drilldown_result.json"
    with open(str(meta_path), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\n{'='*60}")
    print(f"[OK] Drill-Down complete: {len(frames)} frames, {len(url_map)} uploaded")
    print(f"  Gallery: {gallery_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
