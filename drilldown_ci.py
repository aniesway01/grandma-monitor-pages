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


def download_clip(host, port, user, password, camera, start_ts, end_ts, out_path):
    """Download DVR clip with multiple strategies."""
    from urllib.parse import quote
    ch = int(camera[2:])
    iframe = 1 << (ch - 1)
    duration = end_ts - start_ts

    # URL with embedded credentials (avoids -headers compatibility issues on Linux)
    url_auth = (f"http://{quote(user, safe='')}:{quote(password, safe='')}@"
                f"{host}:{port}/cgi-bin/net_video.cgi"
                f"?hq=1&iframe={iframe}&pframe=65535&audio=0&beg={start_ts}&end={end_ts}")
    # URL with -headers auth (fallback)
    url_plain = (f"http://{host}:{port}/cgi-bin/net_video.cgi"
                 f"?hq=1&iframe={iframe}&pframe=65535&audio=0&beg={start_ts}&end={end_ts}")
    auth_b64 = base64.b64encode(f"{user}:{password}".encode()).decode()

    strategies = [
        ("url-auth+libx264", [
            "ffmpeg", "-y", "-analyzeduration", "10000000", "-probesize", "10000000",
            "-i", url_auth,
            "-c:v", "libx264", "-preset", "fast", "-f", "mpegts", str(out_path)]),
        ("url-auth+copy+avi", [
            "ffmpeg", "-y", "-analyzeduration", "10000000", "-probesize", "10000000",
            "-i", url_auth,
            "-c", "copy", "-f", "avi", str(out_path.with_suffix(".avi"))]),
        ("headers+libx264", [
            "ffmpeg", "-y",
            "-headers", f"Authorization: Basic {auth_b64}\r\n",
            "-analyzeduration", "10000000", "-probesize", "10000000",
            "-i", url_plain,
            "-c:v", "libx264", "-preset", "fast", "-f", "mpegts", str(out_path)]),
    ]

    timeout_s = max(int(duration * 5), 300)
    print(f"  Downloading {camera} clip ({duration//60}m{duration%60}s)...")
    t0 = time.time()

    for name, cmd in strategies:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            _, stderr_data = proc.communicate(timeout=timeout_s)
        except (subprocess.TimeoutExpired, KeyboardInterrupt, Exception):
            proc.kill()
            proc.communicate()
            stderr_data = b""
        # Check both .ts and .avi outputs
        for candidate in [out_path, out_path.with_suffix(".avi")]:
            if candidate.exists() and candidate.stat().st_size > 10000:
                elapsed = time.time() - t0
                size_mb = candidate.stat().st_size / 1024 / 1024
                print(f"  OK ({size_mb:.1f}MB, {elapsed:.0f}s, strategy={name})")
                # Normalize to out_path if different file
                if candidate != out_path:
                    if out_path.exists():
                        out_path.unlink()
                    candidate.rename(out_path)
                return True
        # Cleanup failed outputs
        out_path.unlink(missing_ok=True)
        out_path.with_suffix(".avi").unlink(missing_ok=True)
        err_tail = stderr_data.decode(errors="replace").strip()[-300:] if stderr_data else "no stderr"
        print(f"  [{name}] failed: {err_tail}")

    print(f"  FAIL (all encoders failed)")
    return False


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
    clip_path = out_dir / "clip.ts"
    frames_dir = out_dir / "frames"

    print("=" * 60)
    print(f"Drill-Down CI: {camera} {args.date} {args.start}~{args.end}")
    print("=" * 60)

    # Step 1: DVR health check + wait
    print("\n[1/4] DVR health check...")
    if not wait_for_dvr(args.dvr_host, args.dvr_port, args.dvr_user, args.dvr_password):
        print("[FAIL] DVR unavailable after retries")
        sys.exit(1)

    # Step 2: Download clip
    print("\n[2/4] Downloading clip...")
    ok = download_clip(args.dvr_host, args.dvr_port, args.dvr_user, args.dvr_password,
                       camera, start_ts, end_ts, clip_path)
    if not ok:
        print("[FAIL] Clip download failed")
        sys.exit(1)

    # Step 3: Extract frames
    print("\n[3/4] Extracting frames...")
    frames = extract_frames(clip_path, frames_dir, args.interval, start_ts)
    if not frames:
        print("[FAIL] No frames extracted")
        sys.exit(1)

    # Step 4: Upload to Drive
    print("\n[4/4] Uploading to Drive...")
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
