#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""영상 업스케일러

DVD를 비롯한 저화질 영상(MP4, MKV, AVI, WMV, WebM 등)을 HD~4K로 변환한다.
  - 인터레이스 제거(bwdif), 노이즈 제거, Lanczos 확대, 선명화, 화면비/색공간 보정
  - Real-ESRGAN(realesrgan-ncnn-vulkan)이 있으면 AI 업스케일링

인자 없이 실행하면 GUI, 인자를 주면 명령줄 모드로 동작한다.
  py dvd_upscaler.py                      # GUI
  py dvd_upscaler.py in.vob -o out.mp4    # 명령줄
"""
import argparse
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PROBE = ["-analyzeduration", "100M", "-probesize", "100M"]
MAX_CHUNK_FRAMES = 300   # AI 모드에서 한 번에 처리하는 프레임 수(DVD 해상도 기준)
VIDEO_EXTS = ("*.mp4 *.mkv *.avi *.mov *.wmv *.webm *.flv *.m4v *.mpg *.mpeg *.vob *.m2v *.m2p "
              "*.ts *.m2ts *.mts *.3gp *.3g2 *.asf *.divx *.ogv *.rm *.rmvb *.f4v *.mxf *.dv")


class Cancelled(Exception):
    pass


class UpscaleError(Exception):
    pass


# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------
@dataclass
class Options:
    height: int = 1080
    aspect: str = "auto"        # auto | 4:3 | 16:9
    deint: str = "auto"         # auto | on | off
    denoise: str = "weak"       # off | weak | medium | strong
    sharpen: str = "medium"     # off | weak | medium | strong
    quality: str = "high"       # high | medium | small
    container: str = "mp4"      # mp4 | mkv
    audio: str = "auto"         # auto(가능하면 원본 유지) | aac
    ai: bool = False
    ai_model: str = "realesr-animevideov3"
    subs: bool = True           # 자막 트랙과 같은 이름의 자막 파일도 옮기기
    gpu: int = -1               # AI에 쓸 그래픽 카드 번호 (-1 = 자동)


@dataclass
class Source:
    src: str            # ffmpeg 입력 (파일 경로 또는 concat:...)
    label: str          # 목록에 보이는 이름
    stem: str           # 출력 파일 이름의 기본
    is_dvd: bool = False


@dataclass
class MediaInfo:
    width: int
    height: int
    dar: float
    fps: float
    duration: float
    interlaced: bool
    vcodec: str
    acodecs: list = field(default_factory=list)
    hdr: bool = False
    scodecs: list = field(default_factory=list)     # 자막 트랙 코덱 (순서대로)


class Canceller:
    """취소 요청 시 실행 중인 외부 프로세스를 바로 종료한다."""

    def __init__(self):
        self._ev = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    def is_set(self):
        return self._ev.is_set()

    def set(self):
        self._ev.set()
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def add(self, p):
        with self._lock:
            self._procs.add(p)
        if self._ev.is_set():
            p.kill()

    def remove(self, p):
        with self._lock:
            self._procs.discard(p)

    def check(self):
        if self._ev.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------
# 외부 도구 찾기
# --------------------------------------------------------------------------
def _first_file(cands):
    for c in cands:
        if c and Path(c).is_file():
            return str(c)
    return None


def find_ffmpeg():
    la = os.environ.get("LOCALAPPDATA", "")
    cands = [str(p) for p in APP_DIR.glob("tools/**/ffmpeg.exe")]
    cands.append(shutil.which("ffmpeg"))
    if la:
        cands.append(os.path.join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
        cands += glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages",
                                        "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True)
    cands += [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]
    # 마지막 수단: 다른 프로그램에 들어 있는 ffmpeg
    cands += sorted(glob.glob(r"C:\Program Files\CapCut\Apps\*\ffmpeg.exe"), reverse=True)
    return _first_file(cands)


def find_realesrgan():
    cands = [str(p) for p in APP_DIR.glob("tools/**/realesrgan-ncnn-vulkan.exe")]
    cands.append(shutil.which("realesrgan-ncnn-vulkan"))
    return _first_file(cands)


VENC_ORDER = ["libx264", "h264_amf", "h264_nvenc", "h264_qsv", "hevc_amf",
              "h264_mf", "libx265", "mpeg4"]


def venc_args(name, quality, w, h, fps):
    q = {"high": 0, "medium": 1, "small": 2}[quality]
    if name == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str([17, 20, 23][q])]
    if name == "libx265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str([19, 22, 25][q])]
    if name in ("h264_amf", "hevc_amf"):
        qi = [18, 22, 26][q]
        a = ["-c:v", name, "-quality", "quality", "-rc", "cqp",
             "-qp_i", str(qi), "-qp_p", str(qi + 2)]
        if name == "h264_amf":
            a += ["-qp_b", str(qi + 4)]
        return a
    if name == "h264_nvenc":
        return ["-c:v", name, "-preset", "slow", "-rc", "vbr",
                "-cq", str([19, 23, 27][q]), "-b:v", "0"]
    if name == "h264_qsv":
        return ["-c:v", name, "-global_quality", str([19, 23, 27][q])]
    if name == "h264_mf":
        br = int(w * h * max(fps, 24) * [0.14, 0.09, 0.06][q])
        return ["-c:v", name, "-b:v", str(br)]
    return ["-c:v", "mpeg4", "-q:v", str([2, 4, 6][q])]


def _tiny_png():
    """그래픽 카드 목록을 얻을 때 넣어 주는 8x8 회색 PNG."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Tools:
    def __init__(self):
        self.ffmpeg = find_ffmpeg()
        self.realesrgan = find_realesrgan()
        self.filters = set()
        self.encoders = set()
        self.venc = None
        self._venc_cache = {}
        self._gpus = None
        if self.ffmpeg:
            self._probe_caps()

    def out(self, *args):
        return subprocess.run([self.ffmpeg, "-hide_banner", *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, creationflags=NOWIN)

    def _probe_caps(self):
        for line in self.out("-filters").stdout.splitlines():
            m = re.match(r"\s*[A-Z.]{2,4}\s+(\w+)\s+\S+->\S+", line)
            if m:
                self.filters.add(m.group(1))
        for line in self.out("-encoders").stdout.splitlines():
            m = re.match(r"\s*[VAS][A-Z.]{5}\s+(\S+)", line)
            if m:
                self.encoders.add(m.group(1))

    def pick_encoder(self, container="mp4"):
        """실제로 동작하는 비디오 인코더를 고른다. 일부 ffmpeg 빌드는 인코더와
        파일 형식 조합에 따라 실패하므로, 저장할 형식으로 짧게 시험 인코딩해 본다."""
        if container in self._venc_cache:
            self.venc = self._venc_cache[container]
            return self.venc
        tmp = Path(tempfile.mkdtemp(prefix="dvdup_enc_"))
        try:
            for name in VENC_ORDER:
                if name not in self.encoders:
                    continue
                r = self.out("-y", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=30",
                             "-frames:v", "5", "-pix_fmt", "yuv420p",
                             *venc_args(name, "high", 1920, 1080, 30),
                             str(tmp / f"test.{container}"))
                if r.returncode == 0:
                    self._venc_cache[container] = self.venc = name
                    return name
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        raise UpscaleError("사용할 수 있는 비디오 인코더를 찾지 못했습니다. ffmpeg 정식 버전을 설치해 주세요.")

    def list_gpus(self):
        """Real-ESRGAN이 쓸 수 있는 그래픽 카드 목록 [(번호, 이름)]."""
        if self._gpus is not None:
            return self._gpus
        self._gpus = []
        if not self.realesrgan:
            return self._gpus
        exe = Path(self.realesrgan)
        tmp = Path(tempfile.mkdtemp(prefix="dvdup_gpu_"))
        try:
            png = tmp / "in.png"
            png.write_bytes(_tiny_png())
            # 없는 번호를 주면 장치 목록만 출력하고 바로 끝난다
            r = subprocess.run([str(exe), "-i", str(png), "-o", str(tmp / "out.png"), "-g", "9999"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, cwd=str(exe.parent), creationflags=NOWIN,
                               timeout=60)
            seen = {}
            for line in (r.stderr + "\n" + r.stdout).splitlines():
                m = re.match(r"\[(\d+) (.+?)\]\s+queueC=", line.strip())
                if m:
                    seen.setdefault(int(m.group(1)), m.group(2).strip())
            self._gpus = sorted(seen.items())
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return self._gpus

    def gpu_name(self, gpu):
        if gpu is None or gpu < 0:
            return "자동"
        return f"{gpu}: {dict(self.list_gpus()).get(gpu, '알 수 없음')}"

    def describe(self):
        if not self.ffmpeg:
            return "ffmpeg를 찾지 못했습니다. ffmpeg를 설치해야 변환할 수 있습니다."
        s = f"ffmpeg: {self.ffmpeg}"
        if "CapCut" in self.ffmpeg:
            s += "  (CapCut 내장본 사용 중)"
        if self.venc:
            s += f"\n인코더: {self.venc}"
        s += "\nAI(Real-ESRGAN): " + (self.realesrgan or "설치되지 않음")
        return s


# --------------------------------------------------------------------------
# 입력 분석
# --------------------------------------------------------------------------
def make_source(path):
    """파일 또는 DVD 폴더(VIDEO_TS)를 Source로 바꾼다."""
    p = Path(path)
    if p.is_dir():
        vts = p / "VIDEO_TS" if (p / "VIDEO_TS").is_dir() else p
        groups = {}
        for f in vts.iterdir():
            m = re.match(r"VTS_(\d+)_(\d+)\.VOB$", f.name, re.I)
            if m and int(m.group(2)) > 0:          # _0.VOB는 메뉴
                groups.setdefault(m.group(1), []).append(f)
        if not groups:
            raise UpscaleError(f"DVD 영상(VTS_xx_x.VOB)을 찾지 못했습니다: {p}")
        # 용량이 가장 큰 타이틀 = 본편
        best = max(groups.values(), key=lambda fs: sum(x.stat().st_size for x in fs))
        best.sort(key=lambda x: x.name.upper())
        root = vts.parent if vts.name.upper() == "VIDEO_TS" else vts
        stem = root.name or "DVD"
        return Source("concat:" + "|".join(str(f) for f in best),
                      f"[DVD] {root}  (본편 VOB {len(best)}개)", stem, True)
    if not p.is_file():
        raise UpscaleError(f"파일을 찾을 수 없습니다: {p}")
    return Source(str(p), str(p), p.stem, p.suffix.lower() == ".vob")


def sources_from_folder(path):
    """DVD 폴더면 본편 하나, 일반 폴더면 그 안의 영상 파일 전부."""
    p = Path(path)
    try:
        return [make_source(p)]
    except UpscaleError:
        exts = {e.lstrip("*") for e in VIDEO_EXTS.split()}
        files = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts
                       and "_업스케일_" not in f.stem)
        if not files:
            raise UpscaleError(f"이 폴더에서 영상 파일을 찾지 못했습니다: {p}") from None
        return [make_source(f) for f in files]


def probe(tools, src):
    text = tools.out(*PROBE, "-i", src).stderr
    vline = None
    acodecs = []
    scodecs = []
    rot, in_video = 0, False
    for line in text.splitlines():
        if "Stream #" not in line:
            if in_video:        # 휴대폰 영상 등의 회전 정보
                r = (re.search(r"rotation of (-?[\d.]+) degrees", line)
                     or re.search(r"\brotate\s*:\s*(-?[\d.]+)", line))
                if r:
                    rot = int(round(float(r.group(1))))
            continue
        in_video = False
        if ": Video:" in line and vline is None and "attached pic" not in line:
            vline = line
            in_video = True
        m = re.search(r": Audio: (\w+)", line)
        if m:
            acodecs.append(m.group(1))
        m = re.search(r": Subtitle: (\w+)", line)
        if m:
            scodecs.append(m.group(1))
    if vline is None:
        tail = "\n".join(text.strip().splitlines()[-4:])
        raise UpscaleError("영상 스트림을 읽지 못했습니다.\n" + tail)
    m = re.search(r", (\d{2,5})x(\d{2,5})[\s,\[]", vline + " ")
    if not m:
        raise UpscaleError("영상 해상도를 알 수 없습니다.\n" + vline.strip())
    w, h = int(m.group(1)), int(m.group(2))
    d = re.search(r"DAR (\d+):(\d+)", vline)
    dar = int(d.group(1)) / int(d.group(2)) if d and int(d.group(2)) else w / h
    f = re.search(r"([\d.]+) fps", vline) or re.search(r"([\d.]+) tbr", vline)
    fps = float(f.group(1)) if f else 29.97
    if not 1 <= fps <= 120:
        fps = 29.97
    dur = 0.0
    t = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if t:
        dur = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + float(t.group(3))
    vc = re.search(r": Video: (\w+)", vline)
    inter = bool(re.search(r"(top|bottom) first", vline))
    if abs(rot) % 180 == 90:        # ffmpeg가 디코드할 때 자동으로 돌려 주므로 가로세로를 바꿔 둔다
        w, h, dar = h, w, 1 / dar
    hdr = bool(re.search(r"bt2020|smpte2084|arib-std-b67", vline))
    return MediaInfo(w, h, dar, fps, dur, inter, vc.group(1) if vc else "?", acodecs, hdr, scodecs)


def fps_fraction(f):
    for n, d in ((24000, 1001), (30000, 1001), (60000, 1001), (25, 1), (50, 1),
                 (24, 1), (30, 1), (60, 1)):
        if abs(f - n / d) < 0.015:
            return f"{n}/{d}"
    return f"{f:.3f}"


def out_size(info, opts):
    dar = {"4:3": 4 / 3, "16:9": 16 / 9}.get(opts.aspect) or info.dar
    if int(opts.height) == 0:                       # 원본의 2배 (최대 8K)
        h = min(info.height * 2, 4320)
    else:                                           # 원본보다 작게 줄이지는 않는다
        h = max(int(opts.height), info.height)
    h += h % 2
    return int(round(h * dar / 2)) * 2, h


def chunk_frames(info):
    """AI 조각 크기. 해상도가 클수록 임시 파일이 커지므로 프레임 수를 줄인다."""
    return max(30, min(MAX_CHUNK_FRAMES, int(MAX_CHUNK_FRAMES * 720 * 480 / (info.width * info.height))))


def out_tag(opts):
    return ("2배" if int(opts.height) == 0 else f"{opts.height}p") + ("_AI" if opts.ai else "")


def fmt_time(sec):
    sec = int(max(sec, 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------
# 필터
# --------------------------------------------------------------------------
HQDN3D = {"weak": "hqdn3d=2:1.5:3:2.5", "medium": "hqdn3d=4:3:6:4.5", "strong": "hqdn3d=7:5:10:7"}
NLMEANS = {"weak": "nlmeans=s=1.8:p=5:r=7", "medium": "nlmeans=s=3:p=5:r=9",
           "strong": "nlmeans=s=5:p=7:r=11"}
CAS = {"weak": "cas=strength=0.3", "medium": "cas=strength=0.5", "strong": "cas=strength=0.75"}
UNSHARP = {"weak": "unsharp=5:5:0.4:5:5:0", "medium": "unsharp=5:5:0.8:5:5:0",
           "strong": "unsharp=5:5:1.2:5:5:0"}
SOFTER = {"strong": "medium", "medium": "weak", "weak": "off", "off": "off"}


def deint_filters(tools, opts):
    if opts.deint == "off":
        return []
    name = "bwdif" if "bwdif" in tools.filters else "yadif" if "yadif" in tools.filters else None
    if not name:
        return []
    which = "all" if opts.deint == "on" else "interlaced"
    return [f"{name}=mode=send_frame:parity=auto:deint={which}"]


def denoise_filters(tools, opts):
    if opts.denoise == "off":
        return []
    fs = []
    if opts.denoise in ("medium", "strong") and "deblock" in tools.filters:
        fs.append("deblock=filter=weak:block=8")       # MPEG-2 블록 노이즈 완화
    if "hqdn3d" in tools.filters:
        fs.append(HQDN3D[opts.denoise])
    elif "nlmeans" in tools.filters:
        fs.append(NLMEANS[opts.denoise])
    return fs


def sharpen_filters(tools, level):
    if level == "off":
        return []
    if "cas" in tools.filters:
        return [CAS[level]]
    if "unsharp" in tools.filters:
        return [UNSHARP[level]]
    return []


def is_sd(info):
    return info.height <= 576


def color_tags(info):
    """SD(BT.601) → HD(BT.709) 변환 후 출력에 붙이는 태그."""
    if is_sd(info):
        return ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    return []


def scale_filter(w, h, info, flags="lanczos+accurate_rnd+full_chroma_int", rgb_in=False):
    s = f"scale={w}:{h}:flags={flags}"
    if is_sd(info):
        s += ":out_color_matrix=bt709" if rgb_in else ":in_color_matrix=bt601:out_color_matrix=bt709"
    return s


def audio_args(info, opts):
    if not info.acodecs:
        return []
    ok = {"mp4": {"aac", "ac3", "eac3", "mp3"},
          "mkv": {"aac", "ac3", "eac3", "mp3", "mp2", "dts", "flac", "opus", "vorbis"}}[opts.container]
    if opts.audio == "auto" and all(c in ok for c in info.acodecs):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "256k"]


TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
MKV_COPY_SUBS = {"subrip", "ass", "ssa", "webvtt", "dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle"}
SUB_EXTS = {".srt", ".smi", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}


def subtitle_plan(info, opts):
    """옮길 자막 트랙 [(입력에서 몇 번째 자막, 저장 코덱)]과 옮기지 못하는 코덱 목록.
    MKV는 대부분 그대로 담고, MP4는 글자 자막만 담을 수 있다."""
    plan, skipped = [], []
    for i, c in enumerate(info.scodecs):
        if opts.container == "mkv":
            enc = "copy" if c in MKV_COPY_SUBS else "srt" if c in TEXT_SUBS else None
        else:
            enc = "copy" if c == "mov_text" else "mov_text" if c in TEXT_SUBS else None
        if enc:
            plan.append((i, enc))
        else:
            skipped.append(c)
    return plan, skipped


def subtitle_args(plan, inp=0):
    if not plan:
        return ["-sn"]
    a = []
    for i, _ in plan:
        a += ["-map", f"{inp}:s:{i}"]
    for j, (_, enc) in enumerate(plan):
        a += [f"-c:s:{j}", enc]
    return a


def copy_sidecar_subs(source, out, log):
    """영상과 이름이 같은 자막 파일(예: 영화.srt, 영화.ko.smi)을 결과 영상 옆에 같은 규칙으로 복사."""
    if source.src.startswith("concat:"):
        return
    src = Path(source.src)
    prefix = src.stem.lower() + "."
    n = 0
    try:
        files = sorted(src.parent.iterdir())
    except OSError:
        return
    for f in files:
        if not f.is_file() or f.suffix.lower() not in SUB_EXTS or not f.name.lower().startswith(prefix):
            continue
        dst = out.with_name(out.stem + f.name[len(src.stem):])
        if dst.exists():
            continue
        try:
            shutil.copy2(f, dst)
            n += 1
        except OSError:
            pass
    if n:
        log(f"  같은 이름의 자막 파일 {n}개를 결과 영상 옆에 복사했습니다.")


def container_args(opts, venc):
    a = []
    if opts.container == "mp4":
        a += ["-movflags", "+faststart"]
        if venc in ("hevc_amf", "libx265"):
            a += ["-tag:v", "hvc1"]
    return a


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------
def run_ffmpeg(tools, args, cancel, total=0.0, progress=None, label=""):
    errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    cmd = [tools.ffmpeg, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1", *args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    cancel.add(p)
    try:
        t, speed = 0.0, ""
        for line in p.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.lstrip("-").isdigit():
                t = max(int(v) / 1e6, 0.0)
            elif k == "speed":
                speed = v.strip()
            elif k == "progress" and progress:
                frac = min(t / total, 1.0) if total > 0 else None
                tot = f" / {fmt_time(total)}" if total > 0 else ""
                progress(frac, f"{label} {fmt_time(t)}{tot}  (속도 {speed})")
        rc = p.wait()
    finally:
        cancel.remove(p)
        if p.poll() is None:
            p.kill()
    cancel.check()
    if rc != 0:
        errf.seek(0)
        tail = "\n".join(errf.read().strip().splitlines()[-12:])
        raise UpscaleError(f"ffmpeg 오류 (코드 {rc})\n{tail}")


def convert_classic(tools, source, out, info, opts, cancel, progress, log, subs=()):
    w, h = out_size(info, opts)
    venc = tools.pick_encoder(opts.container)
    vf = (deint_filters(tools, opts) + denoise_filters(tools, opts)
          + [scale_filter(w, h, info)] + sharpen_filters(tools, opts.sharpen)
          + ["setsar=1", "format=yuv420p"])
    log(f"  {info.width}x{info.height} → {w}x{h}, 인코더 {venc}")
    def build(plan):
        return [*PROBE, "-i", source.src, "-map", "0:v:0", "-map", "0:a?", *subtitle_args(plan),
                "-dn", "-vf", ",".join(vf), *venc_args(venc, opts.quality, w, h, info.fps),
                *color_tags(info), *audio_args(info, opts), *container_args(opts, venc), str(out)]

    t0 = time.time()
    try:
        run_ffmpeg(tools, build(subs), cancel, info.duration, progress, "변환 중")
    except UpscaleError:
        # 자막 때문에 시작하자마자 실패한 경우에만 자막 없이 다시 시도
        if not subs or time.time() - t0 > 30:
            raise
        log("  참고: 자막 트랙을 함께 저장하지 못해 자막 없이 다시 변환합니다.")
        run_ffmpeg(tools, build(()), cancel, info.duration, progress, "변환 중")


def _read_exact(stream, n):
    buf = stream.read(n)
    if buf is None or len(buf) < n:
        raise EOFError
    return buf


def read_png(stream):
    """image2pipe로 이어져 나오는 PNG 스트림에서 한 장을 꺼낸다. 끝이면 None."""
    sig = stream.read(8)
    if not sig or len(sig) < 8:
        return None
    parts = [sig]
    try:
        while True:
            hdr = _read_exact(stream, 8)
            body = _read_exact(stream, int.from_bytes(hdr[:4], "big") + 4)
            parts += [hdr, body]
            if hdr[4:8] == b"IEND":
                return b"".join(parts)
    except EOFError:
        return None


def ai_scale_factor(model, need):
    if model != "realesr-animevideov3":
        return 4
    return 2 if need <= 2.5 else 3 if need <= 3.5 else 4


def run_realesrgan(tools, in_path, out_path, model, scale, cancel, on_poll=None, gpu=-1):
    exe = Path(tools.realesrgan)
    errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    cmd = [str(exe), "-i", str(Path(in_path).resolve()), "-o", str(Path(out_path).resolve()),
           "-n", model,
           "-s", str(scale), "-f", "png", "-m", str(exe.parent / "models")]
    if gpu is not None and gpu >= 0:
        cmd += ["-g", str(gpu)]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf, stdin=subprocess.DEVNULL,
                         cwd=str(exe.parent), creationflags=NOWIN)
    cancel.add(p)
    try:
        while p.poll() is None:
            if on_poll:
                on_poll()
            time.sleep(0.4)
    finally:
        cancel.remove(p)
        if p.poll() is None:
            p.kill()
    cancel.check()
    if p.returncode != 0:
        errf.seek(0)
        tail = "\n".join(errf.read().strip().splitlines()[-8:])
        if "invalid gpu device" in tail:
            raise UpscaleError(f"선택한 그래픽 카드({gpu}번)를 사용할 수 없습니다. "
                               "그래픽 카드를 '자동'으로 바꿔 주세요.")
        raise UpscaleError(f"Real-ESRGAN 오류 (코드 {p.returncode})\n{tail}")


def _clear_dir(d):
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)


def convert_ai(tools, source, out, info, opts, cancel, progress, log, subs=()):
    """디코드 → (조각 단위로) PNG → Real-ESRGAN → 조각 인코딩 → 이어 붙이기.
    조각은 작업 폴더에 남으므로 중단 후 같은 설정으로 다시 시작하면 이어서 한다."""
    if not tools.realesrgan:
        raise UpscaleError("Real-ESRGAN이 설치되어 있지 않습니다.")
    w, h = out_size(info, opts)
    venc = tools.pick_encoder(opts.container)
    fps = fps_fraction(info.fps)
    chunk = chunk_frames(info)
    scale = ai_scale_factor(opts.ai_model, h / info.height)
    work = out.parent / (out.stem + ".work")
    sig = {"src": source.src, "opts": {k: v for k, v in asdict(opts).items() if k != "gpu"},
           "chunk": chunk,
           "size": [w, h], "venc": venc}
    sig_file = work / "job.json"
    old = None
    if sig_file.is_file():
        try:
            old = json.loads(sig_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
    if old != sig:
        _clear_dir(work)
        sig_file.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
    in_dir, up_dir = work / "in", work / "up"

    def chunk_path(i):
        return work / f"chunk_{i:05d}.{opts.container}"

    done = 0
    while chunk_path(done + 1).is_file():
        done += 1
    if done:
        log(f"  이전 작업 이어서 진행: 조각 {done}개 완료됨")
    total_frames = int(info.duration * info.fps) if info.duration > 0 else 0
    log(f"  {info.width}x{info.height} → AI x{scale} ({opts.ai_model}) → {w}x{h}, 인코더 {venc}")
    log(f"  AI 그래픽 카드: {tools.gpu_name(opts.gpu)}")

    pre = deint_filters(tools, opts) + denoise_filters(tools, opts)
    pre.append(f"fps={fps}:start_time=0")           # 고정 프레임 레이트(오디오 싱크 유지)
    pre.append("scale=iw:ih:flags=accurate_rnd+full_chroma_int"
               + (":in_color_matrix=bt601" if is_sd(info) else ""))
    pre.append("format=rgb24")
    post = ([scale_filter(w, h, info, rgb_in=True)]
            + sharpen_filters(tools, SOFTER[opts.sharpen])   # AI 결과는 이미 선명해서 한 단계 약하게
            + ["setsar=1", "format=yuv420p"])

    errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    dec = subprocess.Popen(
        [tools.ffmpeg, "-hide_banner", "-nostats", *PROBE, "-i", source.src, "-map", "0:v:0",
         "-an", "-sn", "-dn", "-vf", ",".join(pre), "-f", "image2pipe", "-c:v", "png", "-"],
        stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    cancel.add(dec)
    t0 = time.time()
    try:
        skip = done * chunk
        for n in range(skip):
            if read_png(dec.stdout) is None:
                break
            if n % 200 == 0:
                cancel.check()
                progress(None, f"완료된 구간 건너뛰는 중… {n}/{skip} 프레임")
        idx = done
        while True:
            cancel.check()
            _clear_dir(in_dir)
            _clear_dir(up_dir)
            count = 0
            while count < chunk:
                png = read_png(dec.stdout)
                if png is None:
                    break
                count += 1
                (in_dir / f"{count:08d}.png").write_bytes(png)
            if count == 0:
                break
            idx += 1
            base = (idx - 1) * chunk

            def poll():
                n_up = len(os.listdir(up_dir))
                cur = base + n_up
                frac = min(cur / total_frames, 1.0) if total_frames else None
                rate = (cur - skip) / max(time.time() - t0, 1e-6)
                tot = f"/{total_frames}" if total_frames else ""
                progress(frac, f"AI 업스케일 중  프레임 {cur}{tot}  ({rate:.1f} fps)")

            run_realesrgan(tools, in_dir, up_dir, opts.ai_model, scale, cancel, poll, opts.gpu)
            if len(os.listdir(up_dir)) != count:
                raise UpscaleError("Real-ESRGAN이 일부 프레임을 만들지 못했습니다. "
                                   "그래픽 드라이버(Vulkan)와 저장 공간을 확인해 주세요.")
            part = work / f"chunk_part.{opts.container}"
            run_ffmpeg(tools, ["-framerate", fps, "-start_number", "1",
                               "-i", str(up_dir / "%08d.png"), "-vf", ",".join(post),
                               *venc_args(venc, opts.quality, w, h, info.fps),
                               *color_tags(info), str(part)], cancel)
            os.replace(part, chunk_path(idx))
            if count < chunk:
                break
    finally:
        cancel.remove(dec)
        if dec.poll() is None:
            dec.kill()
        dec.wait()
    cancel.check()
    if idx == 0:
        errf.seek(0)
        tail = "\n".join(errf.read().strip().splitlines()[-8:])
        raise UpscaleError("영상을 디코드하지 못했습니다.\n" + tail)

    progress(None, "조각을 이어 붙이고 오디오를 합치는 중…")
    lst = work / "list.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for i in range(1, idx + 1):
            f.write("file '" + str(chunk_path(i)).replace("\\", "/").replace("'", "'\\''") + "'\n")
    def mux(plan):
        run_ffmpeg(tools, ["-f", "concat", "-safe", "0", "-i", str(lst), *PROBE, "-i", source.src,
                           "-map", "0:v:0", "-map", "1:a?", *subtitle_args(plan, 1), "-dn",
                           "-c:v", "copy", *audio_args(info, opts), *container_args(opts, venc),
                           str(out)], cancel)

    try:
        mux(subs)
    except UpscaleError:
        if not subs:
            raise
        log("  참고: 자막 트랙을 함께 저장하지 못해 자막 없이 저장합니다.")
        mux(())
    shutil.rmtree(work, ignore_errors=True)


def convert(tools, source, out, opts, cancel, progress, log):
    info = probe(tools, source.src)
    if int(opts.height) and info.height >= int(opts.height):
        log(f"  참고: 원본({info.width}x{info.height})이 이미 목표 해상도 이상이라 "
            "크기는 그대로 두고 화질 보정만 합니다.")
    if info.hdr:
        log("  참고: HDR 영상은 일반(SDR) 8비트로 저장되어 색이 달라질 수 있습니다.")
    subs, skipped = subtitle_plan(info, opts) if opts.subs else ([], [])
    if subs:
        log(f"  자막 트랙 {len(subs)}개를 함께 저장합니다.")
    if skipped:
        if opts.container == "mp4":
            log(f"  참고: 그림 형식 자막(DVD·블루레이 등) {len(skipped)}개는 MP4에 넣을 수 없어 뺐습니다. "
                "자막을 유지하려면 파일 형식을 MKV로 바꾸세요.")
        else:
            log(f"  참고: 지원하지 않는 형식의 자막 {len(skipped)}개는 뺐습니다. ({', '.join(skipped)})")
    out = Path(out).resolve()      # 외부 도구는 다른 작업 폴더에서 실행되므로 절대 경로로
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if opts.ai:
            convert_ai(tools, source, out, info, opts, cancel, progress, log, subs)
        else:
            convert_classic(tools, source, out, info, opts, cancel, progress, log, subs)
        if opts.subs:
            copy_sidecar_subs(source, out, log)
    except Cancelled:
        if not opts.ai and out.exists():
            try:
                out.unlink()
            except OSError:
                pass
        raise


def make_preview(tools, source, opts, percent, cancel, log):
    """한 장면을 뽑아 [왼쪽: 단순 확대 | 오른쪽: 업스케일 결과] 비교 이미지를 만든다."""
    info = probe(tools, source.src)
    w, h = out_size(info, opts)
    t = info.duration * percent / 100 if info.duration > 0 else 0
    tmp = Path(tempfile.mkdtemp(prefix="dvdup_preview_"))
    result = tmp / "preview_compare.png"
    seek = ["-ss", f"{t:.2f}", *PROBE, "-i", source.src]
    de = deint_filters(tools, opts)
    left = de + [scale_filter(w, h, info, flags="bicubic"), "format=yuv444p"]
    to_rgb = "scale=iw:ih" + (":in_color_matrix=bt709" if is_sd(info) else "") + ",format=rgb24"
    stack = "hstack" if "hstack" in tools.filters else None

    if opts.ai and tools.realesrgan:
        src_png, ai_png = tmp / "src.png", tmp / "ai.png"
        pre = de + denoise_filters(tools, opts) + [
            "scale=iw:ih:flags=accurate_rnd+full_chroma_int"
            + (":in_color_matrix=bt601" if is_sd(info) else ""), "format=rgb24"]
        run_ffmpeg(tools, [*seek, "-map", "0:v:0", "-frames:v", "1", "-vf", ",".join(pre),
                           str(src_png)], cancel)
        scale = ai_scale_factor(opts.ai_model, h / info.height)
        run_realesrgan(tools, src_png, ai_png, opts.ai_model, scale, cancel, gpu=opts.gpu)
        if not ai_png.is_file():
            raise UpscaleError("Real-ESRGAN이 미리보기 프레임을 만들지 못했습니다.")
        right = ([scale_filter(w, h, info, rgb_in=True), "format=yuv444p"]
                 + sharpen_filters(tools, SOFTER[opts.sharpen]))
        inputs = [*seek, "-i", str(ai_png)]
        r_in = "[1:v]"
    else:
        right = (de + denoise_filters(tools, opts) + [scale_filter(w, h, info), "format=yuv444p"]
                 + sharpen_filters(tools, opts.sharpen))
        inputs = [*seek]
        r_in = "[0:v]"

    if stack:
        graph = (f"[0:v]{','.join(left)}[l];{r_in}{','.join(right)}[r];"
                 f"[l][r]hstack,{to_rgb}[o]")
        run_ffmpeg(tools, [*inputs, "-filter_complex", graph, "-map", "[o]",
                           "-frames:v", "1", str(result)], cancel)
        return result
    # hstack이 없는 ffmpeg: 오른쪽(결과)만 저장
    graph = f"{r_in}{','.join(right)},{to_rgb}[o]"
    run_ffmpeg(tools, [*inputs, "-filter_complex", graph, "-map", "[o]",
                       "-frames:v", "1", str(result)], cancel)
    return result


def same_folder_dir(source):
    """원본 영상이 있는 폴더. DVD이거나 그 폴더에 쓸 수 없으면 None."""
    if source.src.startswith("concat:"):
        return None
    parent = Path(source.src).resolve().parent
    try:
        with tempfile.TemporaryFile(dir=parent):
            pass
    except OSError:
        return None
    return str(parent)


def unique_path(p):
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def default_out(source, opts, out_dir):
    return unique_path(Path(out_dir) / f"{source.stem}_업스케일_{out_tag(opts)}.{opts.container}")


# --------------------------------------------------------------------------
# 명령줄
# --------------------------------------------------------------------------
def cli(argv):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="영상 업스케일러")
    ap.add_argument("input", nargs="*",
                    help="영상 파일(MP4, MKV, AVI, VOB 등) 또는 DVD 폴더(VIDEO_TS). 여러 개면 차례로 변환")
    ap.add_argument("-o", "--output", help="저장할 파일 이름 (영상이 하나일 때)")
    ap.add_argument("--out-dir", help="저장할 폴더 (기본: 현재 폴더)")
    ap.add_argument("--same-dir", action="store_true", help="원본 영상과 같은 폴더에 저장")
    ap.add_argument("--height", type=int, default=1080,
                    help="출력 세로 해상도 (예: 720, 1080, 1440, 2160). 0이면 원본의 2배")
    ap.add_argument("--aspect", default="auto", choices=["auto", "4:3", "16:9"])
    ap.add_argument("--deint", default="auto", choices=["auto", "on", "off"])
    ap.add_argument("--denoise", default="weak", choices=["off", "weak", "medium", "strong"])
    ap.add_argument("--sharpen", default="medium", choices=["off", "weak", "medium", "strong"])
    ap.add_argument("--quality", default="high", choices=["high", "medium", "small"])
    ap.add_argument("--container", default="mp4", choices=["mp4", "mkv"])
    ap.add_argument("--audio", default="auto", choices=["auto", "aac"])
    ap.add_argument("--ai", action="store_true", help="Real-ESRGAN AI 업스케일링")
    ap.add_argument("--ai-model", default="realesr-animevideov3")
    ap.add_argument("--no-subs", action="store_true", help="자막을 옮기지 않음")
    ap.add_argument("--gpu", type=int, default=-1, help="AI에 쓸 그래픽 카드 번호 (기본: 자동)")
    ap.add_argument("--list-gpus", action="store_true", help="AI에 쓸 수 있는 그래픽 카드 목록 출력")
    ap.add_argument("--info", action="store_true", help="입력 정보만 출력")
    ap.add_argument("--preview", type=float, metavar="PERCENT", help="비교 이미지만 생성")
    a = ap.parse_args(argv)

    tools = Tools()
    print(tools.describe())
    if a.list_gpus:
        gpus = tools.list_gpus()
        for i, name in gpus:
            print(f"  {i}: {name}")
        if not gpus:
            print("  그래픽 카드를 찾지 못했습니다 (Real-ESRGAN 미설치 또는 Vulkan 미지원).")
        return 0
    if not a.input:
        ap.error("변환할 영상 파일이나 폴더를 지정해 주세요.")
    if not tools.ffmpeg:
        return 2
    opts = Options(a.height, a.aspect, a.deint, a.denoise, a.sharpen, a.quality,
                   a.container, a.audio, a.ai, a.ai_model, not a.no_subs, a.gpu)
    cancel = Canceller()
    if a.output and len(a.input) > 1:
        ap.error("-o는 영상이 하나일 때만 쓸 수 있습니다. 여러 개일 때는 --out-dir을 쓰세요.")
    try:
        sources = [make_source(x) for x in a.input]
        if a.info:
            print(probe(tools, sources[0].src))
            return 0
        if a.preview is not None:
            print("미리보기:", make_preview(tools, sources[0], opts, a.preview, cancel, print))
            return 0
        last = [0.0]

        def progress(frac, text):
            if time.time() - last[0] > 1:
                last[0] = time.time()
                pct = f"{frac * 100:5.1f}% " if frac is not None else ""
                print("  " + pct + text, flush=True)

        keep_awake(True)
        failed = 0
        for n, source in enumerate(sources, 1):
            out_dir = (a.same_dir and same_folder_dir(source)) or a.out_dir or Path.cwd()
            out = Path(a.output) if a.output else default_out(source, opts, out_dir)
            if len(sources) > 1:
                print(f"[{n}/{len(sources)}] {source.label}")
            t0 = time.time()
            try:
                convert(tools, source, out, opts, cancel, progress, print)
                print(f"완료: {out}  ({fmt_time(time.time() - t0)})")
            except UpscaleError as e:
                failed += 1
                print("오류:", e, file=sys.stderr)
        return 1 if failed else 0
    except UpscaleError as e:
        print("오류:", e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        cancel.set()
        print("중단됨", file=sys.stderr)
        return 130


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
RES = {"720p (HD)": 720, "1080p (Full HD)": 1080, "1440p (QHD)": 1440, "2160p (4K)": 2160,
       "원본의 2배": 0}
ASPECT = {"자동": "auto", "4:3": "4:3", "16:9 (와이드)": "16:9"}
DEINT = {"자동": "auto", "항상 적용": "on", "끄기": "off"}
LEVEL = {"끄기": "off", "약하게": "weak", "보통": "medium", "강하게": "strong"}
QUALITY = {"높음": "high", "보통": "medium", "용량 절약": "small"}
CONTAINER = {"MP4": "mp4", "MKV": "mkv"}
AUDIO = {"원본 유지 (가능할 때)": "auto", "AAC로 변환": "aac"}
AIMODEL = {"빠름 (애니·일반 겸용)": "realesr-animevideov3",
           "실사 고품질 (매우 느림)": "realesrgan-x4plus",
           "애니 고품질 (매우 느림)": "realesrgan-x4plus-anime"}


QUEUE_FILE = Path(os.environ.get("UPSCALER_STATE") or APP_DIR / "queue.json")
ST_WAIT, ST_RUN, ST_DONE, ST_FAIL = "대기", "변환 중", "완료", "실패"


def keep_awake(on):
    """변환하는 동안 PC가 저절로 절전에 들어가지 않게 한다(화면은 꺼져도 된다).
    호출한 스레드에만 적용되므로 변환을 돌리는 스레드에서 부른다."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        flags = es_continuous | (es_system_required if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:                                   # noqa: BLE001
        return False


@dataclass
class QItem:
    iid: str
    path: str               # 사용자가 추가한 경로 (대기열 저장용)
    source: Source
    status: str = ST_WAIT
    note: str = ""


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    choices = {"res": RES, "aspect": ASPECT, "deint": DEINT, "denoise": LEVEL, "sharpen": LEVEL,
               "quality": QUALITY, "container": CONTAINER, "audio": AUDIO, "model": AIMODEL}

    class App:
        def __init__(self, root):
            self.root = root
            root.title("영상 업스케일러")
            root.minsize(760, 660)
            self.tools = None
            self.items = []             # 대기열 (위에서부터 차례로 변환)
            self._seq = 0
            self.q = queue.Queue()      # 작업 스레드 → 화면
            self.reply = queue.Queue()  # 화면 → 작업 스레드 (다음 작업)
            self.cancel = None
            self.busy = False
            self.gpu_map = {"자동": -1}
            self._restoring = True
            self._saved = self._read_state()
            self._build()
            self._restore_state()
            self._restoring = False
            threading.Thread(target=self._init_tools, daemon=True).start()
            root.after(100, self._poll)
            root.protocol("WM_DELETE_WINDOW", self._close)

        # ---- 화면 구성 ----
        def _combo(self, parent, row, col, label, values, default):
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="e", padx=(10, 4), pady=4)
            var = tk.StringVar(value=default)
            cb = ttk.Combobox(parent, textvariable=var, values=list(values), state="readonly", width=26)
            cb.grid(row=row, column=col + 1, sticky="w", pady=4)
            return var, cb

        def _build(self):
            root = self.root
            pad = {"padx": 10, "pady": 5}
            root.columnconfigure(0, weight=1)

            f_in = ttk.LabelFrame(root, text="1. 대기열 — 위에서부터 하나씩 차례로 변환합니다")
            f_in.grid(row=0, column=0, sticky="nsew", **pad)
            f_in.columnconfigure(0, weight=1)
            f_in.rowconfigure(1, weight=1)
            status_w = 170
            try:
                from tkinter import font as tkfont
                fnt = tkfont.nametofont("TkDefaultFont")
                ttk.Style().configure("Treeview", rowheight=int(fnt.metrics("linespace") * 1.35))
                status_w = fnt.measure("변환 중 100% 00:00:00") + 24
            except Exception:                           # noqa: BLE001
                pass
            self.tree = ttk.Treeview(f_in, columns=("name", "status"), show="headings", height=5,
                                     selectmode="extended")
            self.tree.heading("name", text="영상")
            self.tree.heading("status", text="상태")
            self.tree.column("name", stretch=True, width=480, anchor="w")
            self.tree.column("status", stretch=False, width=status_w, anchor="center")
            self.tree.tag_configure(ST_DONE, foreground="#1a7f37")
            self.tree.tag_configure(ST_FAIL, foreground="#c62828")
            self.tree.tag_configure(ST_RUN, foreground="#0b57d0")
            self.tree.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(8, 0), pady=(8, 4))
            sbq = ttk.Scrollbar(f_in, command=self.tree.yview)
            sbq.grid(row=0, column=1, rowspan=2, sticky="ns", pady=(8, 4))
            self.tree.configure(yscrollcommand=sbq.set)
            ttk.Button(f_in, text="파일 추가…", command=self._add_files).grid(
                row=0, column=2, sticky="new", padx=8, pady=(8, 2))
            ttk.Button(f_in, text="폴더 / DVD 추가…", command=self._add_folder).grid(
                row=1, column=2, sticky="new", padx=8, pady=2)
            f_q = ttk.Frame(f_in)
            f_q.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
            for text, cmd in (("▲ 위로", lambda: self._move(-1)), ("▼ 아래로", lambda: self._move(1)),
                              ("선택 삭제", self._remove), ("다시 시도", self._retry),
                              ("완료 항목 정리", self._clear_done)):
                ttk.Button(f_q, text=text, command=cmd).pack(side="left", padx=(0, 6))

            f_opt = ttk.LabelFrame(root, text="2. 설정")
            f_opt.grid(row=1, column=0, sticky="ew", **pad)
            self.v_res, _ = self._combo(f_opt, 0, 0, "출력 해상도", RES, "1080p (Full HD)")
            self.v_aspect, _ = self._combo(f_opt, 0, 2, "화면비", ASPECT, "자동")
            self.v_deint, _ = self._combo(f_opt, 1, 0, "인터레이스(줄무늬) 제거", DEINT, "자동")
            self.v_denoise, _ = self._combo(f_opt, 1, 2, "노이즈 제거", LEVEL, "약하게")
            self.v_sharpen, _ = self._combo(f_opt, 2, 0, "선명하게", LEVEL, "보통")
            self.v_quality, _ = self._combo(f_opt, 2, 2, "저장 화질", QUALITY, "높음")
            self.v_container, _ = self._combo(f_opt, 3, 0, "파일 형식", CONTAINER, "MP4")
            self.v_audio, _ = self._combo(f_opt, 3, 2, "오디오", AUDIO, "원본 유지 (가능할 때)")
            self.v_ai = tk.BooleanVar(value=False)
            self.chk_ai = ttk.Checkbutton(f_opt, text="AI 업스케일링 (Real-ESRGAN) — 훨씬 선명하지만 오래 걸림",
                                          variable=self.v_ai, state="disabled")
            self.chk_ai.grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=4)
            self.v_model, self.cb_model = self._combo(f_opt, 4, 2, "AI 모델", AIMODEL, next(iter(AIMODEL)))
            self.v_subs = tk.BooleanVar(value=True)
            ttk.Checkbutton(f_opt, text="자막도 함께 옮기기 (자막 트랙, 같은 이름의 자막 파일)",
                            variable=self.v_subs).grid(row=5, column=0, columnspan=2, sticky="w",
                                                       padx=10, pady=(0, 6))
            self.v_gpu, self.cb_gpu = self._combo(f_opt, 5, 2, "AI 그래픽 카드", self.gpu_map, "자동")
            self.cb_gpu.configure(state="disabled")

            f_out = ttk.LabelFrame(root, text="3. 저장 위치")
            f_out.grid(row=2, column=0, sticky="ew", **pad)
            f_out.columnconfigure(0, weight=1)
            self.v_outdir = tk.StringVar(value=str(Path.home() / "Videos"))
            ttk.Entry(f_out, textvariable=self.v_outdir).grid(row=0, column=0, sticky="ew", padx=8, pady=8)
            ttk.Button(f_out, text="찾아보기…", command=self._pick_out).grid(row=0, column=1, padx=8)
            self.v_same = tk.BooleanVar(value=False)
            ttk.Checkbutton(f_out, text="원본 영상과 같은 폴더에 저장 (DVD처럼 저장할 수 없는 곳의 영상은 위 폴더에 저장)",
                            variable=self.v_same, command=self._save_state).grid(
                row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

            f_run = ttk.Frame(root)
            f_run.grid(row=3, column=0, sticky="ew", **pad)
            f_run.columnconfigure(5, weight=1)
            self.btn_start = ttk.Button(f_run, text="▶ 대기열 시작", command=self._start, state="disabled")
            self.btn_start.grid(row=0, column=0, padx=(0, 6))
            self.btn_stop = ttk.Button(f_run, text="■ 중지", command=self._stop, state="disabled")
            self.btn_stop.grid(row=0, column=1, padx=6)
            self.btn_prev = ttk.Button(f_run, text="미리보기 (비교 이미지)", command=self._preview, state="disabled")
            self.btn_prev.grid(row=0, column=2, padx=(18, 4))
            ttk.Label(f_run, text="위치 %").grid(row=0, column=3)
            self.v_pos = tk.IntVar(value=30)
            ttk.Spinbox(f_run, from_=0, to=99, textvariable=self.v_pos, width=4).grid(row=0, column=4, padx=4)

            self.bar = ttk.Progressbar(root, maximum=1000)
            self.bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(6, 2))
            self.v_status = tk.StringVar(value="도구를 확인하는 중…")
            ttk.Label(root, textvariable=self.v_status).grid(row=5, column=0, sticky="w", padx=10)

            f_log = ttk.Frame(root)
            f_log.grid(row=6, column=0, sticky="nsew", **pad)
            f_log.columnconfigure(0, weight=1)
            f_log.rowconfigure(0, weight=1)
            root.rowconfigure(6, weight=1)
            self.log_w = tk.Text(f_log, height=6, state="disabled", wrap="word", font="TkDefaultFont")
            self.log_w.grid(row=0, column=0, sticky="nsew")
            sb = ttk.Scrollbar(f_log, command=self.log_w.yview)
            sb.grid(row=0, column=1, sticky="ns")
            self.log_w.configure(yscrollcommand=sb.set)

        # ---- 설정·대기열 저장 (프로그램을 다시 켜도 남아 있도록) ----
        def _read_state(self):
            try:
                data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}

        def _save_state(self):
            if self._restoring:
                return
            data = {
                "settings": {k: getattr(self, "v_" + k).get() for k in choices},
                "ai": bool(self.v_ai.get()), "subs": bool(self.v_subs.get()),
                "gpu": self.v_gpu.get(), "out_dir": self.v_outdir.get(),
                "same_dir": bool(self.v_same.get()),
                # 끝난 항목은 저장하지 않고, 하던 항목은 다시 대기로
                "items": [{"path": it.path, "status": ST_FAIL if it.status == ST_FAIL else ST_WAIT}
                          for it in self.items if it.status != ST_DONE],
            }
            try:
                tmp = QUEUE_FILE.with_name(QUEUE_FILE.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, QUEUE_FILE)
            except OSError:
                pass

        def _restore_state(self):
            st = self._saved
            for k, v in (st.get("settings") or {}).items():
                if k in choices and v in choices[k]:
                    getattr(self, "v_" + k).set(v)
            if "subs" in st:
                self.v_subs.set(bool(st["subs"]))
            if st.get("out_dir"):
                self.v_outdir.set(st["out_dir"])
            self.v_same.set(bool(st.get("same_dir")))
            for d in st.get("items") or []:
                if isinstance(d, dict) and d.get("path"):
                    self._add(d["path"], ST_FAIL if d.get("status") == ST_FAIL else ST_WAIT, quiet=True)
            if self.items:
                self._log(f"지난번에 남은 대기열 {len(self.items)}개를 불러왔습니다.")

        # ---- 도구 ----
        def _init_tools(self):
            try:
                tools = Tools()
                if tools.ffmpeg:
                    tools.pick_encoder()
                tools.list_gpus()
                self.q.put(("tools", tools, None))
            except Exception as e:                      # noqa: BLE001
                self.q.put(("tools", None, str(e)))

        def _tools_ready(self, tools, err):
            self.tools = tools
            if err or not tools or not tools.ffmpeg:
                self.v_status.set("ffmpeg가 필요합니다.")
                self._log(err or "ffmpeg를 찾지 못했습니다.\n"
                          "PowerShell에서  winget install Gyan.FFmpeg  로 설치한 뒤 다시 실행해 주세요.")
                return
            self._log(tools.describe())
            if "hqdn3d" not in tools.filters and "settings" not in self._saved:
                # 빠른 노이즈 제거 필터가 없는 ffmpeg: 느린 대체 필터뿐이라 기본값을 끔으로
                self.v_denoise.set("끄기")
                self._log("이 ffmpeg에는 빠른 노이즈 제거 필터가 없어 '노이즈 제거'를 끔으로 두었습니다. "
                          "켜면 변환이 3~4배 느려집니다.")
            if tools.realesrgan:
                self.chk_ai.configure(state="normal")
                for i, name in tools.list_gpus():
                    short = name.replace("(TM)", "").replace("(R)", "").replace("  ", " ")
                    self.gpu_map[f"{i}: {short}"] = i
                self.cb_gpu.configure(values=list(self.gpu_map), state="readonly")
                if len(self.gpu_map) > 2:
                    self._log("그래픽 카드가 여러 개입니다. 'AI 그래픽 카드'에서 성능이 좋은 쪽을 고를 수 있습니다.")
                if self._saved.get("ai"):
                    self.v_ai.set(True)
                if self._saved.get("gpu") in self.gpu_map:
                    self.v_gpu.set(self._saved["gpu"])
            else:
                self._log("AI 업스케일링을 쓰려면 realesrgan-ncnn-vulkan을 이 프로그램 옆 tools 폴더에 넣어 주세요.")
            self.v_status.set("준비됨")
            self._set_busy(False)

        # ---- 대기열 ----
        @staticmethod
        def _name(s):
            if s.src.startswith("concat:"):
                return s.label
            f = Path(s.src)
            return f"{f.name}    ({f.parent})"

        def _row(self, it):
            text = it.status + (f" {it.note}" if it.note else "")
            self.tree.item(it.iid, values=(self._name(it.source), text), tags=(it.status,))

        def _find(self, iid):
            return next((it for it in self.items if it.iid == iid), None)

        def _selected(self):
            sel = set(self.tree.selection())
            return [it for it in self.items if it.iid in sel]

        def _add(self, path, status=ST_WAIT, quiet=False):
            try:
                s = make_source(path)
            except UpscaleError as e:
                if quiet:
                    self._log(f"대기열에서 뺐습니다(파일을 찾을 수 없음): {path}")
                else:
                    messagebox.showerror("영상 업스케일러", str(e))
                return None
            if any(it.source.src == s.src and it.status in (ST_WAIT, ST_RUN) for it in self.items):
                self._log(f"이미 대기열에 있습니다: {s.label}")
                return None
            self._seq += 1
            it = QItem(f"q{self._seq}", str(path), s, status)
            self.items.append(it)
            self.tree.insert("", "end", iid=it.iid, values=(self._name(s), status), tags=(status,))
            if s.is_dvd and not quiet:
                self._log("참고: 복사 방지(CSS)가 걸린 상업용 DVD의 VOB는 화면이 깨져 나옵니다. "
                          "먼저 '미리보기'로 정상인지 확인해 보세요.")
            self._save_state()
            return it

        def _add_files(self):
            for p in filedialog.askopenfilenames(title="영상 파일 선택 (여러 개 선택 가능)", filetypes=[
                    ("영상 파일", VIDEO_EXTS), ("모든 파일", "*.*")]):
                self._add(p)

        def _add_folder(self):
            p = filedialog.askdirectory(title="영상이 든 폴더 또는 DVD 드라이브(VIDEO_TS) 선택")
            if not p:
                return
            try:
                found = sources_from_folder(p)
            except UpscaleError as e:
                messagebox.showerror("영상 업스케일러", str(e))
                return
            for s in found:
                self._add(s.src if not s.src.startswith("concat:") else p)

        def _move(self, d):
            sel = set(self.tree.selection())
            items = self.items
            order = range(len(items)) if d < 0 else range(len(items) - 1, -1, -1)
            for i in order:
                j = i + d
                if items[i].iid in sel and 0 <= j < len(items) and items[j].iid not in sel:
                    items[i], items[j] = items[j], items[i]
            for n, it in enumerate(items):
                self.tree.move(it.iid, "", n)
            self._save_state()

        def _remove(self):
            for it in self._selected():
                if it.status == ST_RUN:
                    self._log("변환 중인 영상은 삭제할 수 없습니다. 먼저 '중지'를 눌러 주세요.")
                    continue
                self.items.remove(it)
                self.tree.delete(it.iid)
            self._save_state()

        def _retry(self):
            targets = self._selected() or [it for it in self.items if it.status == ST_FAIL]
            for it in targets:
                if it.status in (ST_FAIL, ST_DONE):
                    it.status, it.note = ST_WAIT, ""
                    self._row(it)
            self._save_state()

        def _clear_done(self):
            for it in [x for x in self.items if x.status == ST_DONE]:
                self.items.remove(it)
                self.tree.delete(it.iid)
            self._save_state()

        def _pick_out(self):
            p = filedialog.askdirectory(title="저장할 폴더")
            if p:
                self.v_outdir.set(p)

        def _options(self):
            return Options(RES[self.v_res.get()], ASPECT[self.v_aspect.get()], DEINT[self.v_deint.get()],
                           LEVEL[self.v_denoise.get()], LEVEL[self.v_sharpen.get()],
                           QUALITY[self.v_quality.get()], CONTAINER[self.v_container.get()],
                           AUDIO[self.v_audio.get()], bool(self.v_ai.get()), AIMODEL[self.v_model.get()],
                           bool(self.v_subs.get()), self.gpu_map.get(self.v_gpu.get(), -1))

        # ---- 실행 ----
        def _set_busy(self, busy):
            self.busy = busy
            ready = "disabled" if busy or not (self.tools and self.tools.ffmpeg) else "normal"
            self.btn_start.configure(state=ready)
            self.btn_prev.configure(state=ready)
            self.btn_stop.configure(state="normal" if busy else "disabled")

        def _log(self, text):
            self.log_w.configure(state="normal")
            self.log_w.insert("end", text + "\n")
            self.log_w.see("end")
            self.log_w.configure(state="disabled")

        def _start(self):
            if not any(it.status == ST_WAIT for it in self.items):
                messagebox.showinfo("영상 업스케일러", "대기 중인 영상이 없습니다. 먼저 영상을 추가해 주세요.")
                return
            if not self.v_same.get() and not self.v_outdir.get().strip():
                messagebox.showinfo("영상 업스케일러", "저장 위치를 지정해 주세요.")
                return
            self.cancel = Canceller()
            self._set_busy(True)
            self._save_state()
            self._log("대기열을 시작합니다. 변환하는 동안에는 PC가 저절로 절전에 들어가지 않습니다.")
            threading.Thread(target=self._work, args=(self.cancel,), daemon=True).start()

        def _next_job(self):
            """(화면 스레드) 다음 대기 항목을 꺼내 작업 스레드에 넘긴다. 설정은 이 시점의 값을 쓴다."""
            it = next((x for x in self.items if x.status == ST_WAIT), None)
            if it is None:
                self.reply.put(None)
                return
            it.status, it.note = ST_RUN, ""
            self._row(it)
            self.tree.see(it.iid)
            self.bar["value"] = 0
            self._save_state()
            out_dir = self.v_outdir.get().strip() or str(Path.home() / "Videos")
            if self.v_same.get():
                same = same_folder_dir(it.source)
                if same:
                    out_dir = same
                else:
                    self._log(f"참고: '{it.source.stem}'은(는) 원본 폴더에 저장할 수 없어 "
                              f"지정한 저장 위치에 저장합니다. ({out_dir})")
            self.reply.put((it.iid, it.source, self._options(), out_dir))

        def _work(self, cancel):
            """(작업 스레드) 대기열이 빌 때까지 하나씩 꺼내 변환한다. 실행 중에 추가된 것도 이어서 한다."""
            keep_awake(True)
            ok = fail = 0
            t_all = time.time()
            cur = None
            try:
                while True:
                    cancel.check()
                    self.q.put(("next",))
                    job = self.reply.get()
                    if job is None:
                        break
                    iid, s, opts, out_dir = job
                    cur = iid
                    out = default_out(s, opts, out_dir)
                    if opts.ai:        # 이어 하기 위해 AI 모드는 파일 이름을 고정
                        out = Path(out_dir) / f"{s.stem}_업스케일_{out_tag(opts)}.{opts.container}"
                    self.q.put(("log", f"[{time.strftime('%H:%M')}] {s.label}\n  → {out}"))
                    t0 = time.time()

                    def progress(frac, text, t0=t0, iid=iid):
                        eta = ""
                        if frac and frac > 0.01:
                            eta = "  남은 시간 약 " + fmt_time((time.time() - t0) * (1 - frac) / frac)
                        self.q.put(("progress", frac, text + eta, iid))

                    try:
                        convert(self.tools, s, out, opts, cancel, progress,
                                lambda m: self.q.put(("log", m)))
                        ok += 1
                        took = fmt_time(time.time() - t0)
                        self.q.put(("item", iid, ST_DONE, took))
                        self.q.put(("log", f"  [{time.strftime('%H:%M')}] 완료 ({took})"))
                    except Cancelled:
                        raise
                    except Exception as e:              # noqa: BLE001  하나가 실패해도 다음 영상으로
                        fail += 1
                        self.q.put(("item", iid, ST_FAIL, ""))
                        reason = " / ".join(str(e).strip().splitlines()[:2])
                        self.q.put(("log", f"  [{time.strftime('%H:%M')}] 실패: {reason}"))
                    cur = None
                self.q.put(("done", f"대기열을 모두 마쳤습니다: 완료 {ok}개, 실패 {fail}개 "
                                    f"(총 {fmt_time(time.time() - t_all)})"))
            except Cancelled:
                if cur:
                    self.q.put(("item", cur, ST_WAIT, ""))
                self.q.put(("done", f"중지했습니다 (완료 {ok}개). 다시 시작하면 남은 영상부터 합니다."))
            except Exception as e:                      # noqa: BLE001
                self.q.put(("done", f"오류: {e}"))
            finally:
                keep_awake(False)

        def _preview(self):
            if not self.items:
                messagebox.showinfo("영상 업스케일러", "먼저 영상을 추가해 주세요.")
                return
            s = (self._selected() or self.items)[0].source
            opts = self._options()
            try:
                pos = min(max(int(self.v_pos.get()), 0), 99)
            except (tk.TclError, ValueError):
                pos = 30
            self.cancel = Canceller()
            self._set_busy(True)
            self.v_status.set("미리보기 만드는 중…")

            def work(cancel=self.cancel):
                try:
                    p = make_preview(self.tools, s, opts, pos, cancel, lambda m: None)
                    self.q.put(("log", f"미리보기: 왼쪽 = 단순 확대, 오른쪽 = 업스케일 결과\n  {p}"))
                    os.startfile(p)                     # noqa: S606 (기본 이미지 뷰어로 열기)
                    self.q.put(("done", "미리보기를 열었습니다."))
                except Cancelled:
                    self.q.put(("done", "중지했습니다."))
                except Exception as e:                  # noqa: BLE001
                    self.q.put(("done", f"미리보기 실패: {e}"))

            threading.Thread(target=work, daemon=True).start()

        def _stop(self):
            if self.cancel:
                self.cancel.set()
                self.v_status.set("중지하는 중…")

        def _poll(self):
            try:
                while True:
                    m = self.q.get_nowait()
                    if m[0] == "log":
                        self._log(m[1])
                    elif m[0] == "next":
                        self._next_job()
                    elif m[0] == "progress":
                        it = self._find(m[3])
                        if m[1] is not None:
                            self.bar["value"] = int(m[1] * 1000)
                        if it and it.status == ST_RUN:
                            note = f"{int(m[1] * 100)}%" if m[1] is not None else ""
                            if note != it.note:
                                it.note = note
                                self._row(it)
                        waiting = sum(1 for x in self.items if x.status == ST_WAIT)
                        name = it.source.stem if it else ""
                        self.v_status.set(f"{name} — {m[2]}  (대기 {waiting}개)")
                    elif m[0] == "item":
                        it = self._find(m[1])
                        if it:
                            it.status, it.note = m[2], m[3]
                            self._row(it)
                            self._save_state()
                    elif m[0] == "tools":
                        self._tools_ready(m[1], m[2])
                    elif m[0] == "done":
                        self.v_status.set(m[1].splitlines()[0])
                        self._log(m[1])
                        self.bar["value"] = 0
                        self._set_busy(False)
                        self._save_state()
            except queue.Empty:
                pass
            self.root.after(100, self._poll)

        def _close(self):
            if self.busy and not messagebox.askyesno(
                    "영상 업스케일러", "변환 중입니다. 중지하고 닫을까요?\n"
                    "남은 대기열은 저장되어 다음에 켜면 다시 나타납니다."):
                return
            self._save_state()
            if self.cancel:
                self.cancel.set()
            self.root.destroy()

    root = tk.Tk()
    try:
        from tkinter import font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family="Malgun Gothic", size=10)
    except Exception:                                   # noqa: BLE001
        pass
    app = App(root)
    return root, app


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    _root, _app = gui()
    _root.mainloop()
